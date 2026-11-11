#!/usr/bin/env python3
"""backends/tart.py 2026-11-11 启动/安装链路审计的回归测试。

全离线：不装、不起、不连任何真 tart。wait_ready 用假的 exec_vm 和一个计数用的
sleep；_tart 的 capture=False 用一个退出码非零的假 tart 脚本。

测的是这次审计修掉的两件事：
  · wait_ready 原来是固定 sleep(10) 且全程零输出。tart exec 挂住时 240 秒的
    预算实际会变成 240 + 24x20 = 720 秒，用户等了十二分钟才看到超时；而且等待
    期间一个字的进度都没有。现在 1、2、4、8 封顶 10 的退避，每轮打一行。
  · run_vm 原来把 tart 的 stdout/stderr 全指到 DEVNULL。tart 起不来时用户干等
    240 秒，最后只等到「客人机在 240 秒内未就绪」——把 tart 的失败说成客人机
    没就绪。现在 tart.log 落在 VM 目录，超时时尾部附在错误里。
  · _tart 的 clone 原来把 tart 的进度条捕获后整个丢掉，10 到 15 分钟零输出。
    现在 capture=False 让 tart 自己写终端。

跑法：make test 或 python3 -I scripts/test-tart-backend.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # 让 `backends` 这个包可导入


def _load_backend():
    from backends.tart import TartBackend
    return TartBackend


class TartCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tart-backend-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.TartBackend = _load_backend()

    def wait_for_tart(self, name: str, secs: float = 5.0) -> None:
        """等 run_vm 起的那个 tart 退出。

        run_vm 是异步的（tart run 前台阻塞，所以它是后台起的），起手就返回——
        不等等看的话，日志文件还是空的，而空文件既可能是「输出被丢了」也可能是
        「还没写」。两条都要区分。
        """
        pidfile = self.tmp / "home" / "vms" / name / "tart.pid"
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            return
        deadline = time.time() + secs
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except OSError:
                return

    def backend(self, **cfg):
        base = {"agentvm_home": str(self.tmp / "home"),
                "tart_bin": str(self.tmp / "tart")}
        base.update(cfg)
        return self.TartBackend(base)

    def virtual_clock(self):
        """把 time.time / time.sleep 换成一套虚拟时钟。

        为什么需要：wait_ready 的循环条件是 `while time.time() < deadline`。只把
        sleep 换成空操作的话，循环会按真实时钟空转满整个 timeout——第一次写这个
        测试时 timeout=40 就真的烧了 40 秒 CPU，而断言的东西一秒都用不到。
        现在虚拟时钟跟着 sleep 前进，几毫秒跑完一个 40 秒的等待。

        返回 (slept, restore)。slept 是实际请求过的睡眠时长列表。
        """
        slept: list[float] = []
        clock = {"t": 1000.0}
        real_time, real_sleep = time.time, time.sleep

        def fake_time():
            return clock["t"]

        def fake_sleep(s):
            slept.append(s)
            clock["t"] += s

        time.time, time.sleep = fake_time, fake_sleep

        def restore():
            time.time, time.sleep = real_time, real_sleep

        self.addCleanup(restore)
        return slept


class TestWaitReadyBackoff(TartCase):
    """等客人机就绪的节奏：原来固定 10 秒、全程零输出。"""

    def test_the_interval_backs_off_instead_of_sitting_at_ten(self):
        from backends.base import BackendError
        b = self.backend()
        b.exec_vm = lambda *a, **k: (_ for _ in ()).throw(BackendError("nope"))

        slept = self.virtual_clock()
        with self.assertRaises(BackendError):
            b.wait_ready("x", timeout=40)

        # 1、2、4、8 然后封顶 10。原来这里恒是 10。
        self.assertEqual(slept[:4], [1, 2, 4, 8],
                         f"退避不对：{slept[:8]}")
        self.assertLessEqual(max(slept), 10, "封顶值不该超过 10 秒")

    def test_it_prints_progress_while_waiting(self):
        """等待期间必须有输出，否则用户分不清「在等」和「卡死」。"""
        import io
        import contextlib
        from backends.base import BackendError
        b = self.backend()
        b.exec_vm = lambda *a, **k: (_ for _ in ()).throw(BackendError("nope"))

        self.virtual_clock()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(BackendError):
                b.wait_ready("x", timeout=10)

        self.assertIn("等客人机就绪", buf.getvalue(),
                      "等待期间一个字的进度都没有")

    def test_a_ready_guest_returns_without_waiting(self):
        b = self.backend()
        b.exec_vm = lambda *a, **k: "ok"
        b.wait_ready("x", timeout=5)      # 不该抛，也不该睡


class TestRunVmKeepsTartsOwnOutput(TartCase):
    """run_vm 原来把 tart 的输出全丢进 DEVNULL。"""

    def _fake_tart(self, body: str):
        p = self.tmp / "tart"
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
        return p

    def test_the_log_file_lands_in_the_vm_dir(self):
        self._fake_tart("echo 'booting vm' >&2; exit 0")
        b = self.backend()
        b.run_vm("myvm")
        self.wait_for_tart("myvm")
        logf = self.tmp / "home" / "vms" / "myvm" / "tart.log"
        self.assertTrue(logf.is_file(),
                        "tart 的输出没有落到 VM 目录下的 tart.log")
        # 空文件不算：DEVNULL 那个写法也会留下一个创建了但没内容的文件，
        # 只断言 is_file() 会漏掉它。必须真有字节进去。
        self.assertGreater(logf.stat().st_size, 0,
                           "tart.log 是空的——tart 的输出还是被丢了")
        b.stop_vm("myvm")

    def test_the_timeout_error_carries_tarts_own_log_tail(self):
        """起不来时不能只说「未就绪」——tart 自己早就退了，原因在它的输出里。"""
        self._fake_tart("echo 'entitlement missing' >&2; exit 3")
        b = self.backend()
        b.run_vm("broken")
        self.wait_for_tart("broken")

        from backends.base import BackendError
        self.virtual_clock()
        with self.assertRaises(BackendError) as cm:
            b.wait_ready("broken", timeout=3)
        msg = str(cm.exception)
        self.assertIn("tart.log", msg,
                      f"错误里没有 tart 自己的日志：{msg!r}")
        self.assertIn("entitlement missing", msg,
                      f"tart 的真实失败原因没被带出来：{msg!r}")

    def test_stop_closes_the_log_handle(self):
        """句柄不关就是泄漏：父进程一直持有一份。"""
        self._fake_tart("exit 0")
        b = self.backend()
        b.run_vm("leak")
        self.assertIn("leak", b._run_logs)
        b.stop_vm("leak")
        self.assertNotIn("leak", b._run_logs,
                         "stop 之后日志句柄还开着")


class TestTartCaptureFalse(TartCase):
    """clone 原来把 tart 的进度条捕获后丢掉，十几分钟零输出。"""

    def _fake_tart(self, body: str):
        p = self.tmp / "tart"
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
        return p

    def test_a_failing_command_with_no_capture_still_names_the_problem(self):
        """capture=False 时两条流都没接住，错误消息不能是空的。"""
        self._fake_tart("echo 'clone failed: registry unreachable' >&2; exit 1")
        b = self.backend()
        from backends.base import BackendError
        with self.assertRaises(BackendError) as cm:
            b._tart("clone", "src", "dst", "--insecure", capture=False)
        msg = str(cm.exception)
        self.assertTrue(msg.strip(), "capture=False 的失败消息是空的")
        self.assertIn("上面看", msg,
                      f"没告诉用户 tart 的输出已经打到终端上了：{msg!r}")

    def test_a_failing_command_with_capture_still_reports_stderr(self):
        """默认路径的行为不能改坏。"""
        self._fake_tart("echo 'boom' >&2; exit 1")
        b = self.backend()
        from backends.base import BackendError
        with self.assertRaises(BackendError) as cm:
            b._tart("list")
        self.assertIn("boom", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
