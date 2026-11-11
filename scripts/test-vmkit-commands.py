#!/usr/bin/env python3
"""vmkit.py / winarm-vm.py 2026-11-11 命令层审计的回归测试。

全离线：不起任何虚拟机、不连网、不碰 ~/.agentvm 和 ~/Library。会写文件的地方
都把 vmkit 的 VMS / IMAGES / HOME 指到临时目录，winarm 那边把 HOME 指到临时
目录（它的 cmd_autostart 用的是 Path.home()，改 HOME 就够了）。

测的是这次审计修掉的真 bug，每条注释里写了「原来的写法 → 什么后果」：
  · delete 只给 macOS VM 卸开机自启 plist，另外三种删完留一个每 15 秒跑一次
    的 launchd 任务
  · clone 的附加盘复制失败不看返回码，照样 save_vm、照样报「已克隆成 X」
  · clone macOS VM 不写 vm.json，产物是一台 list 看不见的 25 GB 孤儿
  · Windows VM 的 snapshot list 不发结构化事件，app 里永远显示「还没有快照」
  · configure 改 macOS VM 不同步 vm.json，界面显示旧值、登录名给错
  · stop 丢掉 SSH 关机的返回码，失败也干等 120 秒再强杀
  · all_vms 不防「合法 JSON 但不是对象」，一份坏 vm.json 打崩 list/status
  · 缺字段的 vm.json 让 start 甩 KeyError 堆栈而不是一句人话
  · configure / share / disk 是无锁的读-改-写，两个命令同时跑静默丢一个改动
  · disk add 的半成品留下一个自助清理不掉的残文件
  · winarm 的 status 回退路径会写盘，把 restore 刚做的清理撤掉
  · winarm 的 autostart 把路径原样插进要执行的 shell 脚本

跑法：make test 或 python3 -I scripts/test-vmkit-commands.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    """按路径加载一个「当脚本跑」的模块。

    这两个文件从来都是当脚本跑的（模块名 __main__），直接 import 会
    ModuleNotFoundError。importlib 按路径加载绕开这件事，文件末尾都有
    `if __name__ == "__main__"` 守卫，加载不会触发 main()。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[attr-defined]
    return mod


vm = _load("vmkit_cmd_under_test", HERE / "vmkit.py")
wa = _load("winarm_cmd_under_test", HERE / "winarm-vm.py")


def _args(**kw):
    """造一个 argparse.Namespace。缺的字段给 None，和 argparse 的默认值一致。"""
    import argparse
    base = dict(name=None, new_name=None, action=None, tag=None, force=False,
                cpu=None, mem=None, disk=None, user=None, password=None,
                width=None, height=None, size=None, format="raw", bus=None,
                path=None, iso=None, seed=None, ssh_key=None, disable=False)
    base.update(kw)
    return argparse.Namespace(**base)


