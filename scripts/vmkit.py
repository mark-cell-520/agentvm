#!/usr/bin/env python3
"""vmkit — agentvm 的统一后端：一条命令装好一个能用的虚拟机。

它做三件事，全部无交互：

1. 从 catalog.json 里选一个系统（Windows 11 ARM64 或若干 Linux 云镜像），
   带断点续传和校验和地把镜像下到本地；
2. 建虚拟磁盘，写好无人值守配置；
3. 用 QEMU + HVF 启动，等到 SSH 就绪。

Windows 和 Linux 走两条不同的装法，但对外是同一套命令：

    Windows  原始 ISO 是「要装」的：注 virtio 驱动进 WIM、写
             autounattend.xml、重建成 UEFI 可启动 ISO，然后跑 Setup。
             这部分逻辑已在 winarm-vm.py 里实测跑通，vmkit 直接委托它，
             不重写一遍——重写就等于把踩过的坑再踩一次。

    Linux    云镜像本来就是「装好」的：qcow2 里已经是完整的系统。所以
             vmkit 只做三件事：qcow2 上叠一层可增长 overlay、用
             cloud-init 的 NoCloud 配置盘把账号和 SSH 密钥喂进去、
             启动。没有安装程序要跑，一分钟就能登录。

输出协议
--------
stdout 每行一个 JSON 对象，界面直接解析：

    {"t":"stage","id":"download","label":"下载镜像"}
    {"t":"progress","id":"download","frac":0.42,"bytes":1,"total":2,"speed":3}
    {"t":"log","level":"info|warn|err","msg":"..."}
    {"t":"done","vm":{...}}
    {"t":"error","msg":"..."}

不是 JSON 的行按普通日志显示。这是有意的：被委托的 winarm-vm.py 里有几处
直接 print，逐行改它反而容易碰坏已经跑通逻辑，不如让客户端宽容一点。

目录布局
--------
    ~/.agentvm/images/<id>.<fmt>      基础镜像，多个 VM 共用一份
    ~/.agentvm/vms/<名字>/vm.json     这个 VM 的配置
    ~/.agentvm/vms/<名字>/disk.qcow2  虚拟磁盘（Linux）
    ~/.agentvm/vms/<名字>/data.img    虚拟磁盘（Windows，raw）
    ~/.agentvm/vms/<名字>/seed.iso     cloud-init 配置盘（Linux）
    ~/.agentvm/vms/<名字>/qga.sock     QEMU guest agent 套接字（Linux）

用法
----
    vmkit.py catalog                     列出可选系统
    vmkit.py check                       检查宿主机能不能跑
    vmkit.py install --distro debian-13 --name d1
    vmkit.py list
    vmkit.py status --name d1
    vmkit.py start --name d1 [--headless]
    vmkit.py stop --name d1
    vmkit.py guest --name d1 ip          问客人机要 IP / 执行命令（走 guest agent）
    vmkit.py delete --name d1
    vmkit.py open --name d1              把虚拟机窗口带到前台
    vmkit.py autostart --name d1 [--disable]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import yaml
except ImportError:      # 只在拼 cloud-config 时才需要，见 linux_seed
    yaml = None

HOME = Path.home()
ROOT = HOME / ".agentvm"
IMAGES = ROOT / "images"
VMS = ROOT / "vms"
ASSETS = ROOT / "assets"
HERE = Path(__file__).resolve().parent
CATALOG = HERE / "catalog.json"
WINARM = HERE / "winarm-vm.py"
AUTOINSTALL = HERE / "autoinstall.py"

# 端口分配起点。避开常见的 5900（macOS 屏幕共享）和 22/3389。
PORT_BASE_SSH = 20000
PORT_BASE_RDP = 21000
PORT_BASE_VNC = 22000
# 本地 ISO 装的 NAS 类系统，主要界面是 Web UI。fnOS 用 5666，别的系统多用
# 80/443——这里只转 5666：实测 fnOS 装完 Web UI 就在这个口上，从宿主机
# curl 得到 HTTP 200 和 <title>飞牛 fnOS</title>。80/443 不转，宿主机上
# 大概率已经有东西占了，转过去必然冲突。
PORT_BASE_WEB = 23000
WEB_GUEST_PORT = 5666

# 本地包的盘小于这么多字节就算「没装过」。真正的安装至少会写进去几百 MB
# （fnOS 1.2.07 实测 7.5G），而这个门槛要低到不会把任何一次真实安装误判掉。
LOCAL_MIN_DISK = 32 << 20

# QEMU 的 -vnc 收的是显示编号，实际监听 5900 + 编号。vm.json 里存编号，
# 界面和 app 用的是端口。两个空间不能混着比，见 alloc_vnc_display。
VNC_DISPLAY_BASE = PORT_BASE_VNC - 5900      # 16100
PORT_VNC_BASE = 5900                         # QEMU 的显示编号基数


# --------------------------------------------------------------- 事件输出 --

class Emit:
    """把进度和日志写成一行一个 JSON。--json 关掉时退回人话。"""

    on = True

    @classmethod
    def _w(cls, obj: dict) -> None:
        if cls.on:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        else:
            # 人话模式。事件还是走同一条路，只是换个印法，省得维护两套输出。
            t = obj.get("t")
            if t == "log":
                print(f"[{obj.get('level', 'info')}] {obj.get('msg', '')}",
                      flush=True)
            elif t == "stage":
                print(f"==> {obj.get('label', '')}", flush=True)
            elif t == "error":
                print(f"错误：{obj.get('msg', '')}", file=sys.stderr, flush=True)
            elif t in ("done", "vms", "catalog", "check"):
                print(json.dumps(obj, ensure_ascii=False, indent=2), flush=True)
        sys.stdout.flush()

    @classmethod
    def stage(cls, sid: str, label: str) -> None:
        cls._w({"t": "stage", "id": sid, "label": label})

    @classmethod
    def log(cls, msg: str, level: str = "info") -> None:
        cls._w({"t": "log", "level": level, "msg": msg})

    @classmethod
    def progress(cls, sid: str, frac: float, nbytes: int,
                 total: int, speed: float) -> None:
        cls._w({"t": "progress", "id": sid, "frac": round(frac, 4),
                "bytes": nbytes, "total": total, "speed": round(speed)})

    @classmethod
    def done(cls, vm: dict) -> None:
        cls._w({"t": "done", "vm": vm})

    @classmethod
    def error(cls, msg: str) -> None:
        cls._w({"t": "error", "msg": msg})


def log(msg: str) -> None:
    Emit.log(msg, "info")


def ok(msg: str) -> None:
    Emit.log(msg, "info")


def warn(msg: str) -> None:
    Emit.log(msg, "warn")


def die(msg: str) -> None:
    Emit.error(msg)
    sys.exit(1)


def run(cmd: list[str], *, check: bool = True, capture: bool = True,
        timeout: int | None = None, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=check, capture_output=capture,
                              text=True, timeout=timeout, **kw)
    except subprocess.CalledProcessError as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()[:2000]
        die(f"命令失败：{' '.join(cmd[:6])}\n{out}")
    except subprocess.TimeoutExpired:
        die(f"命令超时：{' '.join(cmd[:6])}")
    raise AssertionError


def which(name: str) -> str | None:
    """按名字找工具，PATH 里没有也去 Homebrew 常见前缀下找。

    不能只 shutil.which：QEMU 装在 ~/hb/bin，而那个目录只在交互 shell 的
    PATH 里。launchd、cron、app 拉起的进程都可能不带它，只认 PATH 会把明明
    装好了的工具报成「缺少依赖」。实测踩过：从命令行后台起一次 Windows 安装，
    PATH 里没有 ~/hb/bin，依赖检查一次报缺 qemu/wimlib/mkisofs 三样，
    其实三个都在 ~/hb/bin 下。
    """
    hit = shutil.which(name)
    if hit:
        return hit
    env = os.environ.get("HOMEBREW_PREFIX")
    cands = [Path(env) / "bin" / name] if env else []
    cands += [HOME / "hb" / "bin" / name,
              Path("/opt/homebrew/bin") / name,
              Path("/usr/local/bin") / name,
              # hdiutil、osascript 这些 macOS 自带的在 /usr/bin，PATH 被削得
              # 很干净时（cron、某些 launchd 任务）也会找不到
              Path("/usr/bin") / name,
              Path("/bin") / name]
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def find_tool(name: str) -> str:
    """把工具名解析成绝对路径，找不到就说清楚去哪装。"""
    hit = which(name)
    if hit:
        return hit
    die(f"找不到 {name}。PATH 里没有，~/hb、/opt/homebrew、/usr/local 下也没有。"
        f"设 HOMEBREW_PREFIX 指过去，或先装它（brew install qemu）。")


def find_vmz() -> Path | None:
    """vmz 在哪儿。找不到返回 None，由调用方决定这是不是致命错误。

    为什么不像 find_tool 那样直接 die：vmz 只有 macOS 客人机用得到，
    Windows / Linux 两条路完全不碰它。为一个用不到的工具把整条命令
    毙掉，用户会看到「找不到 vmz」而他根本不想装 macOS。

    候选路径里有**两个编译位置**，所以不能「第一个找到的就算」：
    `app/build.sh` 编到 `scripts/vmz/vmz`，`install.sh` 编到
    `~/.agentvm/bin/vmz`。两边各自都会被刷新，也各自会变旧——只按固定顺序
    取第一个，就会在源码已经改过之后还用着一份旧二进制。实测踩过
    （2026-11-10）：下载器的 bug 早已修掉、`scripts/vmz/vmz` 是新的，
    而 find_vmz 落到 `~/.agentvm/bin/vmz` 那份三天前的构建上，于是新代码
    一行都没跑，故障表现和没修一模一样，白排查了半天。

    所以除了 AGENTVM_VMZ（用户显式指定，最高优先）之外，剩下的候选
    全部收集起来，取 mtime 最新的那个。
    """
    env = os.environ.get("AGENTVM_VMZ")
    cands: list[Path] = []
    if env:
        cands.append(Path(env))
    # HERE 已经是 scripts/ 了，别再拼一次 scripts。原来是
    # HERE / "scripts" / "vmz" / "vmz"，解析出来是 scripts/scripts/vmz/vmz，
    # 永远不存在；于是真正该被找到的 scripts/vmz/vmz 从来没进过候选，
    # 直接落到下面那份旧的。
    cands += [
        HERE / "vmz" / "vmz",
        HERE.parent / "bin" / "vmz",
        HOME / ".agentvm" / "bin" / "vmz",
        Path("/usr/local/bin/vmz"),
        Path("/opt/homebrew/bin/vmz"),
    ]
    found = [c for c in cands if c.is_file() and os.access(c, os.X_OK)]
    if env and found:
        # 显式指定就认它，哪怕它更旧——那是用户自己的选择。
        return found[0]
    if not found:
        w = which("vmz")
        return Path(w) if w else None
    return max(found, key=lambda p: p.stat().st_mtime)


def _self_cmd() -> str:
    """命令行里怎么调自己。错误消息里要给一条能复制粘贴的命令。

    不能只写 `vmkit.py`：它没有装到 PATH 上（install.sh 只装 vmz），
    用户在终端里照抄会 command not found，而这行字本来是想帮他脱困的。
    """
    return f"python3 {Path(__file__).resolve()}"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]?I?B?)$")


def parse_size(s) -> int:
    """'4G' / '512M' / '64MiB' / 字节数 → 字节数。

    原来用「剥后缀再 float()」的写法，两个坑：
      1. '64MiB' 变成 '64MIB'，float() 直接 ValueError 裸栈到用户脸上；
         '--disk ""' 同样。
      2. 认得 GB 但不统一：'64GB' 在这里返回 68719476736，而 catalog 安装路
         把这个串原样交给 qemu-img，qemu-img 11.1.3 报
         `Invalid image size specified: '64GB'`。同一个合法写法三条路两种命运
         （本地 ISO 路走 truncate 成功、catalog 路失败、configure 走字节数又成功）。
    现在用正则白名单，非法输入给一句人话（列出可用写法），并且 vmkit 内部一律
    以字节数下发，不再把 '64G' 这种串递给外部工具。
    """
    if isinstance(s, (int, float)):
        return int(s)
    t = str(s).strip().upper()
    if not t:
        die("大小不能是空的。写法像 64G、512M、1.5T 或纯字节数。")
    m = _SIZE_RE.match(t)
    if not m:
        die(f"看不懂这个大小：{s!r}。"
            f"写法像 64G、512MiB、1.5T，或纯字节数（68719476736）。")
    num = float(m.group(1))
    suf = m.group(2)
    mult = 1
    # 先判两位后缀（KB/MB/GB/TB/KiB/...），再判一位
    for s2, mul in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3), ("TB", 1024**4),
                    ("KIB", 1024), ("MIB", 1024**2), ("GIB", 1024**3), ("TIB", 1024**4)):
        if suf == s2:
            mult = mul
            break
    else:
        for s1, mul in (("K", 1024), ("M", 1024**2), ("G", 1024**3), ("T", 1024**4)):
            if suf == s1:
                mult = mul
                break
    return int(num * mult)


# ------------------------------------------------------------------ 目录 --

def vm_dir(name: str) -> Path:
    return VMS / name


def vm_file(name: str) -> Path:
    return vm_dir(name) / "vm.json"


def load_vm(name: str) -> dict:
    p = vm_file(name)
    if not p.is_file():
        die(f"没有这个虚拟机：{name}。用 list 看现有的。")
    try:
        vm = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"{p} 不是合法 JSON：{e}")
    # name 和 kind 两个键必须存在——后面到处是 `vm["kind"]` 这种直接下标。
    #
    # **不在这儿 die。** 第一版对缺字段的文件直接 die，实测那是个倒退：
    # status --name 和 list 本来能把它显示成「没装完」并给一个 --force 入口
    # （vm_broken 的全部意义就是这个），die 之后这条路也没了；更糟的是
    # install --name X --force 自己也要 load_vm，于是错误消息里推荐的那条恢复
    # 路径本身就跑不通。现在补齐默认值让下标访问安全，真正的问题由 vm_broken
    # 报出来（见那儿对「认不出的 kind」的处理）。
    #
    # 背景：vm.json 是会被写一半的。_save_vm_impl 是原子 replace，但旧版本、
    # 外部工具、磁盘满都会留下「合法 JSON 但缺字段」的文件。而 cmd_open、
    # cmd_window、cmd_stop、cmd_disk 原来都直接 `vm["kind"]`——一个坏文件就让
    # 整条命令 KeyError 裸栈，用户只是想看一眼这台机器，得到的却是一段 Python
    # 堆栈。本文件其余地方早就注释过「vm.json 一律用 .get」，这几处是漏的。
    # 2026-11-11 审计补上。
    if not isinstance(vm, dict):
        die(f"{p} 内容不是一个对象：{vm!r}")
    # setdefault 而不是 vm["kind"] = ...：文件里已经有值的（正常情况）不能被
    # 覆写，只有真缺的时候才补空串。
    vm.setdefault("name", name)
    vm.setdefault("kind", "")
    return vm


def save_vm(vm: dict) -> None:
    """写 vm.json，权限 0600。

    里面有明文 password。项目规约是「密钥落地 0600」，而这里原来用默认
    umask 落成 0644——同机上任何本地用户都能读到每一台虚拟机的密码。
    旁边 pubkey_text 写私钥时是 0600，说明这是漏的不是取舍。
    """
    _save_vm_impl(vm)
    try:
        os.chmod(str(vm_file(vm.get("name", ""))), 0o600)
    except OSError:
        pass


def _save_vm_impl(vm: dict) -> None:
    vm["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = vm_file(vm["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    # 临时文件必须一创建就是 0600。原来用 write_text，按默认 umask 落成
    # 0644，而里面是明文 password——同机上任何本地用户都能读每一台虚拟机
    # 的口令；replace 之后才 chmod 0600，中间那段窗口足够被人读走。
    # fsync 也不能省：不落盘就换名，崩起来留下的是一个空的 vm.json，
    # 而 load_vm 只会说「不是合法 JSON」。
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(vm, ensure_ascii=False, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(p)


def all_vms() -> list[dict]:
    out = []
    if not VMS.is_dir():
        return out
    for p in sorted(VMS.iterdir()):
        f = p / "vm.json"
        if f.is_file():
            try:
                out.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                continue
    return out


def load_catalog() -> dict:
    if not CATALOG.is_file():
        die(f"找不到镜像目录 {CATALOG}")
    return json.loads(CATALOG.read_text())


def find_distro(catalog: dict, distro_id: str) -> dict:
    for d in catalog["distros"]:
        if d["id"] == distro_id:
            return d
    die(f"目录里没有 {distro_id}。可选："
        + ", ".join(d["id"] for d in catalog["distros"]))


# ---------------------------------------------------------------- 下载 --

def remote_size(url: str) -> int:
    """用 Range 试探拿总大小。不支持的服务器返回 0，进度条就只显示已下载量。"""
    try:
        r = subprocess.run(
            [find_tool("curl"), "-sS", "-L", "-I", "--max-time", "20", url],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if line.lower().startswith("content-length:"):
                return int(line.split(":", 1)[1].strip())
    except (subprocess.SubprocessError, ValueError):
        pass
    return 0


def download(url: str, dest: Path, sid: str = "download",
             expect: int = 0) -> Path:
    """带断点续传和真实进度的下载。

    用 curl 拉、用轮询文件大小算进度，而不是自己拼 Range：curl 的 --retry
    和 -C - 是现成的，自己实现一遍等于把重试逻辑再写一次还更容易错。
    GitHub 的 release asset 单连接只有几十 KB/s，但 1 MB 的 virtio 包和
    微软 CDN 都能接受；真正的大文件（Windows ISO 4.3 GB）走的是不限速的
    微软 CDN。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if dest.is_file() and dest.stat().st_size > 0:
        ok(f"镜像已在本地：{dest}（{human(dest.stat().st_size)}）")
        return dest

    have = part.stat().st_size if part.is_file() else 0
    if have and expect and have >= expect:
        part.replace(dest)
        ok(f"续传发现已下完：{dest}")
        return dest
    if have:
        log(f"续传，已有 {human(have)}")

    total = expect or remote_size(url)
    cmd = [find_tool("curl"), "-sS", "-L", "--retry", "5", "--retry-delay", "2",
           "--connect-timeout", "20"]
    if have:
        cmd += ["-C", "-"]
    cmd += ["-o", str(part), url]

    log(f"开始下载 {url}")
    t0 = time.time()
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                n = part.stat().st_size if part.exists() else 0
            except OSError:
                n = 0
            dt = max(time.time() - t0, 1e-6)
            speed = (n - have) / dt
            Emit.progress(sid, (n / total) if total else 0, n, total, speed)
            stop.wait(1.0)

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        stop.set()
        th.join(timeout=2)

    if r.returncode != 0:
        err = (r.stderr or "").strip()[:500]
        # curl 33 = 服务器不接受 Range 请求（HTTP server does not support ranged
        # requests），416 = Range 越界。这两种情况下 .part 里已经有内容，而重跑
        # 还是带 -C -、还是同样失败——原来笼统提示「重跑同一条命令会从断点继续」，
        # 用户会无限重跑一个必然失败的尝试，永远想不到要去删 .part。
        if r.returncode in (33, 416):
            die(f"下载失败（curl {r.returncode}）：服务器不接受断点续传。\n{err}\n"
                f"删掉 {part} 再重跑，就会从头下。")
        die(f"下载失败（curl {r.returncode}）：{err}\n"
            f"重跑同一条命令会从断点继续（已有 {human(have)}）。")
    if not part.is_file():
        die("下载结束但没有文件，curl 没报错。")

    got = part.stat().st_size
    if expect and got != expect:
        die(f"大小不对：期望 {expect} 字节，实际 {got}。删掉 {part} 重下。")
    part.replace(dest)
    dt = max(time.time() - t0, 1e-6)
    Emit.progress(sid, 1.0, got, total, (got - have) / dt)
    ok(f"下载完成：{dest}（{human(got)}，平均 {human((got - have) / dt)}/s）")
    return dest


def verify(path: Path, distro: dict) -> None:
    """校验镜像。没有校验和的条目不阻塞，但要说清楚没校验。"""
    algo = "sha512" if distro.get("sha512") else "sha256"
    want = distro.get(algo)
    if not want:
        warn(f"目录里没有 {path.name} 的校验和，跳过校验")
        return
    Emit.stage("verify", "校验镜像完整性")
    h = hashlib.new(algo)
    n = 0
    with path.open("rb") as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            Emit.progress("verify", n / max(path.stat().st_size, 1), n,
                          path.stat().st_size, 0)
    got = h.hexdigest()
    if got != want:
        die(f"{algo} 不匹配，镜像可能被截断或篡改。\n期望 {want}\n实际 {got}\n"
            f"删掉 {path} 重下。")
    ok(f"{algo} 校验通过")


# ------------------------------------------------------------ 环境检查 --

def cmd_check() -> int:
    info: dict = {"ok": True, "arch": platform.machine(), "items": []}
    if platform.machine() != "arm64":
        info["ok"] = False
        info["items"].append({"name": "芯片架构", "ok": False,
                              "detail": "需要 Apple Silicon（arm64），"
                                        f"当前 {platform.machine()}"})
    else:
        info["items"].append({"name": "芯片架构", "ok": True, "detail": "arm64"})

    for tool, hint in (("qemu-system-aarch64", "brew install qemu"),
                       ("qemu-img", "brew install qemu"),
                       ("wimlib-imagex", "brew install wimlib（Windows 安装需要）")):
        # which 自带 ~/hb、/opt/homebrew、/usr/local 兜底，所以这里不用再手写
        # 一遍——原来只兜底了 ~/hb/bin 一处，PATH 干净时会把装好的工具报成缺。
        p = which(tool)
        info["items"].append({"name": tool, "ok": bool(p),
                              "detail": p or f"未安装，{hint}"})
        if not p:
            info["ok"] = False

    # hdiutil 是 macOS 自带，cloud-init 配置盘靠它做。缺了就不是 macOS。
    p = which("hdiutil")
    info["items"].append({"name": "hdiutil", "ok": bool(p),
                          "detail": p or "未找到，这不是 macOS？"})
    if not p:
        info["ok"] = False

    # macOS 客人机要的东西和另外两种不一样：不碰 QEMU，要的是一个带
    # com.apple.security.virtualization entitlement 的 Swift 小程序。
    # 找不到 vmz 的时候不能把整个 check 判失败——那会让 Windows/Linux
    # 用户看到一片红，而他们压根不需要这个。
    vz = find_vmz()
    info["vmz"] = str(vz) if vz else None
    info["items"].append({
        "name": "vmz（macOS 客人机）", "ok": vz is not None,
        "detail": str(vz) if vz else "未安装。macOS 虚拟机需要它，另外两种不受影响"})

    df = shutil.disk_usage(HOME)
    free = df.free
    info["free_bytes"] = free
    # macOS 的恢复镜像 25.6 GB，加上装完之后系统盘实际写的三十多 GB。
    # 但镜像有全局缓存（~/.agentvm/images/ipsw）：缓存命中的话这一次只占
    # 系统盘那一份，实测 27 GB。所以这两个数分开报——照 60 GB 一刀切会把
    # 「镜像已经在本地」的用户判成不够，而他们其实只差 28 GB。
    # 镜像大小从 catalog 取，不写死：Apple 换个版本这个数就变了。
    try:
        mac = next(d for d in load_catalog()["distros"] if d.get("kind") == "macos")
        ipsw_size = int(mac.get("size") or 0)
    except (StopIteration, ValueError, OSError):
        ipsw_size = 0
    disk_actual = 28 << 30      # 实测装完写入约 27 GB，留点余量
    macos_need = disk_actual
    info["items"].append({
        "name": "磁盘剩余", "ok": free > (12 << 30),
        "detail": f"{human(free)}（Windows 需要 12 GB 以上，"
                  f"macOS 虚拟机需要 {human(macos_need + ipsw_size)}"
                  f"；恢复镜像已有缓存时只要 {human(macos_need)}）"})
    if free <= (12 << 30):
        info["ok"] = False
    if free <= macos_need:
        info["macos_free_ok"] = False
    else:
        info.setdefault("macos_free_ok", True)

    Emit._w({"t": "check", **info})
    return 0 if info["ok"] else 1


# -------------------------------------------------------------- 端口分配 --

