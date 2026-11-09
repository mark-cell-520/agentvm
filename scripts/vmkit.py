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

用法
----
    vmkit.py catalog                     列出可选系统
    vmkit.py check                       检查宿主机能不能跑
    vmkit.py install --distro debian-13 --name d1
    vmkit.py list
    vmkit.py status --name d1
    vmkit.py start --name d1 [--headless]
    vmkit.py stop --name d1
    vmkit.py delete --name d1
    vmkit.py open --name d1              把虚拟机窗口带到前台
    vmkit.py autostart --name d1 [--disable]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".agentvm"
IMAGES = ROOT / "images"
VMS = ROOT / "vms"
ASSETS = ROOT / "assets"
HERE = Path(__file__).resolve().parent
CATALOG = HERE / "catalog.json"
WINARM = HERE / "winarm-vm.py"

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
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"{p} 不是合法 JSON：{e}")


def save_vm(vm: dict) -> None:
    vm["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = vm_file(vm["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(vm, ensure_ascii=False, indent=2))
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

    df = shutil.disk_usage(HOME)
    free = df.free
    info["free_bytes"] = free
    info["items"].append({
        "name": "磁盘剩余", "ok": free > (12 << 30),
        "detail": f"{human(free)}（Windows 需要 10 GB 以上）"})
    if free <= (12 << 30):
        info["ok"] = False

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
    """
    used = used_ports()
    for d in range(VNC_DISPLAY_BASE, VNC_DISPLAY_BASE + 4000):
        if d in taken_displays:
            continue
        if PORT_VNC_BASE + (d - VNC_DISPLAY_BASE) in used:
            continue
        return d
    die("VNC 显示编号分配失败：4000 个都占满了")


# ------------------------------------------------------------ cloud-init --

# 登录名的白名单。cloud-init 拿它调 useradd，非法字符让建用户这一步失败，
# 而失败发生在首次启动的客人机里，宿主机只看到「SSH 连不上」。
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _yaml_scalar(s: str) -> str:
    """把一个任意字符串变成安全的 YAML 双引号标量。

    json.dumps 出来的串（含两端双引号、内部引号和反斜杠都转好）本身就是合法
    YAML 双引号标量，所以直接拿来用，不用自己写转义。
    """
    return json.dumps(str(s), ensure_ascii=False)


def linux_seed(vm: dict) -> Path:
    """生成 cloud-init 的 NoCloud 配置盘。

    为什么用云镜像 + NoCloud，而不是 netinst 加 preseed：云镜像本身就是
    装好的系统，qcow2 叠一层 overlay 就能跑，没有安装程序要交互；账号、
    密码、SSH 公钥全从配置盘喂进去。用 netinst 的话要多伺候一套 preseed
    和安装期网络镜像，出问题的地方多一个数量级。

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
    ud = f"""#cloud-config
# 由 agentvm 生成，改这个文件不如改 vm.json 后重跑 install。
hostname: {_yaml_scalar(vm['name'])}
fqdn: {_yaml_scalar(vm['name'])}.local
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

# 云镜像默认只装最小集。把常用的补上，省得用户登进去再 apt 一遍。
packages:
  - openssh-server
  - curl
  - ca-certificates
  - sudo

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
  - [ sh, -c, "systemctl enable --now getty@tty0.service || true" ]
  # 控制台空闲默认会黑屏（kernel 的 consoleblank，默认 10 分钟）。VM 常常
  # 一放就是几小时，用户回来看见一个全黑窗口，会以为虚拟机死了。往每个 VT
  # 各发一次 setterm 关掉它。注意 setterm 是对「当前终端」生效的，所以要把
  # stdout 重定向到具体的 /dev/ttyN 才能作用到那个 VT。
  - [ sh, -c, "for n in 0 1 2 3 4 5 6; do setterm -blank 0 -powersave off -powerdown 0 >/dev/tty$n 2>/dev/null || true; done" ]
  - [ sh, -c, "cloud-init status --wait >/dev/null 2>&1 || true" ]
"""
    (src / "user-data").write_text(ud)
    (src / "meta-data").write_text(
        f"instance-id: {_yaml_scalar(vm['name'])}-001\n"
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
    linux_seed(vm)

    Emit.stage("boot", "首次启动")
    start_linux(vm, wait_ssh=True)

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


def cmd_install_local(vm: dict, source: Path) -> dict:
    """从用户自己挑的本地 ISO 装一个虚拟机。

    和另两条路最大的区别：**这条不可能全自动**。Windows 有应答文件可喂，
    Linux 云镜像压根没有安装程序，而 fnOS 这类定制安装器是图形交互的——要人选
    盘、确认格式化、看进度。所以这里只做能做的：建盘、建 EFI 变量、把安装器
    跑起来，然后明确告诉用户「剩下的你在画面里点完」。

    不假装能自动装完。原来那种「等 SSH 就绪」的循环在这条路上只会空等四十分钟
    然后报超时，而用户早就装完了——撒谎比报错更难排查。

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

    Emit.log("")
    Emit.log("安装器已经跑起来了，画面在 app 的「控制台」里。在那里完成安装：",
             "info")
    Emit.log("  1. 选安装位置（我们建的盘会以 vda 出现）", "info")
    Emit.log("  2. 确认分区大小，下一步，确认格式化", "info")
    Emit.log("  3. 等它复制完文件、设好网络，然后重启", "info")
    Emit.log("重启后它会再次进入安装器——这是正常的，直接关机即可。", "info")
    Emit.log(f"然后用 start --name {name} 就从硬盘进系统。", "info")
    Emit.log("命令行要看画面：vmkit.py screenshot --name " + name, "info")
    Emit.log("想要一个原生 macOS 窗口：vmkit.py window --name " + name, "info")

    # 不置 ready：装没装完只有用户知道。等他 start 过一次才算数。
    save_vm(vm)
    return vm


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
    args += ["-vnc", f"127.0.0.1:{vm['vnc_display']}"]
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
        "-netdev", (f"user,id=net0,"
                    f"hostfwd=tcp:127.0.0.1:{vm['ssh_port']}-:22,"
                    f"hostfwd=tcp:127.0.0.1:{vm.get('web_port', 0)}"
                    f"-:{WEB_GUEST_PORT}"),
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
    args += ["-vnc", f"127.0.0.1:{vm['vnc_display']}"]
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


def start_linux(vm: dict, wait_ssh: bool = False) -> None:
    if qemu_pid(vm):
        ok(f"VM 已在运行（pid {qemu_pid(vm)}）")
        return
    d = vm_dir(vm["name"])
    for f in ("disk.qcow2", "seed.iso"):
        if not (d / f).is_file():
            die(f"VM 不完整，缺 {f}。重新 install。")
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
        return
    Emit.stage("ssh", "等 SSH 就绪")
    deadline = time.time() + 300
    t0 = time.time()
    while time.time() < deadline:
        if linux_ssh_ready(vm):
            ok(f"SSH 已就绪，用时 {int(time.time() - t0)} 秒")
            return
        if not qemu_pid(vm):
            die(f"QEMU 中途退出了，看 {d / 'qemu.log'}")
        Emit.progress("ssh", (time.time() - t0) / 300,
                      int(time.time() - t0), 300, 0)
        time.sleep(3)
    warn("等了 5 分钟 SSH 仍未就绪。VM 还在跑，用 open 看窗口，"
         f"或 screenshot 抓画面，或看 {d / 'qemu.log'}")


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
        while time.time() < deadline:
            if w.ssh_ready(cfg):
                ok(f"SSH 已就绪，用时 {int(time.time() - t0)} 秒")
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
    d = vm_dir(name)

    # 先看有没有一个安装已经在跑。原来没有任何检查：连点两次安装（或 app 的
    # install.pid 轮询有个窗口期）就会起第二个安装进程，两边同时 qemu-img
    # create 同一个 disk.qcow2、同时 save_vm 写同一个 vm.json、同时起两个
    # QEMU 抢同一组端口——磁盘和配置都损坏，而两份日志交错在一起无法复盘。
    # install.pid 写了（下面），但原来只有 app 读，vmkit 自己从不看。
    pidf = d / "install.pid"
    if pidf.is_file():
        try:
            old = int(pidf.read_text().strip())
        except ValueError:
            old = 0
        if old > 0 and _pid_alive(old):
            die(f"这个虚拟机的安装已经在跑（pid {old}）。等它结束，或先停掉它：\n"
                f"  kill {old}\n"
                f"日志：{d / 'install.log'}")

    d.mkdir(parents=True, exist_ok=True)
    logf = d / "install.log"

    argv = [sys.executable, str(Path(__file__).resolve())]
    # 把原始参数原样传过去，只去掉 --detach
    for a in sys.argv[1:]:
        if a == "--detach":
            continue
        argv.append(a)

    # 追加而不是覆盖：重跑一次不会把上次的日志冲掉，出问题时能回看
    with logf.open("a") as f:
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 开始安装 {name} =====\n")
        f.flush()
        proc = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(HERE))
    # pid 落盘。GUI 靠它判断「这个安装还在跑吗、要不要接着显示进度」。
    # 不用 pgrep 匹配命令行：命令行里参数顺序不固定，写出来的模式很容易对不上，
    # 而且会误匹配到别的进程。
    (d / "install.pid").write_text(str(proc.pid))
    Emit.done({"name": name, "log": str(logf), "pid": proc.pid,
               "detached": True})
    return 0


def cmd_install(args) -> int:
    if platform.machine() != "arm64" or platform.system() != "Darwin":
        die("只在 Apple Silicon 的 macOS 上有效。")
    name = args.name
    if not name.replace("-", "").replace("_", "").isalnum():
        die(f"名字只能含字母数字和 - _：{name}")
    if vm_file(name).exists() and not args.force:
        die(f"{name} 已存在。换个名字，或加 --force 重装。")

    # 本地安装包这条路不走 catalog：没有下载地址、没有校验和、没有推荐配置，
    # 全靠用户给的一个路径。所以先把它分叉出来，别让后面的 catalog 查找绊倒。
    if args.source or args.source_url:
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
        taken = {v["ssh_port"] for v in all_vms() if v["name"] != name}
        taken_rdp = {v.get("rdp_port", 0) for v in all_vms() if v["name"] != name}
        taken_vnc = {v.get("vnc_display", 0) for v in all_vms() if v["name"] != name}
        taken_web = {v.get("web_port", 0) for v in all_vms() if v["name"] != name}
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
            "web_port": alloc_port(PORT_BASE_WEB, taken_web),
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
        vm = cmd_install_local(vm, src)
        Emit.done(vm_record(vm))
        return 0

    cat = load_catalog()
    distro = find_distro(cat, args.distro)
    if not name.replace("-", "").replace("_", "").isalnum():
        die(f"名字只能含字母数字和 - _：{name}")
    if vm_file(name).exists():
        if not args.force:
            die(f"{name} 已存在。换个名字，或加 --force 重装。")
        # --force 必须真的清掉旧盘。原来只重写 cloud-init 种子，旧的文件系统
        # 和旧数据原样留着：用户以为得到一台干净机器，实际上一轮的脏数据全在，
        # 而 --help 写的是「覆盖重装」。这种事比不给 --force 更糟——至少那个
        # 会明确报错。
        if qemu_pid({"name": name}):
            die(f"{name} 正在运行。先 stop 再 --force 重装，不然会删掉正在用的盘。")
        warn(f"--force：清掉 {vm_dir(name)} 重新装（里面的数据一起没）")
        _rmtree(vm_dir(name))

    # 一律 .get()：一个写了一半的 vm.json 缺 ssh_port 时，这里 KeyError 崩掉，
    # 而用户只是想装一个跟那个坏字段无关的新虚拟机。
    taken = {v.get("ssh_port", 0) for v in all_vms() if v.get("name") != name}
    taken_rdp = {v.get("rdp_port", 0) for v in all_vms() if v["name"] != name}
    taken_vnc = {v.get("vnc_display", 0) for v in all_vms() if v["name"] != name}
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

    if distro["kind"] == "windows":
        vm = cmd_install_windows(vm, distro)
    else:
        vm = cmd_install_linux(vm, distro)
    Emit.done(vm_record(vm))
    return 0


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


def cmd_start(args) -> int:
    vm = load_vm(args.name)
    headless = resolve_headless(vm, args)
    if vm["kind"] == "windows":
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
    elif vm["kind"] == "local":
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
    if not qemu_pid(vm):
        die("VM 没在运行，先 start")
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
    pid = qemu_pid(vm)
    if not pid:
        ok("VM 没在运行")
        return 0
    if vm["kind"] == "windows":
        w = winarm()
        # 原来只设 VM_DIR。winarm 的 Cfg() 于是回落到默认的 ~/.winarm/ci_key，
        # 和安装时用的 vm["ssh_key"] 不是同一把钥匙，关机流程第一步的 SSH 认证
        # 必然失败。见 winarm_env 的注释。
        os.environ.update(winarm_env(vm, True))
        try:
            w.cmd_stop(w.Cfg())
        except w._Die as e:
            die(str(e))
        return 0
    # Linux：先试 SSH 关机，再 ACPI，最后才强杀。qcow2 被强杀有丢数据风险。
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
    ok("已强杀")
    return 0


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
    disk = d / ("data.img" if vm.get("kind") in ("windows", "local")
                else "disk.qcow2")
    rec = {
        "name": name, "distro": vm.get("distro", ""), "kind": vm.get("kind", ""),
        "running": bool(pid), "pid": pid, "ready": vm.get("ready", False),
        "cpus": vm.get("cpus", 2), "ram": vm.get("ram", "2G"),
        "disk": vm.get("disk", "32G"),
        "user": vm.get("user", "agent"), "ssh_port": vm.get("ssh_port", 0),
        "rdp_port": vm.get("rdp_port"),
        "vnc_port": 5900 + vm.get("vnc_display", 0),
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
    if vm["kind"] == "linux":
        f = vm_dir(vm["name"]) / "disk.qcow2"
        return f if f.is_file() else None
    for d in vm.get("disks", []):
        if d.get("format") == "qcow2" and Path(d["file"]).is_file():
            return Path(d["file"])
    return None


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
    name = vm["name"]

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
    if not new.replace("-", "").replace("_", "").isalnum():
        die(f"名字只能含字母数字和 - _：{new}")
    if vm_file(new).exists() and not args.force:
        die(f"{new} 已存在。换个名字，或加 --force 覆盖。")
    if qemu_pid(src):
        die("源虚拟机正在运行，先 stop 再克隆——运行中复制出来的状态不一致。")

    d_new = vm_dir(new)
    d_new.mkdir(parents=True, exist_ok=True)
    Emit.stage("clone", f"克隆 {src['name']} → {new}")

    # 系统盘
    if src["kind"] == "linux":
        base = vm_dir(src["name"]) / "disk.qcow2"
        if not base.is_file():
            die(f"源的盘不在：{base}")
        # 再叠一层 overlay，底册还是同一个云镜像。多个 VM 共用一份底册，
        # 各自写入时才真正占空间。
        run([find_tool("qemu-img"), "create", "-f", "qcow2",
             "-b", str(base), "-F", "qcow2", str(d_new / "disk.qcow2")])
        ok("已建 overlay 磁盘（与源共用底册）")
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
    if src["kind"] == "linux":
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
    taken_web = {v.get("web_port", 0) for v in all_vms()}

    vm = dict(src)
    vm["name"] = new
    vm["ssh_port"] = alloc_port(PORT_BASE_SSH, taken_ssh)
    vm["rdp_port"] = (alloc_port(PORT_BASE_RDP, taken_rdp)
                      if src.get("rdp_port") else None)
    vm["vnc_display"] = alloc_vnc_display(taken_vnc)
    vm["web_port"] = (alloc_port(PORT_BASE_WEB, taken_web)
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
        # 开机自启的 plist 必须一起卸掉。原来只 _rmtree(d)：plist 还留在
        # ~/Library/LaunchAgents/，而它指向的 wrapper 就在被删掉的 VM 目录里。
        # 之后每次登录 launchd 执行一个不存在的脚本 → 非零退出 → KeepAlive 加
        # ThrottleInterval 15 → 每 15 秒重试一次，永不停止，还会往已删除目录写
        # 日志失败。用户看到的是「删了的虚拟机还在每隔一会儿折腾一下」。
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
    """把虚拟机窗口带到前台。

    launchd 或 GUI 拉起的 Cocoa 窗口不会自动抢焦点，用户经常以为窗口没开。
    用 AppleScript activate 一次最省事，比枚举窗口可靠。
    """
    vm = load_vm(args.name)
    if not qemu_pid(vm):
        die("VM 没在运行，先 start")
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
    if not mon.exists():
        die("VM 没在运行（monitor 套接字不在），先 start")
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
    name = vm["name"]
    changed: list[str] = []

    if args.cpu is not None:
        top = host_cpus()
        if not 1 <= args.cpu <= top:
            die(f"CPU 核心要给 1 到 {top} 之间，你给了 {args.cpu}")
        if args.cpu != vm["cpus"]:
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
        if want != parse_size(vm["ram"]):
            vm["ram"] = args.mem.upper()
            changed.append(f"内存 {vm['ram']}")

    if args.disk:
        want = parse_size(args.disk)
        cur = parse_size(vm["disk"])
        # 下限 1G。原来没有：`--disk 64` 被解析成 64 字节，qemu-img 老实建成一个
        # 512 字节的盘，QEMU 起来就是一个空盘，用户完全看不出所以然。
        if want < 1 << 30:
            die(f"磁盘至少 1G，你给了 {args.disk}")
        if want < cur:
            die(f"磁盘只能往大改：现在是 {vm['disk']}，你要给 {args.disk}。"
                f"改小会把超出去的数据截掉，文件系统不会跟着缩。")
        if want > cur:
            if qemu_pid(vm):
                die("虚拟机正在运行，镜像被占着写锁，改不了大小。先 stop。")
            d = vm_dir(name)
            if vm["kind"] == "linux":
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
        if vm["kind"] == "linux":
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

    if args.ssh_port is not None and args.ssh_port != vm["ssh_port"]:
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


def cmd_autostart(args) -> int:
    vm = load_vm(args.name)
    d = vm_dir(vm["name"])
    label = f"com.agentvm.{vm['name']}"
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

    if vm["kind"] == "windows":
        inner = (f'exec "{py}" "{WINARM}" start --foreground < /dev/null')
    else:
        inner = f'exec "{py}" "{script}" start --name {vm["name"]} --headless < /dev/null'

    hb = find_tool("qemu-system-aarch64").rsplit("/bin/", 1)[0]
    wrapper.write_text(f"""#!/bin/sh
# 由 vmkit 生成。launchd 不继承交互 shell 的 PATH，而 QEMU 在 {hb}/bin。
export PATH="{hb}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOMEBREW_PREFIX="{hb}"
export VM_DIR="{d}"
# 开机自启走无窗口模式，否则每次登录 Mac 都弹一个虚拟机窗口。要看窗口就
# 用 vmkit start（不带 --headless）或 open。
export HEADLESS="1"
{inner}
""")
    wrapper.chmod(0o755)

    if plist.exists():
        run(["launchctl", "unload", str(plist)], check=False)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array><string>{wrapper}</string></array>
    <key>RunAtLoad</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>{d / 'launchd.log'}</string>
    <key>StandardErrorPath</key>
    <string>{d / 'launchd.log'}</string>
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
    p.add_argument("--password")
    p.add_argument("--ssh-key")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--wait", type=int, default=40, help="等就绪的分钟数")
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
        return cmd_install(args)
    if args.cmd == "status":
        # status 允许不带 --name（看全部），所以要在下面的检查之前处理
        return cmd_status(args)
    if not args.name:
        die(f"{args.cmd} 需要 --name")
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
    if args.cmd == "screenshot":
        return cmd_screenshot(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
