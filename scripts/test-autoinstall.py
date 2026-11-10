#!/usr/bin/env python3
"""scripts/autoinstall.py 的单元测试。

     python3 -I scripts/test-autoinstall.py

全部离线可跑：画面是合成出来的，服务端是进程内的假 socket。故意不连真虚拟机——
真虚拟机那套已经在 2026-11-11 的端到端实测里跑过（fnOS 从「选择安装位置」到
Web 界面 HTTP 200 全程无人点击），那一遍的成本是十几分钟和一块 16G 的盘，不该
塞进每次提交。

测的都是**曾经判错过**的东西，每一条背后都是一次实测：

  · 主按钮和「被选中的表格行」是同一种蓝，只按大小取会把表格行当成按钮，
    点上去屏幕纹丝不动（2026-11-11 第一次自动安装就这么白跑了三次）。
  · GRUB 是一整块蓝底，不拦就会把它当成一个巨大的下一步按钮。
  · 安装进度页 8% 和 18% 之间只有 0.13% 的采样点变化，低于「画面稳了」的
    门槛——所以驱动器另有一条「盘还在不在长」的判据，光看画面会以为它卡住。
  · 暗屏是重启的痕迹，没这个信号就分不清「重启后退回安装器」和「向导里
    退回上一屏」。
"""

import importlib.util
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("autoinstall",
                                               HERE / "autoinstall.py")
ai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ai)          # type: ignore[attr-defined]

W, H = 800, 600
BLUE = (51, 116, 219)                 # fnOS 主按钮的实测颜色


# ------------------------------------------------------------------ 合成画面 --

def px_bytes(rgb):
    """RGB 三元组 → 帧里的 4 字节。帧的内存序是 B,G,R,pad，不是 R,G,B。"""
    r, g, b = rgb
    return bytes((b, g, r, 0xFF))


def blank(rgb=(245, 245, 245), w=W, h=H):
    """一张纯色底。"""
    return px_bytes(rgb) * (w * h)


def rect(fr, x0, y0, x1, y1, rgb, w=W):
    fr = bytearray(fr)
    px = px_bytes(rgb)
    for y in range(y0, y1):
        base = y * w * 4
        fr[base + x0 * 4:base + x1 * 4] = px * (x1 - x0)
    return bytes(fr)


def dark_frame(w=W, h=H):
    return blank((8, 8, 8), w, h)


def button_screen():
    """浅底 + 右下一个蓝按钮，就是 fnOS 的「自定义设置」那一屏。"""
    return rect(blank(), 640, 462, 755, 505, BLUE)


def row_screen():
    """浅底 + 一条细长的蓝条，就是第一屏里被选中的表格行 [vda]。"""
    return rect(blank(), 191, 239, 608, 257, BLUE)


def both_screen():
    """表格行和按钮同时在场——必须认出按钮，不能认出表格行。"""
    f = rect(blank(), 191, 239, 608, 257, BLUE)
    return rect(f, 640, 462, 755, 505, BLUE)


def text_screen():
    """浅底 + 一小块蓝（模拟蓝色文字的实心笔画），面积不够，不算按钮。"""
    return rect(blank(), 300, 300, 316, 306, BLUE)


def line_screen():
    """浅底 + 一条细长蓝条（表格分隔线、下划线），宽高比超了，不算按钮。"""
    return rect(blank(), 120, 300, 680, 304, BLUE)


def grub_screen():
    """深蓝底的一大块，就是 GRUB 的菜单框。"""
    return rect(blank((0, 0, 120)), 20, 60, 780, 520, (0, 0, 120))


def progress_screen(pct):
    """安装进度页：浅底 + 一条绿色进度条 + 百分比文字那块。"""
    f = blank()
    f = rect(f, 100, 310, 700, 322, (210, 210, 210))       # 进度条槽
    f = rect(f, 100, 310, 100 + int(600 * pct), 322, (80, 175, 80))
    return f


# --------------------------------------------------------------- 假 RFB 服务端 --