def used_ports() -> set[int]:
    """已经占用的 TCP 端口。VM 之间不能撞，也不能撞宿主机上已有的服务。"""
    used: set[int] = set()
    out = subprocess.run([find_tool("lsof"), "-nP", "-iTCP", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        tail = parts[8]
        if ":" in tail:
            try:
                used.add(int(tail.rsplit(":", 1)[1]))
            except ValueError:
                pass
    return used


def alloc_port(base: int, taken: set[int]) -> int:
    used = taken | used_ports()
    for p in range(base, base + 4000):
        if p not in used:
            return p
    die(f"端口分配失败：{base} 起 4000 个都占满了")


def alloc_vnc_display(taken_displays: set[int]) -> int:
    """分配 VNC 显示编号。

    这里有两个编号空间，混在一起就会出事：vm.json 里存的是**显示编号**
    （16100 起），而 QEMU 实际监听的是 5900 + 编号（22000 起）。

    原来的写法是 `alloc_port(PORT_BASE_VNC, taken) - 5900`，而 taken 里收的是
    别的 VM 存下来的显示编号（16100）。拿 16100 去和 22000 比，永远比不中——
    结果是**每个虚拟机拿到的都是 16100**，第二个虚拟机一起动 VNC 就绑不上，
    控制台全是黑的。实测踩过：fnos 和 win11 的 vm.json 里 vnc_display 都是
    16100，两个都没法同时用控制台。

    所以两边分开比：显示编号和显示编号比，端口和端口比。

    **端口那一侧原来还是错的**（2026-11-10 复测发现）：修好 taken 比较之后，
    端口比较写成了 `PORT_VNC_BASE + (d - VNC_DISPLAY_BASE)`。对 d = 16100 那
    就是 5900 + 0 = 5900，于是它去比对的是 5900 到 9899 这一整段，跟 QEMU
    真正会绑的 22000 到 25999 毫无关系——宿主机上已经有东西占着 22000 时
    这里看不出来，照样把 16100 发出去，QEMU 起来才报
    `-vnc 127.0.0.1:16100: Failed to find an available port: Address already
    in use`，而那句话里的 16100 是显示编号，用户照着去找端口会找到空气。
    实测：一台跑着的 QEMU 占着 22000，alloc_vnc_display(set()) 仍返回 16100。
    QEMU 的端口就是 `5900 + 显示编号`，没有偏移量可减。
    """
    used = used_ports()
    for d in range(VNC_DISPLAY_BASE, VNC_DISPLAY_BASE + 4000):
        if d in taken_displays:
            continue
        if PORT_VNC_BASE + d in used:
            continue
        return d
    die("VNC 显示编号分配失败：4000 个都占满了")


# ------------------------------------------------------------ cloud-init --

# 登录名的白名单。cloud-init 拿它调 useradd，非法字符让建用户这一步失败，
# 而失败发生在首次启动的客人机里，宿主机只看到「SSH 连不上」。
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def indent(text: str, n: int) -> str:
    """给多行文本每行加 n 个空格，供嵌进 YAML 的 `content: |` 块。

    YAML 的块标量看的是缩进，少一个空格整块就不是它的内容——那种错不会报错，
    只会让 cloud-init 拿到一份错乱的用户数据。所以宁可走函数也不手写空格。
    """
    pad = " " * n
    return "\n".join(pad + line if line.strip() else "" for line in text.splitlines())


def _yaml_scalar(s: str) -> str:
    """把一个任意字符串变成安全的 YAML 双引号标量。

    json.dumps 出来的串（含两端双引号、内部引号和反斜杠都转好）本身就是合法
    YAML 双引号标量，所以直接拿来用，不用自己写转义。
    """
    return json.dumps(str(s), ensure_ascii=False)


def _apt_suite(distro: dict) -> str:
    """从 catalog 的镜像地址里解出 suite 名（noble / jammy / trixie）。

    两种地址形状：
      cloud-images.ubuntu.com/<suite>/current/xxx.img
      cloud.debian.org/images/cloud/<suite>/latest/xxx.qcow2

    解不出来就返回空串，调用方退回官方源、不测速——猜一个错的 suite 会让
    探测全数 404，白等一轮超时。
    """
    u = distro.get("url", "")
    m = re.search(r"cloud-images\.ubuntu\.com/([^/]+)/", u)
    if m:
        return m.group(1)
    m = re.search(r"/images/cloud/([^/]+)/", u)
    if m:
        return m.group(1)
    return ""


# 首启 apt 的镜像候选。**为什么不写死用官方源**：2026-11-10 实测同一个
# 15.3 MB 的 Packages.xz，ports.ubuntu.com 要 13.13 秒（1.17 MB/s），
# 清华源 0.81 秒（19 MB/s），差 16 倍。cloud-init 首启要下约 40 MB 索引
# （解压后落盘 229 MB），这一段就是「SSH 12 秒就绪、guest agent 却还要
# 25 到 140 秒才上线」的全部原因。
#
# 也不写死用国内源：那会把海外用户卡死。所以装的时候现场测一轮取最快的，
# 测不了就退回官方源。指定过的（AGENTVM_APT_MIRROR 或 vm.json 的
# apt_mirror）直接用它，不测。
_APT_MIRRORS = {
    "ubuntu": [
        "http://ports.ubuntu.com/ubuntu-ports",
        "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/",
        "https://mirrors.aliyun.com/ubuntu-ports/",
        "https://mirrors.ustc.edu.cn/ubuntu-ports/",
    ],
    "debian": [
        "http://deb.debian.org/debian",
        "https://mirrors.tuna.tsinghua.edu.cn/debian/",
        "https://mirrors.aliyun.com/debian/",
        "https://mirrors.ustc.edu.cn/debian/",
    ],
}

# 每个镜像下多大的片段来测速。2 MB：足够分辨 0.3 MB/s 和 3 MB/s 这种量级差，
# 又不至于在慢源上把安装卡住。四个源并行、每个有独立超时，见 _mirror_speed。
_MIRROR_PROBE_BYTES = 2 << 20
# 单个源的测速预算。慢源上多等没意义——它已经证明自己慢了。
_MIRROR_BUDGET = 5.0


def _mirror_speed(base: str, probe: str, budget: float = _MIRROR_BUDGET) -> float:
    """下一个固定大小的 range，返回字节/秒。失败或超时返回 0。"""
    url = base.rstrip("/") + probe
    try:
        r = subprocess.run(
            [find_tool("curl"), "-sS", "-L", "--max-time", f"{budget:g}",
             "-o", os.devnull, "-r", f"0-{_MIRROR_PROBE_BYTES - 1}",
             "-w", "%{speed_download}", url],
            capture_output=True, text=True, timeout=budget + 5)
        return float(r.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def pick_apt_mirror(vm: dict, distro: dict) -> str:
    """给这台 VM 选一个 apt 镜像，返回 deb822 的 URIs 值（空串 = 用镜像默认）。

    现场测速而不是写死：官方源与国内镜像差一个数量级，而这个差距反过来在
    海外也是成立的。测速总共约几秒（四个源并行），而首启能省几十秒。

    suite 解不出来时不测：探测路径是 `/dists/<suite>/main/binary-arm64/Packages.xz`，
     suite 错了全部 404，测出来是「官方源最快」——一个完全错误的结论。
    """
    forced = (os.environ.get("AGENTVM_APT_MIRROR")
              or vm.get("apt_mirror") or "").strip()
    if forced:
        return forced.rstrip("/") + "/"

    suite = _apt_suite(distro)
    family = "debian" if distro.get("id", "").startswith("debian") else "ubuntu"
    cands = _APT_MIRRORS.get(family) or []
    if not suite or not cands:
        return ""
    probe = f"/dists/{suite}/main/binary-arm64/Packages.xz"
    Emit.stage("mirror", "测速选 apt 镜像")
    # 并行测：串行四个源在最慢的那个上能白等半分钟。
    speeds: dict[str, float] = {}
    threads = []
    for base in cands:
        def _go(b: str = base) -> None:
            speeds[b] = _mirror_speed(b, probe)
        t = threading.Thread(target=_go, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=_MIRROR_BUDGET + 3)
    best = max(cands, key=lambda b: speeds.get(b, 0.0))
    got = speeds.get(best, 0.0)
    if got <= 0:
        # 一个都没测出来（没网、或者全部超时）。不猜，用镜像默认源。
        log("apt 镜像测速没结果，用云镜像自带的源")
        return ""
    for b in cands:
        if speeds.get(b):
            log(f"  apt 镜像 {b.split('/')[2]:32s} {speeds[b] / 1e6:6.1f} MB/s")
    log(f"选用最快的 apt 镜像：{best}（{got / 1e6:.1f} MB/s）")
    return best.rstrip("/") + "/"


# 首启 apt 调优。写进 /etc/apt/apt.conf.d/99zz-agentvm-fastindex，apt 每次启动
# 都重读这个目录，所以一定生效；`99zz` 开头是为了排在 cloud-init 自己那两个
# 片段（90cloud-init-aptproxy、94cloud-init-config）和镜像自带的
# 99needrestart / 99update-notifier 后面，不被它们盖掉。
#
# **四个设置都是实测出来的**（2026-11-10，Ubuntu 24.04 ARM64 客人机，清空
# /var/lib/apt/lists 后从头 apt-get update）：
#
#   基线（官方源）                        155.24 s   152 MB 落盘
#   + Acquire::Languages none              （省 8.25 MB 网络 / 64 MB 落盘）
#   + Acquire::GzipIndexes true             78.89 s    64 MB
#   + Acquire::ForceIPv4 true               58.67 s    64 MB
#   + 关 DEP-11 / CNF 索引目标              45.77 s    57 MB
#
# 没有把 Acquire::http::Pipeline-Depth 放进来：它只值 11%（79→70 秒），却是
# 唯一一个「镜像不支持就整次 update 失败」的设置，不值得拿可靠性换。
#
# 代价，说清楚免得以后有人查「为什么 apt show 没有中文描述」：
#   Languages none    `apt show` / `apt-cache show` 不再有本地化描述
#   GzipIndexes       apt 每次构建包缓存要现场解压，弱 CPU 上略慢
#   DEP-11 / CNF 关   没有软件中心元数据；`command-not-found` 失效
#   三者对「装一个包」都没有影响，实测 qemu-guest-agent 照常装上。
_APT_FASTINDEX = """\
# 由 agentvm 生成。删掉这个文件就回到 apt 默认行为。
# 依据与各项代价见 scripts/vmkit.py 的 _APT_FASTINDEX 注释。
Acquire::Languages "none";
Acquire::GzipIndexes "true";
Acquire::ForceIPv4 "true";
Acquire::IndexTargets::deb::DEP-11::DefaultEnabled "false";
Acquire::IndexTargets::deb::CNF::DefaultEnabled "false";
"""

# Debian trixie 云镜像的源。镜像自带的那份两个问题：security 指向不带
# `/debian-security` 后缀的树（404），以及开着 deb-src（源码索引，装二进制包
# 用不到，每套件多一倍索引目标）。2026-11-10 实测四个镜像的
# `/debian/dists/trixie-security/Release` 全 404、`/debian-security/` 全 200。
_DEBIAN_SOURCES = """\
# 由 agentvm 生成。cloud-init 的 preserve_sources_list 不让它覆盖这个文件。
Types: deb
URIs: {main}
Suites: {suite} {suite}-updates {suite}-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: {security}
Suites: {suite}-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""


def linux_seed(vm: dict, distro: dict | None = None) -> Path:
    """生成 cloud-init 的 NoCloud 配置盘。

    为什么用云镜像 + NoCloud，而不是 netinst 加 preseed：云镜像本身就是
    装好的系统，qcow2 叠一层 overlay 就能跑，没有安装程序要交互；账号、
    密码、SSH 公钥全从配置盘喂进去。用 netinst 的话要多伺候一套 preseed
    和安装期网络镜像，出问题的地方多一个数量级。

    distro 给了才会做 apt 镜像测速（见 pick_apt_mirror）。只重写种子盘而不
    重装的时候没有 catalog 条目，那时沿用云镜像自带的源。

    卷标必须是 cidata，这是 cloud-init 的 NoCloud 数据源认的标签；
    hdiutil 的 -default-volume-name 会给 ISO9660 的 CIDATA，正好对上。

    用户名和密码必须转义后再进 YAML。原来是 f-string 原样插值：
      * 密码 `p@ss #word` 生成 `passwd: p@ss #word`，YAML 把 `#word` 当注释，
        实际密码变成 `p@ss`——用户拿原密码登不进去，而两边都说成功了。
      * 密码以 `{` `[` `*` `&` 开头会被当成流式映射/别名，cloud-init 整体解析
        失败，连账号都建不出来。
      * 用户名含 `:` 或 `#` 直接坏。
    json.dumps 产出的双引号串是合法 YAML 标量，什么字符都安全。
    """
    d = vm_dir(vm["name"])
    src = d / "seed"
    _rmtree(src)
    src.mkdir(parents=True)

    user = _yaml_scalar(vm["user"])
    pw = _yaml_scalar(vm["password"])
    # 登录名另做一道白名单：cloud-init 拿它建系统用户，非法字符会让 useradd
    # 失败，而失败发生在首次启动的客人机里，宿主机这边只看到 SSH 连不上。
    if not _USER_RE.match(vm["user"]):
        die(f"登录名不合法：{vm['user']!r}。要像 debian、agent、vm01 这样："
            f"小写字母或下线开头，后面跟小写字母、数字、下划线、连字符。")
    pub = pubkey_text(vm)
    # fqdn 的 .local 必须放在引号**里面**。`fqdn: {_yaml_scalar(name)}.local`
    # 生成的是 `fqdn: "d1".local`——双引号标量后面紧跟一个裸标量，YAML 直接
    # 语法错误，cloud-init 会把整份 user-data 丢掉：账号、密码、SSH 公钥、
    # packages 全都不生效，表象是「VM 起来了但登不进去」，而宿主机一路成功。
    # meta-data 的 instance-id 后缀是同一个病。这条是审计时漏掉的：同一次
    # 审计修了密码注入（见 docs/AGENTVM-APP.md「cloud-init 的 YAML 注入」），
    # 但漏了「引号外不能跟内容」这一条。
    fqdn = _yaml_scalar(vm['name'] + '.local')
    instance_id = _yaml_scalar(vm['name'] + '-001')
    mirror = pick_apt_mirror(vm, distro)
    is_debian = bool(distro) and str(distro.get("id", "")).startswith("debian")
    apt_block = ""
    extra_files = ""
    if is_debian:
        # Debian 和 Ubuntu 的源机制不一样，得分开处理，2026-11-10 两条都实测过。
        #
        # **Ubuntu**：`apt.primary` / `apt.security` 直接生效（cloud-init 的
        # cc_apt_configure 会重写 ubuntu.sources，而 arm64 的 primary 和
        # security 都指向官方源，所以两个都要给）。
        #
        # **Debian trixie 云镜像根本不吃这一套**：它的源是
        # `mirror+file:///etc/apt/mirrors/debian.list`，由那个文件里的地址
        # 重定向，cloud-init 的 apt.primary 改了也没用（实测 URIs 原样不动）。
        # 而镜像自带的 `debian-security.list` 写的是
        # `https://mirrors.tuna.tsinghua.edu.cn/debian/`——**不带 security 后缀**，
        # 于是 trixie-security 404。实测：清华/阿里/USTC/官方四个源的
        # `/debian/dists/trixie-security/Release` 全部 404，而它们的
        # `/debian-security/dists/trixie-security/Release` 全部 200。
        # Debian 的 security 套件在这些镜像上是一条独立的树。
        #
        # 这一条就是 docs/PITFALLS.md M8「Debian 13 apt 卡住」的真正原因：
        # apt 拿到 404 会重试，cloud-init 的 package_update_upgrade_install
        # 最终报 `does not have a Release file` 失败，`cloud-init status`
        # 变成 error——而 SSH 和窗口都是好的，VM 看着像装成功了。
        #
        # 修法：preserve_sources_list 让 cloud-init 别重写源，然后自己写一份
        # 正确的 deb822。顺带把 deb-src 去掉——那是源码索引，装二进制包用不到，
        # 每套件少一半索引目标。
        if mirror:
            root = mirror.rstrip("/")
            # 候选列表里 Debian 项自带 `/debian` 尾巴，去掉才是镜像根
            if root.endswith("/debian"):
                root = root[: -len("/debian")]
        else:
            root = "http://deb.debian.org"
        main_uri = root + "/debian/"
        sec_uri = root + "/debian-security/"
        suite = _apt_suite(distro) or "trixie"
        apt_block = (
            f"\n# preserve_sources_list：不让 cloud-init 重写源，下面那份\n"
            f"# deb822 才是最终生效的。\n"
            f"apt:\n"
            f"  preserve_sources_list: true\n")
        extra_files = (
            f"\n  # Debian 的 security 套件在镜像上是一条独立的树（<镜像>/debian-security），\n"
            f"  # 而云镜像自带的 debian-security.list 少写了这一段，导致 404。\n"
            f"  - path: /etc/apt/sources.list.d/debian.sources\n"
            f"    permissions: '0644'\n"
            f"    content: |\n"
            f"{indent(_DEBIAN_SOURCES.format(main=main_uri, security=sec_uri, suite=suite), 6)}\n")
    elif mirror:
        # **primary / security 是列表，不是单个字典。** 2026-11-10 实测踩过：
        # 写成 `primary: {{arches: [...], uri: ...}}` 时 cloud-init 的 schema
        # 校验报 warning、`config-apt_configure` 直接
        # `AttributeError: 'str' object has no attribute 'get'` 崩掉，
        # `cloud-init status` 变成 error，而 packages 那条路照样跑成功——
        # 表象是「镜像没换成、cloud-init 报错」，两件事看起来无关。
        apt_block = (
            f"\n# 用测速选出来的快镜像。官方源实测 0.3 MB/s、国内镜像 7.6 MB/s，\n"
            f"# 首启那 40 MB 索引就是 guest agent 迟迟不上线的原因。\n"
            f"apt:\n"
            f"  primary:\n"
            f"    - arches: [default]\n"
            f"      uri: {mirror}\n"
            f"  security:\n"
            f"    - arches: [default]\n"
            f"      uri: {mirror}\n")
    ud = f"""#cloud-config
# 由 agentvm 生成，改这个文件不如改 vm.json 后重跑 install。
hostname: {_yaml_scalar(vm['name'])}
fqdn: {fqdn}
manage_etc_hosts: true

users:
  - name: {user}
    gecos: {user}
    groups: [adm, sudo, wheel]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: false
    passwd: {pw}
    ssh_authorized_keys:
{pub}

ssh_pwauth: true
disable_root: true
chpasswd:
  expire: false

write_files:
  - path: /etc/apt/apt.conf.d/99zz-agentvm-fastindex
    permissions: '0644'
    content: |
{indent(_APT_FASTINDEX, 6)}
{extra_files}{apt_block}
# 云镜像默认只装最小集。把常用的补上，省得用户登进去再 apt 一遍。
packages:
  - openssh-server
  - curl
  - ca-certificates
  - sudo
  # QEMU guest agent。装上它宿主机就能不靠 SSH 在客人机里执行命令、要它
  # 优雅关机、问它的 IP 和磁盘——磁盘在线 resize 一类功能要的就是这些。
  # 装的是 Debian/Ubuntu 源里的 qemu-guest-agent，零额外下载成本（几十 KB），
  # 而换来的是一条不依赖客人机网络的通道：sshd 没起来、IP 变了、防火墙把
  # 22 挡了，SSH 都用不了，virtio-serial 照样通。
  # 源里没有这个包时 cloud-init 只记一条警告继续跑，不会让首次启动失败。
  - qemu-guest-agent

# Ubuntu 的服务名是 ssh，Debian 的是 sshd。两条都试，哪条存在用哪条。
# 不写这条的话某些云镜像首次启动 sshd 未必是 enabled 的，表现为
# 「VM 起来了但 SSH 连不上」，而 VNC 里看只是一个正常登录提示符。
#
# getty@tty0 是给窗口用的。云镜像默认只把控制台接到串口，framebuffer 上
# 什么都没画，Cocoa 窗口就是纯黑——不是没启动，是没人往 /dev/fb0 写字。
# /dev/fb0 是 ramfb 被 edk2 交接出来的，设备节点本来就在，缺的只是一个
# 登录终端。打开 getty@tty0 就有登录提示符，窗口才看得见东西。
runcmd:
  - [ sh, -c, "systemctl enable --now ssh || systemctl enable --now sshd || true" ]
  # guest agent 的服务。apt 装上时一般是 enabled 的，但云镜像裁剪过的
  # systemd 未必，而且它是**新包**——老 VM 升级时原来没有这一行。显式
  # enable --now 一次，失败也只是回到「宿主机那边探不到 agent」，
  # 不影响 SSH 和窗口。
  - [ sh, -c, "systemctl enable --now qemu-guest-agent || true" ]
  - [ sh, -c, "systemctl enable --now getty@tty0.service || true" ]
  # 控制台空闲默认会黑屏（kernel 的 consoleblank，默认 10 分钟）。VM 常常
  # 一放就是几小时，用户回来看见一个全黑窗口，会以为虚拟机死了。往每个 VT
  # 各发一次 setterm 关掉它。注意 setterm 是对「当前终端」生效的，所以要把
  # stdout 重定向到具体的 /dev/ttyN 才能作用到那个 VT。
  - [ sh, -c, "for n in 0 1 2 3 4 5 6; do setterm -blank 0 -powersave off -powerdown 0 >/dev/tty$n 2>/dev/null || true; done" ]
"""
    # 落盘前先自己解一遍 YAML。cloud-config 是拼出来的 f-string，一个缩进错
    # 就能让整份作废——而作废的现场是「VM 起来了但登不进去」，宿主机这一路
    # 全是成功。实测踩过两次：`fqdn: "x".local` 引号外跟内容（整份被丢），
    # 和 `apt.primary` 写成字典而不是列表（schema 校验 warning 加
    # apt_configure 崩掉，cloud-init status 变 error）。两次都是装机之后才
    # 发现，而这里一行 parse 就能当场抓住。
    if yaml is None:
        warn("没有 PyYAML，跳过 cloud-config 自检（pip install pyyaml 可补上）")
    else:
        try:
            parsed = yaml.safe_load(ud)
        except yaml.YAMLError as e:
            die(f"生成的 cloud-config 不是合法 YAML：{e}\n这是 agentvm 的 bug，"
                f"请把这段报错提到 issue。")
        if not isinstance(parsed, dict):
            die(f"生成的 cloud-config 不是一个映射：{type(parsed).__name__}。"
                f"这是 agentvm 的 bug。")
        # apt.primary / apt.security 必须是列表。写成字典时 cloud-init 的
        # apt_configure 会 AttributeError 崩掉，而 packages 照样装成功——
        # 现场是「镜像没换成 + cloud-init 报错」，两件事看不出关联。
        for key in ("primary", "security"):
            v = (parsed.get("apt") or {}).get(key)
            if v is not None and not isinstance(v, list):
                die(f"cloud-config 里 apt.{key} 必须是列表，实际是 "
                    f"{type(v).__name__}。这是 agentvm 的 bug。")
        if not isinstance(parsed.get("write_files"), list):
            die("cloud-config 里 write_files 必须是列表。这是 agentvm 的 bug。")

    (src / "user-data").write_text(ud)
    (src / "meta-data").write_text(
        f"instance-id: {instance_id}\n"
        f"local-hostname: {_yaml_scalar(vm['name'])}\n")

    out = d / "seed.iso"
    if out.exists():
        out.unlink()
    mkisofs = which("mkisofs") or which("genisoimage") or which("xorrisofs")
    if mkisofs:
        run([mkisofs, "-output", str(out), "-volid", "cidata",
             "-joliet", "-rock", str(src)])
    else:
        # hdiutil 是 macOS 自带，不装 cdrtools 也能配 Linux VM。
        run(["hdiutil", "makehybrid", "-o", str(out), "-iso", "-joliet",
             "-default-volume-name", "cidata",
             "-iso-volume-name", "cidata",
             "-joliet-volume-name", "cidata", str(src)])
    if not out.is_file() or out.stat().st_size < 1024:
        die(f"配置盘没做成：{out}")
    _rmtree(src)
    ok(f"cloud-init 配置盘已生成：{out}")
    return out


def pubkey_text(vm: dict) -> str:
    """取 SSH 公钥文本。没有就现场生成一对，缩进成 YAML 列表项。"""
    kp = Path(vm.get("ssh_key", str(HOME / ".agentvm" / "id_ed25519")))
    if not kp.is_file():
        kp.parent.mkdir(parents=True, exist_ok=True)
        # 必须走 find_tool：launchd/cron 拉起的 install 或 autostart 路径 PATH
        # 被削得很干净，裸 "ssh-keygen" 会 FileNotFoundError 裸栈到用户脸上，
        # 而 run() 只接 CalledProcessError/TimeoutExpired。
        run([find_tool("ssh-keygen"), "-t", "ed25519", "-N", "",
             "-C", f"agentvm-{vm['name']}", "-f", str(kp)], check=False)
        os.chmod(kp, 0o600)
    pub = Path(str(kp) + ".pub")
    if not pub.is_file():
        die(f"没有公钥 {pub}")
    text = pub.read_text().strip()
    return "\n".join(f"      - {text}" for _ in (0,))


def install_plan(vm: dict, distro: dict) -> None:
    """开装之前把要走的路说一遍：几步、大约多久、要占多少空间、哪些能复用。

    为什么值得多说这几行：三条路的耗时差三个数量级——Linux 云镜像下载完就能
    登（十几秒），Windows 要真跑 Setup（八到十二分钟），macOS 要先下 25.6 GB
    的恢复镜像再装（二十几分钟）。原来一开始只有一句「下载 XXX」，用户没法
    判断该去泡杯茶还是守在屏幕前。

    更要紧的是磁盘。macOS 那条路原来下到一半才因为空间不足失败，那时候
    25 GB 已经白下了；而缓存的 IPSW 能让这一次只占系统盘那一份。把账算在
    前面，用户可以在开始前就决定要不要先清点空间。

    「共享」和「独占」分开说：底册镜像、Windows ISO、virtio 驱动、IPSW 都在
    ~/.agentvm/images 里，全机器共用、随 VM 的创建与删除生灭无关；overlay
    和系统盘才是每台虚拟机自己的。混在一起说会让人以为删了一台 VM 就腾出
    了整个镜像的空间。
    """
    kind = distro["kind"]
    name = vm["name"]
    mins = distro.get("install_minutes") or []
    span = f"，预计 {mins[0]} 到 {mins[1]} 分钟" if len(mins) == 2 else ""

    if kind == "linux":
        img = IMAGES / f"{distro['id']}.{distro.get('format', 'qcow2')}"
        have = img.is_file() and img.stat().st_size == distro["size"]
        Emit.log(f"计划：1. 下载或复用底册镜像 → 2. 建 overlay 磁盘 → "
                 f"3. 写 cloud-init 配置盘 → 4. 首次启动并等 SSH{span}")
        Emit.log(f"磁盘：底册镜像 {human(distro['size'])}"
                 + ("（已在本地，多台虚拟机共用）" if have else "（多台虚拟机共用）")
                 + f"，本机额外占用的只有 overlay 实际写入的部分（通常几十 MB）")
    elif kind == "windows":
        iso = IMAGES / f"{distro['id']}.iso"
        have = iso.is_file() and iso.stat().st_size == distro["size"]
        Emit.log(f"计划：1. 下载或复用安装 ISO → 2. 注入 virtio 驱动、"
                 f"写应答文件 → 3. 创建磁盘 → 4. 全自动安装 → "
                 f"5. 优雅关机并用窗口重新拉起{span}")
        Emit.log(f"磁盘：安装 ISO {human(distro['size'])}"
                 + ("（已在本地，共用）" if have else "（共用）")
                 + f"，本机磁盘按 {vm['disk']} 稀疏分配，实际占用随安装写入增长")
        Emit.log("安装期间画面在 app 的「控制台」里，命令行每 20 秒抓一帧到 "
                 f"{vm_dir(name) / 'last-screen.ppm'}")
    else:
        # macOS。IPSW 有全局缓存（~/.agentvm/images/ipsw），第二台起不用再下
        # 那 25.6 GB；这一条要明说，否则用户看到「秒过」会以为坏了。
        # catalog 里 macOS 条目的 url 是空的（地址要运行时问 Apple 拿），所以
        # 不能像另两条那样用 distro["url"] 推文件名——直接看缓存目录里有什么。
        cache_dir = IMAGES / "ipsw"
        cached_file = None
        if cache_dir.is_dir():
            for f in sorted(cache_dir.iterdir()):
                if f.is_file() and f.stat().st_size > 0:
                    cached_file = f
                    break
        if cached_file is None:
            own = vm_dir(name) / "ipsw"
            if own.is_dir():
                for f in sorted(own.iterdir()):
                    if f.is_file() and f.stat().st_size > 0:
                        cached_file = f
                        break
        if cached_file is not None:
            # catalog 的 install_minutes 是「含下载」的数（30 到 60 分钟里
            # 二十来分钟在下载）。镜像已在本地，这一段整段省掉，照实说，
            # 别让人守着屏幕等一个不会发生的下载。
            span2 = "，预计 5 到 10 分钟（镜像已在本地，不含下载）"
            Emit.log(f"计划：1. 复用本地恢复镜像 {cached_file.name} → "
                     f"2. 读取硬件模型 → 3. 建系统盘 → 4. 安装 macOS{span2}")
            Emit.log(f"磁盘：恢复镜像 {human(cached_file.stat().st_size)} 已在本地"
                     f"（全局缓存，0 字节新占空间），系统盘按 {vm['disk']} 稀疏分配，"
                     f"实测装完写入约 27 GB")
        else:
            Emit.log(f"计划：1. 下载恢复镜像（约 {human(distro['size'])}）→ "
                     f"2. 读取硬件模型 → 3. 建系统盘 → 4. 安装 macOS{span}")
            Emit.log(f"磁盘：恢复镜像 {human(distro['size'])} + 系统盘实测约 27 GB，"
                     f"峰值约 52 GB。镜像下完会进全局缓存，之后再装 macOS "
                     f"虚拟机不用再下")
        Emit.log("注意：macOS 的安装必须在能看到画面的进程里跑，"
                 "关掉这个终端或 app 窗口等于放弃这次安装")


def _rmtree(p: Path) -> None:
    """真删掉一棵目录树，删不掉就说删不掉。

    不能 `shutil.rmtree(p, ignore_errors=True)`：winarm 准备介质时会把驱动暂存
    目录建成 555（只读），rmtree 删不掉只读目录里的条目就整棵留下，而
    ignore_errors 把失败吞掉——结果是 delete 报告「已删除」，磁盘上却还留着
    几百 MB，下一次 install 还可能读到上一轮的残留。实测踩过。
    """
    if not p.exists():
        return
    # 先把目录都补上写权限，否则里面的条目删不掉
    for sub in p.rglob("*"):
        if sub.is_dir():
            try:
                sub.chmod(0o755)
            except OSError:
                pass
    try:
        shutil.rmtree(p)
    except OSError as e:
        die(f"删不掉 {p}：{e}")


# ------------------------------------------------------------ Linux 安装 --

def cmd_install_linux(vm: dict, distro: dict) -> dict:
    name = vm["name"]
    Emit.stage("download", f"下载 {distro['name']}")
    img = IMAGES / f"{distro['id']}.{distro.get('format', 'qcow2')}"
    if not (img.is_file() and img.stat().st_size == distro["size"]):
        download(distro["url"], img, "download", distro["size"])
    verify(img, distro)

    Emit.stage("disk", "创建虚拟磁盘")
    d = vm_dir(name)
    disk = d / "disk.qcow2"
    if disk.exists():
        ok(f"磁盘已存在：{disk}")
    else:
        # overlay 而不是整盘拷贝：底册镜像多个 VM 共用一份，各自写入时才
        # 真正占空间。大小给到用户选的值，cloud-init 的 growpart 会把
        # 根分区撑满整个盘。
        #
        # 大小一律转成字节数再交给 qemu-img。原来直接把 vm["disk"]（'32G' 这种
        # 串）传过去：qemu-img 11.1.3 认 '32G' 但**不认 '32GB'**，报
        # `Invalid image size specified: '32GB'`。而同一台机器上
        # `install --source x.iso --disk 32GB`（走 truncate）成功、
        # `configure --disk 32GB`（走字节数）也成功——同一个写法三条路两种命运。
        # 用户从界面上选到的就是 '32G' 这种，一改成 '32GB' 就只在 catalog 路炸。
        run([find_tool("qemu-img"), "create", "-f", "qcow2",
             "-b", str(img), "-F", distro.get("format", "qcow2"),
             str(disk), str(parse_size(vm["disk"]))])
        ok(f"已建 {vm['disk']} overlay 磁盘 {disk}（底册 {img.name}）")

    Emit.stage("seed", "写入 cloud-init 账号配置")
    linux_seed(vm, distro)

    Emit.stage("boot", "首次启动")
    # SSH 等不到就**不能**置 ready。见 start_linux 里那段注释：原来失败和成功
    # 走同一条出口，装失败的机器会被记成装好，用户拿给出的 ssh 命令去连，
    # 连不上，而所有状态查询都说正常。
    if not start_linux(vm, wait_ssh=True):
        save_vm(vm)
        die(f"虚拟机 {name} 没装完：等了 5 分钟 SSH 都没就绪。\n"
            f"VM 还在跑，先看看它卡在哪儿：\n"
            f"  {_self_cmd()} open --name {name}\n"
            f"  {_self_cmd()} screenshot --name {name}\n"
            f"日志：{vm_dir(name) / 'qemu.log'}\n"
            f"确认是客人机自己的问题（apt 卡住、cloud-init 报错）之后，"
            f"加 --force 重装：\n"
            f"  {_self_cmd()} install --name {name} --force")

    ssh_cmd = (f"ssh -i {vm['ssh_key']} -p {vm['ssh_port']} "
               f"{vm['user']}@127.0.0.1")
    vm["ready"] = True
    vm["ssh_command"] = ssh_cmd
    save_vm(vm)
    ok(f"虚拟机 {name} 已就绪。登录：{ssh_cmd}")
    return vm


def local_source_from_url(name: str, url: str) -> Path:
    """把用户自己贴的安装包网址下到本地，返回路径。

    放在 <vm>/iso/ 下而不是 ~/Downloads：那是用户自己的目录，不该由我们往里
    写东西；而 VM 目录本来就是这个 VM 的私有空间，重装、删除都跟着它走。

    校验只做大小，不做哈希——用户贴的地址我们没有可信的校验和，编一个出来
    比不验更糟。下完会明确说「未校验」。
    """
    if not re.match(r"^https?://", url):
        die(f"这不是一个网址：{url}")
    d = vm_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    # 从网址里取文件名。没有路径段（比如是个跳转）就按 iso 命名。
    stem = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    # 白名单：原来的判据只有「非空且含一个点」，于是 `..` 能过关（含点），
    # dest 变成 <vm>/iso/.. 也就是 iso 目录本身，接着 part.replace(dest) 对
    # 一个目录做 replace，OSError 裸栈到用户脸上。`/` 同理。
    if (not stem or "." not in stem or stem.startswith(".")
            or "/" in stem or "\\" in stem or stem in (".", "..")):
        stem = "custom.iso"
    dest = d / "iso" / stem
    Emit.stage("download", f"下载安装包 {stem}")
    expect = remote_size(url)
    if expect and expect < 32 * 1024 * 1024:
        die(f"这个网址只有 {human(expect)}，不像个安装镜像：{url}")
    download(url, dest, sid="download", expect=expect)
    if not dest.is_file() or dest.stat().st_size < 32 * 1024 * 1024:
        die(f"下载下来的东西只有 {human(dest.stat().st_size) if dest.exists() else 0}，"
            f"不像个安装镜像。确认网址给的是 ISO 本身，不是下载页。")
    warn(f"没有校验和可对，只确认了大小是 {human(dest.stat().st_size)}")
    return dest


def _reboot_local_from_disk(vm: dict) -> bool:
    """让一台本地 ISO 装的虚拟机从硬盘进系统。

    只有一种情况会走到这儿：安装器自己走完了，但光盘没拔掉，重启后又退回了
    安装器。fnOS 在最后一屏明说「请拔出安装引导盘」，而 QEMU 这边光盘是
    `-drive ...media=cdrom`，用户没法真的伸手拔线——只能我们替它拔。

    为什么是「停掉再起」而不是 `system_reset`：2026-11-11 实测，拔了盘之后发
    system_reset，固件仍然按 NVRAM 里那个指向光盘的启动项走，介质不在就落到
    UEFI Interactive Shell——用户看到一屏英文 shell，比退回安装器更难懂。
    而 QEMU 整个停掉、启动参数里不带光盘重起，固件只能看到硬盘，一定从盘启。
    这条路也就是用户以后 `start --name X` 走的那条，顺便一起验了。

    返回真表示「已经让它重启了」。
    """
    name = vm["name"]
    d = vm_dir(name)
    ai = autoinstall()
    mon = d / "monitor.sock"
    if not ai.cd_is_empty(mon):
        if not (ai.eject_cd(mon)
                or (ai.eject_cd_force(mon) and ai.cd_is_empty(mon))):
            warn("光盘取不出来，仍然试着从硬盘起一次")
    log("停掉虚拟机，不带安装光盘重新启动")
    _stop_vm_by_name(name)
    vm["attach_iso"] = False
    save_vm(vm)
    start_local(vm)
    return True


def cmd_install_local(vm: dict, source: Path, auto: bool = True,
                      wait_minutes: int = 40) -> dict:
    """从用户自己挑的本地 ISO 装一个虚拟机。

    这条路的安装器是图形交互的（fnOS 这类定制安装器要人选盘、确认格式化、
    看进度、设网络），所以**默认由 autoinstall 那个驱动器自己点完**。
    2026-11-11 实测：fnOS 1.2.07 从「选择安装位置」到 Web 界面 HTTP 200，
    全程自动，没有一个手点的按钮。做法和边界见 autoinstall.py 的 docstring。

    `--no-auto` 退回原来的手动流程：把安装器跑起来，然后明确告诉用户剩下的
    在画面里点完。那条路留着不是偷懒——有些安装器（或者用户自己改过的系统）
    的点法不在驱动器的判据里，硬点比不点更危险。

    画面给谁看：**不给 QEMU 原生窗口，只给 VNC**，理由见 local_qemu_args 里
    `headless` 那段。用户通过 app 里内嵌的控制台操作，那是一个真 RFB 客户端，
    不是隔几秒抓一张图——安装时要拖动滚动条、在输入框里打字，抓图做不到。
    """
    name = vm["name"]
    d = vm_dir(name)
    Emit.stage("disk", "创建虚拟磁盘")
    disk = d / "data.img"
    if disk.exists():
        ok(f"磁盘已存在：{disk}")
    else:
        # truncate 语义：稀疏文件，写到了才真正占空间
        with open(disk, "wb") as f:
            f.truncate(parse_size(vm["disk"]))
        ok(f"已建 {vm['disk']} 空盘 {disk}")

    if not (d / "efi-vars.fd").exists():
        _, vars_tmpl = find_edk2()
        shutil.copy2(vars_tmpl, d / "efi-vars.fd")
        ok(f"已初始化 EFI NVRAM {d / 'efi-vars.fd'}")

    Emit.stage("boot", "启动安装器")
    # 交互安装也不给原生窗口，只给 VNC。这条和 Windows 那条「安装必须无窗口」
    # 是同一个理由（这个版本 QEMU 没有 -no-quit），但这里还多一层：安装过程
    # 本身要在 app 里可视化完成，用户点的是 app 内嵌控制台，不是另一个窗口。
    # 想要原生窗口随时可以：vmkit.py window --name <名字>，或 start --window。
    vm["headless"] = True
    save_vm(vm)
    start_local(vm, with_iso=True)

    if not auto:
        _local_manual_instructions(vm)
        # 不置 ready：装没装完只有用户知道。等他 start 过一次才算数。
        save_vm(vm)
        return vm

    # ---- 自动安装 ----
    Emit.stage("install", "自动安装中")
    ai = autoinstall()
    result = ai.drive(vm, d, budget_minutes=wait_minutes,
                      emit=lambda m, lv="info": Emit.log(m, lv))

    if result == "done":
        return _local_mark_ready(vm)

    if result == "reboot":
        # 装完了，但光盘没弹掉，重启退回了安装器。替它拔盘重启一次。
        Emit.stage("install", "重启进系统")
        _reboot_local_from_disk(vm)
        deadline = time.time() + 600
        while time.time() < deadline:
            if ai.service_ready(vm):
                return _local_mark_ready(vm)
            time.sleep(3)
        warn("拔了光盘重启之后服务还是没起来。盘上应该已经装好了，"
             "您可以自己在画面里看一眼，或者重试一次安装。")

    # manual / timeout / 上面那个拔盘重启也没等到服务：VM 留着运行，
    # 控制权交还用户。这时候绝不能报「装完了」——那是最难排查的一种谎。
    _local_manual_instructions(vm)
    save_vm(vm)
    return vm


def _local_mark_ready(vm: dict) -> dict:
    """自动安装走完了：置 ready、告诉用户从哪儿进这个系统。"""
    vm["ready"] = True
    save_vm(vm)
    Emit.log("")
    Emit.log(f"虚拟机 {vm['name']} 装好了，系统已经自己起来了。", "info")
    _local_after_ready(vm)
    save_vm(vm)
    return vm


def _local_after_ready(vm: dict) -> None:
    """装好之后告诉用户从哪儿进这个系统。"""
    name = vm["name"]
    web = vm.get("web_port")
    if web:
        Emit.log(f"  Web 界面：http://127.0.0.1:{web}", "info")
    port = vm.get("ssh_port")
    if port:
        Emit.log(f"  SSH：ssh -p {port} {vm.get('user', 'agent')}@127.0.0.1",
                 "info")
    Emit.log(f"命令行要看画面：vmkit.py screenshot --name {name}", "info")
    Emit.log(f"想要一个原生 macOS 窗口：vmkit.py window --name {name}", "info")


def _local_manual_instructions(vm: dict) -> None:
    """把控制权交还用户时，说清楚现在屏幕上有什么、下一步点什么。"""
    name = vm["name"]
    Emit.log("")
    Emit.log("安装器已经跑起来了，画面在 app 的「控制台」里。在那里完成安装：",
             "info")
    Emit.log("  1. 选安装位置（我们建的盘会以 vda 出现）", "info")
    Emit.log("  2. 确认分区大小，下一步，确认格式化", "info")
    Emit.log("  3. 等它复制完文件、设好网络，然后重启", "info")
    Emit.log("重启后它会再次进入安装器——这是正常的，直接关机即可。", "info")
    Emit.log(f"然后用 start --name {name} 就从硬盘进系统。", "info")
    Emit.log(f"命令行要看画面：vmkit.py screenshot --name {name}", "info")
    Emit.log(f"想要一个原生 macOS 窗口：vmkit.py window --name {name}", "info")


def extra_disk_args(vm: dict) -> list[str]:
    """附加数据盘的 QEMU 参数。

    系统盘是安装时建的那一个（data.img / disk.qcow2），这里是之后另外加的。
    两者分开记：系统盘的路径和格式由安装方式决定，附加盘是用户随时加的，
    vm.json 里是个列表。

    不设 bootindex：附加盘不该可启动。设了的话固件可能从数据盘启，用户会看到
    「系统不见了」而其实是启动顺序变了。
    """
    out: list[str] = []
    for i, d in enumerate(vm.get("disks", [])):
        did = d["id"]
        if not Path(d["file"]).is_file():
            warn(f"附加盘 {did} 的文件不在了：{d['file']}，跳过")
            continue
        # QEMU 的 -drive/-virtfs 用逗号分隔键值对，路径里一个逗号就会把参数
        # 切成两半（`~/My,Dir` 被当成 path=~/My 加一个叫 Dir 的键），挂载失败
        # 而且报错完全不指回真实原因。原来不检查，用户拿一个含逗号的目录来共
        # 享就静默坏掉。现在直接拒绝，说清要改什么。
        if "," in d["file"] or "," in did:
            die(f"附加盘 {did} 的路径里含逗号，QEMU 的参数语法不支持：{d['file']}\n"
                f"把虚拟机目录或这块盘挪到不含逗号的路径下。")
        out += ["-drive",
                f"file={d['file']},format={d.get('format', 'raw')},if=none,id={did}"]
        if d.get("bus") == "usb-storage":
            out += ["-device", f"usb-storage,drive={did}"]
        else:
            # bootindex 从 10 起：系统盘是 1，安装光盘是 2，附加盘排在后头。
            # **必须给。** 不给的话固件自己枚举顺序，多一块空盘就被插到前面，
            # 启动时先去试那块没有引导记录的盘，试完往下走有可能直接落到 HTTP
            # 启动——表象是「加了一块盘，系统起不来了」。实测踩过。
            out += ["-device", f"virtio-blk-pci,drive={did},bootindex={10 + i}"]
    return out


def share_args(vm: dict) -> list[str]:
    """共享目录的 QEMU 参数（virtio-9p）。

    为什么用 9p 而不是 samba：9p 是 QEMU 内置的，不需要宿主机上跑 smbd，
    也不需要客人机装额外驱动（Linux 内核自带 9p，Windows 需要 virtio-win 里的
    那个驱动）。代价是性能一般，拿来传文件够了，不适合当工作目录。
    """
    out: list[str] = []
    for sh in vm.get("shares", []):
        tag = sh["tag"]
        path = sh["path"]
        if not Path(path).is_dir():
            warn(f"共享目录不在了：{path}，跳过")
            continue
        # 逗号会把 -virtfs 的键值对切断，见 extra_disk_args 里的同一段说明。
        # 标签同时还要防 `=`——`mount_tag=a=b` 会被解析成键 a 值 b。
        if "," in path or "," in tag:
            die(f"共享目录的路径或标签里含逗号，QEMU 不支持：{path} / {tag}")
        if "=" in tag:
            die(f"共享目录的标签里不能含等号：{tag}")
        out += ["-virtfs", f"local,path={path},mount_tag={tag},"
                           f"security_model=mapped,id={tag}"]
    return out


def linux_qemu_args(vm: dict) -> list[str]:
    code, vars_tmpl = find_edk2()
    d = vm_dir(vm["name"])
    seed = d / "seed.iso"
    args = [
        "-name", vm["name"],
        "-accel", "hvf",
        "-cpu", "host",
        "-M", "virt,highmem=on",
        "-m", vm["ram"],
        "-smp", str(vm["cpus"]),
        # UEFI：code 只读共用，vars 每个 VM 私有可写
        "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,file={d / 'efi-vars.fd'}",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd",
        "-device", "usb-tablet",
        # 系统盘。virtio-blk，云镜像里带驱动。
        # bootindex=1 是显式给的：`if=virtio` 自动创建的设备没有启动序号，固件
        # 只能自己枚举，多一块附加盘就被插队（见 extra_disk_args 的注释）。
        # 换成 if=none + 显式 device 不改变 PCI 位置，老虚拟机的 NVRAM 启动项
        # 照样认得。
        "-drive", f"file={d / 'disk.qcow2'},format=qcow2,if=none,id=sysdisk",
        "-device", "virtio-blk-pci,drive=sysdisk,bootindex=1",
        # cloud-init 配置盘。每次都挂：账号或公钥改了重跑 install 就能生效
        "-drive", f"file={seed},format=raw,media=cdrom,readonly=on",
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{vm['ssh_port']}-:22",
        "-device", "virtio-net-pci,netdev=net0",
        # QEMU guest agent：virtio-serial 总线 + 一个 virtserialport，客人机里
        # qemu-guest-agent 把 /dev/virtio-ports/org.qemu.guest_agent.0 接到宿主
        # 的 qga.sock 上。三个参数是一套，少一个 QEMU 起不来（virtserialport
        # 找不到它的 chardev）或者连上没有 agent 应答。
        #
        # chardev 的 id 显式给成 qga0，不靠 QEMU 自动编号：自动编号是
        # chardev0，谁第一个创建谁拿走，以后这条 argv 里再加一个 chardev
        # （比如串口控制台）就会把它挤成 chardev1，而 virtserialport 里写的
        # 还是 chardev0——连上一台 nobody 在听的 socket，现象是「guest agent
        # 时好时坏」，排查半天。
        #
        # server=on,wait=off：QEMU 一启动就建好 socket 等客人机连过来，
        # 不因为有没人连着的端口就卡住启动。
        "-device", "virtio-serial",
        "-chardev", f"socket,path={qga_sock_path(vm)},server=on,wait=off,id=qga0",
        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
        "-device", "virtio-rng-pci",
        "-rtc", "base=localtime",
        "-monitor", f"unix:{d / 'monitor.sock'},server,nowait",
        "-pidfile", str(d / "qemu.pid"),
    ]
    # framebuffer 两种模式都要有。ramfb 是纯内存 framebuffer，不占资源；
    # 没有它 -vga none 下 screendump 会静默失败，排障时看不见画面。
    args += extra_disk_args(vm)
    args += share_args(vm)
    args += ["-vga", "none", "-device", "ramfb"]
    args += ["-display", "none"] if vm.get("headless") else ["-display", "cocoa"]
    args += ["-vnc", f"127.0.0.1:{vm['vnc_display']},share=force-shared"]
    return args


def local_qemu_args(vm: dict, with_iso: bool) -> list[str]:
    """引导一个用户自己挑的本地安装 ISO。

    和另外两条路的区别：

    - 不像 Linux 云镜像那条：没有 overlay 底册、没有 cloud-init 配置盘，
      盘是新建的 raw 空盘，ISO 是主角。
    - 不像 Windows 那条：不注入 virtio 驱动、不写应答文件。Debian 系安装器
      内核自带 virtio 模块，实测直接认出盘（fnOS 安装器列表里出现
      `[vda] HDD 16.00 GB`）；而这类定制安装器是图形交互的，没有应答文件可喂。

    `with_iso` 是关键开关，**装完之后必须为假**。实测：盘上明明写好了
    GPT（BOOT 的 ESP + SYSTEM 的根分区），但只要光盘还挂着，重启就会退回
    安装器而不是进装好的系统——固件在有多个可启动项时不按 bootindex 选，
    而是沿用 NVRAM 里上次的启动项。挂了一夜光盘的结果是用户永远进不去系统，
    还以为是没装成。所以：install 时挂光盘开安装器，start 时不挂只从盘启。

    两个 bootindex 都留着是给 install 阶段用的：空盘没有可启动项，固件自然
    落到光盘。
    """
    code, _ = find_edk2()
    d = vm_dir(vm["name"])
    args = [
        "-name", vm["name"],
        "-accel", "hvf",
        "-cpu", "host",
        "-M", "virt,highmem=on",
        "-m", vm["ram"],
        "-smp", str(vm["cpus"]),
        # UEFI：code 只读共用，vars 每个 VM 私有可写
        "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,file={d / 'efi-vars.fd'}",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd",
        "-device", "usb-tablet",
        # 空盘，virtio-blk。bootindex 显式给 1，好让光盘排在后头
        "-drive", f"file={d / 'data.img'},format=raw,if=none,id=hd",
        "-device", "virtio-blk-pci,drive=hd,bootindex=1",
        "-device", "virtio-rng-pci",
        "-rtc", "base=localtime",
        # 端口只绑 127.0.0.1。web_port 转 NAS 的 Web UI（fnOS 是 5666），
        # ssh_port 备用——有些安装器装完会起 sshd
        #
        # web_port 是 None 时整条 hostfwd 不拼。原来用 .get('web_port', 0)：
        # 键存在而值是 None（23000-23999 全占满、或者从别的 VM 克隆来的）
        # 时它返回 None，拼出 hostfwd=tcp:127.0.0.1:None-:5666，
        # QEMU 起不来而报错说的是端口格式，看不出根因在这儿。
        "-netdev", ("user,id=net0,"
                    f"hostfwd=tcp:127.0.0.1:{vm.get('ssh_port', 0)}-:22"
                    + (f",hostfwd=tcp:127.0.0.1:{vm['web_port']}"
                       f"-:{WEB_GUEST_PORT}" if vm.get("web_port") else "")),
        "-device", "virtio-net-pci,netdev=net0",
        "-monitor", f"unix:{d / 'monitor.sock'},server,nowait",
        "-pidfile", str(d / "qemu.pid"),
    ]
    if with_iso:
        src = vm.get("source")
        if not src or not Path(src).is_file():
            die(f"安装包不见了：{src}。重新指定 --source，或用 start 直接从盘启。")
        args += [
            "-drive", f"file={src},format=raw,if=none,id=cd,media=cdrom,readonly=on",
            "-device", "usb-storage,drive=cd,bootindex=2",
        ]
    # framebuffer 设备不能省。没有它 -vga none 下 screendump 会静默失败，
    # 排障时看不见画面；而这类安装器全靠 framebuffer 画界面。ramfb 同时是
    # VNC 的帧来源——app 里内嵌控制台看到的就是这块显存。
    args += extra_disk_args(vm)
    args += share_args(vm)
    args += ["-vga", "none", "-device", "ramfb"]
    # 安装期间 headless：不给原生窗口，只给 VNC。
    #
    # 两个理由叠在一起：
    # 1. 这个版本 QEMU 没有 -no-quit，点窗口红按钮或 ⌘W 会直接把 QEMU 退掉
    #    （系统日志里 mouseUp → terminate: → NSTerminateNow），安装跟着没，
    #    而 qemu.log 是空的、退出码还是 0。Windows 那边实测连着踩三次。
    # 2. 这条路的安装过程本来就要在 app 里可视化完成——app 内嵌的是一个真
    #    RFB 客户端（拖滚动条、打字都行），不是隔几秒抓一张图。再多弹一个
    #    原生窗口只是同一个画面的第二个入口，还带着上面那个坑。
    # 想要原生窗口：vmkit.py window --name X（会重启一次客人机，显示模式是
    # QEMU 启动参数，跑起来之后改不了）。
    args += ["-display", "none"] if vm.get("headless") else ["-display", "cocoa"]
    args += ["-vnc", f"127.0.0.1:{vm['vnc_display']},share=force-shared"]
    return args


def start_local(vm: dict, with_iso: bool | None = None) -> None:
    """起一个本地 ISO 装的 VM。

    with_iso 不传就按 vm["attach_iso"] 走，默认假——不挂光盘，从盘启。
    理由见 local_qemu_args 的注释：挂着光盘重启有可能退回安装器，表现为
    「装完了又回到安装界面」，用户会以为没装成而重点一遍。

    传 True 只有两种时候：install 阶段（要进安装器），以及用户在「配置」里
    明确挂了「挂载安装光盘」——那种是想重跑安装器，属于明知故犯，界面上会警告。
    """
    if with_iso is None:
        with_iso = bool(vm.get("attach_iso", False))
    if qemu_pid(vm):
        ok(f"VM 已在运行（pid {qemu_pid(vm)}）")
        return
    d = vm_dir(vm["name"])
    if not (d / "data.img").exists():
        die("盘还没建。先跑 install --source <iso>。")
    log("启动 QEMU（HVF 硬件加速）")
    with open(d / "qemu.log", "ab") as lf:
        subprocess.Popen([find_tool("qemu-system-aarch64"),
                          *local_qemu_args(vm, with_iso)],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        if qemu_pid(vm):
            ok(f"已启动 pid {qemu_pid(vm)}，日志 {d / 'qemu.log'}")
            return
    die(f"QEMU 启动失败，看 {d / 'qemu.log'}")


def find_edk2() -> tuple[Path, Path]:
    cands = [Path(os.environ.get("HOMEBREW_PREFIX", str(HOME / "hb"))),
             Path("/opt/homebrew"), Path("/usr/local")]
    for base in cands:
        code = base / "share" / "qemu" / "edk2-aarch64-code.fd"
        vars_ = base / "share" / "qemu" / "edk2-arm-vars.fd"
        if code.is_file() and vars_.is_file():
            return code, vars_
    die("找不到 edk2-aarch64-code.fd / edk2-arm-vars.fd，它们随 QEMU 一起安装")


def _pid_alive(pid: int) -> bool:
    """一个进程号现在是不是真的有个活进程。

    不能 `os.kill(pid, 0)`：僵尸进程照样返回成功。spawn_detached_install 靠这个
    判断「上次安装是不是还在跑」，而安装是被强杀之后最常见的状态就是僵尸——
    用 os.kill 会永远认为上一次还在跑，从此再也装不了任何一个虚拟机。
    判据和 qemu_pid 一致（排除 Z 状态），只是这里不核对进程名：安装进程是
    我们自己的 python，重启之后命令行可能变。
    """
    if pid <= 0:
        return False
    r = subprocess.run(["ps", "-p", str(pid), "-o", "state=,comm="],
                       capture_output=True, text=True)
    line = r.stdout.strip()
    if not line:
        return False
    return not line.split(None, 1)[0].startswith("Z")


def qemu_pid(vm: dict) -> int | None:
    """VM 的 QEMU 进程号，没在跑就返回 None。

    原来只用 `os.kill(pid, 0)` 判断在不在，两个坑：

    1. **僵尸进程也算「在跑」。** 对僵尸发 0 号信号一样成功。QEMU 是
       start_new_session 出去的孤儿进程，父进程早退了，回收要靠 launchd，
       强杀之后有几十毫秒到几秒它还是僵尸。实测踩过：`window` 命令强杀之后
       `start_local` 看到 pid 还「活着」，直接返回「VM 已在运行」——结果既没
       有窗口也没重启，用户看到的是虚拟机凭空没了，而日志说它还在跑。
    2. **pid 会被复用。** 只认 pidfile 里的数字，不核对是不是我们的 QEMU，
       别的进程占了同一个号就会被当成 VM 在跑。所以用 ps 连进程名一起核对。

    ps 一次调用同时回答「在不在」「是不是僵尸」「是不是 QEMU」，比发信号可靠，
    代价是每次多起一个进程（几毫秒），status 这类轮询接口完全可以接受。
    """
    # macOS 客人机没有 QEMU：跑它的是 vmz（Swift + VZ），pid 写在
    # macvm.pid 里。这里一起认了，否则 status 对 macOS VM 永远报「已关机」，
    # 而用户明明开着窗口——界面和实际相反是最难查的一类 bug。
    if vm.get("kind") == "macos":
        return macos_pid(vm)
    p = vm_dir(vm["name"]) / "qemu.pid"
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text().strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    r = subprocess.run(["ps", "-p", str(pid), "-o", "state=,comm="],
                       capture_output=True, text=True)
    line = r.stdout.strip()
    if not line:
        return None
    parts = line.split(None, 1)
    if parts[0].startswith("Z"):
        return None
    if len(parts) < 2 or "qemu" not in parts[1]:
        return None
    return pid


def _read_macvm_pid(path: Path) -> tuple[str, int] | None:
    """读 macvm.pid，返回 (宿主类型, pid)。文件不在或内容坏了返回 None。

    内容形如 `vmz:1234` 或 `app:1234`：承载 VM 的是 vmz 进程还是
    AgentVM.app 自己。MacVMEngine.macWritePID 明确区分这个前缀，因为
    vmz stop 只准对 vmz 宿主发信号——对 app 发 SIGTERM 会把整个 app
    打死（app 没有 SIGTERM 处理），而用户看到的是「已停」，实际 app 没了、
    VM 属于非优雅停机。

    原来这里直接 int(text)：带前缀的内容一律 ValueError → 恒返回 None。
    后果是一整条假绿：status/vm_record 对所有 macOS VM 永远报「已关机」，
    stop 报「没在运行」返回 0，delete 于是不停机就 _rmtree，把 disk.img
    从正在跑的 VZ 机器脚下抽走。

    老格式（只有数字）当成 vmz 宿主，跟 Swift 侧 macReadPIDFile 一致。
    """
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    host, sep, pid_text = text.partition(":")
    if not sep:
        # 没有冒号：老格式，整串就是 pid
        host, pid_text = "vmz", text
    host = host.strip()
    pid_text = pid_text.strip()
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    if pid <= 0:
        return None
    if host not in ("vmz", "app"):
        host = "vmz"
    return host, pid


def macos_pid_entry(vm: dict) -> tuple[str, int] | None:
    """(宿主, pid)。宿主是 "vmz" 或 "app"，进程不在了返回 None。

    要区分宿主时必须用这个，不能只用 macos_pid：VM 在 app 里跑着，
    命令行没有任何办法停它，而停不下来的机器也不能删。
    """
    p = vm_dir(vm["name"]) / "macvm.pid"
    if not p.is_file():
        return None
    entry = _read_macvm_pid(p)
    if not entry:
        return None
    _host, pid = entry
    if not _pid_alive(pid):
        return None
    return entry


def macos_pid(vm: dict) -> int | None:
    """macOS 客人机的进程号，没在跑就 None。

    两种宿主都算在跑：VM 在 app 里也是真的在跑，界面显示「已关机」
    而用户明明开着窗口是最难查的一类 bug。能不能停是另一个问题，
    由 macos_pid_entry 回答。
    """
    entry = macos_pid_entry(vm)
    return entry[1] if entry else None


def linux_ssh_ready(vm: dict, timeout: float = 8) -> bool:
    """真发一条 SSH 命令拿结果，不是探端口。

    QEMU 的 hostfwd 在 VM 启动那一刻就把宿主机侧端口绑好了，nc -z 必然通，
    跟客人机起没起来无关。只有真登录成功才算就绪。
    """
    key = vm["ssh_key"]
    r = subprocess.run(
        [find_tool("ssh"), "-i", key, "-p", str(vm["ssh_port"]),
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", f"ConnectTimeout={int(timeout)}",
         "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
         f"{vm['user']}@127.0.0.1", "true"],
        capture_output=True, text=True)
    return r.returncode == 0


def start_linux(vm: dict, wait_ssh: bool = False) -> bool:
    """起一台 Linux 虚拟机。返回 SSH 到底等没等到（不等时恒为真）。"""
    if qemu_pid(vm):
        ok(f"VM 已在运行（pid {qemu_pid(vm)}）")
        return
    d = vm_dir(vm["name"])
    for f in ("disk.qcow2", "seed.iso"):
        if not (d / f).is_file():
            # 这句话必须是能照着做的一条路。原来只写「重新 install」，而
            # install 紧跟着回「xxx 已存在。换个名字，或加 --force 重装。」——
            # 两句都是死胡同：既起不动，也没法原地修好，用户只能自己去删目录。
            # 实测踩过（2026-11-10，用户原话「VM 不完整，缺 disk.qcow2。
            # 重新 install。一直跳出无法关闭」）。
            die(f"VM 不完整，缺 {f}——这一台上次没装完。\n"
                f"原地修好它（会清掉 {d} 重新装）：\n"
                f"  {_self_cmd()} install --name {vm['name']} --force\n"
                f"确定不要了：\n"
                f"  {_self_cmd()} delete --name {vm['name']}")
    if not (d / "efi-vars.fd").is_file():
        _, vars_tmpl = find_edk2()
        shutil.copy(vars_tmpl, d / "efi-vars.fd")

    args = linux_qemu_args(vm)
    log("启动 QEMU（HVF 硬件加速）")
    with open(d / "qemu.log", "ab") as lf:
        subprocess.Popen([find_tool("qemu-system-aarch64"), *args],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        if qemu_pid(vm):
            ok(f"已启动 pid {qemu_pid(vm)}，日志 {d / 'qemu.log'}")
            break
    else:
        die(f"QEMU 启动失败，看 {d / 'qemu.log'}")

    if not wait_ssh:
        return True
    Emit.stage("ssh", "等 SSH 就绪")
    deadline = time.time() + 300
    t0 = time.time()
    while time.time() < deadline:
        if linux_ssh_ready(vm):
            ok(f"SSH 已就绪，用时 {int(time.time() - t0)} 秒")
            _wait_guest_agent(vm)
            return True
        if not qemu_pid(vm):
            die(f"QEMU 中途退出了，看 {d / 'qemu.log'}")
        Emit.progress("ssh", (time.time() - t0) / 300,
                      int(time.time() - t0), 300, 0)
        time.sleep(3)
    # 返回假，让调用方知道这台**没装成**。
    #
    # 原来这里只是 warn 一句然后照常 return None，控制流接着往下走，失败和成功
    # 走的是同一条出口。后果是一串假绿：ready=True、ssh_command 已给出、
    # vm_broken 返回空串、_refuse_existing 会说「已存在，加 --force」而不是
    # 「上次没装完」。用户拿给出的命令去连，连不上，而所有状态查询都说正常。
    # 2026-11-11 审计补上。
    warn("等了 5 分钟 SSH 仍未就绪。VM 还在跑，用 open 看窗口，"
         f"或 screenshot 抓画面，或看 {d / 'qemu.log'}")
    return False


def _wait_guest_agent(vm: dict, timeout: float = 180.0) -> bool:
    """等 guest agent 上线，最多 timeout 秒，期间报进度。返回等没等到。

    为什么是「等」而不是「探一次就走」：SSH 就绪不等于 guest agent 可用，
    而这两件事在日志里原来长得一模一样（都是「已就绪」）。差在哪儿：SSH 由
    云镜像自带，agent 要 cloud-init 现装——实测 Ubuntu 24.04 ARM64，SSH 12 秒
    就通，而 qemu-guest-agent 要到 140 秒后才 active，因为 cloud-init 的
    apt-get update 要先下 150 MB 索引（universe 的 Packages 一个文件就 72 MB）。
    原来这里只探一次，不通就 warn，于是 install 报告「已就绪」，用户随后用
    `guest` 子命令或 agent 优先关机时才发现不通，而排障时没有任何线索指向 apt。

    有上限、报进度：等不是无限期的。Debian 13 那条 apt 卡在 sqv 里的路
    （docs/PITFALLS.md M8）等一辈子也不会来，到点就 warn 走人——VM 照样
    能用，只是 agent 那条路暂时不走。

    上限给 180 秒而不是 30 秒：实测最慢的一条（Ubuntu 24.04）是 140 秒，
    留一倍余量；而 catalog 里 Linux 的预计时间本来就写「1 到 3 分钟」，
    等满了也还在承诺范围内。
    """
    if vm.get("kind") != "linux":
        return True
    sock = qga_sock_path(vm)
    if not sock.exists():
        # socket 都不在说明 QEMU 没带这个 chardev，那是装机参数问题，
        # 不是客人机没装包——两种说法给用户指的方向完全不同，也不用等。
        warn(f"没有 guest agent 套接字（{sock}）。这台 VM 起不来"
             f"qemu-guest-agent，guest 子命令和 agent 优先的关机都用不了。")
        return False
    try:
        qa = qga()
    except OSError:
        warn(f"加载不了 guest agent 客户端（{qga_lib_path()}）。"
             f"guest 子命令和 agent 优先的关机都用不了。")
        return False

    Emit.stage("agent", "等 guest agent 上线（cloud-init 正在装 "
                        "qemu-guest-agent）")
    t0 = time.time()
    last = -1.0
    while time.time() - t0 < timeout:
        if qa.qga_ping(sock, timeout=5):
            ok(f"guest agent 已就绪，又用了 {int(time.time() - t0)} 秒")
            return True
        if not qemu_pid(vm):
            warn(f"QEMU 中途退出了，agent 不等了。看 {vm_dir(vm['name']) / 'qemu.log'}")
            return False
        el = time.time() - t0
        if el - last >= 10:
            last = el
            Emit.progress("agent", el / timeout, int(el), int(timeout), 0)
            Emit.log(f"还在等 guest agent（已 {int(el)} 秒）…… "
                     f"apt 要下 150 MB 索引，慢是正常的", "info")
        time.sleep(3)
    warn("等了 3 分钟 guest agent 仍不通。SSH 和窗口都能用，VM 是可用的；"
         "但 guest 子命令和 stop 的 agent 优先关机要等它装上。\n"
         "    在客人机里看：cloud-init status && systemctl status "
         "qemu-guest-agent\n"
         "    apt 卡住的话见 docs/PITFALLS.md 的 M8（Debian 系 apt 挂在 "
         "sqv 签名验证上）")
    return False


# ----------------------------------------------------------- Windows 安装 --

_winarm = None


def winarm():
    """惰性加载 winarm-vm.py。

    Windows 的介质改造（注驱动、写应答文件、重建 ISO）已经在
    winarm-vm.py 里实测跑通过一整轮，vmkit 不重写，直接委托。重写一遍
    等于把已经踩过的坑再踩一次，而这条路径恰恰是最容易踩坑的。
    """
    global _winarm
    if _winarm is None:
        spec = importlib.util.spec_from_file_location("winarm", WINARM)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]

        class _Die(Exception):
            pass

        # 把它的日志接到 vmkit 的事件流上。它有几处直接 print，那些会以
        # 非 JSON 行落到 stdout，客户端按普通日志显示，不影响解析。
        mod.log = lambda m: Emit.log(str(m), "info")
        mod.ok = lambda m: Emit.log(str(m), "info")
        mod.warn = lambda m: Emit.log(str(m), "warn")
        mod.die = lambda m: (_ for _ in ()).throw(_Die(str(m)))
        _winarm = mod
        _winarm._Die = _Die  # type: ignore[attr-defined]
    return _winarm


_autoinstall = None


def autoinstall():
    """惰性加载 autoinstall.py——本地安装包的一键自动安装驱动器。

    和 winarm() 同一个理由：只有本地 ISO 这条路真的用到它。另外几条路
    （Linux 云镜像、Windows 应答文件、macOS IPSW）压根没有图形向导要点，
    为一个用不到的特性引依赖没有意义。
    """
    global _autoinstall
    if _autoinstall is None:
        spec = importlib.util.spec_from_file_location("autoinstall",
                                                      AUTOINSTALL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        _autoinstall = mod
    return _autoinstall


def _stop_vm_by_name(name: str) -> None:
    """按名字停一台虚拟机。

    cmd_stop 收的是 argparse 的 Namespace，而这里是从安装流程内部调的，
    手上只有一个字符串。造一个最小 Namespace 比把 cmd_stop 拆成两半干净。
    """
    cmd_stop(argparse.Namespace(name=name))


_qga = None
_qga_missing_said = False


def qga_lib_path() -> Path:
    """guest agent 客户端（scripts/lib/qga.py）在哪。

    默认就在本文件旁边的 lib/ 下。多认一个环境变量 AGENTVM_QGA_LIB 是因为
    app 打包时只拷了 vmkit.py、winarm-vm.py、catalog.json 三样进
    Resources/backend/，scripts/lib/ 整个不在里面（见 app/build.sh 的 cp 清单，
    那是不能碰的文件）。从 app 里起的 VM 因此找不到这个客户端——要么在那边
    的清单里补一行 cp，要么用这个变量指向仓库里的那一份。
    """
    env = os.environ.get("AGENTVM_QGA_LIB")
    if env:
        return Path(env).expanduser()
    return HERE / "lib" / "qga.py"


def qga():
    """惰性加载 scripts/lib/qga.py（QEMU guest agent 客户端）。

    和 winarm() 一个理由：只有真的用到 guest agent 时才加载。用 importlib
    按路径装而不是 `from lib import qga`：lib/ 不是包，那样得把 scripts/
    塞进 sys.path，而 lib 是个太通用的名字，会和别处同名模块撞。
    """
    global _qga
    if _qga is None:
        lib = qga_lib_path()
        if not lib.is_file():
            # FileNotFoundError 是 OSError，qga_shutdown_wait 接的就是它。
            raise FileNotFoundError(str(lib))
        spec = importlib.util.spec_from_file_location("qga", lib)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        _qga = mod
    return _qga


def qga_sock_path(vm: dict) -> Path:
    """这台 VM 的 guest agent 套接字。

    和 monitor.sock、qemu.pid 放在同一个目录，是项目现有约定：临时文件落
    VM 目录而不是 /tmp。/tmp 会被系统清理，而且多用户机器上谁能连上这个
    socket，谁就能在客人机里以 root 执行命令。
    """
    return vm_dir(vm["name"]) / "qga.sock"


def qga_shutdown_wait(vm: dict, wait: int = 120) -> bool:
    """客人机里装了 qemu-guest-agent 就走它关机，返回是否已经停下。

    排在 SSH 前面的理由：这条路不依赖客人机网络。sshd 没起来、IP 变了、
    防火墙把 22 挡了，SSH 那条都试不了，而 guest agent 走的是
    virtio-serial，只要 QEMU 带着这个设备就通。

    **socket 存在不等于 agent 在跑**：QEMU 只要带了这个 chardev 就会把
    qga.sock 建出来，而 guest 里要等 cloud-init 装上 qemu-guest-agent 并起
    服务；加参数之前就起着的老 VM 更是只有 socket 没有 agent。所以先 ping
    探活，ping 不通就安静返回假，让调用方走原来的路——在关机链路上报错
    只会把一条本来能走通的路搅黄。
    """
    sock = qga_sock_path(vm)
    if not sock.exists():
        return False
    try:
        qa = qga()
    except OSError:
        # 只有客户端文件真的丢了才会走到这儿。VM 还在跑，不能为这个 die，
        # 后面的 ACPI 和强杀照样能停。
        #
        # 但要说出来，而且只说一次：静默跳过的话，用户会以为 guest agent 装了
        # 却没生效，跑去客人机里排查，而问题在宿主机这一侧。说一次是因为 stop
        # 不是热循环，但一条命令里可能停好几台，每台都刷一遍没有意义。
        global _qga_missing_said
        if not _qga_missing_said:
            _qga_missing_said = True
            warn(f"guest agent 客户端不在：{qga_lib_path()}，跳过这条关机路。\n"
                 f"  app 打包时 scripts/lib/ 没进 Resources/backend/，"
                 f"可用 AGENTVM_QGA_LIB 指向仓库里的那一份。")
        return False
    try:
        if not qa.qga_ping(sock, timeout=5):
            return False
        log("guest agent 可达，让它走自己的关机流程")
        qa.qga_shutdown(sock)
    except qa.QGAError as e:
        # ping 刚通，所以原因不可能是「agent 没在跑」——那是上一行已经排除
        # 掉的。这里的真实原因只剩：连不上、超时、或这个 agent 版本不认识
        # guest-shutdown。文案要能指回去，别再让人去 guest 里查一个已经
        # 证明在跑的服务。
        warn(f"guest agent 关机没成：{e}")
        return False
    for _ in range(wait):
        time.sleep(1)
        if not qemu_pid(vm):
            ok("已优雅关机")
            return True
    warn(f"等了 {wait} 秒 guest agent 还没关掉，试下一条路")
    return False


def virtio_ready() -> Path:
    """virtio 驱动包解压后的目录。没有就下载解压。

    目录结构是实测出来的，不是猜的：virtio-win-0.1.285.tar.xz 解开后根下
    直接就是 ARM64/、Balloon/、NetKVM/ 这些，没有额外的 drv/ 一层。所以
    VIRTIO_PATH 指向的是「解开后的那个目录」本身。winarm-vm.py 的
    collect_drivers 会去 <这里>/ARM64/w11 找合集驱动，再去
    <这里>/<驱动名>/w11/ARM64 找 per-driver 驱动，两边都要有。
    """
    drv = ASSETS / "virtio" / "drv"
    if (drv / "ARM64" / "w11").is_dir():
        return drv
    cat = load_catalog()
    v = cat.get("virtio", {})
    if not v.get("url"):
        die("catalog.json 里没有 virtio 驱动包地址")
    drv.parent.mkdir(parents=True, exist_ok=True)
    tar = drv.parent / Path(v["url"]).name
    Emit.stage("virtio", "下载 virtio 驱动")
    if not (tar.is_file() and tar.stat().st_size > 0):
        download(v["url"], tar, "virtio")
    if v.get("sha256"):
        verify(tar, {"sha256": v["sha256"]})
    Emit.stage("virtio", "解压 virtio 驱动")
    # 归档是不可信数据：解压到独立目录，不用 tar 的花哨参数
    drv.mkdir(parents=True, exist_ok=True)
    run([find_tool("tar"), "-xJf", str(tar), "-C", str(drv)])
    if not (drv / "ARM64" / "w11").is_dir():
        die(f"解压了但没看到 {drv}/ARM64/w11，包结构变了？")
    ok(f"virtio 驱动就绪：{drv}")
    return drv


def screendump(mon: Path, out: Path) -> bool:
    """通过 QEMU 监控套接字抓一帧 framebuffer。

    headless 安装期间唯一的「看客人机停在哪」的手段。ramfb 是设备不是
    显示前端，所以 -display none 下照样能抓到——实测 UEFI Shell 画面
    清晰可读。
    """
    if not mon.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(10)
            sk.connect(str(mon))
            # 绝对不能发 quit，那是让 QEMU 退出
            sk.sendall(f"screendump {out}\n".encode())
            time.sleep(1.5)
        return out.is_file() and out.stat().st_size > 1024
    except OSError:
        return False


def cmd_install_windows(vm: dict, distro: dict) -> dict:
    name = vm["name"]
    w = winarm()

    Emit.stage("download", f"下载 {distro['name']}")
    iso = IMAGES / f"{distro['id']}.iso"
    if not (iso.is_file() and iso.stat().st_size == distro["size"]):
        download(distro["url"], iso, "download", distro["size"])
    verify(iso, distro)

    virtio_ready()

    # winarm 全靠环境变量配置，把 vm.json 翻译过去
    d = vm_dir(name)
    env = {
        "VM_DIR": str(d),
        "ISO_PATH": str(iso),
        "VIRTIO_PATH": str(virtio_ready()),
        "DISK_SIZE": vm["disk"],
        "RAM_SIZE": vm["ram"],
        "CPU_CORES": str(vm["cpus"]),
        "USERNAME": vm["user"],
        "PASSWORD": vm["password"],
        "SSH_KEY": vm["ssh_key"],
        "SSH_PORT": str(vm["ssh_port"]),
        "RDP_PORT": str(vm["rdp_port"]),
        "VNC_DISPLAY": str(vm["vnc_display"]),
        # 安装阶段强制 headless，不看 vm["headless"]。
        #
        # 实测踩过：HEADLESS=0 时 QEMU 用 Cocoa 前端开一个原生窗口，而这个
        # 版本 QEMU 没有 -no-quit（-help 里只有 -no-reboot/-no-shutdown），
        # 点窗口红按钮或按 ⌘W 会走 NSApplication terminate: 直接把 QEMU
        # 退掉——系统日志里能清楚看到 mouseUp → terminate: → NSTerminateNow。
        # 安装要跑十几分钟，这期间那个窗口没有任何用处，却是一个能把整个
        # 安装干掉、且不留任何日志的开关。所以安装期间不给窗口，只留 VNC
        # 备用通道；要看画面用 screenshot 命令抓 ramfb。
        # 装完之后 start（不带 --headless）照样开窗口，不影响日常使用。
        "HEADLESS": "1",
    }
    os.environ.update(env)
    cfg = w.Cfg()

    Emit.stage("prepare", "改造安装介质（注驱动、写应答文件、重建 ISO）")
    try:
        w.cmd_prepare(cfg)
        Emit.stage("create", "创建虚拟磁盘")
        w.cmd_create(cfg)
        Emit.stage("boot", "安装 Windows（约 10 到 15 分钟）")
        w.cmd_start(cfg, install=True)
        Emit.stage("ssh", "等安装完成")
        deadline = time.time() + vm.get("wait_minutes", 40) * 60
        t0 = time.time()
        last_shot = 0.0
        ssh_up = False
        while time.time() < deadline:
            if w.ssh_ready(cfg):
                ok(f"SSH 已就绪，用时 {int(time.time() - t0)} 秒")
                ssh_up = True
                break
            if not w.qemu_running(cfg):
                # 不能只说「看 qemu.log」。实测那个文件在这种情况下是空的：
                # 窗口被关掉时 QEMU 走 NSApplication terminate，stderr 一个字
                # 都不写，退出码还是 0，既没崩溃报告也没日志。只说看日志等于
                # 把人指向一个空文件。所以这里把最可能的原因和留存的最后一帧
                # 画面一起说出来。
                shot = d / "last-screen.ppm"
                extra = (f"最后画面已存 {shot}，可用 screenshot 或转成 png 看"
                         if shot.is_file() and shot.stat().st_size > 1024
                         else "没抓到画面")
                die("QEMU 中途退出了，Windows 安装没跑完。"
                    "安装阶段本来就是无窗口运行的，所以不是窗口被关；"
                    "最常见的原因是客人机重启时被外部信号打断，"
                    f"或宿主机资源不够。{extra}。"
                    f"虚拟机现状用 status --name {name} 看，"
                    "确认后可以重跑 install 接着装")
            # 滚动留一帧。安装要十几分钟，中途死了总得知道它停在哪一步；
            # 而且这也是 headless 期间用户唯一能看画面的入口。
            if time.time() - last_shot >= 20:
                last_shot = time.time()
                screendump(d / "monitor.sock", d / "last-screen.ppm")
            Emit.progress("ssh", (time.time() - t0) /
                          (vm.get("wait_minutes", 40) * 60),
                          int(time.time() - t0),
                          vm.get("wait_minutes", 40) * 60, 0)
            time.sleep(5)
        else:
            warn("超时仍未就绪，VM 还在跑。看日志或 open 开窗口。")
        # 超时**不能**置 ready。原来这里 warn 一句就照常往下走，失败和成功走
        # 同一条出口：装失败的 Windows 会被记成装好，界面给「远程桌面 / VNC」
        # 入口，用户连上一个没装完的系统。同 start_linux 里那段注释。
        if not ssh_up:
            save_vm(vm)
            die(f"虚拟机 {name} 没装完：等了 "
                f"{vm.get('wait_minutes', 40)} 分钟 SSH 都没就绪。\n"
                f"VM 还在跑，先看看它卡在哪儿：\n"
                f"  {_self_cmd()} open --name {name}\n"
                f"  {_self_cmd()} screenshot --name {name}\n"
                f"确认是 Setup 卡住或 virtio 盘没被识别之后，加 --force 重装：\n"
                f"  {_self_cmd()} install --name {name} --force")
    except winarm()._Die as e:
        die(str(e))

    # 安装是 headless 跑的，而用户要的是一个能看的窗口。所以装完之后优雅关机，
    # 再用 Cocoa 前端拉起来。装都装完了，重启一次没有代价；反倒是让用户对着一个
    # 「装好了但没窗口」的 VM 自己想办法更糟。
    # 失败不判安装失败：VM 已经就绪，窗口可以事后用 start 补。
    try:
        Emit.stage("window", "打开虚拟机窗口")
        # 光改环境变量不够：cfg 在前面就已经构造完了，headless 是那时从环境
        # 读进来存下的普通属性，之后再改 os.environ 它不跟着变。实测第一次改
        # 只动了 os.environ，结果重启出来的 QEMU 还是 -display none，用户依然
        # 没有窗口。两处都要设。
        os.environ["HEADLESS"] = "0"
        cfg.headless = False
        w.cmd_stop(cfg)
        w.cmd_start(cfg, install=False)
        vm["headless"] = False
        ok("虚拟机窗口已打开")
    except Exception as e:  # noqa: BLE001 — 收尾失败不能把安装判失败
        warn(f"安装已完成，但打开窗口没成功：{e}。用 start --name {name} 再试")

    # winarm 的 free_vnc_display() 在 5900+display 被占时会自动 +1，真实编号
    # 写进 <vm>/vnc.display。不回读的话 vm.json 里还是旧编号，界面给的
    # 「open vnc://127.0.0.1:xxxxx」连的是没人听的端口，而 status 显示一切
    # 正常。cmd_start / cmd_window 两条路早就接了 _sync_windows_vnc，安装这条
    # 自己直接调 w.cmd_start 的路漏了。2026-11-11 审计补上。
    _sync_windows_vnc(vm)

    vm["ready"] = True
    vm["ssh_command"] = (f"ssh -i {vm['ssh_key']} -p {vm['ssh_port']} "
                         f"{vm['user']}@127.0.0.1")
    vm["rdp_command"] = (f"open vnc://127.0.0.1:{5900 + vm['vnc_display']} "
                         f"（或 Microsoft Remote Desktop 连 "
                         f"127.0.0.1:{vm['rdp_port']}）")
    save_vm(vm)
    ok(f"虚拟机 {name} 已就绪。登录：{vm['ssh_command']}")
    return vm


# ------------------------------------------------------------ 生命周期 --

_VM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")

# 快照名白名单。会原样进 qemu-img 的 argv，所以禁连字符开头（`--tag=-c`
# 会被 qemu-img 当成自己的选项），长度也限死。
_SNAP_TAG_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}")


def check_vm_name(name: str) -> None:
    """VM 名白名单。所有入口共用，包括 detach 那条以前漏掉的路。

    这个名字会拼进文件路径（~/.agentvm/vms/<name>）、plist 标签、以及
    PowerShell 单引号字符串。挡在入口处比在每个使用点各自判断可靠——
    漏一个使用点就是一次路径穿越或命令注入。

    必须 fullmatch：match 只要求开头对上，而 `$` 允许匹配末尾换行之前，
    "vm\n后面任意内容" 能过校验——拼进路径和 plist 的就是另一回事了。
    """
    if not _VM_NAME_RE.fullmatch(name or ""):
        die(f"虚拟机名字只能以字母数字开头，含字母数字和 . _ -，"
            f"最长 63 个字符：{name!r}")


def _macos_vm_stub(name: str, args) -> dict:
    """macOS VM 的最小 vm.json。

    vmkit 的 list/status/start/configure 全认 vm.json，而 macOS 的运行时
    配置在 vmz 写的 macvm.json 里。两边各写各的，字段不重叠。
    """
    return {
        "name": name,
        "distro": args.distro or "macos",
        "kind": "macos",
        "cpus": args.cpu or 4,
        "ram": args.mem or "8G",
        "disk": args.disk or "80G",
        "user": args.user or "agent",
        # macOS VM 没有这三个端口：VZ 的 NAT 不对外监听，也没有 VNC。
        # 给 0 而不是省略——app 那侧这些字段不是可选的，缺了整条条目会被
        # 静默丢掉，界面表现为「没有虚拟机」。
        "ssh_port": 0,
        "rdp_port": None,
        "vnc_display": 0,
        "ssh_key": str(args.ssh_key or (HOME / ".agentvm" / "id_ed25519")),
        # headless 恒真：VZ 的画面只能在 app 进程里显示，没有第二种模式。
        "headless": True,
        "wait_minutes": args.wait,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ready": False,
    }


def _vmz_password_args(name: str, password: str | None) -> list[str]:
    """把密码写进 0600 文件，返回 --password-file 参数。

    为什么不拼进 argv：`ps` 对所有本地用户可见完整命令行，而 macOS 安装要
    跑几十分钟。项目规约写的是「密钥一律从文件读取，落地 0600，用完即删，
    全程不经过命令行参数」——这条路径一开始没照着做。

    文件放在 VM 目录里而不是 /tmp：同一个 VM 的安装只可能有一个（install.pid
    互斥），不会串；而 /tmp 里留一个明文密码文件更容易被扫到。
    vmz 读完即删。
    """
    if not password:
        return []
    d = vm_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "vmz.pass"
    # O_EXCL：已存在就说明有另一个安装在跑，别覆盖。
    try:
        fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            os.unlink(str(f))
        except OSError:
            pass
        fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(password)
    os.chmod(str(f), 0o600)
    return ["--password-file", str(f)]


def resolve_password(args) -> str | None:
    """把 --password-file 读进 args.password，读到手就删掉文件。

    为什么要有文件这条路：app 侧用 `--password <明文>` 起 vmkit，父进程和
    Windows/Linux 子进程的明文口令就在 argv 里，`ps -ax` 对所有本地用户
    可见，而装一个 Windows 要十几分钟——暴露窗口跟着安装时长走。
    _vmz_password_args 早就换成「0600 文件 + 读完即删」，install/configure
    这两个入口却一直是明文 argv，口径不一是漏的不是取舍。

    与 _vmz_password_args 同一套做法：文件放调用方给的位置，读进来立刻
    unlink，中途失败也一样——口令不该在磁盘上过夜。

    注意：app 侧（app/Sources/*.swift）目前仍在拼 --password，换成
    --password-file 由负责 app 的那一路做；Python 侧先把入口和读取逻辑
    备好，两边接口一致就能直接换。
    """
    f = getattr(args, "password_file", None)
    if not f:
        return getattr(args, "password", None)
    p = Path(f).expanduser()
    try:
        raw = p.read_bytes()
    except OSError as e:
        die(f"读不了 --password-file {p}：{e}")
    try:
        p.unlink()
    except OSError:
        pass
    val = raw.decode("utf-8", "replace").strip()
    if not val:
        die(f"--password-file {p} 是空的")
    args.password = val
    return val


def _forward_password(argv: list[str], name: str, value: str | None) -> list[str]:
    """后台安装时把 argv 里的口令换成一个 0600 文件路径。

    后台安装是「父进程照原样再起一遍自己」，口令就这么原样进了子进程的
    argv，而它要活十几分钟——父进程那份已经够糟，子进程这份一样能 ps 到。
    改成父进程先落一个 0600 文件，子进程用 --password-file 指过去，
    读完即删（见 resolve_password）。文件在 VM 目录里而不是 /tmp：
    同一个 VM 的安装只可能有一个（install.pid 互斥），不会串。

    value 由调用方从 args.password 取（resolve_password 已经把
    --password-file 并进那一项），这里只负责把两种写法从 argv 上摘掉。
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--password", "--password-file") and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith(("--password=", "--password-file=")):
            i += 1
            continue
        out.append(a)
        i += 1
    if value:
        d = vm_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        f = d / "vmkit.pass"
        fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        os.chmod(str(f), 0o600)
        out += ["--password-file", str(f)]
    return out


def _vmz_install_env() -> dict:
    """给 vmz 的环境。PATH 要补全，因为 launchd 拉起的 app 不带 shell PATH。"""
    env = dict(os.environ)
    home = str(HOME)
    env["PATH"] = f"{home}/hb/bin:/opt/homebrew/bin:/usr/local/bin:" \
        f"/usr/bin:/bin:/usr/sbin:/sbin"
    env["HOME"] = home
    return env


# install.pid 上那把 flock 的 fd。**必须留着不关**：进程退出时内核才释放锁，
# 而这个进程就是整个安装期间要占着锁的那一个。存到模块级变量，免得被 GC 掉
# （GC 会调 close，锁就提前放了）。
_INSTALL_LOCK_FD: int | None = None


def _claim_install_lock(name: str) -> Path:
    """占住这台虚拟机的 install.pid，返回那个路径。

    **互斥必须由 flock 做，不能只靠 O_CREAT|O_EXCL。** O_EXCL 只保证「建文件」
    那一下不重复，而这里要保证的是整个安装期间只有一个人在里面。第一版写的是
    O_EXCL + 「上一个死了就 unlink 重试」，实测八个线程同时抢有四个赢家：每个
    输家都读到占位的 0、判定「没在跑」、unlink、然后重试——unlink 把别人刚建出
    来的文件也一起删了，锁就形同虚设。2026-11-11 自己写测试量出来的。

    flock(LOCK_EX|LOCK_NB) 由内核保证同一时刻只有一个持有者，而且上一个持有者
    进程退出时内核自动释放——「上一个崩了/被强杀了，文件还在」这种情况天然能
    接手，不需要 unlink 重试那一套。

    文件内容仍然写 pid：app 的「停止」和「接上还在跑的安装」都读它。写的是
    os.getpid()，也就是真正干活的那个进程（后台子进程，或前台安装本身）。
    拿到 flock 之后还多查一句 _pid_alive：正常情况下拿不到锁就进不来，这一句
    只为旧版本留下的「有 pid 没锁」文件兜底。
    """
    global _INSTALL_LOCK_FD
    d = vm_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    pidf = d / "install.pid"
    try:
        fd = os.open(str(pidf), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        die(f"建不了 {pidf}：{e}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        try:
            other = int(pidf.read_text().strip())
        except (OSError, ValueError):
            other = 0
        die(f"这个虚拟机的安装已经在跑（pid {other or '?'}）。等它结束，"
            f"或先停掉它：\n"
            f"  kill {other or '?'}\n"
            f"日志：{d / 'install.log'}")
    # 拿到锁了。文件里可能还写着上一个的 pid，它已经不在了（否则锁在它手上），
    # 但如果不是「旧版本留下的有 pid 没锁」而是真有活进程，如实拒绝。
    try:
        old = int(os.pread(fd, 32, 0).decode().strip() or "0")
    except (OSError, ValueError):
        old = 0
    if old > 0 and old != os.getpid() and _pid_alive(old):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        die(f"这个虚拟机的安装已经在跑（pid {old}）。等它结束，或先停掉它：\n"
            f"  kill {old}\n"
            f"日志：{d / 'install.log'}")
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.lseek(fd, 0, os.SEEK_SET)
    # fd 必须留着：进程退出时内核才释放锁。存到模块级变量，免得被 GC 掉。
    if _INSTALL_LOCK_FD is not None:
        try:
            os.close(_INSTALL_LOCK_FD)
        except OSError:
            pass
    _INSTALL_LOCK_FD = fd
    return pidf


def _set_install_pid(pidf: Path, pid: int) -> None:
    """把 install.pid 的内容改写成 pid。只改内容，不重新建文件——重建等于放弃
    那把 flock。写失败不致命：app 那边读到空或读不到会按「没在跑」处理，最坏是
    让用户重点一次安装。"""
    try:
        fd = os.open(str(pidf), os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, str(pid).encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def _install_signal_guard(name: str) -> None:
    """安装期间收到 SIGTERM/SIGINT 时，把这次装的那台客人机一起带走。

    为什么必须要有：app 的「停止」按钮杀的是 install.pid，而非 macOS 路径下
    那个 pid 就是 vmkit 自己。vmkit 原来没有信号处理器，收到 SIGTERM 当场
    死掉——而 QEMU 是 start_new_session 出去的另一个进程组，它还在继续往
    data.img 写。用户看到「已取消」，实际上安装还在跑：此时同名重装，
    winarm 看到磁盘已存在就复用、看到 qemu_running 就报「VM 已在运行」，
    新安装顺着旧 VM 走到「SSH 就绪」，状态完全对不上。

    只杀本次安装名下的 pid 文件。原来遍历 VMS.glob("*/qemu.pid") 把所有 VM
    的 QEMU 都 SIGTERM：取消一次安装，会把用户其他正在跑的虚拟机一起干掉，
    而那些机器跟这次安装毫无关系，数据还可能没落盘。

    杀的办法是读 pid 文件，不是 killpg：QEMU 主动脱离了我们的进程组，
    pgid 对不上。

    **SIGHUP 也要接。** 2026-11-10 实测：装到一半把终端窗口关掉（或
    `vmkit install ... | head` 让管道先关），宿主机这边收到 SIGHUP/SIGPIPE，
    而这里原来只挂了 SIGTERM 和 SIGINT——于是 vmkit 静默消失，QEMU 作为
    start_new_session 出来的孤儿继续跑，占着 2 GB 内存、SSH 端口和 VNC 端口，
    而用户以为「取消了就什么都没了」。pid 文件还在，所以 `stop` 还能停，
    但没有任何东西告诉用户这儿还挂着一台。
    """
    def handler(signum, frame):
        d = vm_dir(name)
        pids: list[int] = []
        q = d / "qemu.pid"
        if q.is_file():
            try:
                pids.append(int(q.read_text().strip()))
            except (OSError, ValueError):
                pass
        # macOS 这条路的客人机是 vmz，pid 在 macvm.pid 里，而且带宿主前缀
        entry = _read_macvm_pid(d / "macvm.pid")
        if entry:
            pids.append(entry[1])
        for pid in pids:
            if pid > 0 and _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        # install.pid 也要清。os._exit 不走 finally，main() 里那道清理在这儿
        # 不生效；而留着一个死 pid 的后果见 main() 里 install 那段注释。
        try:
            (d / "install.pid").unlink()
        except OSError:
            pass
        # 自己也退。用 os._exit 而不是 sys.exit：信号处理器里抛异常会被
        # Python 转成 traceback，而这时候用户已经点了「停止」，不想看栈。
        os._exit(130)

    # SIGPIPE 也要：Python 默认把它设成 SIG_IGN、往关闭的管道写时抛
    # BrokenPipeError，那个异常没人接，一样是静默退出加一个孤儿 QEMU。
    # 挂上 handler 之后它和 SIGTERM 走同一条清理路。
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # 不在主线程时装不了（后台安装那条路是 setsid 出去的新进程，
            # 那里面就是主线程，能装上）。装不上不致命。
            pass


def spawn_detached_install(args) -> int:
    """把安装甩到后台跑，父进程立刻返回。

    为什么需要这一步：装一个 Windows 要十几分钟，而驱动它的 GUI 是个普通
    macOS app——用户关掉窗口 app 就退出，子进程跟着没，十几分钟的下载和
    安装全部作废。实测踩过：下完 4.3 GB 镜像、校验通过、正在改造介质的
    时候 app 窗口被关，整个安装凭空消失。

    做法是起一个 setsid 的兄弟进程（start_new_session=True），stdout 重定向
    到 <vm>/install.log，父进程只负责把日志路径交出去。setsid 让它脱离 app
    的会话，app 退出时它不会收到 SIGHUP。

    父进程不写 vm.json，只建目录。vm.json 由真正干活的那个子进程写，
    避免两边同时写一个文件。
    """
    name = args.name
    # 名字先过白名单，而且要在碰任何路径之前。
    #
    # vm_dir() 是 VMS / name 直接拼，`--name /tmp/x` 解析出来就是 /tmp/x
    # （pathlib 里绝对路径会盖掉前面的），`--name ../../foo` 直接跳出
    # ~/.agentvm/vms。下面 _refuse_existing 会去 stat 那个路径上的 vm.json，
    # 所以校验必须排在它前面——读一个用户指定的任意路径也比不读好，而写
    # （mkdir / install.log / install.pid）是原来的老问题，一起堵掉。
    check_vm_name(name)
    # 先把 --password-file 并进 args.password：下面 macOS 那条分支直接用
    # args.password 造 0600 文件，另一条分支要把它转成文件传给子进程。
    # 两处都在这儿取，免得同一个入口两种行为。
    resolve_password(args)
    # 「已存在」这道校验必须在分叉之前、在写任何文件之前做。
    #
    # 原来只写在 cmd_install 里，而 app 永远走 --detach：父进程一声不响就把
    # 子进程起出来、写好 install.pid、向 app 报「安装已开始」，子进程一秒后
    # 死在「已存在」上。app 那边已经切到「正在安装」页——用户看到一句错误钉在
    # 一个没有出口的页面上，既重试不了也关不掉。实测踩过（2026-11-10）。
    #
    # 对 macOS 还多一层：下面那个分支会 save_vm(_macos_vm_stub(...))，
    # 不在这儿拦住的话，重装一台装好的 mac 会先把它的 vm.json 覆写成空壳。
    _refuse_existing(name, args)
    d = vm_dir(name)

    # 父进程只做一次便宜的存在性检查，把最常见的情况（连点两次）提前挡掉并给一句
    # 能照着做的话。真正的互斥在子进程里：下面 Popen 出来的那个进程会再跑一遍
    # cmd_install，由 _claim_install_lock 用 flock 占住 install.pid 并写上自己的
    # pid。父进程不占锁——它几十毫秒就退出了，占着没有任何意义，反而会让子进程
    # 以为自己输给了别人。
    #
    # 原来这里是「is_file() 读一下 → …几百毫秒后 Popen → write_text」，检查和
    # 落盘之间隔着 catalog 读取、find_vmz()、argv 组装，两条几乎同时执行的
    # install 都能通过检查，各自起一个安装进程，然后各自写 install.pid，后写的
    # 覆盖前一条的——两个安装同时 qemu-img create 同一个 disk.qcow2、同时 save_vm
    # 同一个 vm.json、同时起两个 QEMU 抢同一组端口，磁盘和配置都损坏，而两份日志
    # 交错在一起无法复盘。install.pid 是这里唯一的互斥手段，而它是在子进程起来
    # 之后才落盘的。2026-11-11 审计补上。
    pidf = d / "install.pid"
    if pidf.is_file():
        try:
            old = int(pidf.read_text().strip())
        except (OSError, ValueError):
            old = 0
        if old > 0 and old != os.getpid() and _pid_alive(old):
            die(f"这个虚拟机的安装已经在跑（pid {old}）。等它结束，或先停掉它：\n"
                f"  kill {old}\n"
                f"日志：{d / 'install.log'}")

    d.mkdir(parents=True, exist_ok=True)
    logf = d / "install.log"

    # macOS 这一种的后端是另一个程序（vmz，Swift 写的，因为 VZ 只有
    # Swift/ObjC API）。这里必须起它而不是再跑一遍 vmkit.py：VZMacOSInstaller
    # 需要一个活的 VZVirtualMachine，而那个对象只在 vmz 进程里。
    # 甩后台的机制照旧（setsid + 日志重定向），所以 app 关掉安装照样跑完。
    distro = None
    if args.distro:
        # 不能直接用 find_distro：它找不到就 die，而那声错误会由父进程先打到
        # 界面上，紧接着父进程又报「安装已开始」——用户看到一句错误加一句成功，
        # 而子进程会把同样的错误再打一遍，日志里同一个问题出现三次。
        # 实测踩过（2026-11-10）。这里只要「是不是 macOS」一个判据，
        # 找不到就当不是，真正的报错留给子进程说一次。
        cat = load_catalog()
        distro = next((x for x in cat.get("distros", [])
                       if x.get("id") == args.distro), None)

    if distro is not None and distro.get("kind") == "macos":
        vmz = find_vmz()
        if not vmz:
            die("找不到 vmz。macOS 虚拟机需要它：\n"
                "  ./install.sh 会把它编出来并装到 ~/.agentvm/bin/vmz\n"
                "或者自己编：swiftc -O -sdk $(xcrun --sdk macosx --show-sdk-path) "
                "-target arm64-apple-macos14.0 -o ~/.agentvm/bin/vmz "
                "app/Sources/MacVMEngine.swift scripts/vmz/main.swift\n"
                "（编完要签名：codesign -s - --entitlements "
                "app/Resources/entitlements.plist ~/.agentvm/bin/vmz）")
        argv = [str(vmz), "install", "--name", name]
        for flag, attr in (("--cpu", "cpu"), ("--mem", "mem"),
                           ("--disk", "disk"), ("--user", "user")):
            v = getattr(args, attr, None)
            if v is not None:
                argv += [flag, str(v)]
        if args.force:
            argv.append("--force")
        if args.source:
            # vmz 认的是 --ipsw，vmkit 这边叫 --source（跟另外两种安装包
            # 共用一个参数名）。两套名字在边界上换一次，别让用户记两套。
            argv += ["--ipsw", args.source]
        # 密码走文件，不走命令行参数。项目自己的规约是「密钥一律从文件读取，
        # 经 base64 传输，落地 0600，用完即删，全程不经过命令行参数」，
        # 而 macOS 这条路径一开始是把密码直接拼进 argv 的：ps 对所有本地
        # 用户可见完整 argv，安装要跑几十分钟，等于把密码挂在外面那么久。
        argv += _vmz_password_args(name, args.password)
        env = _vmz_install_env()
        # vm.json 现在就得写。原来只由「真正干活的那个子进程」写，而 macOS
        # 的子进程是 vmz，它只写自己的 macvm.json、永远不写 vmkit 的 vm.json：
        # 装完之后 vmkit list 是空的、status/start/configure 全部报「没有这个
        # 虚拟机」，app 里也看不到这台机器。这不是「以后再补」的小疏漏，
        # 是装完就不可见。
        d.mkdir(parents=True, exist_ok=True)
        save_vm(_macos_vm_stub(name, args))
    else:
        # 另外两种：再跑一遍 vmkit 自己，把 --detach 去掉。
        # 这一段必须在——原来它被 macOS 分支整段替换掉了，结果 app
        # （永远拼 --detach）装 Windows/Linux 100% UnboundLocalError，
        # 父进程连 install.log 都没交出去。
        argv = [sys.executable, str(Path(__file__).resolve())]
        for a in sys.argv[1:]:
            if a == "--detach":
                continue
            argv.append(a)
        # 口令不能顺着 argv 传给子进程，换成 0600 文件指过去
        argv = _forward_password(argv, name, args.password)
        # 环境跟着父进程走，不加任何标记。子进程占 install.pid 靠的是 flock：
        # 父进程没占，所以子进程不会「输给自己」。第一版在这儿设过
        # AGENTVM_INSTALL_CHILD 让子进程别占锁，那是在父进程也占的前提下才有
        # 必要——改成父进程只做存在性检查之后，这个标记不再需要，删掉少一个隐式
        # 契约。2026-11-11。
        env = None

    # 追加而不是覆盖：重跑一次不会把上次的日志冲掉，出问题时能回看
    with logf.open("a") as f:
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 开始安装 {name} =====\n")
        f.flush()
        proc = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(HERE), env=env)
    # pid 落盘。GUI 靠它判断「这个安装还在跑吗、要不要接着显示进度」。
    # 不用 pgrep 匹配命令行：命令行里参数顺序不固定，写出来的模式很容易对不上，
    # 而且会误匹配到别的进程。
    # 父进程只是把子进程的 pid 抄一份在这儿，方便 app 在子进程还没跑到占锁那一步
    # 时也能读到东西；子进程随后会用自己的 pid 覆写（_claim_install_lock）。
    # 不重新建文件——那会放弃锁，也让 app 有一小段时间读到不存在的文件。
    _set_install_pid(pidf, proc.pid)
    Emit.done({"name": name, "log": str(logf), "pid": proc.pid,
               "detached": True})
    return 0


def _refuse_existing(name: str, args) -> None:
    """同名虚拟机已经存在、又没给 --force 时，给人一句能照着做的话。

    两处 install（catalog 路、本地安装包路）和后台安装路都要这道校验。
    原来它只写在 cmd_install 里，而 app 永远走 --detach：父进程一声不响就把
    子进程起出来、写好 install.pid、向 app 报「安装已开始」，子进程一秒后死
    在「已存在」上。app 那边已经切到「正在安装」页——用户看到一句错误钉在
    一个没有出口的页面上，既重试不了也关不掉。实测踩过（2026-11-10）。

    旧 VM 是半成品时要特别说一句：那种机器起不动也修不好，--force 正是给它
    准备的，不说清楚用户就以为数据会没，不敢按。
    """
    if args.force or not vm_file(name).exists():
        return
    broken = vm_broken(load_vm(name))
    if broken:
        die(f"{name} 已存在，而且它上次没装完（{broken}）——这样一台既起不动"
            f"又修不好的机器，加 --force 会清掉 {vm_dir(name)} 重新装：\n"
            f"  {_self_cmd()} install --name {name} --force")
    die(f"{name} 已存在。换个名字，或加 --force 重装（里面的数据一起没）。")


def _reopen_install_log(name: str) -> None:
    """--force 重装之后，把 stdout/stderr 重新指回新的 install.log。

    为什么需要：后台安装（app 走的那条）是父进程先打开 install.log（append）
    再把子进程起出来，子进程继承那个 fd。而 --force 会 `_rmtree` 整个 VM 目录，
    那个文件被 unlink——子进程还在往一个已经不在目录树里的 inode 写。用户看到
    的是 install.log 不存在、或者停在上一轮的内容，app 的 tailLog 一个事件都
    读不到，界面是一个完全不动的「正在安装」。实测踩过（2026-11-10，
    复现步骤：app 里对一台没装完的虚拟机点「重装」）。

    只在确实是后台子进程时调用（调用方用 install.pid 的存在判断）。命令行
    前台跑的用户 stdout 是终端，不能把它改道进文件里。

    install.pid 也要补回来：父进程写的那份跟着旧目录一起被删了，而 app 的
    「停止」按钮和重启后的「接上还在跑的安装」都靠这个文件找进程。少了它，
    用户点停止打在一个空文件上，什么都不会发生。
    """
    logf = vm_dir(name) / "install.log"
    try:
        logf.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(logf), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        fd = -1
    try:
        if fd >= 0:
            # 父进程写的那行表头跟着旧 inode 一起没了，在这儿补一份，日志才连得上。
            os.write(fd, f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}"
                         f" 开始安装 {name} =====\n".encode())
            # sys.stdout 是包在 fd 1 上的对象，换掉 fd 1 它就跟着换文件，
            # 不需要重新赋值 sys.stdout。
            os.dup2(fd, 1)
            os.dup2(fd, 2)
    except OSError:
        pass
    finally:
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        _set_install_pid(vm_dir(name) / "install.pid", os.getpid())
    except OSError:
        pass


def cmd_install(args) -> int:
    name = args.name
    # 白名单必须排在**所有**用 name 拼路径的东西之前：下面
    # _install_signal_guard 的信号处理器里就用 vm_dir(name) 拼路径，
    # resolve_password 也要拿它建 0600 文件。排在 platform 检查之后、
    # _install_signal_guard 之后都来不及。
    #
    # 用全仓库共用的那份白名单，**不能自己写一份宽的**。
    # 原来这里（和 cmd_clone 的 --new-name）写的是
    # `name.replace("-","").replace("_","").isalnum()`，比 check_vm_name 的
    # `[A-Za-z0-9][A-Za-z0-9._-]{0,62}` 宽：Python 的 isalnum() 对 Unicode
    # 字母数字都为真，于是 `install --name 虚拟机` 能建出目录
    # ~/.agentvm/vms/虚拟机，而随后 start/stop/status/delete 全都在 main() 的
    # check_vm_name 上 die。产物是一台 list 看得见、delete 删不掉的虚拟机，
    # 只能手工 rm。而且 install 是 main() 里最早分派的几个命令之一，走不到
    # 4741 行那道统一校验。2026-11-11 审计补上。
    # （顺带：这里原来同一句检查写了两遍，复制粘贴留下的。）
    check_vm_name(name)
    resolve_password(args)
    _install_signal_guard(name)
    if platform.machine() != "arm64" or platform.system() != "Darwin":
        die("只在 Apple Silicon 的 macOS 上有效。")
    # 占 install.pid。**前台和后台子进程都走这一句**，而且这是唯一真正的互斥点。
    #
    # 前台不占的话，它会和后台那条互不设防：前台安装从来不写 install.pid
    # （只有 spawn_detached_install 写），于是「终端里跑着一个前台 install」的
    # 同时再跑一次 `install --detach`，后台父进程读到的还是空文件，照常起第二个
    # 安装。后台子进程（就是不带 --detach 再跑一遍 cmd_install 的那个）占这把锁，
    # 于是两个子进程之间也互斥——原来它们是各写各的 install.pid，后写的覆盖前一条。
    # 释放靠 main() 里 install 那段 finally 的 unlink，以及 _install_signal_guard
    # 的信号处理器（os._exit 不走 finally）。
    #
    # 为什么不需要 AGENTVM_INSTALL_CHILD 之类的标记区分「我是子进程」：
    # spawn_detached_install 的父进程只做存在性检查、不占锁，所以子进程不会
    # 「输给自己」。第一版让父进程也占、再用环境变量告诉子进程别占，那是多余的。
    _claim_install_lock(name)
    _refuse_existing(name, args)

    # --force 必须在分叉之前、在下载之前就把旧目录清掉。
    #
    # 两个顺序上的理由：
    #   · 必须在本地包那条路之前：那条路会用 local_source_from_url 把用户贴的
    #     网址下到 <vm>/iso/，下完再删目录等于把几 GB 的下载一起扔掉；
    #   · 必须在 catalog 那条路自己那段之前：它下面 2860 行起也有一份一样的
    #     处理，而目录已经没了之后 vm_file(name).exists() 为假，那份会自然跳过，
    #     不会二次清盘。
    # 原来本地包这条路上什么都没有：只写一个全新的 vm.json 就往下跑，三个后果
    # 叠加——(a) cmd_install_local 里 `if disk.exists(): ok("磁盘已存在")`，
    # 旧 data.img 连数据一起复用，而 --help 写的是「同名就整个目录删掉重装」；
    # (b) start_local 第一句就是 `if qemu_pid(vm): ok("VM 已在运行")`，安装器
    # 压根没重启，autoinstall 对着那台已经进系统的旧 VM 点；(c) 新 vm.json 是
    # 全新字典，没有 disks / shares 键，附加盘条目从配置里消失但文件还在盘上，
    # 成为孤儿。_refuse_existing 里「加 --force 会清掉 <dir> 重新装」这句话
    # 在这条路上因此是不成立的。2026-11-11 审计补上。
    if args.force and vm_file(name).exists():
        # macOS 的进程号在 macvm.pid 里，qemu_pid 收的字典没 kind 就永远返回
        # None——那会对一个正在跑的 macOS VM 说「没在运行」然后删盘。
        old = load_vm(name)
        if qemu_pid(old if old else {"name": name}):
            die(f"{name} 正在运行。先 stop 再 --force 重装，"
                f"不然会删掉正在用的盘。")
        # 后台安装那条路：父进程在分叉之前就打开了 install.log（append 模式），
        # 而下面这一删会把它一起 unlink。先记下「我们是那个后台子进程」，
        # 好在删完之后把 stdout 重新接回新文件。
        detached = (vm_dir(name) / "install.pid").is_file()
        warn(f"--force：清掉 {vm_dir(name)} 重新装（里面的数据一起没）")
        _rmtree(vm_dir(name))
        # 锁必须重新占。上面 _rmtree 把 install.pid 一起 unlink 了，而 flock 是绑在
        # 那个（已经不在了的）inode 上的：fd 还开着、锁还算占着，但路径上已经没有
        # 文件，别人新建一个就是另一把锁。不补这一句的话，--force 重装的整个安装期
        # 间是没有任何互斥的。2026-11-11 审计补上。
        _claim_install_lock(name)
        if detached:
            _reopen_install_log(name)

    # 本地安装包这条路不走 catalog：没有下载地址、没有校验和、没有推荐配置，
    # 全靠用户给的一个路径。所以先把它分叉出来，别让后面的 catalog 查找绊倒。
    #
    # 唯一的例外是 macOS + --source：那个 source 是本地 IPSW，而 macOS 的
    # 安装本来就要从恢复镜像来，catalog 条目只是个「默认从 Apple 下」的标记。
    # 不在这儿放行的话，用户手上已经有一个 IPSW 时会被塞进 local 那条路，
    # 拿它当一个普通 ISO 装——而那整套（cloud-init 种子盘、QEMU 参数）对
    # macOS 完全不适用，失败信息还会很难懂。
    macos_ipsw = None
    if args.source and args.distro:
        try:
            if find_distro(load_catalog(), args.distro).get("kind") == "macos":
                macos_ipsw = args.source
        except SystemExit:
            macos_ipsw = None

    if (args.source or args.source_url) and not macos_ipsw:
        if args.source and args.source_url:
            die("--source 和 --source-url 只能给一个")
        if args.source_url:
            # 用户自己贴的网址：先下到 <vm>/iso/ 再当本地包装。和 catalog 那条
            # 共用 download()，所以断点续传和真实进度都是现成的。
            # 没有校验和可对（用户自己贴的地址，我们没有可信的哈希），所以
            # 只校验大小，并且下完明确说一声没校验——不装作验过。
            src = local_source_from_url(name, args.source_url)
        else:
            src = Path(args.source).expanduser()
            if not src.is_file():
                die(f"找不到安装包：{src}")
            if not os.access(src, os.R_OK):
                die(f"读不了这个文件：{src}（检查权限）")
            if src.stat().st_size < 32 * 1024 * 1024:
                die(f"这个文件只有 {human(src.stat().st_size)}，不像个安装镜像")
        # 一律 .get()：一个写了一半的 vm.json 缺 ssh_port 时，这里 KeyError
        # 会让整条 install 崩掉，而用户只是想装一个跟那个坏字段无关的新 VM。
        # （vm_record 那边早就踩过并修好了同一个坑，这两处是漏的。）
        taken = {v.get("ssh_port", 0) for v in all_vms() if v.get("name") != name}
        taken_rdp = {v.get("rdp_port", 0) or 0 for v in all_vms() if v.get("name") != name}
        taken_vnc = {v.get("vnc_display", 0) or 0 for v in all_vms() if v.get("name") != name}
        taken_web = {v.get("web_port", 0) or 0 for v in all_vms() if v.get("name") != name}
        vm = {
            "name": name,
            # distro 记文件名，界面好显示「装的是哪个包」
            "distro": src.name,
            "kind": "local",
            "source": str(src.resolve()),
            "cpus": args.cpu or 2,
            "ram": args.mem or "2G",
            # 本地包里有些要的空间不小（fnOS 的 TRIMFS 要 5.8 GB），默认给大点
            "disk": args.disk or "32G",
            "user": args.user or "agent",
            "password": args.password or "agentvm",
            "ssh_port": alloc_port(PORT_BASE_SSH, taken),
            "rdp_port": None,
            # alloc_port 在 23000-23999 全占满时返回 0。存成 0 而不是 None：
            # local_qemu_args 那里 `vm.get('web_port', 0)` 对「键存在但值是
            # None」返回 None，会拼出 hostfwd=tcp:127.0.0.1:None-:5666，
            # QEMU 直接起不来。
            "web_port": alloc_port(PORT_BASE_WEB, taken_web) or None,
            "vnc_display": alloc_vnc_display(taken_vnc),
            "ssh_key": str(args.ssh_key or (HOME / ".agentvm" / "id_ed25519")),
            # 装的时候就是无窗口的：画面走 VNC，用户在 app 内嵌控制台里点完
            # 整个安装器。cmd_install_local 会再确认一次，这里先存对，
            # 免得 install 还没跑到那儿 status 就显示成有窗口。
            "headless": True,
            "wait_minutes": args.wait,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ready": False,
        }
        vm_dir(name).mkdir(parents=True, exist_ok=True)
        save_vm(vm)
        Emit.stage("source", f"使用本地安装包 {src.name}"
                            f"（{human(src.stat().st_size)}）")
        vm = cmd_install_local(vm, src, auto=not args.no_auto,
                               wait_minutes=args.wait)
        Emit.done(vm_record(vm))
        return 0

    cat = load_catalog()
    distro = find_distro(cat, args.distro)
    # _refuse_existing 已经在函数开头拦过一次，这里再拦是因为 catalog 查找
    # （可能因为 distro 名字不对而 die）和本地包那条路都在它之后。重复调用
    # 是幂等的：给了 --force 或目录不存在就立刻返回。
    # 名字的白名单在函数开头用 check_vm_name 统一过了，这里不再自己写一份
    # （原来这儿还有一句更宽的 isalnum 检查，2026-11-11 审计删掉）。
    _refuse_existing(name, args)
    if vm_file(name).exists():
        # --force 的停机和清盘已经在函数开头做过了（见那儿的长注释：必须做在
        # 本地包那条路和下载之前）。走到这里 vm.json 还在，只有一种情况——
        # 两次调用之间又有人建了一台同名的。只补停机检查并清掉，不重复 warn。
        old = load_vm(name)
        if qemu_pid(old if old else {"name": name}):
            die(f"{name} 正在运行。先 stop 再 --force 重装，不然会删掉正在用的盘。")
        _rmtree(vm_dir(name))

    # 一律 .get()：一个写了一半的 vm.json 缺 ssh_port 时，这里 KeyError 崩掉，
    # 而用户只是想装一个跟那个坏字段无关的新虚拟机。
    taken = {v.get("ssh_port", 0) for v in all_vms() if v.get("name") != name}
    taken_rdp = {v.get("rdp_port", 0) or 0 for v in all_vms() if v.get("name") != name}
    taken_vnc = {v.get("vnc_display", 0) or 0 for v in all_vms() if v.get("name") != name}
    ssh_port = alloc_port(PORT_BASE_SSH, taken)
    rdp_port = alloc_port(PORT_BASE_RDP, taken_rdp)
    vnc_display = alloc_vnc_display(taken_vnc)

    vm = {
        "name": name,
        "distro": distro["id"],
        "kind": distro["kind"],
        "cpus": args.cpu or distro.get("default_cpus", 2),
        "ram": args.mem or distro.get("default_ram", "2G"),
        "disk": args.disk or distro.get("default_disk", "32G"),
        "user": args.user or distro.get("default_user", "agent"),
        "password": args.password or "agentvm",
        "ssh_port": ssh_port,
        "rdp_port": rdp_port if distro["kind"] == "windows" else None,
        "vnc_display": vnc_display,
        "ssh_key": str(args.ssh_key or (HOME / ".agentvm" / "id_ed25519")),
        "headless": bool(args.headless),
        "wait_minutes": args.wait,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ready": False,
    }
    vm_dir(name).mkdir(parents=True, exist_ok=True)
    save_vm(vm)

    # 先把要走的路说一遍（几步、多久、占多少空间、哪些能复用）。放在这儿而
    # 不是各条 cmd_install_* 里面：local 那条没有 catalog、没有预计时间，
    # 说不出来就不说，见 cmd_install_local 开头那几行 Emit.log。
    install_plan(vm, distro)
    if distro["kind"] == "windows":
        vm = cmd_install_windows(vm, distro)
    elif distro["kind"] == "macos":
        vm = cmd_install_macos(vm, distro, ipsw=macos_ipsw)
    else:
        vm = cmd_install_linux(vm, distro)
    Emit.done(vm_record(vm))
    return 0


def cmd_install_macos(vm: dict, distro: dict, ipsw: str | None = None) -> dict:
    """macOS 客人机的安装：委托给 vmz，然后等它跑完。

    为什么是「等它跑完」而不是起完就返回：VZMacOSInstaller 需要一个活的
    VZVirtualMachine，而那个对象只在 vmz 进程里活着。vmkit 这边起一个
    `vmz install` 前台跑，把它的 JSON 事件原样转出去——下载进度、安装阶段
    都这么到界面上。和 Windows 那条路一样，安装期间界面不能是死的。

    这条路上 vmkit 只做三件事：建目录、写 vm.json、当中继。
    """
    vmz = find_vmz()
    if not vmz:
        die("找不到 vmz。macOS 虚拟机需要它：\n"
            "  ./install.sh 会把它编出来并装到 ~/.agentvm/bin/vmz\n"
            "或者自己编（见 scripts/vmz/main.swift 顶部注释）。")

    # 磁盘要先看一眼。原来一律按 60 GB 估：25.6 GB 镜像 + 装完三十多 GB。
    # 但镜像现在有全局缓存（~/.agentvm/images/ipsw），缓存命中的话这一次只占
    # 系统盘那一份，实测 27 GB——照 60 GB 估会把「够用」报成「可能失败」。
    # vmz 那边自己也会用同一份缓存再判一次，两边一致。
    # catalog 里 macOS 条目的 url 是空的（地址运行时才问 Apple 要），所以不能
    # 用 distro["url"] 推文件名，直接看缓存目录里有没有东西。
    def _ipsw_present() -> bool:
        if ipsw and Path(ipsw).expanduser().is_file():
            return True
        for d in (IMAGES / "ipsw", vm_dir(name) / "ipsw"):
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file() and f.stat().st_size > 0:
                        return True
        return False
    ipsw_cached = _ipsw_present()
    disk_actual = 28 << 30      # 实测装完写入约 27 GB，留点余量
    need = disk_actual if ipsw_cached else (disk_actual + distro["size"])
    free = shutil.disk_usage(HOME).free
    if free < need:
        warn(f"磁盘只剩 {human(free)}，装 macOS 虚拟机需要约 {human(need)}"
             f"（{'恢复镜像已有缓存，只算系统盘' if ipsw_cached else '恢复镜像 + 系统盘'}）。"
             f"可能会在安装中途失败。")

    argv = [str(vmz), "install", "--name", vm["name"],
            "--cpu", str(vm["cpus"]), "--mem", vm["ram"],
            "--disk", vm["disk"], "--user", vm["user"]]
    # 密码走 0600 文件，不进命令行参数。见 _vmz_password_args 的注释。
    argv += _vmz_password_args(vm["name"], vm.get("password"))
    # 自己手上有一个 IPSW 就走本地那份，不去 Apple 下 25.6 GB。
    # install --source /path/to.ipsw 就是干这个用的。
    #
    # 注意 ipsw 是参数传进来的，不是函数里读 args：cmd_install_macos 也被
    # 后台安装那条路调到（spawn_detached_install 里另起了一份进程），
    # 那边没有 args 对象——实测踩过，NameError 裸栈到用户脸上。
    if ipsw:
        argv += ["--ipsw", str(Path(ipsw).expanduser())]

    # 事件原样转出去。vmz 输出的是和 vmkit 同一套协议（一行一个 JSON），
    # 所以这里只要把 stdout 的行喂给 StreamParser 的同类逻辑就行。
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            cwd=str(HERE), env=_vmz_install_env())
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            # vmz 里有几处直接 print（跟 winarm-vm.py 一样），宽容一点
            Emit.log(line)
            continue
        Emit._w(ev)
    rc = proc.wait()
    if rc != 0:
        die(f"vmz 安装失败（退出码 {rc}）。完整日志："
            f"{vm_dir(vm['name']) / 'install.log'}")

    # vmz 那边把配置写在 <vm>/macvm.json，vmkit 的 vm.json 也补一份，
    # 两边各自管各自的字段，不互相覆盖（见 MacVMSpec 的注释）。
    try:
        spec = json.loads((vm_dir(vm["name"]) / "macvm.json").read_text())
        if isinstance(spec, dict):
            vm["build"] = spec.get("build", "")
    except (OSError, ValueError):
        # 装完了但 macvm.json 读不了。不致命——build 只是界面显示用，
        # 但要说一声，不然用户看到一台没有版本号的虚拟机不知道装的是什么。
        warn("装完了，但读不到 macvm.json，界面上不会显示 macOS 版本号")
    vm["ready"] = True
    save_vm(vm)
    ok(f"虚拟机 {vm['name']} 已就绪。"
       f"macOS 装好后用「启动」打开，第一次启动会自动建账号 {vm['user']} "
       f"并打开远程登录。")
    return vm


def resolve_headless(vm: dict, args) -> bool:
    """这次启动要不要原生窗口。

    规则：`--window` 强制要窗口，`--headless` 强制不要，两个都不给就沿用
    vm.json 里存的值。

    原来是 `vm["headless"] = bool(args.headless)`，即不带 flag 就一定是 False
    （有窗口）。本地安装包这条路不适用：它装的时候就是无窗口的（画面走 VNC、
    在 app 内嵌控制台里操作），装完之后用户点一次「启动」，按老规则会突然弹
    一个原生窗口出来，而 app 里的控制台还在——同一个画面的两个入口，很莫名。
    显示模式是 QEMU 的启动参数，跑起来改不了，所以只能按存下来的值走；
    要窗口就显式 `--window`（见 cmd_window）。
    """
    if getattr(args, "window", False):
        want = False
    elif getattr(args, "headless", False):
        want = True
    else:
        want = bool(vm.get("headless", False))
    vm["headless"] = want
    save_vm(vm)
    return want


def winarm_env(vm: dict, headless: bool) -> dict:
    """给 winarm-vm.py 准备环境变量。

    为什么单独抽一个函数：install 那条路把九个变量全设了，而 stop / start /
    window 三条路只设 VM_DIR。winarm 的 Cfg() 对没设的变量回落到默认值
    `~/.winarm/ci_key` + 用户名 `Docker`——而这个 VM 安装时用的是
    `vm["ssh_key"]`（默认 ~/.agentvm/id_ed25519，公钥已经打进 ISO）。两把不是
    同一把钥匙，于是 stop 时 ssh_ready() 永远认证失败：跳过优雅关机 → ACPI
    等 90 秒（登录界面没人理电源键事件）→ kill -9 强杀 raw NTFS 盘。
    每次 stop 都固定复现：先卡 90 秒，再丢客人机未落盘数据。
    """
    return {
        "VM_DIR": str(vm_dir(vm["name"])),
        "RAM_SIZE": vm["ram"],
        "CPU_CORES": str(vm["cpus"]),
        "USERNAME": vm["user"],
        "PASSWORD": vm["password"],
        "SSH_KEY": vm["ssh_key"],
        "SSH_PORT": str(vm["ssh_port"]),
        "RDP_PORT": str(vm["rdp_port"]),
        "VNC_DISPLAY": str(vm["vnc_display"]),
        "HEADLESS": "1" if headless else "0",
    }


def _sync_windows_vnc(vm: dict) -> None:
    """把 winarm 实际用的 VNC 编号读回 vm.json。

    winarm 的 free_vnc_display() 在 5900+display 被别的程序占用时会自动 +1，
    并把真实编号写进 <vm>/vnc.display（它自己的注释就写着这个坑：「实测报
    5902、实际监听 5901」）。vmkit 原来只把 VNC_DISPLAY 传过去、之后从不回读，
    vm_record 永远报 5900 + vm["vnc_display"]。22000 被占掉时 QEMU 听的是
    22001，而 status 和 app 报 22000——用户照着重连必然失败，而两边都「正常」。
    """
    if vm.get("kind") != "windows":
        return
    p = vm_dir(vm["name"]) / "vnc.display"
    try:
        real = int(p.read_text().strip())
    except (OSError, ValueError):
        return
    if real <= 0 or real == vm.get("vnc_display"):
        return
    vm["vnc_display"] = real
    save_vm(vm)
    warn(f"VNC 端口被占用，实际改用 {5900 + real}（已写回 vm.json）")


def _relay(proc: subprocess.Popen, what: str) -> int:
    """把一个子进程的 JSON 事件原样转出去，返回它的退出码。

    vmz 输出的是和 vmkit 同一套协议（stdout 一行一个 JSON），所以只要按行
    解析再交给 Emit 就行。不是 JSON 的行当普通日志——vmz 和 winarm 一样
    有几处直接 print，客户端宽容一点比去改那条已经跑通的路径安全。
    """
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            Emit.log(line)
            continue
        Emit._w(ev)
    return proc.wait()


def start_macos(vm: dict) -> None:
    """前台跑起一个 macOS 客人机，直到它停。

    VZVirtualMachine 不能跨进程传递，所以 VM 跟 vmz 进程同生共死：
    这个函数会一直占着终端，Ctrl-C 或客人机自己选了关机才返回。
    """
    vmz = find_vmz()
    if not vmz:
        die("找不到 vmz。macOS 虚拟机需要它（./install.sh 会装好）。")
    spec = vm_dir(vm["name"]) / "macvm.json"
    if not spec.is_file():
        die(f"没有 {spec}。这个虚拟机可能不是 macOS 类型的。")
    try:
        loaded = json.loads(spec.read_text())
        if not isinstance(loaded, dict) or not loaded.get("installed"):
            die("macOS 还没装完。没有系统的盘启动只会卡在黑屏，先跑 install。")
    except ValueError:
        die(f"{spec} 不是合法 JSON，可能是写坏了。删掉这个虚拟机重装。")
    except OSError as e:
        die(f"读不了 {spec}：{e}")
    Emit.stage("start", f"启动 {vm['name']}")
    proc = subprocess.Popen([str(vmz), "start", "--name", vm["name"]],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=str(HERE), env=_vmz_install_env())
    rc = _relay(proc, "start")
    if rc != 0:
        die(f"{vm['name']} 退出了（退出码 {rc}）")


def cmd_start(args) -> int:
    vm = load_vm(args.name)
    headless = resolve_headless(vm, args)
    # 一律 .get("kind")：写了一半的 vm.json 没有 kind，直接下标就是 KeyError
    # 裸栈，而用户看到的只是「启动失败」四个字后面跟一个栈。
    kind = vm.get("kind")
    if kind == "windows":
        w = winarm()
        os.environ.update(winarm_env(vm, headless))
        # 必须接 winarm 的 _Die：它用 die() 表示「盘没建」「安装介质不在」
        # 「QEMU 起不来」这些用户可处理的错误。cmd_stop 和 cmd_install_windows
        # 都接了，就 start / window 这两处漏了——漏了的后果是 Python 栈带着
        # `_Die: VM 还没建。先跑 create。` 裸奔到用户脸上。
        try:
            w.cmd_start(w.Cfg())
        except w._Die as e:
            die(str(e))
        _sync_windows_vnc(vm)
    elif kind == "macos":
        # macOS 客人机是前台跑的，不是起了就返回。
        #
        # 原因写在 MacVMEngine 顶部：VZVirtualMachine 不能跨进程传递，
        # VM 的生命周期跟持有它的进程绑定。所以 `vmkit start` 对一个 macOS
        # VM 来说等于「这个终端就是虚拟机的寿命」——Ctrl-C 优雅关机，
        # 关终端客人机跟着停。这不是设计选择，是框架限制。
        # app 里不走这条路，它在自己进程里跑 VZ，所以有关闭按钮和画面。
        start_macos(vm)
    elif kind == "local":
        # 从盘启动意味着用户已经在安装器里点完了。原来这里不会置 ready，
        # 于是界面一直显示「未就绪」、登录信息也不给——用户明明已经装好。
        # 装没装完只有用户知道，他选择 start 就是最明确的表态。
        vm["ready"] = True
        save_vm(vm)
        start_local(vm)
    else:
        start_linux(vm)
    return 0


def cmd_window(args) -> int:
    """用原生 macOS 窗口打开虚拟机，并把窗口带到前台。

    为什么需要单独一条命令：**显示模式是 QEMU 的启动参数，跑起来之后改不了**。
    本地安装包这条路安装期间是无窗口的（画面走 VNC，在 app 内嵌控制台里
    操作，理由见 local_qemu_args），所以「我想要一个真窗口」只能重启一次
    QEMU 才能做到。这里把「重启 + 开窗口 + 带到前台」合成一步，并且提前说清楚
    会重启客人机——不说是撒谎。

    如果本来就有窗口（headless 为假），什么都不做，只带到前台。
    """
    vm = load_vm(args.name)
    if vm["kind"] == "macos":
        die("macOS 虚拟机没有「原生窗口模式」这个开关：它的窗口就是 app 里那个"
            "VZVirtualMachineView，不存在第二种显示方式。\n"
            "在 app 里点「打开窗口」即可。")
    if not qemu_pid(vm):
        # 原来只有一句「VM 没在运行，先 start」。那是写给命令行读者看的，
        # 而 app 把后端的 stderr 原样弹成对话框——用户看到的就是一句英文味
        # 儿的指令，既没有按钮也没有下一步。2026-11-10 用户截图报的就是这个
        # （点关机状态下的「用系统窗口打开」）。app 侧已经改成本条命令不达这儿，
        # 但命令行和别的方式还是会到，所以这句话得是能照着做的一条路。
        die(f"{args.name} 没在运行，没有窗口可打开。\n"
            f"要带着原生窗口起来：\n"
            f"  {_self_cmd()} start --name {args.name} --window")
    if vm.get("headless"):
        warn("要切换成窗口模式，得重启一次虚拟机")
        cmd_stop(args)
        vm["headless"] = False
        save_vm(vm)
        if vm["kind"] == "windows":
            w = winarm()
            os.environ.update(winarm_env(vm, False))
            try:
                w.cmd_start(w.Cfg())
            except w._Die as e:
                die(str(e))
            _sync_windows_vnc(vm)
        elif vm["kind"] == "local":
            start_local(vm)
        else:
            start_linux(vm)
    else:
        ok("本来就有窗口，只带到前台")
    cmd_open(args)
    return 0


def cmd_stop(args) -> int:
    vm = load_vm(args.name)
    if vm.get("kind") == "macos":
        # macOS 客人机的进程是 vmz，停它要发 SIGTERM——vmz 内部有个
        # signal handler，收到就走 VZ 的优雅停机（machine.stop），
        # 让 macOS 正常 flush 磁盘。直接 kill -9 会在 APFS 里留下
        # 未干净的日志，下次启动跑好几分钟 fsck。
        entry = macos_pid_entry(vm)
        if not entry:
            ok("VM 没在运行")
            return 0
        host, pid = entry
        if host == "app":
            # 在 app 里跑着的 VM 命令行停不了：vmz stop 只对 vmz 宿主发
            # 信号，对 app 发 SIGTERM 会连 app 一起打死（它没有处理函数），
            # 而 VM 是非优雅停机。这里明说，而不是返回 0 假装停过了。
            die(f"{vm.get('name')} 正在 AgentVM.app 里运行（pid {pid}），"
                f"命令行停不了它。\n"
                f"请在 app 里选中这台虚拟机，点关闭/关机。")
        vmz = find_vmz()
        if not vmz:
            die("找不到 vmz，停不了 macOS 虚拟机。")
        Emit.stage("stop", f"停止 {vm.get('name')}")
        proc = subprocess.Popen([str(vmz), "stop", "--name", vm.get("name")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=str(HERE), env=_vmz_install_env())
        rc = _relay(proc, "stop")
        if rc != 0:
            die(f"没停下来。可以 kill {pid}，但会丢客人机里没落盘的数据。")
        return 0
    pid = qemu_pid(vm)
    if not pid:
        ok("VM 没在运行")
        return 0
    if vm.get("kind") == "windows":
        w = winarm()
        # 原来只设 VM_DIR。winarm 的 Cfg() 于是回落到默认的 ~/.winarm/ci_key，
        # 和安装时用的 vm["ssh_key"] 不是同一把钥匙，关机流程第一步的 SSH 认证
        # 必然失败。见 winarm_env 的注释。
        os.environ.update(winarm_env(vm, True))
        try:
            w.cmd_stop(w.Cfg())
        except w._Die as e:
            die(str(e))
        # 发完 -9 必须等它真的退干净再返回。
        #
        # winarm 的 cmd_stop 结尾是 `kill -9; ok("已强杀")`，没有等待——而 kill
        # 是异步的：信号发出去到进程变成僵尸/消失之间有一段窗口，这时候 ps 还
        # 看得见它。紧接着任何「起虚拟机」都会走 qemu_running → 看到「VM 已在
        # 运行」→ 什么都不做就返回。用户改的 CPU/内存/显示模式一个都没生效，
        # 而日志说停过了。
        # Linux/local 那条路（下面几行）早就写了这段轮询，Windows 是委托给
        # winarm 的，那道保护没覆盖到。cmd_delete 也会走到这儿：stop 报完
        # 「已强杀」就删 data.img，而 QEMU 可能还在往那个文件里写。
        # 2026-11-11 审计补上。
        for _ in range(50):
            if not qemu_pid(vm):
                break
            time.sleep(0.2)
        return 0
    # Linux：guest agent → SSH → ACPI → 强杀，一路降级。
    # qcow2 被强杀有丢数据风险，所以前面每一层都值得试；而 agent 排在 SSH
    # 前面是因为它不依赖客人机网络，理由见 qga_shutdown_wait 的注释。
    if qga_shutdown_wait(vm):
        return 0
    if linux_ssh_ready(vm, timeout=5):
        log("SSH 可达，走客人机自己的关机流程")
        subprocess.run([find_tool("ssh"), "-i", vm["ssh_key"],
                        "-p", str(vm["ssh_port"]),
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
                        f"{vm['user']}@127.0.0.1", "sudo shutdown -h now"],
                       capture_output=True, text=True)
        for _ in range(120):
            time.sleep(1)
            if not qemu_pid(vm):
                ok("已优雅关机")
                return 0
    d = vm_dir(vm["name"])
    mon = d / "monitor.sock"
    if mon.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
                sk.settimeout(5)
                sk.connect(str(mon))
                sk.sendall(b"system_powerdown\n")
            log("已发 ACPI 电源键事件")
            for _ in range(60):
                time.sleep(1)
                if not qemu_pid(vm):
                    ok("已优雅关机")
                    return 0
        except OSError:
            pass
    warn("优雅关机超时，强杀。qcow2 可能留下脏数据，下次开机会自愈")
    run(["kill", "-9", str(pid)], check=False)
    # 发完 -9 必须等它真的退干净再返回。
    #
    # kill 是异步的：信号发出去到进程变成僵尸/消失之间有一段窗口，这时候 ps
    # 还看得见它。紧接着任何「起虚拟机」都会走 qemu_pid → 看到「VM 已在运行」
    # → 什么都不做就返回。用户看到的是虚拟机凭空没了，而日志说它还在跑。
    # 2026-11-11 实测踩到：自动安装的拔盘重启路径，日志依次是
    # 「已强杀」「VM 已在运行（pid N）」，然后卡在永远起不来的状态。
    for _ in range(50):
        if not qemu_pid(vm):
            break
        time.sleep(0.2)
    ok("已强杀")
    return 0


def cmd_guest(args) -> int:
    """通过 QEMU guest agent 碰客人机，不依赖客人机网络。

        vmkit.py guest --name d1 ping
        vmkit.py guest --name d1 ip              客人机的 IPv4 地址
        vmkit.py guest --name d1 disks           客人机能看到的块设备
        vmkit.py guest --name d1 exec -- uname -a
        vmkit.py guest --name d1 shutdown        优雅关机（stop 走的就是它）

    和 SSH 的分工：这条走 virtio-serial，客人机没网、sshd 没起来、防火墙把
    22 挡了都能用；代价是要客人机里装着 qemu-guest-agent（Linux 云镜像从
    install 起就带着，见 linux_seed 的 packages）。

    Windows 侧用不了：qemu-ga 的 Windows ARM64 安装包目前没有正式 release
    产物（virtio-win-guest-tools-installer PR #100 / #104 只有 CI artifact，
    UTM #5134 说未移植到 ARM64 Windows），所以 winarm 那边没有这个 socket，
    这条命令在 Windows VM 上会直接告诉你 socket 不在。
    """
    vm = load_vm(args.name)
    sock = qga_sock_path(vm)
    if vm.get("kind") != "linux" and not sock.exists():
        # 提前把「为什么没有」说清。只说「socket 不存在」的话，Windows 用户
        # 会去装 qemu-guest-agent，然后发现宿主机侧根本没挂这个设备。
        die(f"{vm['name']} 是 {vm.get('kind')} 客人机，没有 guest agent 套接字。\n"
            f"Linux 云镜像从 install 起就带（见 linux_seed 的 packages）；\n"
            f"Windows ARM64 等 qemu-ga 的正式安装包，见 README"
            f"「客人机代理与共享目录」。")
    qa = qga()

    if args.action == "exec":
        argv = list(args.command)
        # REMAINDER 会把分隔用的 `--` 也吃进来，剥掉它，否则 sh 会拿到一个
        # 名叫 `--` 的第一个参数。
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            die("要给命令，例如：guest --name d1 exec -- uname -a")
        try:
            r = qa.qga_exec(sock, argv, timeout=args.timeout)
        except qa.QGAError as e:
            die(str(e))
        if r["out"]:
            sys.stdout.write(r["out"])
            if not r["out"].endswith("\n"):
                sys.stdout.write("\n")
        if r["err"]:
            sys.stderr.write(r["err"])
        # 客人机里的退出码要透出来。被信号打死时 QGA 给的 exitcode 是假，
        # 那也算失败，给 1。
        code = r["exitcode"]
        return int(code) if isinstance(code, int) else 1

    try:
        if args.action == "ping":
            # ping 一次就够。qga_ping 是「一次连接一个请求」，调两次就是两条
            # 连接、两倍等待，而这里要的只是一个真假。
            reachable = qa.qga_ping(sock)
            ok("guest agent 可达" if reachable else
               f"guest agent 不可达：{sock}")
            return 0 if reachable else 1
        if args.action == "ip":
            ips = qa.qga_ips(sock)
            if not ips:
                warn("guest agent 没报出任何 IPv4 地址")
                return 1
            for ip in ips:
                ok(ip)
            Emit.done({"ips": ips})
            return 0
        if args.action == "disks":
            disks = qa.qga_disks(sock)
            for d in disks:
                name = d.get("name", "?")
                parts = ", ".join(str(p.get("partition", "?"))
                                  for p in (d.get("partitions") or []))
                ok(f"{name}" + (f"  分区 {parts}" if parts else "  （没有分区）"))
            Emit.done({"disks": disks})
            return 0
        if args.action == "shutdown":
            qa.qga_shutdown(sock)
            log("已通知客人机关机，它正在走自己的停止流程（进程退出要等几秒到几十秒）")
            return 0
    except qa.QGAError as e:
        die(str(e))
    die(f"不认识的动作：{args.action}")


def vm_broken(vm: dict) -> str:
    """这台虚拟机是不是半成品——缺了启动必需的文件。不缺返回空串。

    为什么需要这个状态：install 中途失败（下载断了、磁盘建不出来、用户点了
    「停止」）会留下一个「只有 vm.json」的目录。此时 list / status 把它当成
    一台正常虚拟机列出来，用户点「启动」才拿到一句「VM 不完整，缺
    disk.qcow2。重新 install。」——而 install 紧接着回「已存在，换个名字或加
    --force」。两句都是死胡同。实测踩过（2026-11-10）。

    把这个状态显式发出去，界面就能显示「没装完」并给一个「重装」按钮，
    install 的「已存在」那句也能直接说清 --force 能修好它。
    """
    name = vm.get("name", "")
    if not name:
        return ""
    kind = vm.get("kind", "")
    # macOS 的系统盘是 raw 的 disk.img（VZ 只认 raw），Windows 和本地 ISO 是
    # data.img，Linux 云镜像是 qcow2 overlay 加 cloud-init 种子盘。
    if kind == "macos":
        need = ("disk.img",)
    elif kind in ("windows", "local"):
        need = ("data.img",)
    elif kind == "linux":
        need = ("disk.qcow2", "seed.iso")
    else:
        # 认不出的 kind **不能**按 linux 判。原来这儿是个 `else`，把「未知」
        # 和「linux」合并了：一台其实是 local 的 VM 只要 kind 字段坏了（手改过、
        # 或者安装早期写的那一版），就会被判成「缺 disk.qcow2、seed.iso」，
        # _refuse_existing 于是劝用户 --force 重装——照 cmd_install 那边的行为，
        # 用户照做才发现是另一回事。如实说「认不出」比猜一个强。
        # 2026-11-11 审计补上。
        return f"vm.json 里的 kind 是 {kind!r}，不是认识的那几种"
    d = vm_dir(name)
    missing = [f for f in need if not (d / f).is_file()]
    if missing:
        return "缺 " + "、".join(missing)
    # 文件在，但里面是空的。
    #
    # 本地包这条路建的是稀疏空盘：`truncate` 一下就是用户选的 32G，一个字节都没写。
    # 安装器没跑起来、或者刚起来就被掐了，留下的正是这种状态。它不算「缺文件」，
    # 于是 list / status 把它当成一台正常虚拟机，用户点「启动」→ 固件找不到任何
    # 可启动项 → 落进 UEFI Interactive Shell，一屏英文，比报错难懂得多。
    # 2026-11-11 实测：linux1 就是这么个状态（32G 盘、0 字节、stale pid 和
    # monitor.sock 都还在），而所有命令都报告「已关机」——听着像没事。
    if kind == "local":
        try:
            used = (d / "data.img").stat().st_blocks * 512
        except OSError:
            used = 0
        if used < LOCAL_MIN_DISK:
            return ("盘还是空的，安装没写进去"
                    "（重启装一次就好，这台没有可用数据）")
    return ""


def vm_record(vm: dict) -> dict:
    """把一个 vm.json 字典转成界面要的那份记录。

    单独抽出来是因为 install 和 status 都要发这个形状，而两边原本各写一份、
    字段还不一样：install 的 `Emit.done(vm)` 发的是 vm.json 原文，里面是
    `vnc_display`、没有 `ssh_command` 和 `disk_used_bytes`。app 那边解不出来
    ——而 Swift 的解码失败是静默的，界面表现为装完了却一直停在「正在安装」页，
    单独在终端里跑同样的命令却是好的。实测踩过。

    所有字段一律用 .get() 加默认值，不能用 vm["..."]。vm.json 是会被写一半的
    （安装中断、用户手改、旧版本留下的），一个缺 ssh_port 的坏文件原来能让
    `status`（不带 --name 时遍历全部 VM）整条命令 KeyError 崩掉——用户看不到
    任何虚拟机的状态，而终端里单独查那一个是好的。
    """
    pid = qemu_pid(vm)
    name = vm.get("name", "")
    d = vm_dir(name) if name else VMS
    # raw 盘的是 data.img，qcow2 overlay 的是 disk.qcow2。本地 ISO 装的
    # 也是 raw 盘，漏了它会去 stat 一个不存在的文件，占用显示成 0。
    # macOS 客人机的盘是 raw 的 disk.img（VZ 只认 raw），跟本地 ISO 那条路
    # 一样；另外两种是 qcow2 overlay。
    disk = d / ("data.img" if vm.get("kind") in ("windows", "local")
                else "disk.qcow2")
    if vm.get("kind") == "macos":
        disk = d / "disk.img"
    rec = {
        "name": name, "distro": vm.get("distro", ""), "kind": vm.get("kind", ""),
        "running": bool(pid), "pid": pid, "ready": vm.get("ready", False),
        "cpus": vm.get("cpus", 2), "ram": vm.get("ram", "2G"),
        "disk": vm.get("disk", "32G"),
        "user": vm.get("user", "agent"), "ssh_port": vm.get("ssh_port", 0),
        "rdp_port": vm.get("rdp_port"),
        # macOS 客人机没有 VNC：VZ 不提供 VNC server，画面走 app 里的
        # VZVirtualMachineView。给 0 而不是 null——app 那侧 vncPort 是 Int，
        # 解不出 null 整条条目会被静默丢掉。
        "vnc_port": 0 if vm.get("kind") == "macos" else 5900 + vm.get("vnc_display", 0),
        "ssh_port": 0 if vm.get("kind") == "macos" else vm.get("ssh_port", 0),
        # 有没有原生窗口。app 据此决定显不显示「用系统窗口打开」——无窗口
        # 跑的 VM 点它是重启一次客人机，得先跟用户说清楚。
        "headless": bool(vm.get("headless", False)),
        "ssh_command": vm.get("ssh_command", ""),
        # 本地 ISO 装的 NAS 类系统，主要界面是 Web UI。只有这条 kind 有，
        # 别处是 null，界面据此决定显不显示这一行。
        "web_port": vm.get("web_port"),
        "source": vm.get("source"),
        # 启动时挂不挂安装光盘。只有本地包装的虚拟机有这项，界面据此显示那个
        # 开关，重启时也按它决定挂不挂 ISO。默认假——挂着光盘重启有可能退回
        # 安装器而不是进装好的系统，见 local_qemu_args 的注释。
        "attach_iso": bool(vm.get("attach_iso", False)),
        # 附加盘和共享目录。列表可能为空，但不能没有这个键——app 那边解不出来的
        # 话整个条目被静默丢掉，界面表现为「没有虚拟机」。
        "disks": list(vm.get("disks", [])),
        "shares": list(vm.get("shares", [])),
        # 稀疏盘的真实占用，不是逻辑大小。64G 的盘 st_size 永远是 68.7e9，
        # 实际可能只写了 6 GB——用 st_size 会让界面显示「磁盘已用 64 GB」，
        # 用户据此判断磁盘快满了纯粹是误导。st_blocks * 512 才是分配出去的块。
        "disk_used_bytes": (disk.stat().st_blocks * 512
                            if disk.exists() else 0),
    }
    if pid and vm.get("kind") == "linux":
        rec["ssh_ok"] = linux_ssh_ready(vm, timeout=4)
    if vm.get("kind") == "macos":
        # 界面要靠这几个字段区分 macOS VM：有没有装完、装的哪个 build、
        # 分辨率是多少。缺一个 app 那边就解不出来，而 Swift 的解码失败
        # 是静默的——表现为列表里少一台，终端里看却在。
        try:
            spec = json.loads((d / "macvm.json").read_text())
        except (OSError, ValueError):
            # 装到一半 vm.json 有了而 macvm.json 还没有（vmz 还没跑到写它
            # 那一步）。这时候别显示「0x0」——给安装时的默认值，
            # 跟用户刚在向导里填的一致。
            spec = {"width": 1440, "height": 900, "build": "", "installed": False}
        rec["ready"] = bool(spec.get("installed"))
        rec["mac_build"] = spec.get("build", "")
        rec["mac_resolution"] = f"{spec.get('width', 1440)}x{spec.get('height', 900)}"
        rec["mac_installed"] = bool(spec.get("installed"))
    # 半成品状态。界面据此显示「没装完」并给「重装」按钮；空串表示文件齐全。
    # 放在最后：macOS 那段会改 ready，与 broken 各管一件事，不要互相覆盖。
    rec["broken"] = vm_broken(vm)
    return rec


def cmd_status(args) -> int:
    if args.name:
        vms = [load_vm(args.name)]
    else:
        vms = all_vms()
    Emit._w({"t": "vms", "vms": [vm_record(v) for v in vms]})
    return 0


# ------------------------------------------------------- 附加盘 / 快照 / 克隆 / 共享 --

def _next_disk_id(vm: dict) -> str:
    used = {d["id"] for d in vm.get("disks", [])}
    for i in range(1, 100):
        if f"data{i}" not in used:
            return f"data{i}"
    die("附加盘编号用完了（data1 到 data99）")


def cmd_disk(args) -> int:
    """管理附加数据盘：加一块、摘一块、看有几块。

    系统盘是安装时建的那一个，这儿加的是另外的。为什么要分开：装完之后想加一块
    盘存数据（NAS 最常见），原来只能删了重装——而删了重装系统里的东西全没。

    新盘是空的，客人机里还要分区格式化才能用：
    Linux `mkfs.ext4 /dev/vdb1` 再 mount，Windows 用磁盘管理初始化。
    """
    vm = load_vm(args.name)
    name = vm["name"]
    disks = vm.setdefault("disks", [])

    if args.action == "list":
        if not disks:
            ok(f"{name} 没有附加盘，只有安装时建的系统盘")
        for d in disks:
            size = "-"
            f = Path(d["file"])
            if f.is_file():
                size = human(f.stat().st_blocks * 512)
            ok(f"{d['id']}  {d['file']}  {d.get('format', 'raw')}  "
               f"{d.get('bus', 'virtio-blk')}  实际占用 {size}")
        Emit.done({"disks": disks})
        return 0

    if args.action == "add":
        if vm["kind"] == "windows":
            die("Windows 那条路的磁盘参数是 winarm-vm.py 拼的，暂时不支持附加盘。"
                "Linux 和自己挑的安装包都可以。")
        if not args.size:
            die("加盘要给大小，例如 --size 64G")
        want = parse_size(args.size)
        if want < 1 << 30:
            die(f"附加盘至少 1G，你给了 {args.size}")
        did = _next_disk_id(vm)
        d = vm_dir(name)
        ext = "qcow2" if args.format == "qcow2" else "img"
        target = d / f"{did}.{ext}"
        if target.exists():
            die(f"{target} 已存在，换个虚拟机名或先删掉它")
        Emit.stage("disk", f"创建附加盘 {did}（{args.size}）")
        if args.format == "qcow2":
            run([find_tool("qemu-img"), "create", "-f", "qcow2",
                 str(target), str(want)])
        else:
            # 稀疏文件：truncate 只登记逻辑大小，写到了才真占空间
            with open(target, "wb") as fh:
                fh.truncate(want)
        disks.append({"id": did, "file": str(target), "format": args.format,
                      "bus": args.bus, "size": args.size.upper()})
        save_vm(vm)
        ok(f"已加附加盘 {did}：{target}（{args.size} {args.format}，"
           f"{args.bus}，稀疏，写到了才占空间）")
        if qemu_pid(vm):
            warn("虚拟机正在运行，这块盘要重启后才挂上。")
        else:
            ok("下次启动生效")
        warn("新盘是空的，系统里还要分区格式化：Linux 用 mkfs.ext4 再 mount，"
             "Windows 用磁盘管理初始化。")
        Emit.done(vm_record(vm))
        return 0

    if args.action == "remove":
        if not args.disk:
            die("要指明摘哪块，例如 --disk data1")
        target = next((d for d in disks if d["id"] == args.disk), None)
        if target is None:
            die(f"没有叫 {args.disk} 的附加盘。现有的："
                + ("、".join(d["id"] for d in disks) or "（一个都没有）"))
        if qemu_pid(vm):
            die("虚拟机正在运行，先 stop 再摘盘。")
        f = Path(target["file"])
        if f.is_file() and not args.keep_file:
            # 删文件是不可逆的，而且盘里可能有用户的数据。留 --keep-file 是给
            # 「只想摘下来、数据还要」的情况。
            warn(f"删掉 {f}，里面的数据一起没")
            f.unlink()
        elif f.is_file():
            ok(f"文件留着：{f}（只是不再挂载）")
        disks.remove(target)
        save_vm(vm)
        ok(f"已摘掉附加盘 {args.disk}")
        Emit.done(vm_record(vm))
        return 0

    die(f"不认识的动作：{args.action}")


def _vm_qcow2(vm: dict) -> Path | None:
    """这个虚拟机哪个盘能拍快照。只有 qcow2 有内部快照，raw 盘没有。"""
    if vm.get("kind") == "linux":
        f = vm_dir(vm.get("name", "")) / "disk.qcow2"
        return f if f.is_file() else None
    for d in vm.get("disks", []):
        if d.get("format") == "qcow2" and Path(d["file"]).is_file():
            return Path(d["file"])
    return None


def qcow2_backing(path: Path) -> Path | None:
    """这个 qcow2 真正的底册是哪个文件，没有底册返回 None。

    用 `qemu-img info --output=json` 读 backing-filename，不靠猜。**相对路径
    要按 qcow2 自己所在目录解析**——qemu-img 存的就是当时 `-b` 传进去的那个
    字符串，用户或旧版本传相对路径时它是相对的，直接当绝对路径用会找错文件。

    读不出来（文件不在、不是 qcow2、qemu-img 报错）时返回 None，由调用方决定
    是降级还是报错——这里不 die：list_snapshots 之类的只读路径也用得上。

    一律 resolve()：macOS 上 /var 是指向 /private/var 的符号链接，而调用方
    （clone 建 overlay、delete 比依赖）传进来的路径可能两种形式都有。不归一的话
    「/var/folders/.../base.qcow2」和「/private/var/folders/.../base.qcow2」会被
    当成两个不同的文件，依赖检查静默漏掉。2026-11-11 自己写测试量出来的。
    """
    if not path.is_file():
        return None
    r = run([find_tool("qemu-img"), "info", "--output=json", str(path)],
            check=False)
    try:
        info = json.loads(r.stdout or "{}")
    except (json.JSONDecodeError, AttributeError):
        return None
    bf = info.get("backing-filename")
    if not bf:
        return None
    p = Path(bf)
    return (p if p.is_absolute() else path.parent / p).resolve()


def vm_qcow2_files(vm: dict) -> list[Path]:
    """这台虚拟机哪些盘是 qcow2（可能被别人当底册）。"""
    out = []
    f = vm_dir(vm.get("name", "")) / "disk.qcow2"
    if f.is_file():
        out.append(f)
    for d in vm.get("disks", []):
        p = Path(d["file"])
        if d.get("format") == "qcow2" and p.is_file():
            out.append(p)
    return out


def qcow2_backing_dependents(vm: dict) -> list[str]:
    """哪些虚拟机把 vm 的 qcow2 盘当底册。给 delete 用来拒绝制造一台起不来的克隆。"""
    mine = {p.resolve() for p in vm_qcow2_files(vm)}
    if not mine:
        return []
    out = []
    for v in all_vms():
        if v.get("name") == vm.get("name"):
            continue
        for cand in vm_qcow2_files(v):
            b = qcow2_backing(cand)
            if b is not None and b.resolve() in mine:
                out.append(v.get("name", "?"))
                break
    return sorted(set(out))


def list_snapshots(path: Path) -> list[dict]:
    """列出 qcow2 里的内部快照。

    用 `qemu-img info --output=json`，**不要**用 `snapshot -l` 再去解析那张表。
    那张表按空格切会错位（VM_SIZE 是「0 B」两个词、DATE 也是两个词），按列头位置
    切片也会错（表头的列位置和数据的列位置对不齐，实测这个版本就不齐）。json 是
    现成的结构化输出，字段名不会随版本改。
    """
    r = run([find_tool("qemu-img"), "info", "--output=json", str(path)],
            check=False)
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    out = []
    for sn in info.get("snapshots", []):
        secs = sn.get("date-sec", 0)
        out.append({
            "id": str(sn.get("id", "")),
            "tag": sn.get("name", ""),
            # vm-state-size 是快照里存的虚拟机状态大小，不是磁盘增量
            "size": human(sn.get("vm-state-size", 0)),
            "date": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(secs))
                     if secs else ""),
            "clock": sn.get("vm-clock-sec", 0),
        })
    return out


def check_snapshot_tag(tag: str) -> str:
    """快照名白名单。与 Swift 侧 validateTag 同一套规则。

    这个值会原样进 `qemu-img snapshot -c <tag> <file>` 的 argv：
    `--tag=-c` 会被 qemu-img 当成自己的选项，用户以为是快照名，
    实际是给转换器发了一条别的命令。所以禁 - 开头，长度也限死。
    """
    if not _SNAP_TAG_RE.fullmatch(tag or ""):
        die(f"快照名只能含字母、数字、点、下划线、连字符，"
            f"1 到 64 个字符，不能以连字符开头：{tag!r}")
    return tag


def cmd_snapshot(args) -> int:
    """qcow2 内部快照：拍一张、列出来、回滚、删掉。

    这是「随删随建的试验场」真正需要的东西：装了一堆东西之后想退回干净状态，
    不用重装，回滚就行。秒级，不占额外空间（qcow2 内部快照是增量的）。

    两个限制，都不掩饰：
    1. **只有 qcow2 盘能拍。** 自己挑安装包那条路用的是 raw 盘（安装器直接写块
       设备，qcow2 它不认），拍不了。Linux 云镜像那条是 qcow2，可以。
    2. **回滚时虚拟机必须关机。** 运行中改镜像内容，客人机缓存和磁盘会对不上。
    """
    vm = load_vm(args.name)
    name = vm.get("name", args.name)
    # tag 先过白名单再分叉：macOS 那条要把它交给 vmz，qcow2 那条要拼进
    # qemu-img 的 argv，两边都挡在这儿最省。
    if args.tag:
        check_snapshot_tag(args.tag)

    # macOS 客人机没有 qcow2，快照是整盘 clonefile（APFS 原生能力），
    # 所以整套动作都在 vmz 里。这里只做转发和事件转播。
    if vm.get("kind") == "macos":
        vmz = find_vmz()
        if not vmz:
            die("找不到 vmz，管理不了 macOS 虚拟机的快照。")
        proc = subprocess.Popen(
            [str(vmz), "snapshot", args.action, "--name", name]
            + (["--tag", args.tag] if args.tag else []),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=str(HERE), env=_vmz_install_env())
        rc = _relay(proc, "snapshot")
        if rc != 0:
            die(f"快照操作失败（退出码 {rc}）")
        return 0

    # Windows 那块盘是 raw（安装器直接写块设备，qcow2 它不认），qemu-img
    # snapshot 对它无效。但 winarm 有整套 APFS copy-on-write 克隆的快照实现
    # （cp -c，秒级、增量），以前只有 vmkit 这一个入口时它完全摸不到——
    # `snapshot create` 对 Windows VM 说的是「没有可拍快照的 qcow2 盘」，
    # 而能力一直在，只是没接上。这里补上转发。
    #
    # 两边的词不一样：vmkit 说 create/apply，winarm 说 save/restore。
    # 映射在这儿做，别让用户记两套动词。
    if vm.get("kind") == "windows":
        w = winarm()
        os.environ.update(winarm_env(vm, True))
        act = {"create": "save", "apply": "restore"}.get(args.action, args.action)
        try:
            w.cmd_snapshot(w.Cfg(), act, args.tag or "")
        except w._Die as e:
            die(str(e))
        return 0

    if args.action == "list":
        f = _vm_qcow2(vm)
        if not f:
            ok(f"{name} 没有可拍快照的 qcow2 盘")
            Emit.done({"snapshots": []})
            return 0
        out = list_snapshots(f)
        if not out:
            ok(f"{name} 还没有快照")
        for sn in out:
            ok(f"{sn['tag']}  {sn['size']}  {sn['date']}")
        Emit.done({"snapshots": out})
        return 0

    if not args.tag:
        die(f"snapshot {args.action} 要 --tag 指明哪个快照")

    if args.action == "create":
        f = _vm_qcow2(vm)
        if not f:
            die("没有可拍快照的 qcow2 盘。自己挑安装包装的是 raw 盘，拍不了；"
                "Linux 云镜像那条可以。")
        if qemu_pid(vm):
            # 运行中拍出来的快照，磁盘和内存状态不一致，回滚等于强制断电+丢写入。
            die("虚拟机正在运行。要拍一个干净、回滚后不丢数据的快照，先 stop。")
        Emit.stage("snapshot", f"拍快照 {args.tag}")
        run([find_tool("qemu-img"), "snapshot", "-c", args.tag, str(f)])
        ok(f"已拍快照 {args.tag}。回滚：vmkit.py snapshot apply "
           f"--name {name} --tag {args.tag}")
        Emit.done({"tag": args.tag})
        return 0

    if args.action == "apply":
        f = _vm_qcow2(vm)
        if not f:
            die("没有可回滚的 qcow2 盘")
        if qemu_pid(vm):
            die("虚拟机正在运行，回滚会改镜像内容，先 stop。")
        # 先确认这个 tag 真的存在，否则 qemu-img -a 会静默什么都不做
        tags = [sn["tag"] for sn in list_snapshots(f)]
        if args.tag not in tags:
            die(f"没有叫 {args.tag} 的快照。现有的：{'、'.join(tags) or '（一个都没有）'}")
        warn(f"回滚到 {args.tag}，这个点之后的所有改动都会没")
        run([find_tool("qemu-img"), "snapshot", "-a", args.tag, str(f)])
        ok(f"已回滚到 {args.tag}")
        Emit.done({"tag": args.tag})
        return 0

    if args.action == "delete":
        f = _vm_qcow2(vm)
        if not f:
            die("没有 qcow2 盘")
        if qemu_pid(vm):
            die("虚拟机正在运行，先 stop。")
        run([find_tool("qemu-img"), "snapshot", "-d", args.tag, str(f)])
        ok(f"已删快照 {args.tag}")
        Emit.done({"tag": args.tag})
        return 0

    die(f"不认识的动作：{args.action}")


def cmd_clone(args) -> int:
    """照着一个已经装好的虚拟机复制一份。

    用途：一个干净基线，想试什么东西就克隆一份去折腾，折腾坏了删掉，基线还在。
    「随删随建的试验场」说的就是这个——原来只能从头装一遍。

    Linux 那条路是 qcow2 overlay，克隆只要再叠一层 overlay，底册共用，
    所以是秒级的，不额外占空间（写入的部分各自算）。自己挑安装包那条是 raw 盘，
    只能整个复制文件；APFS 上用 clonefile 是瞬时的，失败再退回普通拷贝。

    克隆出来的是一个新的虚拟机，端口重新分，不跟源撞。
    """
    src = load_vm(args.name)
    new = args.new_name
    # 用共用的白名单。原来这里自己写了一句 isalnum()，比 check_vm_name 宽
    # （"虚拟机".isalnum() 为真），于是能克隆出一台别的命令一律拒绝管理的 VM：
    # list 看得见、delete 删不掉，只能手工 rm。2026-11-11 审计补上。
    check_vm_name(new)
    if vm_file(new).exists() and not args.force:
        die(f"{new} 已存在。换个名字，或加 --force 覆盖。")
    # --force 必须真的清掉目标、且目标不能在运行。
    #
    # 原来 --force 只用来跳过上面那句 die，没有任何清理动作，于是：
    #   · Linux：第二次 clone 时 qemu-img create 发现目标 disk.qcow2 已存在，
    #     直接报错，整条 clone 以一句「命令失败：qemu-img create …」结束；
    #   · local/windows：`cp -c` 会把目标 data.img 换成源的 CoW 克隆，而目标的
    #     QEMU 还攥着旧 inode 继续写；同时 save_vm 用新分配的端口覆写目标的
    #     vm.json，之后 status 报的 SSH/VNC 端口和 QEMU 实际监听的对不上。
    # 源那份运行检查（下面）早就有了，目标这份是漏的。2026-11-11 审计补上。
    if args.force and vm_file(new).exists():
        dst = load_vm(new)
        if qemu_pid(dst if dst else {"name": new}):
            die(f"{new} 正在运行，先 stop 再覆盖克隆——"
                f"不然会把正在用的盘从 QEMU 脚下抽走。")
        warn(f"--force：清掉 {vm_dir(new)} 重新克隆（里面的数据一起没）")
        _rmtree(vm_dir(new))
    # macOS 客人机的克隆在 vmz 里：系统盘走 clonefile，而机器标识和 NVRAM
    # 必须重新生成（同一个 machineIdentifier 跑两台是未定义行为，
    # 实际表现是 iCloud 互相顶、Handoff 串台）。
    if src.get("kind") == "macos":
        vmz = find_vmz()
        if not vmz:
            die("找不到 vmz，克隆不了 macOS 虚拟机。")
        proc = subprocess.Popen(
            [str(vmz), "clone", "--name", src["name"], "--new-name", new],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=str(HERE), env=_vmz_install_env())
        rc = _relay(proc, "clone")
        if rc != 0:
            die(f"克隆失败（退出码 {rc}）")
        return 0
    if qemu_pid(src):
        die("源虚拟机正在运行，先 stop 再克隆——运行中复制出来的状态不一致。")

    d_new = vm_dir(new)
    d_new.mkdir(parents=True, exist_ok=True)
    Emit.stage("clone", f"克隆 {src['name']} → {new}")

    # 系统盘
    if src.get("kind") == "linux":
        base = vm_dir(src["name"]) / "disk.qcow2"
        if not base.is_file():
            die(f"源的盘不在：{base}")
        # 底册必须是源盘**真正的 backing 文件**，也就是 install 时那个共用的
        # 云镜像（~/.agentvm/images/<id>.qcow2）。原来这里直接把
        # `vm_dir(src) / "disk.qcow2"` 当底册——那是源虚拟机自己的 overlay，
        # 注释却写着「底册还是同一个云镜像」。后果：删掉源 VM（cmd_delete 是
        # 无条件 _rmtree）之后，克隆的 disk.qcow2 指向一个不存在的文件，
        # QEMU 起不来（Could not open backing file），而克隆的 vm.json 完好、
        # vm_broken 也查不出来——用户拿到一台「配置齐全但永远启动不了」的机器。
        # 2026-11-11 审计补上。
        backing = qcow2_backing(base)
        if backing is not None and backing.is_file():
            # 再叠一层 overlay，底册还是同一个云镜像。多个 VM 共用一份底册，
            # 各自写入时才真正占空间。
            run([find_tool("qemu-img"), "create", "-f", "qcow2",
                 "-b", str(backing), "-F", "qcow2", str(d_new / "disk.qcow2")])
            ok(f"已建 overlay 磁盘（与源共用底册 {backing.name}）")
        else:
            # 源盘没有底册（整盘拷贝来的、或者 backing 已经丢了）。这时候不能
            # 再叠 overlay：叠一个指向不存在文件的 overlay，克隆出来永远起不来。
            # 改成整盘复制——占一整份空间，但至少是台能用的机器。
            # 原来的写法正是这个情况，只是没人告诉过用户。
            r = run(["cp", "-c", str(base), str(d_new / "disk.qcow2")],
                    check=False)
            if r.returncode != 0 or not (d_new / "disk.qcow2").is_file():
                run(["cp", str(base), str(d_new / "disk.qcow2")])
                warn("clonefile 不可用，退回普通拷贝，会占一整份空间")
            else:
                ok("已用 APFS clonefile 复制磁盘（不占额外空间）")
            warn("源盘没有共用的云镜像底册，克隆出来的是独立一整份")
    else:
        base = vm_dir(src["name"]) / "data.img"
        if not base.is_file():
            die(f"源的盘不在：{base}")
        dst = d_new / "data.img"
        # clonefile 是 APFS 的写时复制，瞬间完成且不占额外空间。
        r = run(["cp", "-c", str(base), str(dst)], check=False)
        if r.returncode != 0 or not dst.is_file():
            run(["cp", str(base), str(dst)])
            warn("clonefile 不可用，退回普通拷贝，会占一整份空间")
        else:
            ok("已用 APFS clonefile 复制磁盘（不占额外空间）")

    # EFI NVRAM：每个 VM 私有，复制一份而不是共用——共用一个会被两个 VM 抢着改
    src_vars = vm_dir(src["name"]) / "efi-vars.fd"
    if src_vars.is_file():
        shutil.copy2(src_vars, d_new / "efi-vars.fd")

    # cloud-init 种子盘必须一起复制。start_linux() 会同时要求 disk.qcow2 和
    # seed.iso 都在，原来只建 overlay 盘和 efi-vars.fd：克隆「成功」之后一启动就
    # die「VM 不完整，缺 seed.iso。重新 install。」——用户拿到一个起不来的 VM，
    # 而提示让他重装，等于这个克隆白做。Linux 那条路是主力机型，这个坑必踩。
    src_seed = vm_dir(src["name"]) / "seed.iso"
    if src.get("kind") == "linux":
        if not src_seed.is_file():
            die(f"源的种子盘不在：{src_seed}。给源跑一次 install 或 configure "
                f"补上，再克隆。")
        # clonefile 优先：瞬时且不占空间，失败再普通拷贝（几十 KB 无所谓）
        r = run(["cp", "-c", str(src_seed), str(d_new / "seed.iso")], check=False)
        if r.returncode != 0:
            shutil.copy2(src_seed, d_new / "seed.iso")
        ok("已复制 cloud-init 种子盘")
    elif src_seed.is_file():
        shutil.copy2(src_seed, d_new / "seed.iso")

    # 附加盘：qcow2 的再叠一层 overlay（和源共用底册），raw 的复制文件
    moved: list[dict] = []
    for d in src.get("disks", []):
        f = Path(d["file"])
        if not f.is_file():
            continue
        nd = dict(d)
        nd["file"] = str(d_new / f.name)
        if d.get("format") == "qcow2":
            run([find_tool("qemu-img"), "create", "-f", "qcow2",
                 "-b", str(f), "-F", "qcow2", nd["file"]], check=False)
        else:
            r = run(["cp", "-c", str(f), nd["file"]], check=False)
            if r.returncode != 0:
                run(["cp", str(f), nd["file"]], check=False)
        moved.append(nd)

    # 端口全部分配新的，免得两个 VM 抢同一个口
    # 一律 .get()：一个写了一半的 vm.json 缺 ssh_port 时，v["ssh_port"] 会让
    # 整条 clone 命令 KeyError 崩掉，而用户只是在克隆一个跟这个坏字段无关的 VM。
    taken_ssh = {v.get("ssh_port", 0) for v in all_vms()}
    taken_rdp = {v.get("rdp_port", 0) for v in all_vms()}
    taken_vnc = {v.get("vnc_display", 0) for v in all_vms()}
    taken_web = {v.get("web_port") or 0 for v in all_vms()}

    vm = dict(src)
    vm["name"] = new
    vm["ssh_port"] = alloc_port(PORT_BASE_SSH, taken_ssh)
    vm["rdp_port"] = (alloc_port(PORT_BASE_RDP, taken_rdp)
                      if src.get("rdp_port") else None)
    vm["vnc_display"] = alloc_vnc_display(taken_vnc)
    vm["web_port"] = (alloc_port(PORT_BASE_WEB, taken_web) or None
                      if src.get("web_port") else None)
    vm["disks"] = moved
    vm["shares"] = [dict(x) for x in src.get("shares", [])]
    # ssh_command / rdp_command 是从源抄来的，里面的端口还是源的。不重算的话
    # 用户复制这条命令去连，连上的是源虚拟机而不是克隆——两个 VM 还都在跑的话
    # 根本分不清。克隆出来的是装好的系统，命令应该直接能用，所以按新端口重算。
    if vm.get("ssh_port"):
        vm["ssh_command"] = (f"ssh -i {vm.get('ssh_key', '')} "
                             f"-p {vm['ssh_port']} "
                             f"{vm.get('user', 'agent')}@127.0.0.1")
    if vm.get("rdp_port"):
        vm["rdp_command"] = (f"open vnc://127.0.0.1:{5900 + vm['vnc_display']}"
                             f" （或 Microsoft Remote Desktop 连 "
                             f"127.0.0.1:{vm['rdp_port']}）")
    else:
        vm.pop("rdp_command", None)
    vm["created"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    vm["updated"] = vm["created"]
    # 源装好了，克隆出来就是装好的。ready 直接继承。
    vm["ready"] = src.get("ready", False)
    vm_dir(new).mkdir(parents=True, exist_ok=True)
    save_vm(vm)
    ok(f"已克隆成 {new}（SSH {vm['ssh_port']}，VNC {5900 + vm['vnc_display']}）")
    ok(f"启动它：vmkit.py start --name {new}")
    Emit.done(vm_record(vm))
    return 0


def cmd_share(args) -> int:
    """共享目录（virtio-9p）。

    只有 Linux 那条路真的有 -virtfs 参数。Windows 的 winarm 参数表里没有，
    macOS 走 VZ 也没有——原来两种都不挡，共享被静默记进 vm.json 而从不生效，
    界面显示「已共享」，用户在客人机里怎么也找不到那个目录。
    """
    vm = load_vm(args.name)
    if vm["kind"] != "linux":
        die(f"共享目录只在 Linux 虚拟机上有效（virtio-9p）。"
            f"{vm['kind']} 这条路的磁盘/共享参数表里没有它，记了也不会挂上。")
    """共享目录：把宿主机上的一个文件夹挂进虚拟机（virtio-9p）。

    为什么需要它：虚拟机里装了东西，想把文件取出来或者放进去，原来只能走 SSH
    scp。9p 是 QEMU 内置的，宿主机不用跑 smbd，客人机也不用装驱动（Linux 内核
    自带，Windows 要 virtio-win 里那个）。

    客人机里要自己挂一次：
        Linux   mkdir -p /mnt/host && mount -t 9p -o trans=virtio,version=9p2000.L <tag> /mnt/host
        Windows scsi stop / scsi start 之后用 Diskpart 看不到，要用
                mountvol 或者第三方 9p 客户端
    这一步替不了——挂载点是客人机里的事，而且每个系统做法不一样。
    """
    vm = load_vm(args.name)
    shares = vm.setdefault("shares", [])

    if args.action == "list":
        if not shares:
            ok(f"{vm['name']} 没有共享目录")
        for sh in shares:
            ok(f"{sh['tag']}  →  {sh['path']}")
        Emit.done({"shares": shares})
        return 0

    if args.action == "add":
        if not args.path:
            die("要给宿主机上的目录，例如 --path ~/Shared")
        if not args.tag:
            die("要给一个挂载标签，例如 --tag host")
        p = Path(args.path).expanduser().resolve()
        if not p.is_dir():
            die(f"目录不存在：{p}")
        if any(sh["tag"] == args.tag for sh in shares):
            die(f"标签 {args.tag} 已经用过了，换一个")
        if any(Path(sh["path"]) == p for sh in shares):
            dup = next(sh["tag"] for sh in shares if Path(sh["path"]) == p)
            die(f"{p} 已经共享过了，标签是 {dup}")
        # 在添加时就挡住逗号和等号，别等到重启才在 share_args 里 die。
        # 用户视角：加的时候说成功，重启说失败，而失败信息在 qemu.log 里。
        if "," in str(p) or "," in args.tag:
            die(f"路径或标签里含逗号，QEMU 的 -virtfs 参数语法不支持：\n"
                f"  路径 {p}\n  标签 {args.tag}\n"
                f"把目录挪到不含逗号的路径下，或换个标签。")
        if "=" in args.tag:
            die(f"标签里不能含等号：{args.tag}")
        shares.append({"tag": args.tag, "path": str(p)})
        save_vm(vm)
        ok(f"已共享 {p}，标签 {args.tag}")
        if qemu_pid(vm):
            warn("虚拟机正在运行，要重启后才挂上。")
        else:
            ok("下次启动生效")
        warn(f"客人机里挂载：mount -t 9p -o trans=virtio,version=9p2000.L "
             f"{args.tag} /mnt/<某个空目录>")
        Emit.done(vm_record(vm))
        return 0

    if args.action == "remove":
        if not args.tag:
            die("要指明摘哪个，例如 --tag host")
        target = next((sh for sh in shares if sh["tag"] == args.tag), None)
        if target is None:
            die(f"没有标签 {args.tag}。现有的："
                + ("、".join(sh["tag"] for sh in shares) or "（一个都没有）"))
        shares.remove(target)
        save_vm(vm)
        ok(f"已摘掉共享 {args.tag}（宿主机上的文件没动）")
        Emit.done(vm_record(vm))
        return 0

    die(f"不认识的动作：{args.action}")


def cmd_delete(args) -> int:
    name = args.name
    d = vm_dir(name)
    # vm.json 没了也要能删。上一次删除如果半途失败（驱动暂存目录是只读的，
    # 就会），目录还在而 vm.json 已经没了，load_vm 会报「没有这个虚拟机」，
    # 用户从此没法把这个残留清掉——只能手去 rm。所以先看目录在不在。
    if not d.exists():
        die(f"没有这个虚拟机：{name}。用 list 看现有的。")
    if (d / "vm.json").is_file():
        vm = load_vm(name)
        if qemu_pid(vm):
            warn("VM 还在跑，先关机再删")
            cmd_stop(args)
        # 别把别人当底册用的盘删掉。
        #
        # clone 建的是 qcow2 overlay，底册指向源盘（或共用的云镜像）。而这里
        # 对 VM 目录是无条件 _rmtree——底册一没，那台克隆的 disk.qcow2 就指着
        # 一个不存在的文件，QEMU 起不来（Could not open backing file），而它的
        # vm.json 完好、vm_broken 也查不出来。用户拿到一台「配置齐全但永远
        # 启动不了」的机器，且没有任何线索指向这次删除。2026-11-11 审计补上。
        dependents = qcow2_backing_dependents(vm)
        if dependents:
            die(f"{name} 的盘被这些虚拟机当底册用着：{'、'.join(dependents)}。"
                f"删掉它，它们会变成「配置齐全但永远启动不了」。\n"
                f"先删这些克隆（它们没有独立数据）：\n"
                + "\n".join(f"  {_self_cmd()} delete --name {n}"
                            for n in dependents))
    elif (d / "macvm.json").is_file():
        # macOS 虚拟机没有 vm.json（它的配置在 macvm.json 里），
        # 但删的时候照样要先停机、照样要卸自启 plist。漏了这一段，
        # 删一个正在跑的 macOS VM 会把盘从 VZ 脚下抽走。
        entry = macos_pid_entry({"name": name})
        if entry:
            host, pid = entry
            if host == "app":
                # 在 app 里跑着的机器命令行停不了，也就不能删：删了就是
                # 把 disk.img 从一个活的 VZVirtualMachine 脚下抽走。
                die(f"{name} 正在 AgentVM.app 里运行（pid {pid}），"
                    f"命令行停不了它，也不能就这么删。\n"
                    f"请在 app 里选中它关机，然后再删。")
            warn("VM 还在跑，先关机再删")
            cmd_stop(args)
        # 开机自启的 plist 必须一起卸掉。原来只 _rmtree(d)：plist 还留在
        # ~/Library/LaunchAgents/，而它指向的 wrapper 就在被删掉的 VM 目录里。
        # 之后每次登录 launchd 执行一个不存在的脚本 → 非零退出 → KeepAlive 加
        # ThrottleInterval 15 → 每 15 秒重试一次，永不停止，还会往已删除目录写
        # 日志失败。用户看到的是「删了的虚拟机还在每隔一会儿折腾一下」。
        # bootout 之后再 unload 是老版本 launchd 的兼容写法，两个都发。
        plist = HOME / "Library" / "LaunchAgents" / f"com.agentvm.{name}.plist"
        if plist.exists():
            run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                check=False)
            run(["launchctl", "unload", str(plist)], check=False)
            plist.unlink(missing_ok=True)
            ok(f"已卸掉开机自启 {plist}")
    _rmtree(d)
    ok(f"已删除 {d}")
    return 0


def cmd_open(args) -> int:
    if load_vm(args.name)["kind"] == "macos":
        die("macOS 虚拟机的画面在 AgentVM app 里（VZVirtualMachineView），"
            "命令行没有窗口可切。\n"
            "打开 app，选中它，点「打开窗口」。")

    """把虚拟机窗口带到前台。

    launchd 或 GUI 拉起的 Cocoa 窗口不会自动抢焦点，用户经常以为窗口没开。
    用 AppleScript activate 一次最省事，比枚举窗口可靠。
    """
    vm = load_vm(args.name)
    if not qemu_pid(vm):
        # 同 cmd_window：这句话会被 app 原样弹成对话框，不能写成命令行口气。
        die(f"{args.name} 没在运行，没有窗口可切到前台。\n"
            f"在 app 里选中它，点「启动并用系统窗口打开」。")
    # headless 跑的 VM 没有窗口可带到前台。原来这里照样 activate 然后报
    # 「已带到前台」，用户到处找窗口找不到——launchd 开机自启和安装阶段
    # 都是这条路径，说谎比说没有办法更糟。
    if vm.get("headless"):
        warn(f"{args.name} 是无窗口运行的，没有窗口可切。"
             f"要看画面用 screenshot --name {args.name}")
        return 0
    r = subprocess.run([find_tool("osascript"), "-e",
                        'tell application "System Events" to tell '
                        'process "qemu-system-aarch64" to set frontmost to true'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # 退一步：只 activate，不要求进程名精确匹配
        subprocess.run([find_tool("osascript"), "-e",
                        'tell application "qemu-system-aarch64" to activate'],
                       capture_output=True, text=True)
    ok("已把虚拟机窗口带到前台")
    return 0


def cmd_screenshot(args) -> int:
    # --out 会原样插进 QEMU monitor 的 HMP 命令。HMP 是换行分隔的，
    # 路径里带一个换行就能接任意 monitor 命令——包括 quit，而这个文件
    # 里好几处注释刚强调过「绝对不能发 quit」。默认路径和 app 都不碰它
    # （app 用 VNC，不截图），所以这是命令行自伤面，但仍然要挡。
    out = args.out or ""
    if any(c in out for c in "\n\r"):
        die("截图路径里不能有换行")
    vm = load_vm(args.name)
    d = vm_dir(vm["name"])
    # 默认输出不落在 /tmp。那里是全局可写的，另一个用户可以先建一个指向某
    # 用户可写文件的符号链接，QEMU 的 screendump 会顺着链接把目标覆盖掉。
    # 默认改到这个 VM 自己的目录——那是它本来就该待的地方，删 VM 时一起清。
    out = Path(args.out or (d / "screen.ppm"))
    # screendump 只会写 PPM（QEMU 的固定格式），没有选择。而 PPM 大部分工具
    # 不认：NSImage 读不了、飞书发不出去、用户双击也打不开。所以这里统一用
    # macOS 自带的 sips 转成 png——转不了就留 PPM，并如实说清楚给的是什么。
    ppm = out.with_suffix(".ppm") if out.suffix.lower() == ".png" else out
    mon = d / "monitor.sock"
    # 先看进程，再看套接字文件。原来只看文件在不在，而 QEMU 退出时不清理它
    # （2026-11-10 实测：linux1 关着机，monitor.sock 和 qemu.pid 都还留着），
    # 于是这条检查永远通过，一路走到 screendump 才失败，报的是「没抓到画面，
    # VM 可能刚退出或没配 framebuffer 设备」——真相是它根本没起动过，那句话
    # 把人往 framebuffer 配置上引。
    if not qemu_pid(vm):
        die(f"{args.name} 没在运行，没有画面可截。\n"
            f"在 app 里选中它，点「启动并用系统窗口打开」，或者先启动再看画面。")
    if not mon.exists():
        die(f"{args.name} 在跑，但 monitor 套接字不在（{mon}）。\n"
            f"这台不是用 vmkit 起的，或者套接字被删了——没有它截不了图。")
    if not screendump(mon, ppm):
        # 失败就必须失败，不能回头用上一次留下的旧文件：一张过期画面比没有
        # 画面更容易让人判断错客人机停在哪。
        die("没抓到画面，VM 可能刚退出或没配 framebuffer 设备")
    png = None
    if ppm.suffix.lower() == ".ppm":
        sips = which("sips")
        if sips:
            cand = ppm.with_suffix(".png")
            r = subprocess.run([sips, "-s", "format", "png", str(ppm),
                                "--out", str(cand)],
                               capture_output=True, text=True)
            if r.returncode == 0 and cand.is_file():
                png = cand
    final = png or ppm
    ok(f"画面已存 {final}")
    Emit.done({"path": str(final)})
    return 0


def host_cpus() -> int:
    try:
        return os.cpu_count() or 2
    except Exception:
        return 2


def host_mem() -> int:
    """宿主机物理内存字节数。读不到就给 8 GB，别让配置卡在这一步。"""
    r = subprocess.run([find_tool("sysctl"), "-n", "hw.memsize"],
                       capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 8 << 30


def cmd_restart(args) -> int:
    """重启虚拟机，让改过的配置生效。

    CPU、内存、显示模式、端口、挂不挂光盘，这些都是 QEMU 的启动参数，改完必须
    重启才生效。原来改完只告诉用户「要重启才生效」，然后用户自己去点关机、再点
    启动——多两步，而且容易忘了。这条命令把两步合成一步，并且说清楚是为了让改动
    生效，不是偷偷重启。

    没在运行就直接启动：用户点「保存并重启」时期望的就是起来，报一句「没在运行」
    让他再点一次没有意义。
    """
    vm = load_vm(args.name)
    if not qemu_pid(vm):
        ok("VM 没在运行，直接启动")
        return cmd_start(args)
    log(f"重启 {args.name}，让改过的配置生效")
    cmd_stop(args)
    return cmd_start(args)


def cmd_configure(args) -> int:
    """改一个已经建好的虚拟机的配置。

    原来配置只能在安装时填一次，装完想加内存只能删了重装——删了重装意味着
    里面的东西全没。这条命令就是补这个缺口。

    三条要说清楚的：

    1. **CPU 和内存改完要重启才生效。** 它们是 QEMU 的启动参数，跑起来之后改不了。
       这里不偷偷重启：虚拟机可能正装着系统、跑着东西，重启是用户自己的决定。
    2. **磁盘只能往大改。** 改小不会让文件系统跟着缩，而 truncate / qemu-img resize
       都会把超出去的部分截掉，客人机里的数据直接对不上。往大改也要客人机里再扩一次
       分区和文件系统（Windows 要 diskpart、Linux 要 growpart + resize2fs），那一步
       在这儿替不了，所以明说。
    3. **改磁盘大小时虚拟机必须关机。** qcow2 镜像被 QEMU 占着写锁，resize 会失败。
    """
    vm = load_vm(args.name)
    # 密码也可能从文件来。放在最前面：configure_macos 和下面那串
    # `if args.password` 都读 args.password，一个入口两种来源容易漏一边。
    resolve_password(args)
    name = vm.get("name", args.name)

    # macOS 客人机的配置不在 vm.json 里，在 macvm.json（vmz 的格式）。
    # 两边各管各的文件：vmkit 的 save_vm 是整体重写，混在一个文件里
    # 迟早互相冲掉。这里单独走一条路，改完照样说清楚要重启才生效。
    if vm.get("kind") == "macos":
        return configure_macos(args)

    changed: list[str] = []

    if args.cpu is not None:
        top = host_cpus()
        if not 1 <= args.cpu <= top:
            die(f"CPU 核心要给 1 到 {top} 之间，你给了 {args.cpu}")
        if args.cpu != vm.get("cpus"):
            vm["cpus"] = args.cpu
            changed.append(f"CPU {args.cpu} 核")

    if args.mem:
        want = parse_size(args.mem)
        if want < 512 << 20:
            die(f"内存至少 512M，你给了 {args.mem}")
        # 给宿主机留 2 GB。QEMU 的客人机线程是真实线程，占太满会把 macOS
        # 自己的界面线程挤下去，表象是键盘鼠标失灵——这不是理论风险。
        ceiling = host_mem() - (2 << 30)
        if want > ceiling:
            die(f"内存最多 {human(ceiling)}（宿主机 {human(host_mem())}，"
                f"要给 macOS 自己留 2 GB）")
        # 一律 .get()：vm.json 是会被写一半的（安装中断、用户手改、旧版本
        # 留下的），缺 ram / disk 的坏文件原来在这里 KeyError 崩掉。同文件
        # vm_record 的注释刚写过这条，这几处是漏的。
        if want != parse_size(vm.get("ram", "0")):
            vm["ram"] = args.mem.upper()
            changed.append(f"内存 {vm['ram']}")

    if args.disk:
        want = parse_size(args.disk)
        cur = parse_size(vm.get("disk", "0"))
        # 下限 1G。原来没有：`--disk 64` 被解析成 64 字节，qemu-img 老实建成一个
        # 512 字节的盘，QEMU 起来就是一个空盘，用户完全看不出所以然。
        if want < 1 << 30:
            die(f"磁盘至少 1G，你给了 {args.disk}")
        if want < cur:
            die(f"磁盘只能往大改：现在是 {vm.get('disk')}，你要给 {args.disk}。"
                f"改小会把超出去的数据截掉，文件系统不会跟着缩。")
        if want > cur:
            if qemu_pid(vm):
                die("虚拟机正在运行，镜像被占着写锁，改不了大小。先 stop。")
            d = vm_dir(name)
            if vm.get("kind") == "linux":
                disk = d / "disk.qcow2"
                if not disk.is_file():
                    die(f"找不到 {disk}")
                r = run([find_tool("qemu-img"), "resize", str(disk), str(want)],
                        check=False)
                if r.returncode != 0:
                    die(f"qemu-img resize 失败：{(r.stderr or '').strip()[:300]}")
            else:
                disk = d / "data.img"
                if not disk.is_file():
                    die(f"找不到 {disk}")
                # 稀疏文件：truncate 只改逻辑大小，写到了才真占空间
                with open(disk, "r+b") as f:
                    f.truncate(want)
            vm["disk"] = args.disk.upper()
            changed.append(f"磁盘 {vm['disk']}（文件已扩大）")

    if args.user:
        vm["user"] = args.user
        changed.append(f"登录名 {args.user}")
    if args.password:
        vm["password"] = args.password
        changed.append("密码")
        # 改密码必须重新生成种子盘。Linux 那条路的密码是 cloud-init 从 seed.iso
        # 里的 NoCloud user-data 读的，而 linux_seed() 只在 install 时调用过一次。
        # 原来这里只改 vm.json 就完事：输出写着「已改 密码 / 下次启动生效」，
        # 重启后 cloud-init 读到的还是旧 user-data，用户拿新密码登不进去，
        # 而界面说已改。这是最难查的那类 bug——两边都「成功」了。
        if vm.get("kind") == "linux":
            try:
                linux_seed(vm)
                changed.append("种子盘已重写（新密码下次启动生效）")
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 — 种子盘建失败不能让配置丢掉
                warn(f"种子盘重写失败（{e}）。vm.json 已改，但登录可能还用旧密码。")

    # ---- 显示模式：有窗口还是没有窗口 ----
    if args.window:
        if vm.get("headless"):
            vm["headless"] = False
            changed.append("显示模式：原生窗口")
    elif args.headless:
        if not vm.get("headless"):
            vm["headless"] = True
            changed.append("显示模式：无窗口（画面走 VNC）")

    # ---- 挂不挂安装光盘（只有本地包装的系统有意义） ----
    if args.no_iso:
        if vm.get("attach_iso"):
            vm["attach_iso"] = False
            changed.append("启动时不挂安装光盘")
    elif args.iso:
        if vm["kind"] != "local":
            die("只有用自己的安装包装的虚拟机才能挂光盘，catalog 里那些没有本地 ISO")
        if not vm.get("source") or not Path(vm["source"]).is_file():
            die(f"安装包不见了：{vm.get('source')}。没法挂。")
        if not vm.get("attach_iso"):
            vm["attach_iso"] = True
            changed.append("启动时挂载安装光盘（会进安装器）")

    # ---- 端口 ----
    # 端口是「重启后生效」的属性里最容易撞的：改完不检查，下次启动 QEMU 绑不上
    # 就直接退出，而 qemu.log 里那句话很容易被忽略。所以在这里就比一遍。
    taken_ports: dict[int, str] = {}
    for other in all_vms():
        if other["name"] == name:
            continue
        for key, val in (("ssh_port", other.get("ssh_port")),
                         ("rdp_port", other.get("rdp_port")),
                         ("web_port", other.get("web_port"))):
            if val:
                taken_ports[val] = f"{other['name']} 的 {key}"
        vd = other.get("vnc_display")
        if vd:
            taken_ports[5900 + vd] = f"{other['name']} 的 VNC"

    def take_port(want: int, what: str) -> None:
        if not 1024 <= want <= 65535:
            die(f"{what} 端口要给 1024 到 65535 之间，你给了 {want}")
        if want in used_ports():
            die(f"端口 {want} 已经被宿主机上别的程序占了，换一个")
        if want in taken_ports:
            die(f"端口 {want} 已经分给 {taken_ports[want]} 了，换一个")
        taken_ports[want] = what

    # 写了一半的 vm.json 可能没有 ssh_port：原来这里直接下标，一个坏文件
    # 就让 configure 整条命令 KeyError 崩掉，而用户只是想改个内存。
    if args.ssh_port is not None and args.ssh_port != vm.get("ssh_port"):
        take_port(args.ssh_port, "SSH")
        vm["ssh_port"] = args.ssh_port
        changed.append(f"SSH 端口 {args.ssh_port}")
    if args.vnc_port is not None:
        # 界面和 status 用端口，vm.json 存显示编号。两边换算，别混。
        # 下限必须是 5900：小于它显示编号就成负数，QEMU 的 -vnc 会当异常值。
        if not 5900 <= args.vnc_port <= 65535:
            die(f"VNC 端口要给 5900 到 65535 之间（QEMU 按 5900+编号 监听），"
                f"你给了 {args.vnc_port}")
        disp = args.vnc_port - 5900
        if disp != vm.get("vnc_display"):
            take_port(args.vnc_port, "VNC")
            vm["vnc_display"] = disp
            changed.append(f"VNC 端口 {args.vnc_port}")
    if args.rdp_port is not None and args.rdp_port != vm.get("rdp_port"):
        take_port(args.rdp_port, "远程桌面")
        vm["rdp_port"] = args.rdp_port
        changed.append(f"远程桌面端口 {args.rdp_port}")
    if args.web_port is not None and args.web_port != vm.get("web_port"):
        take_port(args.web_port, "Web 界面")
        vm["web_port"] = args.web_port
        changed.append(f"Web 界面端口 {args.web_port}")

    if not changed:
        ok("什么都没改")
        Emit.done(vm_record(vm))
        return 0

    save_vm(vm)
    for c in changed:
        ok(f"已改 {c}")
    needs_restart = any(k in " ".join(changed)
                        for k in ("CPU", "内存", "显示模式", "端口", "安装光盘"))
    if needs_restart:
        if qemu_pid(vm):
            warn("这些改动要重启虚拟机才生效。现在它在运行，你自己决定什么时候重启"
                 "（要一键重启：vmkit.py restart --name " + name + "）。")
        else:
            ok("下次启动生效")
    if any("磁盘" in c for c in changed):
        warn("磁盘文件已经扩大了，但客人机里还要再扩一次分区和文件系统："
             "Linux 用 growpart 加 resize2fs，Windows 用 diskpart extend。")
    Emit.done(vm_record(vm))
    return 0


def configure_macos(args) -> int:
    """改 macOS 虚拟机的配置：CPU、内存、账号密码、分辨率。

    磁盘不给改：VZ 的盘是 raw，truncate 大一点虽然不会像 qcow2 那样
    立刻报错，但 APFS 容器大小是记在盘里的，改大之后客人机看不到新空间，
    改小直接破坏文件系统。要更大就删了重装，或者用「存储」里加附加盘。

    全部改完都要重启才生效——它们是创建 VZVirtualMachine 时的参数。
    """
    name = args.name
    spec_path = vm_dir(name) / "macvm.json"
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, ValueError) as e:
        die(f"读不了 {spec_path}：{e}")
    if not isinstance(spec, dict):
        die(f"{spec_path} 内容不是一份配置（是 "
            f"{type(spec).__name__}），读不了。")
    # 磁盘不给改，而且要说出来。原来 --disk 被静默忽略，而 app 的硬件区
    # 照样显示磁盘选择器：用户选了个更大的值，后端回一句「没有要改的」，
    # app 却显示已保存——界面和实际不一致。
    if args.disk:
        die("macOS 虚拟机的磁盘不能改。VZ 的盘是 raw，改大会让 APFS 容器大小"
            "和文件系统对不上（客人机看不到新空间），改小直接破坏文件系统。\n"
            "要更大的盘：删掉重装，或者用「存储」面板加一块数据盘。")
    # VM 在跑时改配置：cpu/内存/分辨率都是创建 VZVirtualMachine 时的参数，
    # 改了也要重启才生效，这跟另外几种一样，说清楚就行。
    if macos_pid({"name": name}):
        warn(f"{name} 正在运行。这些改动要重启之后才生效。")
    changed: list[str] = []

    if args.cpu is not None:
        top = host_cpus()
        if not 1 <= args.cpu <= top:
            die(f"CPU 核心要给 1 到 {top} 之间，你给了 {args.cpu}")
        if args.cpu != spec.get("cpus"):
            spec["cpus"] = args.cpu
            changed.append(f"CPU {args.cpu} 核")
    if args.mem:
        want = parse_size(args.mem)
        if want < 4 << 30:
            # macOS 客人机的下限比 Linux 高：低于 4 GB 连安装器都起不来，
            # 而 VZMacOSConfigurationRequirements 里写的最低值是 4 GB。
            die(f"内存至少 4G（macOS 客人机的最低要求），你给了 {args.mem}")
        if args.mem != spec.get("ram"):
            spec["ram"] = args.mem
            changed.append(f"内存 {args.mem}")
    if args.user:
        if not args.user.replace("-", "").replace("_", "").isalnum():
            die(f"登录名只能含字母数字和 - _：{args.user}")
        if args.user != spec.get("user"):
            spec["user"] = args.user
            changed.append(f"登录名 {args.user}")
    if args.password:
        if len(args.password) < 4:
            die("密码至少 4 个字符（客人机建账号时的最低要求）")
        spec["password"] = args.password
        # 改密码要重新 provisioning，否则新密码在客人机里不生效——
        # 账号已经在第一次启动时建好了，再建一次会撞。
        spec["provisioned"] = False
        changed.append("密码（下次启动会重新建账号）")
    if getattr(args, "width", None) or getattr(args, "height", None):
        w = args.width or spec.get("width", 1440)
        h = args.height or spec.get("height", 900)
        if not 800 <= w <= 5120 or not 600 <= h <= 3200:
            die(f"分辨率要给 800x600 到 5120x3200 之间，你给了 {w}x{h}")
        spec["width"], spec["height"] = w, h
        changed.append(f"分辨率 {w}x{h}")

    if not changed:
        ok("没有要改的")
        return 0
    # 原子写：先写临时文件再 replace。原来直接 write_text 覆写，而 VM 在跑时
    # vmz 自己也会写同一个文件（它启动末尾把 provisioned 置 true 后存盘），
    # 两边同时写一个文件，谁后写谁赢，而中间态可能是一半的 JSON——
    # 下一次读就是「不是合法 JSON」，虚拟机直接不可用。
    tmp = spec_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    # 0600：里面有明文密码。先设权限再 replace，消掉「临时文件是 0644」
    # 的那个窗口。
    os.chmod(str(tmp), 0o600)
    tmp.replace(spec_path)
    for c in changed:
        ok(f"已改 {c}")
    ok("重启之后生效。")
    return 0


def _xml_escape(s) -> str:
    """XML 文本转义。plist 是 XML，名字或路径里出现 & < > 会让 launchd
    解析失败，严重时写出一份 launchctl load 直接报错的文件——而报错信息
    指的是一个 plist 路径，用户看不出是名字里的字符惹的祸。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def cmd_autostart(args) -> int:
    vm = load_vm(args.name)
    # 名字要单独校验。args.name 在 main() 里已经过过白名单，但这里用的是
    # vm["name"]——vm.json 的内容不一定是本工具写的（手改、旧版本、别的
    # 入口），一个带引号或 $ 的名字会原样写进 launchd.sh 和 plist 标签，
    # 而 launchd.sh 是被 launchd 当脚本执行的：那是以当前用户权限的
    # 任意命令执行。
    name = vm.get("name") or args.name
    check_vm_name(name)
    d = vm_dir(name)
    label = f"com.agentvm.{name}"
    plist = HOME / "Library" / "LaunchAgents" / f"{label}.plist"
    wrapper = d / "launchd.sh"
    py = Path(sys.executable).resolve()
    script = Path(__file__).resolve()

    if args.disable:
        if plist.exists():
            run(["launchctl", "unload", str(plist)], check=False)
            plist.unlink()
            ok(f"已卸载 {plist}")
        else:
            ok("本来就没装开机自启")
        return 0

    kind = vm.get("kind")
    if kind == "windows":
        # shlex.quote：路径里带空格或 shell 元字符时，不转义就是把一句话
        # 切成好几个词，或者把引号里的内容变成命令。
        inner = (f"exec {shlex.quote(str(py))} {shlex.quote(str(WINARM))} "
                 f"start --foreground < /dev/null")
    elif kind == "macos":
        # macOS 客人机的 wrapper 调 vmz。它前台跑（VZ 的 VM 跟进程同生共死），
        # 所以 launchd 的 KeepAlive 正好合用：进程一退就再拉起来。
        vmz = find_vmz()
        if not vmz:
            die("找不到 vmz，配不了 macOS 虚拟机的开机自启。")
        inner = (f"exec {shlex.quote(str(vmz))} start "
                 f"--name {shlex.quote(name)} < /dev/null")
    else:
        inner = (f"exec {shlex.quote(str(py))} {shlex.quote(str(script))} start "
                 f"--name {shlex.quote(name)} --headless < /dev/null")

    # QEMU 的路径只有另外三种需要。原来这行在 if/elif 链外面无条件执行，
    # 而 find_tool 找不到就 die——只跑 macOS 虚拟机、没装 QEMU 的机器上，
    # autostart 直接报「找不到 qemu-system-aarch64」，而这个 VM 压根不碰它。
    if kind == "macos":
        hb = str(Path(vmz).parent.parent) if vmz else "/usr"
    else:
        hb = find_tool("qemu-system-aarch64").rsplit("/bin/", 1)[0]
    wrapper.write_text(f"""#!/bin/sh
# 由 vmkit 生成。launchd 不继承交互 shell 的 PATH，而 QEMU 在 {hb}/bin。
export PATH="{hb}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOMEBREW_PREFIX="{hb}"
export VM_DIR="{d}"
# 开机自启走无窗口模式，否则每次登录 Mac 都弹一个虚拟机窗口。要看窗口就
# 用 vmkit start（不带 --headless）或 open。
# macOS 客人机没有窗口模式（VZ 的画面只能在 app 进程里显示），这个变量对它无效。
export HEADLESS="1"
{inner}
""")
    wrapper.chmod(0o755)

    if plist.exists():
        run(["launchctl", "unload", str(plist)], check=False)
    plist.parent.mkdir(parents=True, exist_ok=True)
    # 文本节点一律转义：plist 是 XML，路径和标签里有 & < > 就让这份文件
    # 不再是合法 XML，launchctl load 报的错还指向不到真正的原因。
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_xml_escape(label)}</string>
    <key>ProgramArguments</key>
    <array><string>{_xml_escape(wrapper)}</string></array>
    <key>RunAtLoad</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>{_xml_escape(d / 'launchd.log')}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(d / 'launchd.log')}</string>
</dict>
</plist>
""")
    r = run(["launchctl", "load", str(plist)], check=False)
    if r.returncode != 0:
        die(f"launchctl load 失败：{(r.stderr or r.stdout or '').strip()}")
    ok(f"开机自启已装：{plist}")
    return 0


def cmd_catalog() -> int:
    cat = load_catalog()
    Emit._w({"t": "catalog", "distros": cat["distros"]})
    return 0


def cmd_list() -> int:
    # 必须走 vm_record，不能把 all_vms() 的原文吐出去。vm.json 里有 password
    # 和 ssh_key 的明文——vm_record 特意不含这两项，而 list 这条路原来直接发
    # 原文：app 内存里、app 的日志里都躺着一份明文口令。
    Emit._w({"t": "vms", "vms": [vm_record(v) for v in all_vms()]})
    return 0


# ------------------------------------------------------------------ 入口 --

def main() -> int:
    global Emit
    ap = argparse.ArgumentParser(description="agentvm 统一后端")
    ap.add_argument("--no-json", action="store_true", help="输出人话而不是 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("catalog", help="列出可选系统")
    sub.add_parser("check", help="检查宿主机能不能跑")
    sub.add_parser("list", help="列出虚拟机")

    p = sub.add_parser("install", help="装一个虚拟机")
    p.add_argument("--distro",
                   help="catalog.json 里的系统 id。装本地包时不需要")
    p.add_argument("--source",
                   help="本地安装 ISO 的路径。自己挑的安装包走这条")
    p.add_argument("--source-url",
                   help="安装包的网址。先下到 <vm>/iso/ 再当本地包装")
    p.add_argument("--name", required=True)
    p.add_argument("--cpu", type=int)
    p.add_argument("--mem")
    p.add_argument("--disk")
    p.add_argument("--user")
    p.add_argument("--password", help="密码。它会以明文落在 vm.json 里（0600），"
                                      "能不给就不给")
    p.add_argument("--password-file",
                   help="密码文件，内容读进内存后即删，不进命令行参数")
    p.add_argument("--ssh-key")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--wait", type=int, default=40, help="等就绪的分钟数")
    p.add_argument("--no-auto", action="store_true",
                   help="本地安装包不要自动点安装器，跑起来之后用户自己在"
                        "画面里点完（默认自动点）")
    p.add_argument("--force", action="store_true",
                    help="同名就整个目录删掉重装（里面的数据一起没）")
    p.add_argument("--detach", action="store_true",
                   help="甩到后台跑，父进程只返回日志路径（GUI 用这个）")

    for c in ("start", "stop", "restart", "status", "delete", "open",
              "autostart", "screenshot", "window", "configure"):
        sp = sub.add_parser(c)
        # status 可以不带名字看全部，其余必须指定装哪个 VM
        if c == "status":
            sp.add_argument("--name", nargs="?")
        else:
            sp.add_argument("--name", required=True)
        if c in ("start", "restart", "autostart", "window"):
            sp.add_argument("--headless", action="store_true")
            sp.add_argument("--disable", action="store_true")
        # 只要窗口，忽略存下来的 headless。无窗口跑的 VM 要窗口必须重启一次
        # QEMU，见 cmd_window。
        if c in ("start", "restart", "window"):
            sp.add_argument("--window", action="store_true",
                            help="强制用原生窗口启动（无窗口跑的会重启一次）")
        if c == "screenshot":
            sp.add_argument("--out", default=None,
                            help="存到哪，默认 <虚拟机目录>/screen.ppm")
        if c == "configure":
            sp.add_argument("--cpu", type=int, help="CPU 核心数")
            sp.add_argument("--mem", help="内存，如 4G")
            sp.add_argument("--disk", help="磁盘，只能往大改，如 64G")
            sp.add_argument("--user", help="登录名")
            sp.add_argument("--password", help="密码")
            sp.add_argument("--password-file",
                            help="密码文件，读进内存后即删，不进命令行参数")
            sp.add_argument("--headless", action="store_true",
                            help="改成无窗口运行（画面走 VNC）")
            sp.add_argument("--window", action="store_true",
                            help="改成开原生窗口")
            sp.add_argument("--iso", action="store_true",
                            help="启动时挂上安装光盘（会进安装器，仅本地包有）")
            sp.add_argument("--no-iso", action="store_true",
                            help="启动时不挂安装光盘")
            sp.add_argument("--ssh-port", type=int, help="SSH 端口")
            sp.add_argument("--vnc-port", type=int,
                            help="VNC 端口，实际端口，不是显示编号")
            sp.add_argument("--rdp-port", type=int, help="远程桌面端口")
            sp.add_argument("--web-port", type=int, help="Web 界面端口")
            # 分辨率只有 macOS 客人机有：VZ 的显示尺寸在建 VM 时定死，
            # 跑起来改不了（见 MacVMSpec.width 的注释）。
            sp.add_argument("--width", type=int,
                            help="显示宽度（macOS 虚拟机）")
            sp.add_argument("--height", type=int,
                            help="显示高度（macOS 虚拟机）")

    # 这三个命令的第一个参数是动作，用 choices 限死，省得各自判一遍
    p = sub.add_parser("disk", help="管理附加数据盘")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("--name", required=True)
    p.add_argument("--size", help="加盘时给大小，如 64G")
    p.add_argument("--disk", help="摘盘时指明哪块，如 data1")
    p.add_argument("--format", choices=["raw", "qcow2"], default="raw")
    p.add_argument("--bus", choices=["virtio-blk", "usb-storage"],
                   default="virtio-blk")
    p.add_argument("--keep-file", action="store_true",
                   help="摘盘时只摘不删文件（数据还要）")

    p = sub.add_parser("snapshot", help="qcow2 快照：拍一张、回滚、删")
    p.add_argument("action", choices=["create", "list", "apply", "delete"])
    p.add_argument("--name", required=True)
    p.add_argument("--tag", help="快照名字")
    p.add_argument("--desc", help="备注（写进日志，不写进镜像）")

    p = sub.add_parser("clone", help="照着一个装好的虚拟机复制一份")
    p.add_argument("--name", required=True, help="源")
    p.add_argument("--new-name", required=True, help="目标名字")
    p.add_argument("--force", action="store_true",
                    help="目标已存在就整个删掉重建")

    p = sub.add_parser("share", help="共享目录（virtio-9p）")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("--name", required=True)
    p.add_argument("--tag", help="挂载标签，客人机里按它认")
    p.add_argument("--path", help="宿主机上的目录")

    p = sub.add_parser("guest", help="通过 QEMU guest agent 碰客人机")
    p.add_argument("action", choices=["ping", "ip", "disks", "exec", "shutdown"])
    p.add_argument("--name", required=True)
    p.add_argument("--timeout", type=float, default=60.0,
                   help="exec 等客人机命令结束的秒数")
    # exec 后面跟的是一条要原样送进客人机的命令，必须是 REMAINDER：
    # 固定位置参数会把 `-la` 这类当成 vmkit 自己的选项报「unrecognized」。
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="exec 的要执行的命令，前面加 -- 更稳")

    args = ap.parse_args()
    if args.no_json:
        Emit.on = False

    if args.cmd == "catalog":
        return cmd_catalog()
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "install":
        if args.detach:
            return spawn_detached_install(args)
        try:
            return cmd_install(args)
        finally:
            # install.pid 用完就删。
            #
            # 留着它，app 的 resumeWatching 会拿一个死进程号当真：macOS 的 pid
            # 会回绕复用，一旦这个号被派给无关进程，app 就认为这个安装永远在跑
            # ——顶部横幅钉在「正在安装」、所有按钮因为 busy 一直是灰的，而用户
            # 什么都做不了，只能强杀 app。删掉它，app 那边读到的是「文件不在」，
            # 结论和事实一致。
            # 放在 finally 里：die() 走 sys.exit，正常返回也会走到，两条路都清。
            try:
                (vm_dir(args.name) / "install.pid").unlink()
            except OSError:
                pass
    if args.cmd == "status":
        # status 允许不带 --name（看全部），所以要在下面的检查之前处理
        return cmd_status(args)
    if not args.name:
        die(f"{args.cmd} 需要 --name")
    # 名字先过白名单再进任何子命令。vm_dir() 是 VMS / name 直接拼路径，
    # 而 delete 对这个路径是 _rmtree：`delete --name ..` 删的是
    # ~/.agentvm/vms/.. 也就是 ~/.agentvm——所有 VM 和全部底册镜像一起没。
    # 放在分派之前统一做，因为每个子命令都拿 args.name 拼路径，逐个补校验
    # 漏一个就是一次数据销毁。校验放在 exists 判断之前，所以 delete 照样
    # 能删「只剩目录、没有 vm.json」的残留（那正是删除失败后要清的）。
    check_vm_name(args.name)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "restart":
        return cmd_restart(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "delete":
        return cmd_delete(args)
    if args.cmd == "open":
        return cmd_open(args)
    if args.cmd == "window":
        return cmd_window(args)
    if args.cmd == "autostart":
        return cmd_autostart(args)
    if args.cmd == "configure":
        return cmd_configure(args)
    if args.cmd == "disk":
        return cmd_disk(args)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "clone":
        return cmd_clone(args)
    if args.cmd == "share":
        return cmd_share(args)
    if args.cmd == "guest":
        return cmd_guest(args)
    if args.cmd == "screenshot":
        return cmd_screenshot(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
