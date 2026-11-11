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
import struct
import time
import zlib
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
# 每个采样点在指纹里占几个字节。帧的内存序是 B,G,R,pad 四字节，所以整组取出来
# 比只取前三个字节快得多——见 fingerprint() 的说明。
FP_BPP = 4
# diff_frac 的「单个分量差多少算变了」查表。translate 是 C 级逐字节映射，
# 拿它把一个「逐字节差值」串变成「这个字节算不算变」的 0/1 串。
_DIFF_TABLE = bytes(1 if i > FP_TOL else 0 for i in range(256))
# primary_button 的单槽缓存：(帧, 按钮中心)。见那个函数的 docstring。
# 只留一个槽：驱动器是顺序消费帧的，同一帧连续调几次之后就换下一帧了。
_BTN_CACHE: tuple[bytes, tuple[int, int] | None] | None = None


def fingerprint(frame: bytes, w: int, h: int) -> bytes:
    """画面的粗指纹：按 FP_STEP 采样出来的 BGRA 串，每个采样点 4 字节。

    为什么每组取 4 字节而不是 3 字节（2026-11-11 改）：原来是一个 Python 循环
    逐点拼三字节（1.36ms/次），现在靠 memoryview.cast 一次切一行（0.05ms/次）。
    这一步每轮都要做，安装一跑就是几千轮。

    **cast("I") 是这儿的关键，不能换成对 bytes 直接切片。** `bytes[a:b:16]` 的
    步长按**字节**算，切出来是 200 个单字节，不是 200 个 4 字节组——那等于把指纹
    退化成了「只采样蓝通道的一个字节」，而 fnOS 那个纯蓝按钮的蓝是 219、浅底是
    245，差 26 低于 FP_TOL=40，整块按钮就这么从判据里消失了。cast 成无符号 32
    位整型视图之后，步长按**元素**算，16 字节正好是 4 个像素。第一版就栽在这儿，
    43 条测试一条都没报——所以现在有一条 test_r_channel_difference_is_seen 盯着。
    """
    mv = memoryview(frame).cast("I")
    out = bytearray()
    for y in range(0, h, FP_STEP):
        base = y * w
        out += mv[base:base + w:FP_STEP].tobytes()
    return bytes(out)