class FakeRFB(threading.Thread):
    """进程内的假 RFB 服务端：说握手、收编码协商、按请求发一帧。"""

    def __init__(self, w=W, h=H, frame=None):
        super().__init__(daemon=True)
        self.w, self.h = w, h
        self.frame = frame or button_screen()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.got = {}                    # 客户端发来的东西，按类型记
        self.encodings = []
        self.stop = False

    def run(self):
        conn, _ = self.srv.accept()
        conn.settimeout(10)
        try:
            conn.sendall(b"RFB 003.008\n")
            self.got["version"] = conn.recv(12)
            conn.sendall(b"\x01\x01")                 # 1 种安全类型：None
            self.got["sectype"] = conn.recv(1)
            conn.sendall(b"\x00\x00\x00\x00")         # 认证通过
            self.got["clientinit"] = conn.recv(1)
            name = b"fake"
            conn.sendall(struct.pack(">HH", self.w, self.h)
                         + b"\x20\x18\x00\x01" + b"\x00\xff" * 3
                         + b"\x10\x08\x00" + b"\x00\x00\x00"
                         + struct.pack(">I", len(name)) + name)
            while not self.stop:
                head = self._recv(conn, 1)
                if not head:
                    break
                t = head[0]
                if t == 2:                            # SetEncodings
                    # 类型那 1 字节已经读掉了，剩下 补位 1 + 数量 2 + 每个编码 4。
                    # 曾经写成「先读 7 字节再读 n*4 字节」，编码被数了两遍，
                    # 多啃掉下一条消息的 3 字节——症状是 SetPixelFormat 的类型
                    # 字节变成 0x20，极难查。
                    body = self._recv(conn, 3)
                    n = struct.unpack(">H", body[1:3])[0]
                    self.encodings = [struct.unpack(">i", self._recv(conn, 4))[0]
                                      for _ in range(n)]
                    self.got["setencodings"] = self.encodings
                elif t == 0:                          # SetPixelFormat
                    self._recv(conn, 19)
                    self.got["setpixelformat"] = True
                elif t == 3:                          # FramebufferUpdateRequest
                    body = self._recv(conn, 9)
                    inc = body[0]
                    x, y, rw, rh = struct.unpack(">HHHH", body[1:9])
                    self.got["request"] = (inc, x, y, rw, rh)
                    self.on_request(inc, x, y, rw, rh)
                    conn.sendall(struct.pack(">BBH", 0, 0, 1)
                                 + struct.pack(">HHHHi", x, y, rw, rh, 0)
                                 + self._slice(x, y, rw, rh))
                elif t == 4:                          # KeyEvent
                    body = self._recv(conn, 7)
                    down, ks = body[0], struct.unpack(">I", body[3:7])[0]
                    self.got.setdefault("keys", []).append((down, ks))
                    self.on_key(down, ks)
                elif t == 5:                          # PointerEvent
                    body = self._recv(conn, 5)
                    self.got.setdefault("ptrs", []).append(
                        (body[0], struct.unpack(">HH", body[1:5])))
                elif t == 6:                          # ClientCutText
                    body = self._recv(conn, 7)
                    n = struct.unpack(">I", body[3:7])[0]
                    self._recv(conn, n)
                else:
                    break
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.srv.close()

    def on_request(self, inc, x, y, rw, rh):
        """每收到一次取帧请求就调一次。子类用它改盘的大小。"""

    def on_key(self, down, keysym):
        """每收到一个键盘事件就调一次。子类用它换画面。"""

    def _slice(self, x, y, rw, rh):
        out = bytearray()
        for r in range(rh):
            out += self.frame[(y + r) * self.w * 4 + x * 4:
                              (y + r) * self.w * 4 + (x + rw) * 4]
        return bytes(out)

    @staticmethod
    def _recv(conn, n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                return b""
            buf += c
        return buf


# --------------------------------------------------------------------- 测试 --

class TestPrimaryButton(unittest.TestCase):
    def test_finds_button(self):
        f = button_screen()
        self.assertEqual(ai.primary_button(f, W, H), (697, 483))

    def test_table_row_is_not_a_button(self):
        """第一屏那个坑：表格行是同一种蓝，而且比按钮大。"""
        self.assertIsNone(ai.primary_button(row_screen(), W, H))

    def test_button_wins_over_table_row(self):
        """行和按钮同时在场，要认按钮——实测曾经点到表格行上，屏幕纹丝不动。"""
        self.assertEqual(ai.primary_button(both_screen(), W, H), (697, 483))

    def test_small_blue_blob_is_not_a_button(self):
        self.assertIsNone(ai.primary_button(text_screen(), W, H))

    def test_long_thin_blue_line_is_not_a_button(self):
        self.assertIsNone(ai.primary_button(line_screen(), W, H))

    def test_grub_blue_box_is_not_a_button(self):
        self.assertIsNone(ai.primary_button(grub_screen(), W, H))

    def test_no_blue_at_all(self):
        self.assertIsNone(ai.primary_button(blank((255, 255, 255)), W, H))

    def test_white_button_is_not_found(self):
        """fnOS 第一屏的下一步是白底描边——颜色判据看不见它，所以驱动器的主
        要动作是回车而不是点按钮。这个测试把那条设计决定钉在这儿。"""
        self.assertIsNone(ai.primary_button(blank(), W, H))


class TestSameScreen(unittest.TestCase):
    def sig(self, f):
        return (ai.fingerprint(f, W, H), ai.primary_button(f, W, H))

    def test_identical(self):
        s = self.sig(button_screen())
        self.assertTrue(ai.same_screen(s, s))

    def test_different_wizard_screens(self):
        a = self.sig(button_screen())
        b = self.sig(progress_screen(0.5))
        self.assertFalse(ai.same_screen(a, b))

    def test_progress_bar_moving_is_below_the_pixel_threshold(self):
        """进度条在走，但像素判据分不出来——这正是要有「盘还在长」那条判据的原因。

        实测 8% 和 18% 两帧之间只有 0.13% 的采样点变化，低于 1% 的「画面稳了」
        门槛。光靠画面，驱动器会以为安装器卡在那儿，一遍遍按回车，最后自己撤退
        （2026-11-11 真的这么白跑过一次）。真正挡住它的是 drive() 里
        「盘是不是还在长」那条物理判据，见 TestDriveWithScriptedRFB。
        """
        a = self.sig(progress_screen(0.08))
        b = self.sig(progress_screen(0.18))
        self.assertTrue(ai.same_screen(a, b))

    def test_button_moved_is_different(self):
        """按钮挪了位置算不同屏，哪怕像素变化很小。"""
        a = self.sig(button_screen())
        b = self.sig(rect(blank(), 100, 462, 215, 505, BLUE))
        self.assertFalse(ai.same_screen(a, b))

    def test_one_has_button_one_hasnt(self):
        a = self.sig(button_screen())
        b = self.sig(blank())
        self.assertFalse(ai.same_screen(a, b))

    def test_different_resolution_is_different(self):
        a = (ai.fingerprint(blank(w=800, h=600), 800, 600), None)
        b = (ai.fingerprint(blank(w=640, h=480), 640, 480), None)
        self.assertFalse(ai.same_screen(a, b))


class TestIsDark(unittest.TestCase):
    def test_black_is_dark(self):
        self.assertTrue(ai.is_dark(dark_frame(), W, H))

    def test_grub_blue_is_dark(self):
        self.assertTrue(ai.is_dark(blank((0, 0, 120)), W, H))

    def test_installer_screens_are_not_dark(self):
        for f in (blank(), button_screen(), progress_screen(0.5)):
            self.assertFalse(ai.is_dark(f, W, H))


class TestRFB(unittest.TestCase):
    def setUp(self):
        self.srv = FakeRFB()
        self.srv.start()

    def tearDown(self):
        self.srv.stop = True
        try:
            self.srv.join(timeout=2)
        except RuntimeError:
            pass

    def test_handshake_and_frame(self):
        with ai.RFB(self.srv.port) as r:
            self.assertEqual((r.w, r.h), (W, H))
            self.assertEqual(r.name, "fake")
        # 客户端发完编码协商就往下走了，不等回信。断言之前得给假服务端一点
        # 时间把字节读完——这是测试自己的竞态，不是产品代码的问题。
        time.sleep(0.3)
        # 只报 Raw 编码，不报 Tight/Zlib——这个驱动器只解 Raw
        self.assertEqual(self.srv.got.get("setencodings"), [0])
        self.assertTrue(self.srv.got.get("setpixelformat"))
        self.assertEqual(self.srv.got.get("version"), b"RFB 003.008\n")
        self.assertEqual(self.srv.got.get("clientinit"), b"\x01")

    def test_frame_roundtrip(self):
        want = button_screen()
        self.srv.frame = want
        with ai.RFB(self.srv.port) as r:
            r.request_full()
            got = r.frame()
        self.assertEqual(got, want)
        self.assertEqual(self.srv.got.get("request"), (0, 0, 0, W, H))

    def test_key_and_click(self):
        with ai.RFB(self.srv.port) as r:
            r.key_tap(ai.KEY_RETURN)
            r.click(100, 200)
        time.sleep(0.3)          # 同上：给假服务端读完的时间
        keys = self.srv.got.get("keys", [])
        self.assertEqual([k[0] for k in keys], [1, 0])          # 按下、松开
        self.assertEqual(keys[0][1], ai.KEY_RETURN)
        ptrs = self.srv.got.get("ptrs", [])
        self.assertEqual([p[0] for p in ptrs], [0, 1, 0])       # 移动、按下、松开
        self.assertEqual(ptrs[-1][1], (100, 200))

    def test_serverinit_off_by_one_breaks_everything(self):
        """ServerInit 的头是 24 字节（宽高 4 + 像素格式 16 + 名字长度 4）。
        少读 4 字节会把名字长度漏在流里，之后每条消息都错位。这个测试保证
        没人再把握手改回 20 字节。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            conn, _ = srv.accept()
            conn.sendall(b"RFB 003.008\n")
            conn.recv(12)                 # 客户端版本
            conn.sendall(b"\x01\x01")    # 1 种安全类型：None
            conn.recv(1)                  # 客户端的选择
            conn.sendall(b"\x00\x00\x00\x00")
            conn.recv(1)                  # ClientInit
            conn.sendall(struct.pack(">HH", 64, 48)
                         + b"\x20\x18\x00\x01" + b"\x00\xff" * 3
                         + b"\x10\x08\x00" + b"\x00\x00\x00"
                         + struct.pack(">I", 4) + b"fake")
            time.sleep(1.0)
            conn.close()
            srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        with ai.RFB(port) as r:
            self.assertEqual((r.w, r.h), (64, 48))
            self.assertEqual(r.name, "fake")
        t.join(timeout=2)


class GrowingRFB(FakeRFB):
    """每服务一帧就把盘写大一点——模拟安装器正在复制文件。"""

    def __init__(self, disk, grow=1 << 20):
        super().__init__()
        self.disk = disk
        self.grow = grow

    def on_request(self, *a):
        # 必须写非零字节：APFS 把全零段当稀疏文件，st_blocks 不涨，而
        # disk_used 看的正是 st_blocks——写零等于什么都没测到。
        with open(self.disk, "ab") as f:
            f.write(b"\xff" * self.grow)


class ScriptedRFB(FakeRFB):
    """画面跟着「驱动器按了几次回车」走。

    不按取帧次数换画面：驱动器为了等画面稳定、等盘静止，同一屏会取好几次帧，
    按次数换就会在它还没动作的时候就切走，时间线对不上。按按键换才是确定的。
    """

    def __init__(self, frames):
        super().__init__(frame=frames[0])
        self.frames = list(frames)
        self.presses = 0

    def on_key(self, down, keysym):
        if not down:
            return
        self.presses += 1
        if self.presses < len(self.frames):
            self.frame = self.frames[self.presses]


class TestDriveWithScriptedRFB(unittest.TestCase):
    """drive() 在有假 RFB 服务端之下的行为。

    这几条是 2026-11-11 那次「五个回车之后白白撤退」的直接回归：当时驱动器
    把复制文件的进度页当成了卡住的画面，一遍遍按回车，最后自己放弃了安装。
    """

    def vm(self, port):
        return {"name": "t", "vnc_display": port - 5900,
                "web_port": None, "ssh_port": None}

    def disk(self, d, mb=4):
        # 给 4MB 而阈值压到 1MB：正好卡在阈值上会让 `used > EJECT_MIN_DISK`
        # 变成假（st_blocks*512 可能正好等于文件大小），那种边界坑过一次。
        p = Path(d) / "data.img"
        p.write_bytes(b"\xff" * (mb << 20))      # 非零，见 GrowingRFB 的注释
        return p

    def test_does_not_act_while_disk_is_growing(self):
        """盘还在长就什么都别做——这是比「画面稳没稳」可靠得多的判据。"""
        with tempfile.TemporaryDirectory() as d:
            disk = self.disk(d)
            srv = GrowingRFB(disk)
            srv.start()
            try:
                res = ai.drive(self.vm(srv.port), Path(d),
                               budget_minutes=0.1, emit=None)
            finally:
                srv.stop = True
            self.assertEqual(res, "timeout")
            # 一个键都没按过。按了就会撞上「同一个画面试了三次」主动撤退，
            # 而安装其实好着呢。
            self.assertIsNone(srv.got.get("keys"))
            self.assertIsNone(srv.got.get("ptrs"))

    def test_acts_once_disk_settles(self):
        """盘不长了就该动作——否则会一直干等。"""
        with tempfile.TemporaryDirectory() as d:
            self.disk(d)
            srv = FakeRFB(frame=button_screen())
            srv.start()
            try:
                res = ai.drive(self.vm(srv.port), Path(d),
                               budget_minutes=0.15, emit=None)
            finally:
                srv.stop = True
            # 结果取 timeout 还是 manual 取决于机器快慢（画面一直不变，试满三次
            # 就交还用户）。这条要断言的是「它动作了」，不是它怎么收场。
            self.assertIn(res, ("timeout", "manual"))
            keys = srv.got.get("keys", [])
            self.assertGreaterEqual(len(keys), 1)
            # 主要动作是回车，不是点按钮
            self.assertEqual({k[1] for k in keys}, {ai.KEY_RETURN})

    def test_reboot_loop_is_caught_and_not_repeated(self):
        """装完之后重启又退回安装器：认出来，停手，绝不重复点第一屏。

        重复那一下会把刚装好的盘格式化掉——这是整个驱动器里最危险的一个动作，
        所以这条断言最关键。
        """
        with tempfile.TemporaryDirectory() as d:
            self.disk(d)
            # 盘上数据要够多才认「安装已经开始了」。阈值是产品常量，测试里
            # 临时压低，否则得真写 512MB。
            old_threshold = ai.EJECT_MIN_DISK
            ai.EJECT_MIN_DISK = 1 << 20
            try:
                srv = ScriptedRFB([button_screen(), dark_frame(),
                                   button_screen()])
                srv.start()
                try:
                    res = ai.drive(self.vm(srv.port), Path(d),
                                   budget_minutes=0.3, emit=None)
                finally:
                    srv.stop = True
            finally:
                ai.EJECT_MIN_DISK = old_threshold
            self.assertEqual(res, "reboot")
            keys = srv.got.get("keys", [])
            # 一共只按了两次：第一屏一次、暗屏一次。回到第一屏时没有再按——
            # 那一下会把刚装好的盘格式化掉。
            self.assertEqual(len([k for k in keys if k[0] == 1]), 2)
            self.assertEqual({k[1] for k in keys}, {ai.KEY_RETURN})


class TestMonitorSend(unittest.TestCase):
    def _mon(self, reply):
        srv = socket.socket(socket.AF_UNIX)
        d = tempfile.mkdtemp()
        p = Path(d) / "monitor.sock"
        srv.bind(str(p))
        srv.listen(1)

        def serve():
            conn, _ = srv.accept()
            conn.recv(64)
            conn.sendall(reply)
            time.sleep(0.2)
            conn.close()
            srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return p, t

    def test_ok_reply(self):
        p, t = self._mon(b"(qemu) \n")
        self.assertTrue(ai.monitor_send(p, "eject cd"))
        t.join(timeout=2)

    def test_error_reply(self):
        """客人机还开着光盘时 eject 会报 locked——这必须算失败而不是成功。"""
        p, t = self._mon(b"Error: Device 'cd' is locked and force was not "
                         b"specified\n(qemu) \n")
        self.assertFalse(ai.monitor_send(p, "eject cd"))
        t.join(timeout=2)

    def test_missing_socket(self):
        self.assertFalse(ai.monitor_send(Path("/tmp/没有这个监控套接字"), "x"))

    def test_cd_is_empty(self):
        p, t = self._mon(b"cd: [not inserted]\n(qemu) \n")
        self.assertTrue(ai.cd_is_empty(p))
        t.join(timeout=2)

    def test_cd_still_inserted(self):
        p, t = self._mon(b"cd (#block709): /x.iso (raw, read-only)\n(qemu) \n")
        self.assertFalse(ai.cd_is_empty(p))
        t.join(timeout=2)


class TestServiceReady(unittest.TestCase):
    def _serve(self, first_bytes, hold=0.0, read_first=True):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            try:
                conn, _ = srv.accept()
                conn.settimeout(2)
                # SSH 那条路客户端只读不发，先 recv 会一直堵到超时，
                # 然后就什么都发不出去了。
                if read_first:
                    conn.recv(128)
                if first_bytes:
                    conn.sendall(first_bytes)
                if hold:
                    time.sleep(hold)
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return port, t

    def test_http_ok(self):
        port, t = self._serve(b"HTTP/1.0 200 OK\r\n\r\n")
        self.assertTrue(ai.service_ready({"web_port": port}, timeout=1))
        t.join(timeout=2)

    def test_http_not_http(self):
        port, t = self._serve(b"garbage")
        self.assertFalse(ai.service_ready({"web_port": port}, timeout=1))
        t.join(timeout=2)

    def test_ssh_banner(self):
        port, t = self._serve(b"SSH-2.0-OpenSSH_9.6\r\n", read_first=False)
        self.assertTrue(ai.service_ready({"ssh_port": port}, timeout=1))
        t.join(timeout=2)

    def test_connect_then_close_is_not_ready(self):
        """QEMU 的 hostfwd 会先把连接接住，客人机没监听时才关掉——一个裸的
        TCP connect 通不代表服务起来了，必须读到真响应。"""
        port, t = self._serve(b"")
        self.assertFalse(ai.service_ready({"web_port": port}, timeout=1))
        t.join(timeout=2)

    def test_accept_but_never_answer_is_not_ready(self):
        port, t = self._serve(b"", hold=1.5)
        self.assertFalse(ai.service_ready({"web_port": port}, timeout=0.3))
        t.join(timeout=2)

    def test_no_ports(self):
        self.assertFalse(ai.service_ready({}, timeout=0.2))


class TestDiskUsed(unittest.TestCase):
    def test_sparse_file_reports_real_usage(self):
        """本地 ISO 这条路建的是稀疏文件，st_size 恒等于用户选的大小。
        拿 st_size 判断「装没装上」会永远是「装了」。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "data.img"
            with open(p, "wb") as f:
                f.truncate(16 << 30)
            self.assertEqual(ai.disk_used(Path(d)), 0)
            with open(p, "r+b") as f:
                f.write(b"\xff" * (4 << 20))
            self.assertGreater(ai.disk_used(Path(d)), 1 << 20)
            self.assertLess(ai.disk_used(Path(d)), 16 << 30)

    def test_missing_disk(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ai.disk_used(Path(d)), 0)


class TestDriveEarlyExit(unittest.TestCase):
    """drive() 的几个早退分支：不该碰 VM，也不该抛异常。"""

    def vm(self, **kw):
        v = {"name": "t", "vnc_display": 0, "web_port": None, "ssh_port": None}
        v.update(kw)
        return v

    def test_no_vnc_display(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ai.drive(self.vm(), Path(d)), "manual")

    def test_any_exception_is_swallowed(self):
        """自动安装失败就该退回手动，不该把一个 Python 栈打到安装日志里。"""
        with tempfile.TemporaryDirectory() as d:
            # web_port 指向一个立刻接受的假 HTTP 服务 → drive 直接返回 done
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def serve():
                try:
                    c, _ = srv.accept()
                    c.recv(64)
                    c.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                    c.close()
                except OSError:
                    pass
                finally:
                    srv.close()

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            try:
                self.assertEqual(
                    ai.drive(self.vm(vnc_display=100, web_port=port),
                             Path(d), budget_minutes=0, emit=None),
                    "done")
            finally:
                t.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
