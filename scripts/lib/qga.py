#!/usr/bin/env python3
"""QEMU guest agent（qemu-ga）的宿主机侧最小客户端。

为什么需要它
------------
宿主机原来只有三种办法碰客人机：SSH（要网络通、要 sshd 在跑）、QEMU monitor
的 system_powerdown（要客人机里有交互会话去接电源键事件）、kill -9（丢数据）。
guest agent 是第四条路，也是唯一一条不依赖客人机网络的路：QEMU 在宿主机上开
一个 UNIX socket，qemu-ga 在客人机里把另一头接到 virtio-serial 端口，两边讲
JSON-RPC。于是宿主机可以要客人机优雅关机、在它里面执行命令、问它网卡 IP 和
磁盘信息。磁盘在线 resize（宿主机放大 qcow2 + guest 里 rescan + growpart）
要的就是这些，所以先把这地基铺上。

协议要点：QGA 不是 QMP，别把 QMP 的写法照搬过来
----------------------------------------------
QGA 讲 JSON-RPC，一行一个 JSON 对象，请求和响应都是。它**没有握手问候**：
连上就能发，发完等一行响应，内容是 {"return": ...} 或 {"error": ...}。
QMP 反过来：连上先收到一句 greeting，还必须先 negotiate 能力才能发命令。
把 QMP 那套搬过来会白等一句永远不来的 greeting，表现为「连上了但没响应」。

这里的实现是「一次连接一个请求」。qemu-ga 默认跑在同步模式，一条命令处理完
才收下一条；每次重连避开的是长连接上半路断开后的状态问题，而本项目的用法
（关机、问一次 IP）对连接开销完全不敏感。

socket 放在 VM 自己的目录下（<vm>/qga.sock），和 monitor.sock、qemu.pid 一个
待遇。不放 /tmp：/tmp 会被系统清理，而且多用户机器上谁能连上这个 socket，谁
就能在你所有客人机里以 root 执行命令。

Windows ARM64 上没有它
----------------------
qemu-ga 的 Windows ARM64 安装包（qemu-ga-arm64.msi）目前只有
virtio-win-guest-tools-installer PR #100 / #104 的 CI artifact，没有正式
release 产物；UTM #5134 那边说 guest agent 未移植到 ARM64 Windows。两条证据
对不上，所以宿主机侧只做到「socket 存在就试，不存在就安静走原来的路」，
不替 Windows 硬加 virtio-serial 设备。等正式包出来再补。
"""

from __future__ import annotations

import base64
import json
import shlex
import socket
import time
from pathlib import Path


class QGAError(RuntimeError):
    """guest agent 不可用、超时，或返回了 error。message 直接给人看。"""


def qga_request(sock, payload: dict, timeout: float = 10.0) -> dict:
    """发一个 JSON-RPC 请求，返回响应里的 "return" 字段（可能是 None）。

    sock 是 QEMU -chardev socket 那个文件的路径。**文件存在不等于 agent 在
    跑**：QEMU 只要带了这个 chardev 就会把 socket 建出来，而客人机里要等
    cloud-init 把 qemu-guest-agent 装上并起服务。所以「连上了但一个字都等
    不到」是常态之一，由 timeout 收口，并且和「连不上」分成两种说法——两者
    的修法完全不同（前者去 guest 里看服务，后者去宿主机看 QEMU 参数）。
    """
    path = Path(sock)
    if not path.exists():
        raise QGAError(f"guest agent 套接字不存在：{path}")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(timeout)
            sk.connect(str(path))
            f = sk.makefile("rwb")
            try:
                f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                f.write(b"\n")
                f.flush()
                # 只读一行。guest agent 不会主动推送东西，多读只会等到超时。
                line = f.readline()
            finally:
                f.close()
    except socket.timeout:
        # socket.timeout 在 3.10+ 是 TimeoutError，是 OSError 的子类，所以这个
        # except 必须排在 OSError 前面，否则超时会被当成「连不上」报出去。
        raise QGAError(
            f"guest agent {timeout:g} 秒没响应（{path}）。"
            f"客人机里 qemu-guest-agent 装了吗、起来了吗"
        ) from None
    except OSError as e:
        raise QGAError(
            f"连不上 guest agent（{path}）：{e}。QEMU 没带这个 chardev、或者"
            f"socket 没人 accept 时也是这个错"
        ) from None

    if not line.strip():
        raise QGAError(
            f"guest agent 关掉了连接却没给响应（{path}）。通常是客人机里"
            f"qemu-guest-agent 没在跑")
    try:
        resp = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        raise QGAError(f"guest agent 返回的不是 JSON：{line[:200]!r}") from None
    if not isinstance(resp, dict):
        raise QGAError(f"guest agent 返回的不是一个 JSON 对象：{line[:200]!r}")
    if "error" in resp:
        err = resp["error"]
        if isinstance(err, dict):
            raise QGAError(f"guest agent 报错：{err.get('class')}: {err.get('desc')}")
        raise QGAError(f"guest agent 报错：{err}")
    return resp.get("return")