def diff_frac(a: bytes, b: bytes) -> float:
    """两幅指纹里「变了」的采样点占比。长度不一样直接算全变。

    四条路全都是 C 级，没有 Python 级逐点循环（2026-11-11 改，原来 2.3ms/次）：

    1. 整串相等 → 0。静止等待期每一轮都撞这一条，一次 memcmp 就完事。
    2. 两个大整数异或，一次算出所有分量的差值。
    3. translate 查表，把「差值 > FP_TOL」压成 0/1 串。
    4. 四个通道各自隔 4 取一个再按位或，得到「这个采样点算不算变」，
       数一数非零个数就是变化比例。

    为什么值得这么写：drive() 每一轮都要拿当前帧和 seen 里所有历史帧各比一次
    （判断「是不是又回到这一屏了」「这一屏试过几招」），跑满 200 个动作时一轮
    就是 200 次 diff_frac。按原来 2.3ms 算是每轮 460ms 纯 CPU——宿主机上一个
    Python 进程和 QEMU 抢核，安装会变慢，而这一点在日志里完全看不出来。
    """
    if len(a) != len(b):
        return 1.0
    if a == b:
        return 0.0
    d = (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(
        len(a), "little")
    m = d.translate(_DIFF_TABLE)
    n = (len(a) + FP_BPP - 1) // FP_BPP
    if n == 0:
        return 1.0
    v = (int.from_bytes(m[0::FP_BPP], "little")
         | int.from_bytes(m[1::FP_BPP], "little")
         | int.from_bytes(m[2::FP_BPP], "little")
         | int.from_bytes(m[3::FP_BPP], "little"))
    vb = v.to_bytes(n, "little")
    return (n - vb.count(0)) / n


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
# 「服务起来了没」的探测节奏。原来是每轮一次、超时 1.0 秒——而它顺序试两个
# 端口，遇到 QEMU hostfwd 那种「接住连接但不回数据」的沉默对面时一次就
# 2.005 秒（实测），把整个轮询节奏从 0.5 秒一轮拖到 2.5 秒一轮。现在每
# PROBE_EVERY_SECONDS 秒才探一次，单次超时压到 PROBE_TIMEOUT。drive() 有
# 45 分钟预算，晚 10 秒发现服务起来没有代价。
PROBE_EVERY_SECONDS = 10.0
PROBE_TIMEOUT = 0.3
# 画面连续多少轮没稳过就报警、多少轮就放弃。见 drive() 里那个分支的说明：
# 每轮都变的画面会让「等画面稳」这道门永远 continue，而后面的动作熔断器排在
# 它后面，一个动作都不做、一行日志都不打。600 轮约 8 分钟（按 0.8 秒一轮），
# 2400 轮约 32 分钟——都在 45 分钟的 budget 之内，不会等到超时才暴露。
UNSTABLE_WARN_ROUNDS = 600
UNSTABLE_GIVE_UP = 2400
# 每个动作之前把当前画面存成 <vm 目录>/auto-<序号>.png，只保留最近这么多张。
# 自动安装退成手动时，用户（和我们）第一句话就是「它停在什么屏幕上」——不留
# 现场就只能靠猜。2026-11-11 实测：不留图时盯着日志数回车次数，怎么数都对不上
# 实际走到了哪一屏，白白绕了一圈。
KEEP_FRAMES = 24
# 存成 PNG 而不是 PPM（2026-11-11 改）。两个理由，都能量出来：
#   · 磁盘：PPM 是不压缩的 800×600×3 = 1.4MB 一张，24 张常驻 34MB，而安装器
#     画面是大块纯色加文字，PNG 压完 20~40KB——同一个 VM 目录少占 33MB，
#     那些帧在安装结束之前一张都用不上。
#   · CPU：PPM 那次改造之前，BGR→RGB 是逐像素 Python 循环，实测 62ms/张；
#     改成三道切片赋值之后是 0.3ms，再算上 zlib 压 6 级约 8ms，一共不到 9ms。
#     一个 200 步的安装从 12.5 秒 CPU 降到 1.8 秒。
# 顺带的好处：Read 工具和 Preview 都认 PNG，原来那堆 .ppm 谁都打不开。
FRAME_SUFFIX = ".png"
# 回车的 keysym。这是驱动器的主要动作，见 drive() 里的说明。
KEY_RETURN = 0xFF0D
# 盘上写过这么多字节，才算「安装已经开始了」。没到这个数时不弹光盘、也不认
# 「重启后又回到安装器」——第一屏时盘还是空的，那时候看见重复画面只是点击没生效。
EJECT_MIN_DISK = 512 << 20
# 动作帧的记忆分两段留着，不存全部（2026-11-11 改）。
#
# 原来是一个 list 把每个动作帧的签名都留着，跑满 ACT_LIMIT=200 就是 200 × 120KB
# ≈ 24MB——autoinstall 是和 QEMU 并跑的 Python 进程，这 24MB 是白占的。而这两段
# 记忆的用途完全不同，本来就该分开：
#   · 最早 32 个动作帧（seen_early）：重启检测的唯一依据。「重启后又回到第一屏」
#     要认的是第一屏，它几乎总是最早那几个动作之一。
#   · 最近 16 个动作帧（seen_recent）：「这一屏已经试过几招了」要的是相邻的几次，
#     隔了十几屏再回到同一屏时按回车重新试一遍并没有什么害处。
# 两段不重叠（acts < EARLY_KEEP 时只进 early），所以「试过几次」不会把同一帧数两遍
# ——数重了会提前撞上撤退，把一个只是反应慢的安装器当成点不动。
# 每这么多轮取帧，就回过头来要一次全屏（2026-11-11 加，配合增量更新用）。
# 增量更新靠本地副本在两条更新之间保持画面完整，万一漏了一个矩形，副本就和
# 服务端不一致了，而那种故障从外表看不出来——画面照样在动，只是内容和真的不一样。
# 全屏重同步的代价是一帧 1.9MB，摊到 30 轮上可以忽略。
RESYNC_EVERY = 30
EARLY_KEEP = 32
RECENT_KEEP = 16

# 等一帧愿意等多久，分两种请求给两个数（2026-11-11 加）。
#
# 增量请求：5 秒。原来不分模式、一律 25 秒，而 25 秒这个数只在一种情况下有用——
# 全屏请求下等一帧。增量请求下它直接决定了「画面静止时一轮要多久」：实测
# （/tmp/bench_wait.py，服务端只握手、不回帧也不断开）改造前一轮 25.00 秒，
# 于是「同一个画面试三次就撤」实际要 81 秒，其中 96% 是干等。而 drive() 里那个
# 「画面不动就把轮询放慢」的计数器要 4 轮才生效，4 轮是 100 秒——等于从来没生效过。
#
# 为什么不是 1 秒：要大于 DISK_QUIET(2.0) 和 SETTLE_SECONDS(1.0)，否则会出现
# 「还没等出新帧就断定画面静止」。5 秒也够包容进度页那种「3、4 秒才动一格」的
# 帧间隔，减少漏帧之后把复用帧误判成同屏的机会。
#
# 全屏请求：25 秒不动。这里不能跟着缩：非增量请求下 RFC 6143 要求服务端立即回帧，
# 25 秒还不回说明连接有问题，这时候 raise 让 drive() 退回手动是对的；缩到 5 秒
# 只会把「QEMU 正好忙了一下」误报成连接故障，白丢一次自动安装。
INCR_TIMEOUT = 5.0
FULL_TIMEOUT = 25.0


class FrameMemory:
    """动作帧的两段记忆。

    抽成类是为了能单独测：这两个容器怎么长、长满了之后旧的那段还认不认得出第一屏，
    是重启检测的唯一依据，而它跑一遍 drive() 要一分钟——不值得为它编那么慢的测试。
    """

    def __init__(self) -> None:
        self.early: list = []          # 最早的 EARLY_KEEP 个动作帧
        self.recent: list = []         # 最近 RECENT_KEEP 个动作帧

    def remember(self, sig) -> None:
        # 两段不重叠，见 EARLY_KEEP 那段注释：重叠会让「试过几次」多数一遍，
        # 而多数了会提前撞上撤退，把一个只是反应慢的安装器当成点不动。
        if len(self.early) < EARLY_KEEP:
            self.early.append(sig)
        else:
            self.recent.append(sig)
            if len(self.recent) > RECENT_KEEP:
                self.recent.pop(0)

    def was_seen(self, sig) -> bool:
        return (any(same_screen(sig, s) for s in self.early)
                or any(same_screen(sig, s) for s in self.recent))

    def times_tried(self, sig) -> int:
        return (sum(1 for s in self.early if same_screen(sig, s))
                + sum(1 for s in self.recent if same_screen(sig, s)))

    def __len__(self) -> int:
        return len(self.early) + len(self.recent)


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
        # 关掉 Nagle。这个连接上跑的是「按一下回车 → 等一帧看反应」这种一问一答，
        # 小包被攒起来一起发会直接变成操作延迟。回环上也一样，而且不花钱。
        self.sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sk.settimeout(timeout)
        # 下一帧是全屏请求还是增量请求。全屏要求服务端把整个区域画完（RFC 6143
        # 7.5.3），增量来多少算多少。
        self._want_full = True
        # 本地 framebuffer 副本。增量模式就靠它在两条更新之间保持画面完整；
        # None 表示还没有拿到过任何一帧（第一次必须全屏）。
        self._fb: bytearray | None = None
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
        """要一整帧（incremental=0）。"""
        # FramebufferUpdateRequest 一共 10 字节：类型 1 + incremental 1 +
        # x/y/w/h 各 2。多写一个字节的后果见 SetEncodings 那段注释。
        self._want_full = True
        self.sk.sendall(b"\x03\x00\x00\x00\x00\x00"
                        + self.w.to_bytes(2, "big") + self.h.to_bytes(2, "big"))

    def request_incr(self) -> None:
        """只要变化的那一块（incremental=1）。

        这是 2026-11-11 那次优化的主体，而它省的主要不是我们的 CPU，是 **QEMU 的**。
        这个连接上只有一个客户端（驱动器自己；app 的内嵌控制台是另开的一条），而
        每发一次全屏请求，QEMU 的 VNC 线程就得把整块显存按 Raw 编一遍、推 1.9MB
        过来。驱动器一轮要取一次帧，安装一跑就是几千轮——等于让 QEMU 在整个安装
        期间持续做一件几乎没意义的事，而它做的这些正好和安装器自己抢宿主机 CPU。
        改成增量之后，QEMU 只编变化区域：进度页那几分钟里一帧只有几十 KB。

        代价是本地要维护一份 framebuffer 副本（self._fb，1.9MB）。另外增量模式
        有个必须自己兜的性质：画面真的没变时服务端一条都不发，所以这里的超时不是
        错误，而是「没变」的信号——frame() 在增量模式下超时返回 None。
        """
        self._want_full = False
        self.sk.sendall(b"\x03\x01\x00\x00\x00\x00"
                        + self.w.to_bytes(2, "big") + self.h.to_bytes(2, "big"))

    def frame(self, timeout: float | None = None):
        """取一帧。全屏模式返回 w*h*4 字节；增量模式可能返回 None（画面没变）。

        `timeout` 不给就按当前请求模式选：增量 5 秒、全屏 25 秒（见 INCR_TIMEOUT
        那里的说明）。**模式在 request_full / request_incr 里就已经定了**，这里只是
        照着读——把选择集中在这一处，是为了不让调用方各写一个数：drive() 原来就是
        不传参拿默认值，于是增量请求也按 25 秒等，一轮干等 25 秒。

        全屏请求下会一直读到**整个画面都画满**为止（2026-11-11 修）。RFC 6143
        7.5.3 说服务端可以爱分几条 FramebufferUpdate 就分几条，原来只读第一条
        就返回：没画到的地方留在初始化的全 0 里，就是一张「半屏真画面、半屏纯黑」
        的图。而 is_dark 的判据正是「平均亮度低」——一张半黑帧会被当成「客人机
        重启了」，把 saw_dark 无端置上，放大重启循环判据的误报面（见 drive() 里
        那段）。代价是一个 480k 字节的 mask 加一次 count，正常情况第一帧就画满。

        增量模式下：第一条 FramebufferUpdate 落到本地副本上就返回；等到超时一条
        都没有，返回 None——那说明画面压根没动，调用方该直接用上一帧。
        """
        if timeout is None:
            timeout = FULL_TIMEOUT if self._want_full else INCR_TIMEOUT
        deadline = time.time() + timeout
        if self._fb is None:
            out = bytearray(self.w * self.h * 4)
        else:
            out = self._fb                     # 就地更新本地副本
        # painted 只有全屏模式用得上：它回答「这一帧还有没有区域没画到」，而增量
        # 模式第一条更新就返回、返回处的判断也直接走 `not self._want_full` 那一支。
        # 原来两个分支都老老实实分配一个 w*h 的 mask，1406×744 是 1MB，安装期间
        # 几千轮就是几千次 1MB 的分配加清零，GC 全为此打工，而增量模式下它一个字节
        # 都没被读过。
        painted = bytearray(self.w * self.h) if self._want_full else None
        while True:
            left = deadline - time.time()
            if left <= 0:
                if not self._want_full:
                    return None                 # 增量等待超时 = 画面没变
                raise AutoError("等 RFB 帧超时")
            self.sk.settimeout(left)
            try:
                t = self._recv(1)[0]
            except socket.timeout:
                # 一个字节都没读到就超时。增量模式下这就是「画面没变」——服务端
                # 没有可发的区域，于是什么都不发。注意只在这一处这么解释：矩形数据
                # 读到一半超时是另一回事，那时流已经错位，这个连接不能再用，那个
                # 异常照旧往上冒，让 drive() 退回手动。
                if not self._want_full:
                    return None
                raise AutoError("等 RFB 帧超时")
            if t != 0:                                # 别的消息类型
                self._skip_message(t)
                continue
            self._recv(1)                             # 补位
            nrect = int.from_bytes(self._recv(2), "big")
            # 一个矩形都没有的空更新：不当成画面。
            #
            # 增量模式下服务端有时会回一条空更新（客户端请求了、但没有任何区域
            # 脏）。原来 nrect == 0 时 out 是全 0，返回去就是一张纯黑图，而 is_dark
            # 的判据正是「平均亮度 < 60」——一张假黑帧会被当成「客人机重启了」，
            # 把 saw_dark 无端置上，放大重启循环判据的误报面（见 drive() 里那段）。
            # 现在 out 接着本地副本，continue 只是多等一轮。2026-11-11 审计补上。
            if nrect == 0:
                continue
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
                    if painted is not None:
                        painted[(y + r) * self.w + x:(y + r) * self.w + x + rw] = \
                            b"\x01" * rw
            if not self._want_full or not painted.count(0):
                if len(out) != self.w * self.h * 4:
                    raise AutoError("RFB 帧大小不对")
                self._fb = out                # 留着给下一轮增量续接
                return bytes(out)

    def _skip_message(self, t: int) -> None:
        """把一条非 FramebufferUpdate 的消息从流上读掉。"""
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

    **带一个单槽缓存**（2026-11-11 加）：驱动器在「等画面稳下来」那段时间里会
    拿着同一帧反复调这个函数——原来每轮白算 6.5ms。同一个 frame 对象直接复用
    上一次的结果。判定本身是纯函数（同样的画面必然给出同样的按钮），而指纹相同
    在我们这套判据里就等于同一屏，所以缓存不会把两屏混成一屏。
    """
    global _BTN_CACHE
    c = _BTN_CACHE
    if c is not None and c[0] == frame:
        return c[1]

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
        _BTN_CACHE = (frame, None)
        return None
    pt = (best[1], best[2])
    _BTN_CACHE = (frame, pt)
    return pt


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

    **两个端口都要试，任何一个真的应了就当成就绪。** 原来是 if web / elif ssh：
    web_port 一有值，ssh 那条路一次都走不到。而 vmkit 给每台 kind=="local" 的
    VM 都同时分配两个端口（web_port 转发到 guest:5666，ssh_port 转发到 guest:22）。
    装一台普通 Linux（Debian/Ubuntu server 之类，不是 fnOS 那种 NAS）时，
    sshd 起来了、5666 上什么都没有——hostfwd 接受连接后关掉，第一个分支
    return False，于是 drive() 每一轮都判「服务没起来」，把整个 budget
    （默认 40 分钟）耗完返回 timeout，再打印「请您在画面里点完」，而系统早就
    装好并跑起来了。vmkit 里 local_qemu_args 的注释明写「ssh_port 备用——
    有些安装器装完会起 sshd」，代码把这条唯一的备用路径堵死了。
    2026-11-11 审计补上。
    """
    got: list[bool] = []
    # web 端口：发一个 GET，读到 HTTP 响应头才算。
    web = vm.get("web_port")
    if web:
        try:
            with socket.create_connection(("127.0.0.1", int(web)),
                                          timeout=timeout) as sk:
                sk.settimeout(timeout)
                sk.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                got.append(sk.recv(64).startswith(b"HTTP/"))
        except OSError:
            got.append(False)
    # ssh 端口：读 banner。
    port = vm.get("ssh_port")
    if port:
        try:
            with socket.create_connection(("127.0.0.1", int(port)),
                                          timeout=timeout) as sk:
                sk.settimeout(timeout)
                got.append(sk.recv(64).startswith(b"SSH-"))
        except OSError:
            got.append(False)
    return any(got)


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


def _frame_no(p: Path) -> int:
    """auto-<n>.<后缀> 里的 n，读不出来排到最后。

    只给 save_frame 的向前剪用（见那儿说明为什么不能按字典序排）。
    .ppm 和 .png 都认：早先的版本写 PPM，直接换后缀会让老 VM 目录里那些帧
    逃过剪枝、越堆越多。
    """
    try:
        return int(p.stem.split("-", 1)[1])
    except (IndexError, ValueError):
        return 1 << 30


def _png(w: int, h: int, rgb: bytes) -> bytes:
    """RGB 串 → PNG 字节流。

    自己写而不用 Pillow： Pillow 不是系统 Python 的自带模块，而这段代码跑在用户
    的机器上（vmkit.py 是当脚本跑的，装 app 的时候不会去 pip install 任何东西）。
    PNG 的结构对这个用途足够简单：一个 IHDR、一个用 zlib 压起来的 IDAT（每行前面
    加一个 0 表示「不过滤」）、一个 IEND。zlib 和 struct 都是标准库。
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw += b"\x00"
        raw += rgb[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _next_frame_no(vdir: Path) -> int:
    """这一轮 save_frame 的起始编号：现有 auto-<n> 的最大 n 加一。

    为什么不能从 0 开始：save_frame 的剪枝按 _frame_no 的全局数值序保留最高的
    KEEP_FRAMES 个。如果 VM 目录里留着上一轮编号更高的帧（例如 auto-150.png 起
    24 张），新一轮写出的 auto-0.png 排在最下面，olds[:-24] 会在同一个调用里把
    它刚写的这个文件删掉——之后每一次 save_frame 都是「写完即删」，200 个动作
    全写全删、一张帧都不留，而「它停在什么屏幕上」这个唯一证据正好没了。
    2026-11-11 沙箱实测复现：先放 auto-150..173 共 24 张，再 save_frame(n=0)，
    auto-0.png 在同一个调用里被删；n=1..5 同样如此。

    要复现得让「vm.json 没了而 auto-*.png 还在」——install --force 会 _rmtree
    整个目录，不带 --force 时 vm.json 存在就被拒，所以那是崩溃或手搓出来的状态。
    正因为罕见，它一旦发生就特别难归因。M34 第十九条修的是「剪枝按字典序排」，
    这一条是同一个失败换了个方向回来。
    """
    m = -1
    for p in (*vdir.glob(f"auto-*{FRAME_SUFFIX}"), *vdir.glob("auto-*.ppm")):
        n = _frame_no(p)
        if n < (1 << 30):        # 读不出编号的那些不算
            m = max(m, n)
    return m + 1


def save_frame(vdir: Path, n: int, frame: bytes, w: int, h: int) -> None:
    """把一帧存成 PNG，只留最近 KEEP_FRAMES 张。

    RFB 的内存序是 B,G,R,pad，PNG 要 R,G,B。原来是一个 Python 循环逐像素换
    （62ms/张），现在三道切片赋值：帧里 B 通道是 frame[0::4]、G 是 [1::4]、
    R 是 [2::4]，按 3 的步长写进目标就是 R,G,B 交错。三道都是 C 级。
    """
    png = vdir / f"auto-{n}{FRAME_SUFFIX}"
    try:
        out = bytearray(w * h * 3)
        out[0::3] = frame[2::4]
        out[1::3] = frame[1::4]
        out[2::3] = frame[0::4]
        with open(png, "wb") as f:
            f.write(_png(w, h, bytes(out)))
    except OSError:
        return
    # 向前剪：只留最近这么多张，别让一个 30 分钟的安装在磁盘上堆 600 张
    #
    # **按文件名里的序号排序，不能按文件名字典序。** "auto-9.png" > "auto-39.png"
    # （'9'=0x39 > '3'=0x33），字典序和数值序从 n>=10 起分叉。原来写的是
    # `key=lambda p: p.name`，于是留下的「最近 24 张」其实是一个字典序窗口：
    # 实测跑 40 个动作后 auto-10.ppm~auto-22.ppm 被删掉，而更老的
    # auto-3.ppm~auto-9.ppm 留着；跑满 ACT_LIMIT=200 时有 100 帧（n=100..199）
    # 在 save_frame 写下它的同一个调用里就被删掉，最后那一帧 auto-199.ppm
    # 根本不存在。后果正好和这个功能的目的相反：安装退化成 manual 时，用户和
    # 我们第一眼要看的「它停在什么屏幕上」那一帧不在盘上。2026-11-11 审计补上。
    try:
        olds = sorted([*vdir.glob("auto-*.png"), *vdir.glob("auto-*.ppm")],
                      key=_frame_no)
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
         emit=None, stage=None) -> str:
    """把本地安装器的图形向导自己点完。

    返回值见模块 docstring。**任何异常都不往外抛**：自动安装失败就该退回手动，
    让一个 Python 栈打到安装日志里对用户没有任何帮助。

    `stage` 是可选的第二阶段回调（2026-11-11 加）：(id, 标签)。它和 `emit` 分开是
    因为两者在 app 里长得不一样——stage 换的是标题那一行，emit 是往日志里追加一行。
    等安装器画面那 20~40 秒原来是纯静默的，app 上就挂着「启动安装器」四个字，
    用户看不出它在等什么；现在那段时间有自己的标题。
    """
    def say(msg: str, level: str = "info") -> None:
        if emit:
            try:
                emit(msg, level)
            except Exception:
                pass

    def phase(sid: str, label: str) -> None:
        if stage:
            try:
                stage(sid, label)
            except Exception:
                pass

    mon = vdir / "monitor.sock"
    # 「服务起来了没」的下一次探测时刻。等 VNC 那段和主循环共用它，
    # 两边都不能每轮探一次（见 PROBE_EVERY_SECONDS 的说明）。
    next_probe = 0.0
    port = int(vm.get("vnc_display", 0) or 0) + 5900
    if port <= 5900:
        say("这台虚拟机没有配 VNC 显示编号，没法自动点安装器", "warn")
        return "manual"

    # 先连 RFB，连不上就重试：install 一起步 QEMU 就在了，但 VNC 要等固件把
    # ramfb 初始化出来才听得见。一上来只连一次，连不上就整个自动安装失败——
    # 而那时安装器其实还要十几秒才露面。
    #
    # **重试期间要说人话**（2026-11-11 加）。原来这 120 秒是静默的：app 那边显示
    # 「自动安装中」，底下什么都没有，用户既不知道它在等什么，也看不出卡了没有。
    # 而这一段恰恰是最长的静默期——固件初始化加 ISO 引导实测要 20~40 秒。
    phase("wait", "等安装器画面出现")
    rfb = None
    deadline = time.time() + 120
    waited = 0.0
    # 这段里的就绪探测也走同一个节流。原来它每轮一次、每次最多 2 秒（两个端口
    # 各 1 秒），而慢档下 120 秒窗口里真正能尝试 RFB 连接的次数从约 60 次降到
    # 约 30 次。VNC 通常 20~40 秒就连上，所以影响比主循环小，但既然是同一个
    # 浪费就一起收掉。
    while time.time() < deadline:
        if time.time() >= next_probe:
            next_probe = time.time() + PROBE_EVERY_SECONDS
            if service_ready(vm, timeout=PROBE_TIMEOUT):
                say("客人机的服务已经起来了，不用点安装器")
                return "done"
        try:
            rfb = RFB(port)
            break
        except (OSError, AutoError):
            pass
        left = deadline - time.time()
        if left <= 0:
            break
        nap = min(2.0, left)
        time.sleep(nap)
        waited += nap
        if waited >= 10.0:
            waited = 0.0
            say(f"还在等安装器画面出来（VNC 127.0.0.1:{port}，"
                f"固件初始化加光盘引导要几十秒）")
    if rfb is None:
        say(f"连不上这台虚拟机的 VNC（127.0.0.1:{port}），自动安装退成手动",
            "warn")
        return "manual"

    phase("install", "自动操作安装器")
    say(f"已连上安装画面 {rfb.w}×{rfb.h}，开始自动安装")
    mem = FrameMemory()
    last_sig = None                 # 上一帧（用来判断画面稳没稳）
    prev_sig = None                 # 上一帧（只用来判断画面在不在动）
    saw_copy = False                # 见过「盘上有数据、没按钮、画面在动」的帧
    saw_dark = False                # 上一个动作之后见过暗屏（重启的痕迹）
    # 基线取当前值，不是 0。取 0 的话第一轮一定判成「盘刚长大了」，白白跳过
    # 头两秒的画面——测试里就这么丢掉过一整屏暗屏，把重启循环的识别跳过去了。
    last_used = disk_used(vdir)      # 上一轮看到的盘占用
    grew_at = 0.0                    # 盘最后一次变大的时刻
    last_at = 0.0
    still = 0                        # 画面连续变化了多少次（翻页时多给 settle）
    unstable = 0                     # 连续多少轮画面没稳过（见下面的熔断）
    # 起始编号接在目录里现有帧的后面，不从 0 开始。从 0 开始会让剪枝把
    # 本轮刚写的帧删掉，见 _next_frame_no 的说明。
    acts = _next_frame_no(vdir)   # 一共做了多少个动作（回车或点击）
    ejected = False
    budget_end = time.time() + budget_minutes * 60
    frame: bytes | None = None       # 最近一次真的拿到的画面
    rounds = 0                       # 取过多少轮帧（用来决定什么时候全屏重同步）
    last_saved: bytes | None = None  # 上一帧存过盘的 frame 对象，见 stash

    def stash(frame):
        """存一帧，但同一帧只存一次。

        tried 0 / 1 / else 三条分支各存一次，而增量模式下画面没变时 frame()
        返回 None、got 直接是上一个 bytes 对象——那三张是同一屏。
        实测一屏 1406x744 带文字的安装画面 PNG 是 136.3 KB（纯色底只有 1.5 KB，
        所以绝对量随画面变，三倍放大是结构性的），跑满 ACT_LIMIT=200 就是
        27.91 MB 写进去、剪枝只留 3.35 MB，白写 24.56 MB，折合每小时 17.6 MB
        写放大。2026-11-11 审计补上。

        重启循环和「画面稳不下来」那两处不走这个：它们是关键证据，宁可多存一张。
        """
        nonlocal last_saved
        if frame is None or frame is last_saved:
            return
        last_saved = frame
        save_frame(vdir, acts, frame, rfb.w, rfb.h)

    try:
        with rfb:
            while time.time() < budget_end:
                # 服务就绪探测要降频。
                #
                # 原来是每轮第一句就 service_ready(vm, timeout=1.0)。它顺序试
                # web 和 ssh 两个端口，每个一次 create_connection 加一次 recv，
                # 而 recv 的超时就是传进去的 timeout。QEMU 的 hostfwd「来者不拒」
                # （见 service_ready 的注释）：对面是「接受了连接但永远不回数据」的
                # 沉默监听者时，一次调用 2.005 秒（2026-11-11 实测），两个端口各
                # 占满 1.0 秒。慢档下每轮先付 2.0 秒，再叠 INCR_TIMEOUT 的 5 秒，
                # 一轮 7 秒以上；而画面在动的安装器屏幕上 rfb.frame() 是毫秒级
                # 返回的，轮询节奏完全被这 2 秒主导，从约 0.5 秒一轮掉到约 2.5 秒
                # 一轮——45 分钟预算能覆盖的安装进度少五倍，直接推高撞 timeout
                # 的概率。drive() 本来就有预算，晚 10 秒发现服务起来没有任何代价。
                if time.time() >= next_probe:
                    next_probe = time.time() + PROBE_EVERY_SECONDS
                    if service_ready(vm, timeout=PROBE_TIMEOUT):
                        say(f"客人机服务已就绪，安装完成（自动操作了 {acts} 步）")
                        return "done"
                try:
                    # 平时只要变化的那一块，QEMU 就不用每轮把整块显存编一遍推过来
                    # （见 request_incr 的说明）。每隔 RESYNC_EVERY 轮全屏要一次，
                    # 免得万一漏了一个矩形，本地副本从此和服务端不一致——那种情况下
                    # 画面判据会整个失效，而我们看不出来。
                    #
                    # rounds 从 0 起，所以第一轮就是全屏——本地还没有 framebuffer
                    # 副本，增量请求无从续接。**每轮只发一个请求**：多发一个就会让
                    # 响应和请求错开一位，后面的帧整个对不上（基准测试踩过，症状是
                    # 假服务端线程炸掉、驱动器第一轮就退成 manual）。
                    if rounds % RESYNC_EVERY == 0:
                        rfb.request_full()
                    else:
                        rfb.request_incr()
                    rounds += 1
                    got = rfb.frame()
                except (AutoError, OSError) as e:
                    say(f"取画面失败：{e}", "warn")
                    return "manual"
                if got is None:
                    # 画面压根没变。这不是错误，是增量模式的正常回答。
                    if frame is None:
                        continue             # 还一帧都没拿到过，再等等
                    got = frame
                else:
                    frame = got
                sig = (fingerprint(got, rfb.w, rfb.h),
                       primary_button(got, rfb.w, rfb.h))
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
                    if not saw_copy:
                        # 说一句「这一步要几分钟」。复制阶段是全程最长的一段，
                        # 而画面看起来就是一条几乎不动的进度条——不告诉用户的话，
                        # 它看起来和卡死了没有区别（2026-11-11 实测：这一等就是
                        # 十几分钟，日志里一个字都不多）。
                        say("安装器正在往磁盘复制文件，这一步要几分钟，"
                            "期间不用管它")
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
                #
                # **放在「画面已经稳了」那道门后面**（2026-11-11 挪进来的）。原来
                # 它每轮都拿当前帧和 seen 里全部历史帧各比一次，跑满 200 个动作时
                # 一轮就是 200 次 same_screen——进度页那几分钟里画面每轮都在变，
                # 这一次全历史扫描一次都不会命中，纯白算。而「回到第一屏」这个状态
                # 本身是要稳定下来的：切到一半的帧判不出来，等它稳了再判，结论一样，
                # 而且动作点仍然在这道判断之后，安全性没变（不会先按了回车再发现）。
                dark = is_dark(frame, rfb.w, rfb.h)
                if dark:
                    saw_dark = True

                # 画面要稳一下才动作。安装器切换时要重绘，半帧上按下去会打到空气。
                # 进度页画面一直在动，这道判断自然把它挡住——不会去按一个没意义的回车。
                #
                # 注意 still 数的是「画面连续变化了多少次」，不是「静止了多少轮」：
                # 这个分支只有在 sig 和上一轮不同时才进得来，画面没变时它压根不走。
                # 所以 0.8 秒那一档的含义是「安装器在连续翻页，多给它一点 settle」，
                # 不是「等久了就放慢」——后者由 INCR_TIMEOUT 决定（2026-11-11：
                # 上一轮在这里写的注释说的是后者，和代码实际干的事不是一件）。
                if last_sig is None or not same_screen(sig, last_sig):
                    last_sig, last_at = sig, time.time()
                    still += 1
                    unstable += 1
                    time.sleep(0.8 if still >= 4 else 0.4)
                    # 画面每轮都变时，这个分支会一直 continue 下去——而下面那个
                    # 「同一屏试过几种办法」的熔断器排在它后面，根本走不到。
                    # 安装器屏幕上只要有一个每轮都变的东西（不确定进度条、闪烁的
                    # 光标、角落的时钟、滚动日志窗），same_screen 恒为假，于是
                    # 一个动作都不做、一行日志都不打，一路转到 budget 用完返回
                    # timeout。用户无法区分「安装器在转」和「驱动器死了」，而
                    # budget 是唯一的刹车。
                    # 2026-11-11 审计补上：连续不稳定到 UNSTABLE_WARN_ROUNDS 先
                    # 打一行心跳（让人知道它还活着、在等什么），到
                    # UNSTABLE_GIVE_UP 就退成 manual 并把原因说清楚。
                    if unstable == UNSTABLE_WARN_ROUNDS:
                        say(f"画面已经连续 {unstable} 轮在变，还在静默等它稳下来"
                            f"（进度条一直动的安装器就是这样，属正常）", "warn")
                    if unstable >= UNSTABLE_GIVE_UP:
                        save_frame(vdir, acts, got, rfb.w, rfb.h)
                        say(f"画面连续 {unstable} 轮没稳过，判断不出该点哪里。"
                            f"自动安装停下来，剩下的请您在画面里点完", "warn")
                        return "manual"
                    continue
                still = 0
                unstable = 0
                if time.time() - last_at < SETTLE_SECONDS:
                    time.sleep(0.3)
                    continue

                if used > EJECT_MIN_DISK and saw_dark and mem.was_seen(sig):
                    save_frame(vdir, acts, got, rfb.w, rfb.h)
                    say("安装器重启后又回到了第一屏（光盘还挂着）。"
                        "停下来处理，不重复操作——那一下会把刚装好的盘格式化掉。")
                    return "reboot"

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
                tried = mem.times_tried(sig)
                if tried == 0:
                    say("按回车（默认按钮）")
                    stash(got)
                    rfb.key_tap(KEY_RETURN)
                elif tried == 1 and btn is not None:
                    say(f"回车没推进，改点主按钮 ({btn[0]},{btn[1]})")
                    stash(got)
                    rfb.click(*btn)
                else:
                    stash(got)
                    say("回车和点按钮都试过了，屏幕还是没动。"
                        "自动安装停下来，剩下的请您在画面里点完", "warn")
                    return "manual"
                acts += 1
                mem.remember(sig)
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
