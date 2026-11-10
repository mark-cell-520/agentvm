#!/usr/bin/env python3
"""本地安装包的一键自动安装。

catalog 里那几条路本来就是一键的：Linux 云镜像压根没有安装程序，Windows 有
应答文件可喂，macOS 走 IPSW 全自动。唯独「用户自己挑的本地 ISO」这条路原来
只能手动——fnOS 这类定制安装器是图形向导，要人选盘、确认格式化、看进度、
设网络，然后它还会说一句「请拔出安装引导盘」。

2026-11-11 实测证明这条路也能自动走完：把 fnOS 1.2.07 的安装器整个跑通，
从「选择安装位置」到 Web 界面返回 HTTP 200，全程没有一个手点的按钮。做法
全在这儿：

1. **自己当一个 RFB 客户端连上 QEMU 的 VNC**，抓 framebuffer、发键鼠。
   不借道 app 的内嵌控制台——那个是给人看的，而且它只在 app 前台时才刷新。
   这里要的是能在后台无人值守地判断「屏幕上现在有什么」。
2. **主要动作是按回车**。回车 = 「激活默认按钮」，这是跨 GUI 工具包最通用的
   「往前推进」动作，实测 fnOS 安装器每一屏都认（选择安装位置、确认格式化、
   完成、网络设置、保存成功确认，全靠回车走过去）。它比「找按钮然后点」稳健
   得多：fnOS 第一屏的「下一步」是**白底描边**按钮，颜色判据根本看不见它，
    # 而它又正好在屏幕右下角、不在任何能猜的位置上。
3. **点蓝按钮是备选**。同一个画面按回车没反应时，才退去找主按钮点它。fnOS 的
   主按钮（下一步/确定/保存）是纯蓝实底、次按钮是白底，所以「画面里那块形状
   像按钮的纯蓝实心矩形」就是个不错的瞄准点。判据要过三层：取候选蓝（不能只
   取最多的那一种——第一屏里被选中的表格行也是纯蓝，而且比按钮大）、连通分量
   （同一种蓝会出现在表格行、按钮、蓝色文字好几个不相干的地方）、形状过滤
   （按钮宽高比 2~4，表格行是 23）。
4. **画面稳一下再动作**。安装器切换画面时要重绘，半帧上按下去会打到空气。
5. **该弹光盘时弹光盘**。fnOS 装到 100% 之后明确要求「拔出安装引导盘」，
   否则重启又会退回安装器（固件沿用 NVRAM 里上次的启动项，不按 bootindex
   选）。弹盘的时机用一个很稳的性质判断：**QEMU 的 `eject` 只有在客人机已
   经放开光盘时才成功**。安装还进行着的时候客人机一直开着那个块设备，
   eject 直接报 "Device 'cd' is locked"。所以不需要猜「装完了没有」——
   每隔一会儿试着弹一次，弹得动的那一刻就正是可以弹的时候。
6. **兜底**：万一光盘没弹掉、重启后又回到安装器，靠「同一个画面第二次出现
   且盘上已经有数据」认出来，立刻停手交还用户，绝不重复操作第一次那个按钮
   ——那一下会把整个安装重新触发一遍。

退出条件，按优先级：
   done    服务真的起来了（web_port 或 ssh_port 有真响应）
   reboot  重启后又回到安装器 → 调用方该去掉光盘重启一次
   manual  拿不准，VM 留着运行，用户自己接着点
   timeout 预算用完了

对外入口是 `drive()`。vmkit.py 的 cmd_install_local 调它。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

# 主按钮的颜色判据。fnOS 用的是 (51,116,219)；别的图形安装器主按钮也多是
# 饱和蓝。范围刻意放宽，但要求「明显偏蓝」——白底次按钮的红绿蓝都在 200 左右，
# 过不了 b - r > 40 这道。
BLUE_MIN_B = 100
BLUE_R_GAP = 40
BLUE_G_MIN = 50
BLUE_B_G_GAP = 25
BLUE_R_MAX = 150
# 一块蓝色占掉画面多少比例就算「这不是按钮」。GRUB 的菜单是一整块蓝底，
# 不设这道坎会把它当成一个巨大的下一步按钮点下去。
BLUE_MAX_AREA = 0.30
BLUE_MIN_AREA = 0.0005
# 候选蓝最多看几种。第一屏里被选中的表格行是纯蓝实底且比按钮大，只取计数最多
# 的那一种会漏掉按钮；取前几种逐个算，靠后面的形状过滤把它们分开。
BLUE_TOP_COLORS = 4
# 按钮的宽高比范围。fnOS 的按钮实测在 2.7~3.3 之间；表格行是 23，蓝色文字是
# 碎斑，都被这道坎挡在外面。
BTN_ASPECT_MIN = 1.2
BTN_ASPECT_MAX = 8.0

# 指纹网格：在画面上按这个密度取单点采样，判断「还是刚才那一帧吗」。
# 取单点而不是每格求平均：480k 个像素在纯 Python 里逐个数要一秒上下，而驱动器
# 每轮都要算一次，平白多出两秒。30000 个采样点足够分开安装器的各个画面。
FP_STEP = 4                 # 横竖都隔这么多个像素取一个点
FP_TOL = 40                 # 单个采样点差这么多算「变了」
FP_DIFF_FRAC = 0.01         # 超过 1% 的采样点变了才算换了一帧
# 主按钮挪动超过这么多像素也算换了一帧。
# 为什么把它并进判据：实测两个不同的安装器画面，像素差异只有 1.85%（它们大多
# 数是同一块浅灰底加一个居中的对话框），而同一画面稳定不动时差异是 0.00%。
# 只按像素比，阈值卡在 1% 上，两边余量都只有一倍——真实使用里一个闪烁的文本
# 光标就能把「同一帧」推过线。按钮位置是个离散的高信号特征：完成屏的「下一步」
# 在正中间，网络设置屏的「保存」在右下角，差 148 像素，一眼就能分开。
FP_BTN_TOL = 8


def fingerprint(frame: bytes, w: int, h: int) -> bytes:
    """画面的粗指纹：按 FP_STEP 采样出来的 RGB 串。"""
    out = bytearray()
    for y in range(0, h, FP_STEP):
        base = y * w * 4
        for x in range(0, w, FP_STEP):
            o = base + x * 4
            out += frame[o:o + 3]
    return bytes(out)


def diff_frac(a: bytes, b: bytes) -> float:
    """两幅指纹里「变了」的采样点占比。长度不一样直接算全变。"""
    if len(a) != len(b):
        return 1.0
    diff = 0
    n = 0
    for i in range(0, len(a), 3):
        if (abs(a[i] - b[i]) > FP_TOL or abs(a[i + 1] - b[i + 1]) > FP_TOL
                or abs(a[i + 2] - b[i + 2]) > FP_TOL):
            diff += 1
        n += 1
    return diff / n if n else 1.0


def same_screen(a, b) -> bool:
    """两个画面签名是不是同一屏。

    签名是 `(指纹, 主按钮中心)`。按钮一个有一个没有、或者挪动超过
    FP_BTN_TOL，都算不同屏——见 FP_BTN_TOL 那段注释。
    """
    if (a[1] is None) != (b[1] is None):
        return False
    if a[1] is not None and b[1] is not None:
        if abs(a[1][0] - b[1][0]) > FP_BTN_TOL or abs(a[1][1] - b[1][1]) > FP_BTN_TOL:
            return False
    return diff_frac(a[0], b[0]) <= FP_DIFF_FRAC

# 节奏。做一个动作之后等 2 秒再看：安装器切画面要重绘，太早点会打到上一帧。
SETTLE_SECONDS = 1.0        # 同一帧至少稳这么久才动作
ACT_PAUSE = 2.0             # 做完一个动作等这么久再看
# 盘静止多久才算「安装器忙完了」。复制文件期间 st_blocks 几乎每秒都在涨，
# 这个门槛要把那段时间整个盖住。
DISK_QUIET = 2.0
# 最多做多少个动作。防 runaway：真出岔子时不能让驱动器在那儿按一晚上回车。
ACT_LIMIT = 200
# 每个动作之前把当前画面存成 <vm 目录>/auto-<序号>.ppm，只保留最近这么多张。
# 自动安装退成手动时，用户（和我们）第一句话就是「它停在什么屏幕上」——不留
# 现场就只能靠猜。2026-11-11 实测：不留图时盯着日志数回车次数，怎么数都对不上
# 实际走到了哪一屏，白白绕了一圈。
KEEP_FRAMES = 24
# 回车的 keysym。这是驱动器的主要动作，见 drive() 里的说明。
KEY_RETURN = 0xFF0D
# 盘上写过这么多字节，才算「安装已经开始了」。没到这个数时不弹光盘、也不认
# 「重启后又回到安装器」——第一屏时盘还是空的，那时候看见重复画面只是点击没生效。
EJECT_MIN_DISK = 512 << 20


class AutoError(RuntimeError):
    """自动安装自己出了岔子。调用方应当退回手动，而不是假装装完了。"""


# --------------------------------------------------------------- RFB 客户端 --

class RFB:
    """刚够用的 RFB 3.8 客户端：拿 Raw 帧、发键鼠。

    刻意不实现 Tight/Zlib/CopyRect：安装器的画面是纯色块加文字，Raw 一帧
    800×600×4 = 1.9 MB，本地回环一秒能传好几帧，压缩省下的那点带宽远不够
    抵上多写几百行解压代码的风险。编码协商时只报 Raw，服务端不支持就直接
    报错，比静默降级好。

    像素格式固定成 32bpp 真彩、小端、红移 16/绿移 8/蓝移 0——也就是内存里
    一个像素是 B,G,R,pad 四个字节。QEMU 的默认格式正是这个，显式设一遍是
    为了不依赖它的默认值。
    """

    def __init__(self, port: int, timeout: float = 10.0):
        self.port = port
        self.timeout = timeout
        self.sk = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sk.settimeout(timeout)
        self._handshake()

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sk.recv(n - len(buf))
            if not chunk:
                raise AutoError(f"RFB 连接被对端关掉（已收 {len(buf)}/{n} 字节）")
            buf += chunk
        return buf

    def _handshake(self) -> None:
        ver = self._recv(12)
        if not ver.startswith(b"RFB "):
            raise AutoError(f"端口 {self.port} 不是 RFB 服务端：{ver!r}")
        self.sk.sendall(b"RFB 003.008\n")
        n = self._recv(1)[0]
        if n == 0:
            raise AutoError("RFB 服务端给了失败原因，连不上")
        types = self._recv(n)
        if 1 not in types:
            raise AutoError(f"RFB 服务端不提供 None 认证：{list(types)}")
        self.sk.sendall(b"\x01")
        result = int.from_bytes(self._recv(4), "big")
        if result != 0:
            raise AutoError(f"RFB 认证被拒：{result}")
        self.sk.sendall(b"\x01")            # ClientInit：shared=1
        # ServerInit 是 24 字节头：宽 2 + 高 2 + 像素格式 16 + 名字长度 4，
        # 然后才是名字本体。按 20 字节读会把「名字长度」那 4 字节漏在流里，
        # 之后每条消息都错位——症状是矩形头的 encoding 字段变成一坨 ASCII。
        hdr = self._recv(24)
        self.w = int.from_bytes(hdr[0:2], "big")
        self.h = int.from_bytes(hdr[2:4], "big")
        nlen = int.from_bytes(hdr[20:24], "big")
        self.name = self._recv(nlen).decode("latin1", "replace") if nlen else ""
        # SetEncodings：类型 2 + 补位 1 + 数量 2 + 每个编码 4。只报 Raw(0)。
        # 多写一个字节，服务端会把它当成下一条消息的开头，然后永远等剩下的
        # 字节——表现是接下来每次取帧都超时。
        self.sk.sendall(b"\x02\x00\x00\x01\x00\x00\x00\x00")
        # SetPixelFormat：类型 0 + 补位 3 + 16 字节像素格式。
        self.sk.sendall(b"\x00\x00\x00\x00"
                        b"\x20\x18\x00\x01"          # 32bpp / 24 深 / 小端 / 真彩
                        b"\x00\xff\x00\xff\x00\xff"  # 红绿蓝上限全是 255
                        b"\x10\x08\x00"              # 红移 16、绿移 8、蓝移 0
                        b"\x00\x00\x00")

    def request_full(self) -> None:
        # FramebufferUpdateRequest 一共 10 字节：类型 1 + incremental 1 +
        # x/y/w/h 各 2。多写一个字节的后果见 SetEncodings 那段注释。
        self.sk.sendall(b"\x03\x00\x00\x00\x00\x00"
                        + self.w.to_bytes(2, "big") + self.h.to_bytes(2, "big"))

    def frame(self, timeout: float = 25.0) -> bytes:
        """读一整帧 Raw，返回 w*h*4 字节。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise AutoError("等 RFB 帧超时")
            self.sk.settimeout(left)
            t = self._recv(1)[0]
            if t == 0:                                  # FramebufferUpdate
                self._recv(1)
                nrect = int.from_bytes(self._recv(2), "big")
                out = bytearray(self.w * self.h * 4)
                for _ in range(nrect):
                    rh = self._recv(12)
                    x = int.from_bytes(rh[0:2], "big")
                    y = int.from_bytes(rh[2:4], "big")
                    rw = int.from_bytes(rh[4:6], "big")
                    rhh = int.from_bytes(rh[6:8], "big")
                    enc = int.from_bytes(rh[8:12], "big")
                    if enc != 0:
                        raise AutoError(f"RFB 服务端用了非 Raw 编码 {enc}，"
                                        f"这个驱动器只认 Raw")
                    if rw == 0 or rhh == 0:
                        continue
                    data = self._recv(rw * rhh * 4)
                    if len(data) != rw * rhh * 4:
                        raise AutoError("RFB 矩形数据长度不对")
                    stride = rw * 4
                    for r in range(rhh):
                        off = ((y + r) * self.w + x) * 4
                        out[off:off + stride] = data[r * stride:(r + 1) * stride]
                if len(out) != self.w * self.h * 4:
                    raise AutoError("RFB 帧大小不对")
                return bytes(out)
            if t == 1:                                  # SetColourMapEntries
                self._recv(3)
                n = int.from_bytes(self._recv(2), "big")
                self._recv(n * 6)
            elif t == 2:                                # Bell
                pass
            elif t == 3:                                # ServerCutText
                self._recv(3)
                n = int.from_bytes(self._recv(4), "big")
                self._recv(n)
            else:
                raise AutoError(f"RFB 服务端发来不认识的消息类型 {t}")

    def key_tap(self, keysym: int, hold: float = 0.06) -> None:
        self.sk.sendall(b"\x04\x01\x00\x00" + keysym.to_bytes(4, "big"))
        time.sleep(hold)
        self.sk.sendall(b"\x04\x00\x00\x00" + keysym.to_bytes(4, "big"))

    def click(self, x: int, y: int, hold: float = 0.08) -> None:
        """移动到 (x,y) 再点一下。先送一次无键移动，和真实用户一样。"""
        self.sk.sendall(b"\x05\x00" + x.to_bytes(2, "big") + y.to_bytes(2, "big"))
        time.sleep(0.02)
        self.sk.sendall(b"\x05\x01" + x.to_bytes(2, "big") + y.to_bytes(2, "big"))
        time.sleep(hold)
        self.sk.sendall(b"\x05\x00" + x.to_bytes(2, "big") + y.to_bytes(2, "big"))

    def close(self) -> None:
        try:
            self.sk.close()
        except OSError:
            pass

    def __enter__(self) -> "RFB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------------ 画面分析 --