class Sandbox(unittest.TestCase):
    """每个测试一个临时的 VMS / IMAGES / HOME，跑完就删，并一定改回原值。

    不改回去会怎样：这些测试会去动真机器上的 ~/.agentvm/vms，而那里可能有
    正在跑的虚拟机；winarm 那几个还会往真 ~/Library/LaunchAgents 里写 plist。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmkit-cmd-"))
        self.vms = self.tmp / "vms"
        self.images = self.tmp / "images"
        self.home = self.tmp / "home"
        for d in (self.vms, self.images, self.home):
            d.mkdir(parents=True)
        self._old = (vm.VMS, vm.IMAGES, vm.HOME)
        self._old_home = os.environ.get("HOME")
        vm.VMS, vm.IMAGES, vm.HOME = self.vms, self.images, self.home
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        vm.VMS, vm.IMAGES, vm.HOME = self._old
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 帮手 --

    def write_vm(self, name, **fields):
        d = vm.vm_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"name": name, "kind": "linux", "cpus": 2, "ram": "2G",
               "disk": "32G", "user": "agent", "password": "agentvm",
               "ssh_port": 20000, "rdp_port": None, "vnc_display": 16100,
               "ssh_key": str(self.home / ".agentvm" / "id_ed25519")}
        rec.update(fields)
        (d / "vm.json").write_text(json.dumps(rec))
        return rec

    def capture(self, fn, *a, **kw):
        """跑一个命令，把 stdout 上的 JSON 事件收回来。

        Emit 是一行一个 JSON 写到 stdout，所以重定向 stdout 就能拿到全部事件，
        不用去 patch 内部方法。
        """
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a, **kw)
        events = []
        for line in buf.getvalue().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return rc, events, buf.getvalue()

    def done_event(self, events):
        for ev in events:
            if ev.get("t") == "done":
                return ev
        return None


# --------------------------------------------------------------- delete --

class TestDeleteAutostart(Sandbox):

    def test_delete_unloads_the_plist_for_a_linux_vm(self):
        """Linux VM 的开机自启也要一起卸。

        原来卸 plist 的那段只写在 cmd_delete 的 `elif macvm.json` 分支里，
        有 vm.json 的三种客人走另一条分支，直接 _rmtree 就报「已删除」。
        而 plist 还在 ~/Library/LaunchAgents/ 里且仍是 loaded，它指向的
        wrapper 就在被删掉的 VM 目录里：之后每次登录 launchd 执行一个不存在
        的脚本 → 非零退出 → KeepAlive 加 ThrottleInterval 15 → 每 15 秒重试
        一次，永不停止。用户看到「删了的虚拟机还在每隔一会儿折腾一下」。
        """
        self.write_vm("lin1")
        plist = self.home / "Library" / "LaunchAgents" / "com.agentvm.lin1.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist/>")

        calls = []
        real_run = vm.run
        vm.run = lambda cmd, **kw: (calls.append(cmd),
                                    real_run(["true"], check=False))[1]

        rc, _, _ = self.capture(vm.cmd_delete, _args(name="lin1"))
        vm.run = real_run

        self.assertEqual(rc, 0)
        self.assertFalse(plist.exists(), "plist 没被删掉")
        self.assertTrue(any("bootout" in c for c in calls),
                        "没有发 bootout，launchd 那边还挂着")
        self.assertTrue(any("unload" in c for c in calls),
                        "没有发 unload，老版本 launchd 还挂着")
        self.assertFalse(vm.vm_dir("lin1").exists())


# ---------------------------------------------------------------- clone --

class TestCloneExtraDisk(Sandbox):

    def test_an_extra_disk_that_fails_to_copy_stops_the_clone(self):
        """附加盘没复制成功，克隆必须停下来，不能报成功。

        原来三个命令全部 check=False 且返回码一个都不看，然后无条件
        moved.append(nd)、save_vm(vm)：源盘权限坏了、磁盘满、跨卷失败时
        clone 退出码 0、日志还打「已克隆成 X」，而新 VM 的 vm.json 记着一块
        不存在的盘。启动时 extra_disk_args 只在日志里 warn 一句「文件不在了，
        跳过」，vm_broken 又不检查附加盘——list / status 一路全绿，数据静默消失。
        """
        d = vm.vm_dir("src1")
        d.mkdir(parents=True)
        (d / "disk.qcow2").write_bytes(b"x" * 4096)
        (d / "seed.iso").write_bytes(b"y" * 4096)
        data1 = d / "data1.img"
        data1.write_bytes(b"z" * 4096)
        self.write_vm("src1", disks=[{"id": "data1", "file": str(data1),
                                      "format": "raw", "bus": "virtio-blk",
                                      "size": "64G"}])

        real_run = vm.run
        real_tool = vm.find_tool

        def fake_run(cmd, **kw):
            # 只有附加盘那一两条命令失败，系统盘的 cp -c 照旧「成功」，
            # 这样才走得进附加盘那段。
            if any("data1.img" in str(c) for c in cmd):
                return subprocess.CompletedProcess(cmd, 1, "", "模拟失败")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        # **find_tool 也要挡。** 不挡的话 vm.HOME 被指去临时目录，find_tool 在
        # ~/hb、/opt/homebrew、/usr/local 下都找不到 qemu-img，clone 会在
        # 读底册那一步就 die——测试照样「通过」（assertRaises 等到一个
        # SystemExit、vm.json 也确实没写），但走的完全是另一条路。
        # 第一版正是如此，被反向验证逮到的。
        vm.find_tool = lambda name: f"/fake/{name}"
        vm.run = fake_run
        try:
            with self.assertRaises(SystemExit) as cm:
                vm.cmd_clone(_args(name="src1", new_name="c1"))
        finally:
            vm.run = real_run
            vm.find_tool = real_tool

        # 死因必须真的落在附加盘这一段，不是前半截的某个别的问题上。
        # 第一版正是死在「找不到 qemu-img」上，被反向验证逮到的。
        self.assertEqual(cm.exception.code, 1)
        # 关键：不能留下一份记着不存在文件的 vm.json。
        self.assertFalse(vm.vm_file("c1").exists(),
                         "克隆失败了却写下了 vm.json，新 VM 会「配置齐全但"
                         "永远启动不了」，而 list / status 显示一切正常")


class TestCloneMacos(Sandbox):

    def test_cloning_a_macos_vm_writes_a_vm_json(self):
        """macOS 克隆完要补一份 vm.json。

        原来 vmz relay 成功就 return 0，而 vmkit 的 list / status / start /
        configure 全认 vm.json（install 那条路因此专门补了 _macos_vm_stub）。
        漏了这一手的后果是一台 25 GB 的孤儿：目录里什么都有，list 看不见它，
        status 报「没有这个虚拟机」，只有 delete 因为额外认 macvm.json 还能删。
        """
        d = vm.vm_dir("mac1")
        d.mkdir(parents=True)
        (d / "macvm.json").write_text(json.dumps(
            {"name": "mac1", "cpus": 4, "ram": "8G", "user": "agent"}))
        self.write_vm("mac1", kind="macos", disks=[{"id": "data1",
                                                    "file": str(d / "d.img"),
                                                    "format": "raw"}])

        real_find, real_relay, real_popen = vm.find_vmz, vm._relay, vm.subprocess.Popen
        vm.find_vmz = lambda: Path("/fake/vmz")
        vm._relay = lambda proc, label: 0
        vm.subprocess.Popen = lambda *a, **kw: None
        try:
            rc, _, out = self.capture(vm.cmd_clone,
                                      _args(name="mac1", new_name="mac2"))
        finally:
            vm.find_vmz, vm._relay, vm.subprocess.Popen = (
                real_find, real_relay, real_popen)

        self.assertEqual(rc, 0)
        self.assertTrue(vm.vm_file("mac2").is_file(),
                        "克隆「成功」了却没有 vm.json，这台机器 vmkit 管不了")
        rec = json.loads(vm.vm_file("mac2").read_text())
        self.assertEqual(rec["name"], "mac2")
        self.assertEqual(rec["kind"], "macos")
        # 附加盘不跟过去：vmz 只克隆系统盘，留着源盘的路径等于两台 VM 写同一
        # 块 raw 盘，那比没有更糟。
        self.assertEqual(rec.get("disks"), [])


# ------------------------------------------------------------- snapshot --

class TestWindowsSnapshotList(Sandbox):

    def test_windows_snapshot_list_emits_the_shape_the_app_wants(self):
        """Windows VM 的快照列表要发 `{"t":"done","vm":{"snapshots":[...]}}`。

        原来 windows 分支直接转发给 winarm，而它的 list 只用 print 打人话：
        app 的 handleSnapshots 要求 done 事件且 snapshots 是数组、每项有 tag，
        全部不满足 → state.snapshots 从不更新 → 存储面板永远显示「还没有快照」，
        而 snapshots/base/ 明明在。命令行用户也只拿到两行人话。
        """
        self.write_vm("win1", kind="windows", rdp_port=23389)
        snap = vm.vm_dir("win1") / "snapshots" / "base"
        snap.mkdir(parents=True)
        (snap / "data.img").write_bytes(b"x" * 8192)

        rc, events, out = self.capture(
            vm.cmd_snapshot, _args(name="win1", action="list"))

        self.assertEqual(rc, 0)
        ev = self.done_event(events)
        self.assertIsNotNone(ev, f"没有 done 事件，app 那边一条都收不到：{out}")
        snaps = ev["vm"]["snapshots"]
        self.assertEqual([s["tag"] for s in snaps], ["base"])
        # app 的 compactMap 靠 tag 建 VMSnapshot，缺这一项整条被丢掉。
        self.assertTrue(all(s.get("tag") for s in snaps))


# ------------------------------------------------------- configure macos --

class TestConfigureMacos(Sandbox):

    def test_configure_macos_also_updates_vm_json(self):
        """改完 macvm.json，vm.json 里的同名字段也要改。

        原来只写 macvm.json，而 vm_record 读的是 vm.json 的 cpus / ram / user：
        status 和 app 详情页照旧显示旧值，用户按界面给的登录名去登，登的是一
        个已经不存在的账号。实测 configure --cpu 8 --user bob 之后，macvm.json
        是 8 核 bob，vm.json 还是 4 核 agent。
        """
        d = vm.vm_dir("mac1")
        d.mkdir(parents=True)
        (d / "macvm.json").write_text(json.dumps(
            {"name": "mac1", "cpus": 4, "ram": "8G", "user": "agent",
             "password": "secret-in-macvm", "disk": "80G"}))
        # vm.json 故意不写 password：要验的正是 configure 不会把 macvm.json 里
        # 那个明文密码抄到 0644 的 vm.json 里去。
        (d / "vm.json").write_text(json.dumps(
            {"name": "mac1", "kind": "macos", "cpus": 4, "ram": "8G",
             "user": "agent", "disk": "80G"}))

        rc, _, _ = self.capture(vm.configure_macos, _args(name="mac1", cpu=8,
                                                          user="bob"))
        self.assertEqual(rc, 0)

        rec = json.loads(vm.vm_file("mac1").read_text())
        self.assertEqual(rec["cpus"], 8, "vm.json 里的核数没跟上")
        self.assertEqual(rec["user"], "bob", "vm.json 里的登录名没跟上")
        # 密码故意不跟过去：vm.json 是 0644，macvm.json 是 0600。
        self.assertNotIn("password", rec)


# ----------------------------------------------------------------- stop --

class TestStopSSHShutdown(Sandbox):

    def test_a_failed_ssh_shutdown_is_reported_not_waited_out(self):
        """SSH 关机命令失败要当时就说，不能干等 120 秒再强杀。

        原来 subprocess.run 的返回值整个丢掉了：登录用户没有免密 sudo 时，
        ssh 连得上、探测也过（它跑的是 `true`，不需要 sudo），可关机命令实际
        是失败的。vmkit 无视它照旧轮询 120 秒，再落 ACPI 等 60 秒，最后
        kill -9——每次 stop 固定卡 3 分钟左右，末尾还落一句「优雅关机超时，
        强杀。qcow2 可能留下脏数据」，而真相是第一句命令就没执行成。
        """
        self.write_vm("lin1")

        real_ready = vm.linux_ssh_ready
        real_pid = vm.qemu_pid
        real_run = vm.subprocess.run
        real_shell = vm.run
        real_sleep = vm.time.sleep
        vm.linux_ssh_ready = lambda v, timeout=5: True
        # 前两次说「在跑」（开头的那个检查和 SSH 分支都要过），之后说「没了」：
        # 全程返回 None 会让 cmd_stop 在第一句就退成「VM 没在运行」，
        # 根本走不到 SSH 那段。
        ticks = []

        def fake_pid(v):
            ticks.append(1)
            return 4242 if len(ticks) <= 2 else None
        vm.qemu_pid = fake_pid
        vm.subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 255, "", "agent is not in the sudoers file")
        # **必须挡住 run**：这条路的末尾是真 `kill -9 <pid>`，不挡就是拿测试
        # 里编的 pid 去杀机器上真进程。
        vm.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
        slept = []
        vm.time.sleep = lambda s: slept.append(s)
        try:
            rc, _, out = self.capture(vm.cmd_stop, _args(name="lin1"))
        finally:
            vm.linux_ssh_ready = real_ready
            vm.qemu_pid = real_pid
            vm.subprocess.run = real_run
            vm.run = real_shell
            vm.time.sleep = real_sleep

        self.assertIn("sudoers", out, "失败原因没有告诉用户")
        self.assertLess(len(slept), 5,
                        f"还是进了那个干等循环（sleep 了 {len(slept)} 次）")
        self.assertNotIn("已优雅关机", out)


# ------------------------------------------------------------- all_vms --

class TestAllVmsHardening(Sandbox):

    def test_a_vm_json_that_is_not_an_object_does_not_kill_the_listing(self):
        """一份内容为 `[]` 的 vm.json 不能让 list / status 整条崩掉。

        原来 all_vms 只捕 JSONDecodeError，而 read_text 的 OSError 和「合法
        JSON 但不是对象」都能直接打穿：实测一份内容为 `[]` 的 vm.json 让
        `vmkit list` 甩 AttributeError，stdout 上一个事件都没有，而其他 VM
        都好的——用户想用 list 定位是哪台坏了，偏偏是 list 先死。
        """
        self.write_vm("good1")
        bad = vm.vm_dir("bad1")
        bad.mkdir(parents=True)
        (bad / "vm.json").write_text("[]")

        vms = vm.all_vms()
        self.assertEqual([v["name"] for v in vms], ["good1"])

        rc, events, out = self.capture(vm.cmd_list)
        self.assertEqual(rc, 0)
        # list 发的是 {"t":"vms"} 这一个事件，原来一份坏 vm.json 会让它
        # 连这个都发不出来（AttributeError 直接穿出去）。
        listing = [e for e in events if e.get("t") == "vms"]
        self.assertTrue(listing, f"list 没发出 vms 事件：{out}")
        self.assertEqual([v["name"] for v in listing[0]["vms"]], ["good1"])

    def test_a_vm_json_without_a_name_gets_the_directory_name(self):
        """缺 name 的 vm.json 用目录名补上，和其他命令的兜底一致。"""
        d = vm.vm_dir("noname")
        d.mkdir(parents=True)
        (d / "vm.json").write_text(json.dumps({"kind": "linux"}))
        self.assertEqual([v["name"] for v in vm.all_vms()], ["noname"])


# ------------------------------------------------------------ vm_field --

class TestVmField(Sandbox):

    def test_a_missing_field_gets_a_sentence_not_a_stack_trace(self):
        """缺字段要给一句能照着做的话。

        load_vm 只给 name 和 kind 补默认值，而 vm.json 是会被写一半的（手改、
        旧版本、别的入口）。原来 winarm_env / linux_ssh_ready /
        linux_qemu_args 里直接下标，`vmkit start --name win11` 遇到缺 ram 的
        文件甩一段 KeyError 到用户脸上，而用户只是想启动一台机器。
        """
        rec = self.write_vm("win1", kind="windows")
        del rec["ram"]
        with self.assertRaises(SystemExit):
            vm.winarm_env(rec, True)

    def test_a_complete_record_still_works(self):
        """字段齐的时候一条都不许误报。"""
        rec = self.write_vm("win1", kind="windows", rdp_port=23389)
        env = vm.winarm_env(rec, True)
        self.assertEqual(env["RAM_SIZE"], "2G")
        self.assertEqual(env["SSH_PORT"], "20000")
        self.assertEqual(env["HEADLESS"], "1")


# ------------------------------------------------------------ write lock --

class TestWriteLock(Sandbox):

    def test_the_lock_actually_serializes(self):
        """_VmWriteLock 要真的挡住第二个进来的人。

        configure / share / disk 都是「读 vm.json → 改 → 整份覆写」，
        _save_vm_impl 是原子 replace 但替换掉的是整份文件、不做合并：两个命令
        同时跑，后写的那个把前一个的改动整份吞掉。实测两条 configure 并发
        （一个改 cpu、一个改 mem），两条都输出「已改」、退出码 0，最终 vm.json
        里 cpu 那一项没了，而用户已经被告知改好了。
        """
        name = "lin1"
        got = []
        holder_ready = threading.Event()
        release = threading.Event()

        def hold():
            with vm._VmWriteLock(name):
                holder_ready.set()
                release.wait(5)

        def waiter():
            holder_ready.wait(5)
            t0 = time.monotonic()
            with vm._VmWriteLock(name):
                got.append(time.monotonic() - t0)

        t1 = threading.Thread(target=hold)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        time.sleep(0.3)
        release.set()
        t1.join(5)
        t2.join(5)

        self.assertTrue(got, "第二个拿锁的线程没有拿到锁")
        self.assertGreaterEqual(got[0], 0.2,
                                f"第二个只等了 {got[0]:.3f} 秒，锁没起作用")

    def test_two_configure_processes_do_not_swallow_each_other(self):
        """端到端：两条 configure 真进程并发，两个改动都要活下来。

        上面那条只证明锁能挡。这一条跑真进程，证明挡完之后两份 vm.json 的
        改动真的都在——也就是用户被告知「改好了」的那两件事都还在。
        """
        self.write_vm("lin1")
        loader = (
            "import sys, importlib.util;"
            "from pathlib import Path;"
            f"spec=importlib.util.spec_from_file_location('v',{str(HERE / 'vmkit.py')!r});"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            f"m.VMS=Path({str(self.vms)!r});m.IMAGES=Path({str(self.images)!r});"
            "sys.argv=['vmkit.py']+sys.argv[1:];"
            "raise SystemExit(m.main())"
        )
        env = dict(os.environ)
        procs = [
            subprocess.Popen([sys.executable, "-I", "-c", loader, "configure",
                              "--name", "lin1", "--cpu", "8"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, env=env),
            subprocess.Popen([sys.executable, "-I", "-c", loader, "configure",
                              "--name", "lin1", "--mem", "6G"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, env=env),
        ]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, f"configure 失败了：{err}")

        rec = json.loads(vm.vm_file("lin1").read_text())
        self.assertEqual(rec["cpus"], 8, "cpu 那一项被并发的另一条吞掉了")
        self.assertEqual(rec["ram"], "6G", "内存那一项被并发的另一条吞掉了")


# --------------------------------------------------------------- disk add --

class TestDiskAddLeftover(Sandbox):

    def test_the_leftover_file_error_says_how_to_remove_it(self):
        """残文件的报错要指一条能走的路。

        原来那句话是「换个虚拟机名或先删掉它」——文件就在这台 VM 的目录里，
        换虚拟机名一点用没有；而 disk remove 只认 vm.json 里登记过的项，报
        「没有叫 data1 的附加盘。现有的：（一个都没有）」。上一次加盘中途
        死掉就会留下这种残文件，用户只能自己 rm。
        """
        self.write_vm("lin1")
        leftover = vm.vm_dir("lin1") / "data1.img"
        leftover.write_bytes(b"x" * 4096)

        with self.assertRaises(SystemExit) as cm:
            vm.cmd_disk(_args(name="lin1", action="add", size="64G"))
        # die() 把人话写到 stdout，测试里从事件里捞。
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                vm.cmd_disk(_args(name="lin1", action="add", size="64G"))
            except SystemExit:
                pass
        out = buf.getvalue()
        self.assertIn("rm -f", out, "报错里没有给出清理办法")
        self.assertIn(str(leftover), out)
        self.assertEqual(cm.exception.code, 1)

    def test_a_dangling_symlink_is_not_written_through(self):
        """悬空软链也要挡住。

        exists() 对悬空软链返回假，接着 open(target,"wb") 会顺着它写到那个
        别处去——那可能是用户机器上任何一个路径。
        """
        self.write_vm("lin1")
        elsewhere = self.tmp / "elsewhere.bin"
        elsewhere.write_bytes(b"original")
        target = vm.vm_dir("lin1") / "data1.img"
        target.symlink_to(elsewhere)

        with self.assertRaises(SystemExit):
            vm.cmd_disk(_args(name="lin1", action="add", size="64G"))
        self.assertEqual(elsewhere.read_bytes(), b"original",
                         "顺着悬空软链写到别处去了")


# ---------------------------------------------------------------- winarm --

class TestWinarmVncReadOnly(Sandbox):
    """winarm 的只读路径不能写盘。"""

    def _cfg(self, **env):
        base = dict(VM_DIR=str(self.tmp / "vm"), ISO_PATH="/tmp/x.iso",
                    VIRTIO_PATH="/tmp/virtio")
        base.update(env)
        old = {k: os.environ.get(k) for k in base}
        os.environ.update(base)
        try:
            return wa.Cfg()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_status_does_not_write_the_vnc_display_back(self):
        """没有 vnc.display 时退回配置里那个号，不重新探。

        原来回退去调 free_vnc_display()，而它是会写盘的（它自己的注释就写着
        写盘是为了让 status 报的和 QEMU 实际监听的一致）。于是 snapshot restore
        刚删掉的 vnc.display——它注释里写明「留着会让 status 报一个已经不监听
        的端口」——被下一次只读的 status 又写回来，用户照 status 给的端口连
        上去什么都没有。一次查询把一次清理撤掉了。
        """
        cfg = self._cfg()
        cfg.vm_dir.mkdir(parents=True, exist_ok=True)
        self.assertFalse(cfg.vnc_display_file.exists())
        got = wa.recorded_vnc_display(cfg)
        self.assertEqual(got, int(cfg.vnc_display))
        self.assertFalse(cfg.vnc_display_file.exists(),
                         "一次只读查询把 vnc.display 写回盘上了")

    def test_a_recorded_display_is_still_returned(self):
        """有记录时照旧返回记录值——这条原来的行为就是对的，别改坏。"""
        cfg = self._cfg()
        cfg.vm_dir.mkdir(parents=True, exist_ok=True)
        cfg.vnc_display_file.write_text("16107")
        self.assertEqual(wa.recorded_vnc_display(cfg), 16107)


class TestWinarmAutostartQuoting(Sandbox):

    def test_the_wrapper_quotes_the_vm_dir(self):
        """wrapper 里的路径要转义。

        原来三个插值点全部原样写进 wrapper，而 wrapper 是被 launchd 当
        /bin/sh 脚本执行的：vm_dir 里带一个双引号就能把后面的 export 行闭合并
        接上任意命令，每次登录都会跑一遍。危害仅限命令行自伤（只有本机用户
        自己能传 --vm-dir），但同一个仓库的 vmkit.cmd_autostart 对这一类是
        挡的，所以这是漏挡，不是取舍。
        """
        nasty = str(self.tmp / 'x"; touch ~/PWNED; echo "')
        cfg_vm = Path(nasty)
        cfg_vm.mkdir(parents=True, exist_ok=True)
        # cmd_autostart 开头要确认 VM 已经建好（data.img 和 efi-vars.fd 都在）。
        (cfg_vm / "data.img").write_bytes(b"x" * 4096)
        (cfg_vm / "efi-vars.fd").write_bytes(b"x" * 4096)
        old = {k: os.environ.get(k) for k in ("VM_DIR",)}
        os.environ["VM_DIR"] = nasty
        try:
            cfg = wa.Cfg()
            real_run = wa.run
            wa.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
            try:
                wa.cmd_autostart(cfg)
            finally:
                wa.run = real_run
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        wrapper = Path.home() / ".winarm" / "winarm-launchd.sh"
        self.assertTrue(wrapper.is_file(), "wrapper 没写出来")
        text = wrapper.read_text()
        # 转义之后，注入的那半个引号不会原样出现在行首位置。
        self.assertNotIn(f'export VM_DIR="{nasty}"', text,
                         "vm_dir 原样插进去了，引号能被闭合")
        self.assertIn("touch", text)      # 内容还在，但已经被引号包住
        for line in text.splitlines():
            if line.startswith("export VM_DIR="):
                import shlex
                self.assertEqual(
                    shlex.split(line.split("=", 1)[1])[0], nasty,
                    "这一行按 shell 规则切出来的不是原始 vm_dir，说明没转义")


# ----------------------------------------------------------------- prune --

class TestPrune(Sandbox):
    """prune 找的是「vm.json 里没登记、又不是过程文件」的大文件。"""

    def _vm_with_files(self):
        self.write_vm("d1")
        d = vm.vm_dir("d1")
        (d / "disk.qcow2").write_bytes(b"x" * (2 << 20))    # 有主
        (d / "seed.iso").write_bytes(b"y" * (2 << 20))       # 有主
        (d / "data9.img").write_bytes(b"z" * (3 << 20))      # 孤儿
        (d / "auto-0.ppm").write_bytes(b"p" * (2 << 20))     # 过程文件
        (d / "qemu.log").write_bytes(b"l" * (2 << 20))       # 日志
        (d / "tiny.img").write_bytes(b"t" * 4096)            # 太小
        return d

    def test_prune_reports_the_orphan_but_keeps_everything(self):
        """默认只报不删。

        删文件不可逆，而这个命令的依据是一份清单、清单就可能漏，所以要删必须
        显式 --apply。
        """
        d = self._vm_with_files()
        rc, events, out = self.capture(vm.cmd_prune,
                                       _args(apply=False, min_size="1M"))
        self.assertEqual(rc, 0)
        self.assertIn("data9.img", out)
        self.assertNotIn("disk.qcow2", out, "有主的系统盘被报成孤儿了")
        self.assertNotIn("seed.iso", out, "有主的种子盘被报成孤儿了")
        self.assertNotIn("auto-0.ppm", out, "安装过程文件被报成孤儿了")
        self.assertNotIn("qemu.log", out, "日志被报成孤儿了")
        self.assertNotIn("tiny.img", out, "小于门槛的也被报了")
        self.assertTrue((d / "data9.img").exists(), "没加 --apply 就删了")

    def test_prune_apply_deletes_only_the_orphan(self):
        d = self._vm_with_files()
        rc, _, out = self.capture(vm.cmd_prune,
                                  _args(apply=True, min_size="1M"))
        self.assertEqual(rc, 0)
        self.assertFalse((d / "data9.img").exists(), "孤儿没删掉")
        self.assertTrue((d / "disk.qcow2").is_file(), "有主的盘被删了")
        self.assertTrue((d / "seed.iso").is_file(), "有主的种子盘被删了")
        self.assertTrue((d / "auto-0.ppm").is_file(), "过程文件被删了")

    def test_prune_refuses_to_delete_a_running_vm(self):
        """VM 在跑的时候一律不删：QEMU 正拿着那个文件写。"""
        d = self._vm_with_files()
        real_pid = vm.qemu_pid
        vm.qemu_pid = lambda v: 4242
        try:
            rc, _, out = self.capture(vm.cmd_prune,
                                      _args(apply=True, min_size="1M"))
        finally:
            vm.qemu_pid = real_pid
        self.assertEqual(rc, 0)
        self.assertTrue((d / "data9.img").exists(),
                        "VM 在跑还是删了，等于把盘从 QEMU 脚下抽走")
        self.assertIn("正在运行", out)

    def test_prune_on_a_registered_extra_disk_keeps_it(self):
        """登记过的附加盘不能被当成孤儿——这正是清单漏列的后果。"""
        self.write_vm("d1", disks=[{"id": "data1",
                                    "file": str(vm.vm_dir("d1") / "data1.img"),
                                    "format": "raw"}])
        d = vm.vm_dir("d1")
        (d / "data1.img").write_bytes(b"x" * (3 << 20))
        rc, _, out = self.capture(vm.cmd_prune,
                                  _args(apply=True, min_size="1M"))
        self.assertEqual(rc, 0)
        self.assertTrue((d / "data1.img").is_file(),
                        "登记过的附加盘被 prune 删了")


class TestPruneCLI(Sandbox):
    """走 main() 的那一层：参数怎么落到子命令上。"""

    def test_prune_without_a_name_scans_every_vm(self):
        """`vmkit.py prune` 不给 --name 要扫所有 VM。

        第一版加上子命令之后，真跑 `vmkit.py prune` 死在「prune 需要 --name」：
        main() 里那个统一的 --name 检查排在分派之前，而 prune 和 status 一样是
        「不给名字就看全部」的——那正是它最主要的用法（「磁盘被什么占了」）。
        2026-11-11 实测踩到后补上这条。
        """
        self.write_vm("d1")
        d = vm.vm_dir("d1")
        (d / "data9.img").write_bytes(b"z" * (3 << 20))
        old = sys.argv
        sys.argv = ["vmkit.py", "prune"]
        try:
            rc, _, out = self.capture(vm.main)
        finally:
            sys.argv = old
        self.assertEqual(rc, 0)
        self.assertIn("data9.img", out)
        self.assertTrue((d / "data9.img").exists(), "没加 --apply 就删了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