def qga_ping(sock, timeout: float = 5.0) -> bool:
    """探一下 agent 在不在。返回真假，不抛异常。

    给「先试试，不行就安静走原路」的调用方用（比如关机链路），那里报错只会
    把一条本来能走通的路搅黄。
    """
    try:
        qga_request(sock, {"execute": "guest-ping"}, timeout=timeout)
        return True
    except QGAError:
        return False


def qga_shutdown(sock, timeout: float = 10.0) -> None:
    """让客人机走它自己的关机流程。

    和 monitor 的 system_powerdown 的区别：那个是「电源键事件」，要客人机里
    有交互会话去接，停在登录界面时没人接，等到超时只能强杀；这个是直接告诉
    agent 关机，systemd 会走完整的 stop 流程，该 flush 的都会 flush。
    """
    qga_request(sock, {"execute": "guest-shutdown"}, timeout=timeout)


def qga_network_interfaces(sock, timeout: float = 10.0) -> list:
    """客人机的网卡清单，QGA 原样返回。

    每项形如 {"name": "eth0", "hw-address": ...,
              "ip-addresses": [{"ip-address": "10.0.2.15",
                                "ip-address-type": "ipv4", "prefix": 8}]}。
    """
    out = qga_request(sock, {"execute": "guest-network-get-interfaces"},
                      timeout=timeout)
    return out or []


def qga_ips(sock, timeout: float = 10.0) -> list[str]:
    """只要 IPv4 地址，按 QGA 返回的顺序。"""
    ips: list[str] = []
    for iface in qga_network_interfaces(sock, timeout=timeout):
        for ip in (iface.get("ip-addresses") or []):
            if ip.get("ip-address-type") == "ipv4" and ip.get("ip-address"):
                ips.append(ip["ip-address"])
    return ips


def qga_disks(sock, timeout: float = 10.0) -> list:
    """客人机能看到的块设备，QGA 原样返回（guest-get-disks）。

    qemu-ga 5.2 以上才有这个命令。版本不够时 QGAError 里带的是它的
    CommandNotFound，照着升级 guest 里的 qemu-guest-agent 就行。
    """
    out = qga_request(sock, {"execute": "guest-get-disks"}, timeout=timeout)
    return out or []


def qga_exec(sock, argv, timeout: float = 60.0) -> dict:
    """在客人机里执行命令并等到它结束。

    返回 {"exitcode": int, "out": str, "err": str}。

    三个坑：
      * guest-exec 是异步的，先返回一个 pid，再拿 guest-exec-status 轮询到
        exited 为真。不等就是拿到一个「已派发」的假成功。
      * capture-output 必须给真，否则 status 里没有 out-data，只剩一个退出码
        ——排障时最想要的恰好就是那两段输出。
      * 输出在 status 里是 base64 编码的（agent 是 root，直接塞 JSON 会碰上
        二进制字节），必须解。
    argv 给字符串时按 `sh -c` 跑，和 tart exec 那边的用法保持一致。给列表时
    第一个元素不是绝对路径也套一层 `sh -c`：qemu-ga 的 guest-exec 对裸命令名
    的处理各版本不一样（新的用 g_spawn 带 G_SPAWN_SEARCH_PATH，PATH 里找得到；
    老的只 execv，非绝对路径直接报 Invalid parameter），套一层 sh 两条路都通，
    而且顺手把管道、重定向、shell 内建命令都带上——排障时最常用的恰恰是这些。
    """
    if isinstance(argv, str):
        argv = ["/bin/sh", "-c", argv]
    else:
        argv = [str(a) for a in argv]
    if not argv:
        raise QGAError("要给要执行的命令")
    if not argv[0].startswith("/"):
        # shlex.quote 逐个参数再拼回去，是为了把 `echo "a b"` 这类还原成
        # 原来那条命令行，而不是让 sh 把它拆成两个参数。
        argv = ["/bin/sh", "-c", shlex.join(argv)]
    r = qga_request(sock, {
        "execute": "guest-exec",
        "arguments": {
            "path": argv[0],
            "arg": [str(a) for a in argv[1:]],
            "capture-output": True,
        },
    })
    pid = (r or {}).get("pid")
    if pid is None:
        raise QGAError(f"guest agent 没给 pid：{r!r}")
    deadline = time.time() + timeout
    while True:
        st = qga_request(sock, {
            "execute": "guest-exec-status",
            "arguments": {"pid": pid},
        }) or {}
        if st.get("exited"):
            return {
                "exitcode": st.get("exitcode"),
                "out": _b64(st.get("out-data")),
                "err": _b64(st.get("err-data")),
            }
        if time.time() >= deadline:
            raise QGAError(f"客人机里的命令 {timeout:g} 秒没结束（pid {pid}）")
        time.sleep(0.3)


def _b64(data) -> str:
    """解 guest-exec-status 里的 base64 输出段。缺了就当空串。"""
    if not data:
        return ""
    try:
        return base64.b64decode(data).decode("utf-8", "replace")
    except Exception:
        return ""