def primary_button(frame: bytes, w: int, h: int) -> tuple[int, int] | None:
    """找画面里的主按钮，返回中心坐标；没有就返回 None。

    **它只是个备选判据**，不是驱动器的主要动作——主要动作是按回车（见 drive）。
    实测 fnOS 第一屏的「下一步」是白底描边按钮，颜色判据根本看不见它；而回车
    在每一屏都管用。所以这个函数的价值在于：某些安装器上回车不推进、或者默认
    按钮不是「前进」时，还能有一个瞄准了具体按钮的点法可用。

    判据分三层，每层都是被真实画面逼出来的：

    1. **取候选蓝**。按「明显偏蓝」粗筛，然后**不只看出现最多的那一个颜色**——
       第一屏里被选中的表格行 `[vda]` 是纯蓝实底，比按钮大得多，只取主色会把
       它当成按钮（实测：点到了表格行上，屏幕纹丝不动）。所以取计数最多的前
       几种蓝，每种都单独算一遍。
    2. **连通分量**。同一种蓝会出现在好几个不相干的地方（表格行、按钮、蓝色
       文字），必须拆开一个个看，不能放在一个包围盒里。
    3. **形状过滤**。按钮是矮胖矩形（宽高比 2~4），表格行是细长条（实测 417×18，
       宽高比 23），蓝色文字是碎斑。用宽高比和面积把不像按钮的剔掉。
    """
    counts: dict[tuple[int, int, int], int] = {}
    # 隔行隔列采样做候选，够快也够准——按钮是实心块，采样丢不了它
    for y in range(0, h, 2):
        base = y * w * 4
        row = frame[base:base + w * 4]
        for x in range(0, w, 2):
            o = x * 4
            b, g, r = row[o], row[o + 1], row[o + 2]
            if (b >= BLUE_MIN_B and b - r >= BLUE_R_GAP and g >= BLUE_G_MIN
                    and b - g >= BLUE_B_G_GAP and r <= BLUE_R_MAX):
                key = (r, g, b)
                counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    # 采样密度是 1/4，所以阈值按像素数算要乘回去
    min_px = BLUE_MIN_AREA * w * h
    max_px = BLUE_MAX_AREA * w * h
    best: tuple[int, int, int] | None = None       # (面积, cx, cy)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:BLUE_TOP_COLORS]
    for (fr, fg, fb), cnt in ranked:
        if cnt * 4 < min_px:
            continue
        # 一个像素是 B,G,R,pad 四个字节，所以目标色在流里的位置一定是 4 的倍数。
        # 别的位置也可能凑出同样的三字节，按 % 4 滤掉即可。
        pat = bytes((fb, fg, fr))
        pts: set[int] = set()
        pos = frame.find(pat)
        while pos != -1:
            if pos % 4 == 0:
                pts.add(pos // 4)
            pos = frame.find(pat, pos + 1)
        if not pts:
            continue
        # 连通分量（4 邻域）。用集合判成员，O(1)。
        seen: set[int] = set()
        for start in pts:
            if start in seen:
                continue
            stack = [start]
            seen.add(start)
            comp: list[int] = []
            while stack:
                q = stack.pop()
                comp.append(q)
                for d in (-1, 1, -w, w):
                    r = q + d
                    if r in pts and r not in seen:
                        seen.add(r)
                        stack.append(r)
            if len(comp) < min_px or len(comp) > max_px:
                continue
            xs = [c % w for c in comp]
            ys = [c // w for c in comp]
            bw = max(xs) - min(xs) + 1
            bh = max(ys) - min(ys) + 1
            if not (BTN_ASPECT_MIN <= bw / bh <= BTN_ASPECT_MAX):
                continue
            cx = (min(xs) + max(xs)) // 2
            cy = (min(ys) + max(ys)) // 2
            if best is None or len(comp) > best[0]:
                best = (len(comp), cx, cy)
    if best is None:
        return None
    return best[1], best[2]


# ------------------------------------------------------------ QEMU 监控命令 --

def monitor_send(mon: Path, cmd: str) -> bool:
    """往 QEMU 的 HMP 监控套接字发一条命令，返回它有没有报错。

    回显里带 readline 的光标移动转义序列，所以不能按行解析，只能看输出里
    有没有 "Error"/"error" 字样。这对 eject / system_reset 这类只有成败两种
    结果的命令够用了。
    """
    if not mon.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(3.0)
            sk.connect(str(mon))
            sk.sendall(cmd.encode() + b"\n")
            time.sleep(0.4)
            buf = b""
            try:
                while True:
                    chunk = sk.recv(65536)
                    if not chunk or b"(qemu)" in buf:
                        break
                    buf += chunk
            except socket.timeout:
                pass
    except OSError:
        return False
    text = buf.decode("utf-8", "replace")
    return "Error" not in text and "error" not in text


def eject_cd(mon: Path) -> bool:
    """让 QEMU 把安装光盘弹出来。弹不动就返回 False，不报错。

    为什么允许失败而且是常态：安装还进行着的时候，客人机一直开着那个块设备，
    HMP 的 `eject` 会回 "Device 'cd' is locked and force was not specified"。
    这里的用法是每隔一会儿试着弹一次，弹得动的那一刻就正是可以弹的时候。
    """
    return monitor_send(mon, "eject cd")


def eject_cd_force(mon: Path) -> bool:
    """强行把光盘取出来：先 `eject -f` 解开客人机的占用，再 `eject` 取出介质。

    **只在安装器已经空闲、马上要重启的时候用。** -f 会把客人机正开着的块设备
    强行推开，它要是还在读光盘就会读到错误——安装中途用这个会把安装弄坏。
    而重启循环被识破的那一刻，安装器停在自己第一屏上什么都不干，正是安全的
    时候。2026-11-11 实测：那时 `eject -f cd` + `eject cd` 之后
    `info block` 显示 `cd: [not inserted]`，system_reset 就进了硬盘上的系统。
    """
    if not monitor_send(mon, "eject -f cd"):
        return False
    return monitor_send(mon, "eject cd")


def monitor_ask(mon: Path, cmd: str, wait: float = 1.2) -> str:
    """往 HMP 发一条命令，把回显原文拿回来。"""
    if not mon.exists():
        return ""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(3.0)
            sk.connect(str(mon))
            sk.sendall(cmd.encode() + b"\n")
            time.sleep(wait)
            buf = b""
            try:
                while True:
                    chunk = sk.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if b"(qemu)" in buf:
                        break
            except socket.timeout:
                pass
    except OSError:
        return ""
    return buf.decode("utf-8", "replace")


def cd_is_empty(mon: Path) -> bool:
    """光盘是不是已经取出来了。"""
    return "cd: [not inserted]" in monitor_ask(mon, "info block")


# ------------------------------------------------------------- 服务就绪探测 --

def service_ready(vm: dict, timeout: float = 3.0) -> bool:
    """客人机的服务是不是真的起来了。

    不能只 TCP connect：QEMU 的 user 模式网络在 hostfwd 端口上来者不拒，
    客人机没监听时它也先把连接接住，连不上客人机时才关掉。所以必须真发一个
    请求、读到真响应：Web 端口要读到 "HTTP/"，SSH 端口要读到 "SSH-"。
    """
    web = vm.get("web_port")
    if web:
        try:
            with socket.create_connection(("127.0.0.1", int(web)),
                                          timeout=timeout) as sk:
                sk.settimeout(timeout)
                sk.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                got = sk.recv(64)
            return got.startswith(b"HTTP/")
        except OSError:
            return False
    port = vm.get("ssh_port")
    if port:
        try:
            with socket.create_connection(("127.0.0.1", int(port)),
                                          timeout=timeout) as sk:
                sk.settimeout(timeout)
                got = sk.recv(64)
            return got.startswith(b"SSH-")
        except OSError:
            return False
    return False


def is_dark(frame: bytes, w: int, h: int) -> bool:
    """这一帧是不是「暗屏」——平均亮度很低。

    用来认重启：客人机重启时固件、GRUB、内核消息都会把画面清成黑底，而 fnOS
    安装器的向导页和进度页都是浅灰底。有了这个信号，「重启后又回到安装器」
    才不会和「进度页卡住不动」「向导里退回上一屏」混为一谈——后两种实测都
    撞见过，只看「画面重复出现」会误判，而误判的后果是白白把一个装了一半的
    机器交还用户。
    """
    n = 0
    total = 0
    step = max(1, (w * h) // 4096) * 4      # 采样 4096 个点就够
    for o in range(0, w * h * 4, step):
        total += (frame[o] + frame[o + 1] + frame[o + 2]) // 3
        n += 1
    return n > 0 and total / n < 60


def save_frame(vdir: Path, n: int, frame: bytes, w: int, h: int) -> None:
    """把一帧存成 PPM，只留最近 KEEP_FRAMES 张。

    RFB 的内存序是 B,G,R,pad，PPM 要 R,G,B，每组 4 字节里的 0 和 2 要换过来。
    RFB 的第 0 行是画面顶部，PPM 也是，行序不用动。
    """
    ppm = vdir / f"auto-{n}.ppm"
    try:
        with open(ppm, "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (w, h))
            out = bytearray(w * h * 3)
            for i in range(w * h):
                o = i * 4
                b = frame[o:o + 4]
                out[i * 3:i * 3 + 3] = bytes((b[2], b[1], b[0]))
            f.write(out)
    except OSError:
        return
    # 向前剪：只留最近这么多张，别让一个 30 分钟的安装在磁盘上堆 600 张 1.4MB
    try:
        olds = sorted(vdir.glob("auto-*.ppm"),
                      key=lambda p: p.name)
        for p in olds[:-KEEP_FRAMES]:
            p.unlink()
    except OSError:
        pass


def disk_used(vdir: Path) -> int:
    """系统盘真正占用的字节数。

    用 st_blocks 而不是 st_size：本地 ISO 这条路建的是稀疏文件，st_size 恒等于
    用户选的大小（32G），拿它判断「装没装上」永远是「装了」。

    目录由调用方传进来，不自己 import vmkit：vmkit.py 是当脚本跑的，模块名是
    `__main__`，从来没有注册成 `vmkit`——`from vmkit import vm_dir` 会直接
    ModuleNotFoundError。实测踩过。
    """
    for name in ("data.img", "disk.qcow2"):
        try:
            st = (vdir / name).stat()
        except OSError:
            continue
        return st.st_blocks * 512
    return 0


# -------------------------------------------------------------------- 驱动器 --

def drive(vm: dict, vdir: Path, *, budget_minutes: int = 45,
         emit=None) -> str:
    """把本地安装器的图形向导自己点完。

    返回值见模块 docstring。**任何异常都不往外抛**：自动安装失败就该退回手动，
    让一个 Python 栈打到安装日志里对用户没有任何帮助。
    """
    def say(msg: str, level: str = "info") -> None:
        if emit:
            try:
                emit(msg, level)
            except Exception:
                pass

    mon = vdir / "monitor.sock"
    port = int(vm.get("vnc_display", 0) or 0) + 5900
    if port <= 5900:
        say("这台虚拟机没有配 VNC 显示编号，没法自动点安装器", "warn")
        return "manual"

    # 先连 RFB，连不上就重试：install 一起步 QEMU 就在了，但 VNC 要等固件把
    # ramfb 初始化出来才听得见。一上来只连一次，连不上就整个自动安装失败——
    # 而那时安装器其实还要十几秒才露面。
    rfb = None
    deadline = time.time() + 120
    while time.time() < deadline:
        if service_ready(vm):
            say("客人机的服务已经起来了，不用点安装器")
            return "done"
        try:
            rfb = RFB(port)
            break
        except (OSError, AutoError):
            time.sleep(2.0)
    if rfb is None:
        say(f"连不上这台虚拟机的 VNC（127.0.0.1:{port}），自动安装退成手动",
            "warn")
        return "manual"

    say(f"已连上安装画面 {rfb.w}×{rfb.h}，开始自动安装")
    seen: list = []                 # 见过的画面签名，用来认「又回到这一屏了」
    last_sig = None                 # 上一帧（用来判断画面稳没稳）
    prev_sig = None                 # 上一帧（只用来判断画面在不在动）
    saw_copy = False                # 见过「盘上有数据、没按钮、画面在动」的帧
    saw_dark = False                # 上一个动作之后见过暗屏（重启的痕迹）
    # 基线取当前值，不是 0。取 0 的话第一轮一定判成「盘刚长大了」，白白跳过
    # 头两秒的画面——测试里就这么丢掉过一整屏暗屏，把重启循环的识别跳过去了。
    last_used = disk_used(vdir)      # 上一轮看到的盘占用
    grew_at = 0.0                    # 盘最后一次变大的时刻
    last_at = 0.0
    acts = 0                        # 一共做了多少个动作（回车或点击）
    ejected = False
    budget_end = time.time() + budget_minutes * 60

    try:
        with rfb:
            while time.time() < budget_end:
                if service_ready(vm):
                    say(f"客人机服务已就绪，安装完成（自动操作了 {acts} 步）")
                    return "done"
                try:
                    rfb.request_full()
                    frame = rfb.frame()
                except (AutoError, OSError) as e:
                    say(f"取画面失败：{e}", "warn")
                    return "manual"
                sig = (fingerprint(frame, rfb.w, rfb.h),
                       primary_button(frame, rfb.w, rfb.h))
                btn = sig[1]

                # 没有主按钮时也要继续往下走。fnOS 第一屏的「下一步」是白底描边
                # 按钮，颜色判据看不见它——而回车照样推进。**主要动作是回车，
                # 不是点按钮**，所以「没看见按钮」不构成不动作的理由。
                # 这里只借这个时机记一件事：复制阶段开始了没有。
                # 「复制阶段」信号：盘上已有数据、没按钮、画面在动。
                # 这是判断「什么时候可以拔光盘」的依据，见下面拔盘那段注释。
                used = disk_used(vdir)
                if (btn is None and used > EJECT_MIN_DISK
                        and prev_sig is not None and prev_sig[1] is None
                        and not same_screen(sig, prev_sig)):
                    saw_copy = True
                prev_sig = sig

                # 安装器正在往盘上写的时候，什么都别做。
                #
                # 为什么需要这条：复制文件那几分钟里进度条动得极慢——实测 8% 和
                # 18% 两帧之间只有 0.13% 的采样点变了，低于「画面稳了」的 1% 门槛。
                # 光看画面会以为它停在那儿，于是一遍遍按回车，最后撞上「同一个画面
                # 试了三次」主动撤退，而安装其实好着呢（2026-11-11 实测：五个回车
                # 之后就这样白撤了）。盘是不是在长是个物理信号，比画面可靠得多，
                # 而 stat 一下只要几微秒。
                if used > last_used:
                    last_used = used
                    grew_at = time.time()
                if time.time() - grew_at < DISK_QUIET:
                    last_sig = None
                    time.sleep(0.4)
                    continue

                # 重启后又回到了安装器：必须认出来并且立刻停手。第一屏那个
                # 「下一步」会把整个安装重新触发一遍，盘上刚装好的系统直接被
                # 格式化掉。
                #
                # 判据要三样齐备才算，少一样都会误判：
                #   · 盘上已经有数据（安装开始了）
                #   · 中间经过一段暗屏（重启时固件/内核会把画面清黑，而安装器
                #     的进度页和向导页都是浅色——这一条把「进度页卡住不动」
                #     和「向导里退回上一屏」都排除掉了，实测两种都撞见过）
                #   · 这一屏以前见过
                if (used > EJECT_MIN_DISK and saw_dark
                        and any(same_screen(sig, s) for s in seen)):
                    save_frame(vdir, acts, frame, rfb.w, rfb.h)
                    say("安装器重启后又回到了第一屏（光盘还挂着）。"
                        "停下来处理，不重复操作——那一下会把刚装好的盘格式化掉。")
                    return "reboot"
                dark = is_dark(frame, rfb.w, rfb.h)
                if dark:
                    saw_dark = True

                # 画面要稳一下才动作。安装器切换时要重绘，半帧上按下去会打到空气。
                # 进度页画面一直在动，这道判断自然把它挡住——不会去按一个没意义的回车。
                if last_sig is None or not same_screen(sig, last_sig):
                    last_sig, last_at = sig, time.time()
                    time.sleep(0.4)
                    continue
                if time.time() - last_at < SETTLE_SECONDS:
                    time.sleep(0.3)
                    continue

                # 该拔光盘了。
                #
                # fnOS 装完之后明确要求「请拔出安装引导盘」，否则重启又会退回
                # 安装器（固件沿用 NVRAM 里上次的启动项，不按 bootindex 选）。
                # 而 QEMU 这边光盘是 -drive media=cdrom，用户没法伸手拔线。
                #
                # 时机是整件事的关键，判据分两层：
                #   · 必须先见过复制阶段（盘上有数据、没按钮、画面在动）。复制
                #     期间客人机一直开着光盘，`eject` 只会报 locked。
                #   · 然后出现带按钮的静止画面——安装器空闲了。这时候先试温和的
                #     `eject`，不行再 `eject -f` 解开占用后取盘。
                # 拔早了会把正在读光盘的安装器弄坏，所以不能一见按钮就拔。
                if not ejected and saw_copy and used > EJECT_MIN_DISK:
                    if (eject_cd(mon)
                            or (eject_cd_force(mon) and cd_is_empty(mon))):
                        ejected = True
                        say("安装文件已经复制完，取出安装光盘"
                            "（不拔的话重启又会退回安装器）")

                # 同一个画面已经试过几种办法了？先回车，再点蓝按钮，都不行就撤。
                tried = sum(1 for s in seen if same_screen(sig, s))
                if tried == 0:
                    say("按回车（默认按钮）")
                    save_frame(vdir, acts, frame, rfb.w, rfb.h)
                    rfb.key_tap(KEY_RETURN)
                elif tried == 1 and btn is not None:
                    say(f"回车没推进，改点主按钮 ({btn[0]},{btn[1]})")
                    save_frame(vdir, acts, frame, rfb.w, rfb.h)
                    rfb.click(*btn)
                else:
                    save_frame(vdir, acts, frame, rfb.w, rfb.h)
                    say("回车和点按钮都试过了，屏幕还是没动。"
                        "自动安装停下来，剩下的请您在画面里点完", "warn")
                    return "manual"
                acts += 1
                seen.append(sig)
                # 暗屏的记忆只在「对着一屏浅色画面动过手」之后才清掉。
                # 重启那一趟是：浅色向导页 →（动作）→ 暗屏/GRUB → 浅色第一屏。
                # 要是动作一发生就清，那么在暗屏上按的那个回车就会把记忆抹掉，
                # 等回到第一屏时已经认不出来那是重启循环了。
                if not dark:
                    saw_dark = False
                last_sig = None
                time.sleep(ACT_PAUSE)
                if acts >= ACT_LIMIT:
                    say(f"已经做了 {acts} 个动作还没装完，停下来交还给您", "warn")
                    return "manual"
    except Exception as e:                      # noqa: BLE001 - 见 docstring
        say(f"自动安装出错：{e!r}，退成手动", "warn")
        return "manual"

    say(f"自动安装跑了 {budget_minutes} 分钟还没结束，退成手动", "warn")
    return "timeout"
