#!/usr/bin/env python3
"""vmkit.py 2026-11-11 全局审计的回归测试。

全离线：不起任何虚拟机、不连网、不碰 ~/.agentvm。所有会写文件的地方都把
vmkit 的 VMS / IMAGES 指到临时目录（见 _vmkit）。

测的都是这次审计修掉的真 bug，每条注释里写了「原来的写法 → 什么后果」：
  · check_vm_name 是唯一的名字白名单（原来 install / clone 自己写了一份更宽的）
  · load_vm 对缺 kind / name 的 vm.json 给一句能照着做的话（原来 KeyError 裸栈）
  · vm_broken 认不出的 kind 不再按 linux 判（原来误报「缺 disk.qcow2、seed.iso」）
  · _claim_install_lock 用 O_CREAT|O_EXCL 原子占位（原来检查与落盘之间有窗口）
  · qcow2_backing 读真正的 backing（原来 clone 把源 VM 自己的 overlay 当底册）
  · qcow2_backing_dependents 让 delete 拒绝制造一台起不来的克隆

跑法：make test 或 python3 -I scripts/test-vmkit-audit.py
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


def _load_vmkit():
    """把 vmkit.py 当模块加载。

    它是当脚本跑的（模块名 __main__），从来没有注册成 vmkit——直接
    `import vmkit` 会 ModuleNotFoundError。importlib 按路径加载就绕开了这件事。
    文件末尾有 `if __name__ == "__main__"` 守卫，加载不会触发 main()。
    """
    spec = importlib.util.spec_from_file_location("vmkit_under_test",
                                                 HERE / "vmkit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[attr-defined]
    return mod


vm = _load_vmkit()


class Sandbox(unittest.TestCase):
    """每个测试一个临时的 VMS / IMAGES，跑完就删。

    不设回原值会怎样：这些测试会去动用户真机器上的 ~/.agentvm/vms，而那里面
    可能有正在跑的虚拟机。所以 setUp 里改、tearDown 里一定改回去。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmkit-audit-"))
        self.vms = self.tmp / "vms"
        self.images = self.tmp / "images"
        self.vms.mkdir()
        self.images.mkdir()
        self._old = (vm.VMS, vm.IMAGES)
        vm.VMS = self.vms
        vm.IMAGES = self.images

    def tearDown(self):
        vm.VMS, vm.IMAGES = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 帮手 --

    def write_vm(self, name, **fields):
        d = vm.vm_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"name": name, "kind": "linux", "cpus": 2, "ram": "2G",
               "disk": "32G", "user": "agent", "ssh_port": 20000,
               "vnc_display": 16100}
        rec.update(fields)
        (d / "vm.json").write_text(json.dumps(rec))
        return rec

    def qemu_img(self):
        t = vm.find_tool("qemu-img")
        if not t:
            self.skipTest("没有 qemu-img")
        return t


# ------------------------------------------------------------ 名字白名单 --

class TestVMName(Sandbox):

    def test_install_and_clone_use_the_same_whitelist(self):
        """`install --name 虚拟机` / `clone --new-name 虚拟机` 必须被拒。

        原来这两处自己写 `name.replace("-","").replace("_","").isalnum()`，
        比 check_vm_name 宽：Python 的 isalnum() 对 Unicode 字母数字都为真。
        于是能建出目录 ~/.agentvm/vms/虚拟机，而随后 start/stop/status/delete
        全都在 main() 的 check_vm_name 上 die——一台 list 看得见、delete 删不掉
        的虚拟机，只能手工 rm。
        """
        for bad in ("虚拟机", "vm x", "vm/x", "../etc", "vm\nx", "", "-vm"):
            with self.assertRaises(SystemExit, msg=f"{bad!r} 应该被拒"):
                vm.check_vm_name(bad)

    def test_good_names_still_pass(self):
        for ok in ("linux1", "win-11", "a.b_c-d", "A" * 63):
            vm.check_vm_name(ok)          # 不抛就算过

    def test_64_chars_is_rejected(self):
        """正则上限 63：白名单里 {0,62} 加开头一个正好 63。"""
        with self.assertRaises(SystemExit):
            vm.check_vm_name("a" * 64)


# ------------------------------------------------------- 写一半的 vm.json --

class TestLoadVMRobustness(Sandbox):

    def test_missing_kind_does_not_raise_keyerror(self):
        """缺 kind 的 vm.json 原来让 cmd_open / cmd_window / cmd_stop /
        cmd_disk 直接 KeyError 裸栈——用户只是想看一眼这台机器。现在 load_vm
        补一个空串，下标访问安全了，问题由 vm_broken 报出来。"""
        d = vm.vm_dir("half")
        d.mkdir(parents=True)
        (d / "vm.json").write_text(json.dumps({"name": "half", "cpus": 2}))
        rec = vm.load_vm("half")            # 不该抛
        self.assertEqual(rec["kind"], "")
        self.assertEqual(rec["name"], "half")

    def test_missing_name_falls_back_to_the_directory_name(self):
        d = vm.vm_dir("half")
        d.mkdir(parents=True)
        (d / "vm.json").write_text(json.dumps({"kind": "linux"}))
        self.assertEqual(vm.load_vm("half")["name"], "half")

    def test_existing_values_are_not_overwritten(self):
        """setdefault 的要点：正常文件里的值一个都不能被改。"""
        self.write_vm("good", kind="local")
        rec = vm.load_vm("good")
        self.assertEqual(rec["kind"], "local")
        self.assertEqual(rec["name"], "good")

    def test_non_object_json_is_rejected(self):
        """内容不是一个对象时没法补默认值，只能拒。"""
        d = vm.vm_dir("half")
        d.mkdir(parents=True)
        (d / "vm.json").write_text(json.dumps([1, 2, 3]))
        with self.assertRaises(SystemExit):
            vm.load_vm("half")

    def test_broken_json_is_rejected(self):
        d = vm.vm_dir("half")
        d.mkdir(parents=True)
        (d / "vm.json").write_text("{ 不是 json")
        with self.assertRaises(SystemExit):
            vm.load_vm("half")

    def test_load_vm_does_not_die_so_force_reinstall_still_works(self):
        """这一条钉的是一个真实的倒退：第一版 load_vm 对缺 kind 的文件直接 die，
        而 install --name X --force 自己也要 load_vm——错误消息里推荐的恢复路径
        本身跑不通。所以 load_vm 绝不能 die，只补齐默认值。"""
        d = vm.vm_dir("half")
        d.mkdir(parents=True)
        (d / "vm.json").write_text(json.dumps({"name": "half", "cpus": 2}))
        # _refuse_existing → vm_broken(load_vm(...)) 这条路要能走完
        class A:
            force = False
        try:
            vm._refuse_existing("half", A())
        except SystemExit:
            pass                       # 它会 die（劝你用 --force），但不能 KeyError
        # 而 --force 那条路必须真的走得通：load_vm 不抛
        self.assertEqual(vm.load_vm("half")["kind"], "")

    def test_good_record_loads(self):
        self.write_vm("good")
        self.assertEqual(vm.load_vm("good")["kind"], "linux")


class TestVmBrokenUnknownKind(Sandbox):

    def test_unknown_kind_is_not_judged_as_linux(self):
        """原来 `else: need = ("disk.qcow2", "seed.iso")` 把「未知」和「linux」
        合并了。一台其实是 local 的 VM 只要 kind 字段坏了，就会被判成缺
        disk.qcow2、seed.iso，_refuse_existing 于是劝用户 --force 重装。"""
        broken = vm.vm_broken({"name": "x", "kind": "window"})
        self.assertIn("kind", broken)
        self.assertNotIn("disk.qcow2", broken)

    def test_known_kinds_still_work(self):
        self.assertEqual(vm.vm_broken({"name": "x", "kind": "linux"}),
                         "缺 disk.qcow2、seed.iso")
        self.assertEqual(vm.vm_broken({"name": "x", "kind": "local"}),
                         "缺 data.img")
        self.assertEqual(vm.vm_broken({"name": "x", "kind": "windows"}),
                         "缺 data.img")
        self.assertEqual(vm.vm_broken({"name": "x", "kind": "macos"}),
                         "缺 disk.img")

    def test_no_name_is_not_broken(self):
        self.assertEqual(vm.vm_broken({}), "")


# ------------------------------------------------- install.pid 原子占位 --

class TestInstallLock(Sandbox):

    def test_second_claim_is_refused(self):
        """flock 由内核保证同一时刻只有一个持有者。第一把锁还攥着的时候，
        第二次必须被拒——这正是「连点两次安装」要防的那件事。"""
        vm._claim_install_lock("x")
        with self.assertRaises(SystemExit):
            vm._claim_install_lock("x")

    def test_lock_is_released_when_the_process_dies(self):
        """上一个持有者进程退出时内核自动释放锁，所以「崩了/被强杀了、文件还在」
        天然能接手。用一个肯定不在的 pid 模拟上一个：旧版本留下的「有 pid 没锁」
        文件。"""
        d = vm.vm_dir("x")
        d.mkdir(parents=True)
        (d / "install.pid").write_text("999999999")
        vm._claim_install_lock("x")          # 不该抛

    def test_live_pid_without_lock_is_still_refused(self):
        """旧版本留下的文件里可能写着一个活进程的 pid，而它并不持有 flock
        （它根本没用 flock）。这一句兜底拒绝，免得把一次真在跑的安装覆盖掉。
        用一个真的活着的子进程，不能是自己——写着自己的 pid 时不该拒绝自己。"""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            d = vm.vm_dir("x")
            d.mkdir(parents=True)
            (d / "install.pid").write_text(str(p.pid))
            with self.assertRaises(SystemExit):
                vm._claim_install_lock("x")
        finally:
            p.kill()
            p.wait()

    def test_lock_is_created_0600(self):
        """里面有 pid，而且 install.pid 会被 app 读——不给 0600 的话同机任何
        本地用户都能看到。save_vm 对 vm.json 早就这么做了，同一个理由。"""
        pidf = vm._claim_install_lock("x")
        self.assertEqual(pidf.stat().st_mode & 0o777, 0o600)

    def test_lock_records_our_own_pid(self):
        """app 的「停止」和「接上还在跑的安装」都读这个 pid，它必须是真正干活
        的那个进程。"""
        pidf = vm._claim_install_lock("x")
        self.assertEqual(int(pidf.read_text().strip()), os.getpid())

    def test_concurrent_claims_only_one_wins(self):
        """八个线程同时抢，只有一个能拿到 flock。

        这次修的核心。第一版用的是 O_CREAT|O_EXCL + 「上一个死了就 unlink 重试」，
        实测八个线程有四个赢家：每个输家都读到占位的 0、判定「没在跑」、unlink、
        然后重试——unlink 把别人刚建出来的文件也删了，锁形同虚设。改成 flock 之后
        由内核保证，这一条就是把那个版本钉住的回归测试。
        """
        wins = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                vm._claim_install_lock("race")
                with lock:
                    wins.append(1)
            except SystemExit:
                with lock:
                    wins.append(0)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
        self.assertEqual(sum(wins), 1, f"应有且只有一个赢家，实际 {sum(wins)}")

    def test_set_pid_rewrites_content_without_recreating(self):
        """_set_install_pid 只改内容。重新建文件等于放弃 flock——父进程抄写子
       进程 pid 时走的是这条路。"""
        pidf = vm._claim_install_lock("x")
        before = pidf.stat().st_ino
        vm._set_install_pid(pidf, 4242)
        self.assertEqual(pidf.read_text(), "4242")
        self.assertEqual(pidf.stat().st_ino, before)


# -------------------------------------------------- qcow2 底册与克隆依赖 --

class TestQcow2Backing(Sandbox):

    def make_overlay(self, name, backing):
        """造一个指向 backing 的 qcow2 overlay，返回它的路径。"""
        qemu = self.qemu_img()
        out = vm.vm_dir(name) / "disk.qcow2"
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([qemu, "create", "-f", "qcow2", "-b", str(backing),
                        "-F", "qcow2", str(out), "64M"],
                       check=True, capture_output=True)
        return out

    def test_backing_is_the_real_shared_image(self):
        """这次修的核心：clone 原来把 `vm_dir(src)/"disk.qcow2"` 当底册，也就是
        源虚拟机自己的 overlay，而注释写的是「底册还是同一个云镜像」。删掉源 VM
        （cmd_delete 无条件 _rmtree）之后克隆就指着不存在的文件，QEMU 起不来，
        而克隆的 vm.json 完好、vm_broken 也查不出来。"""
        qemu = self.qemu_img()
        img = self.images / "debian-13.qcow2"
        subprocess.run([qemu, "create", "-f", "qcow2", str(img), "64M"],
                       check=True, capture_output=True)
        overlay = self.make_overlay("src", img)
        self.assertEqual(vm.qcow2_backing(overlay), img.resolve())

    def test_relative_backing_is_resolved_against_the_qcow2_dir(self):
        """qemu-img 存的就是当时 -b 传进去的那个字符串。相对路径按 qcow2 自己
        所在目录解析，不能当绝对路径用。"""
        qemu = self.qemu_img()
        d = vm.vm_dir("src")
        d.mkdir(parents=True)
        img = d / "base.qcow2"
        subprocess.run([qemu, "create", "-f", "qcow2", str(img), "64M"],
                       check=True, capture_output=True)
        out = d / "disk.qcow2"
        subprocess.run([qemu, "create", "-f", "qcow2", "-b", "base.qcow2",
                        "-F", "qcow2", str(out), "64M"],
                       check=True, capture_output=True)
        self.assertEqual(vm.qcow2_backing(out), img.resolve())

    def test_no_backing_returns_none(self):
        qemu = self.qemu_img()
        img = self.images / "plain.qcow2"
        subprocess.run([qemu, "create", "-f", "qcow2", str(img), "64M"],
                       check=True, capture_output=True)
        self.assertIsNone(vm.qcow2_backing(img))

    def test_missing_file_returns_none(self):
        self.assertIsNone(vm.qcow2_backing(self.tmp / "nope.qcow2"))

    def test_dependents_finds_the_clone(self):
        """delete 要知道谁在用它当底册，否则 _rmtree 一删就制造一台
        「配置齐全但永远启动不了」的克隆。"""
        qemu = self.qemu_img()
        img = self.images / "debian-13.qcow2"
        subprocess.run([qemu, "create", "-f", "qcow2", str(img), "64M"],
                       check=True, capture_output=True)
        self.make_overlay("src", img)
        self.make_overlay("clone1", img)
        self.write_vm("src", kind="linux")
        self.write_vm("clone1", kind="linux")
        # clone1 的底册是共享镜像，不是 src 的盘——所以删 src 不该被它挡住
        self.assertEqual(vm.qcow2_backing_dependents(
            vm.load_vm("src")), [])

    def test_dependents_catches_overlay_pointing_at_another_vm(self):
        """旧版 clone 造出来的正是这种：clone 的底册是 src 自己的 overlay。
        这时候删 src 必须被拒。"""
        qemu = self.qemu_img()
        img = self.images / "debian-13.qcow2"
        subprocess.run([qemu, "create", "-f", "qcow2", str(img), "64M"],
                       check=True, capture_output=True)
        src_disk = self.make_overlay("src", img)
        self.make_overlay("legacy-clone", src_disk)
        self.write_vm("src", kind="linux")
        self.write_vm("legacy-clone", kind="linux")
        self.assertEqual(vm.qcow2_backing_dependents(vm.load_vm("src")),
                         ["legacy-clone"])

    def test_raw_disk_vm_has_no_dependents(self):
        """local / windows 的 data.img 是 raw，没有底册，也就没有依赖者。"""
        self.write_vm("fnos", kind="local")
        self.assertEqual(vm.qcow2_backing_dependents(vm.load_vm("fnos")), [])


# ------------------------------------------------------ parse_size 边界 --

class TestParseSize(unittest.TestCase):
    """parse_size 是三条路共用的，而 app 侧那个同名的 macParseSize 这次修了
    溢出 trap。两边语义必须一致（注释里就这么写的），所以顺手钉住边界。"""

    def test_units(self):
        self.assertEqual(vm.parse_size("1K"), 1024)
        self.assertEqual(vm.parse_size("1M"), 1024 * 1024)
        self.assertEqual(vm.parse_size("1G"), 1024 ** 3)
        self.assertEqual(vm.parse_size("1T"), 1024 ** 4)

    def test_gb_is_binary_not_decimal(self):
        """'32G' 按 1024³。按 10⁹ 的话 qemu-img 建出来的盘会小 7%。"""
        self.assertEqual(vm.parse_size("32G"), 32 * 1024 ** 3)

    def test_plain_number_is_bytes(self):
        self.assertEqual(vm.parse_size("1024"), 1024)

    def test_empty_is_rejected_not_zero(self):
        """空串原来会给 0，而 0 字节的盘后面才炸。现在入口就 die。"""
        with self.assertRaises(SystemExit):
            vm.parse_size("")

    def test_zero(self):
        self.assertEqual(vm.parse_size("0"), 0)

    def test_negative_is_rejected_not_silently_zero(self):
        """负数原来会走到后面某个地方才炸，现在入口就 die——一个负字节数的盘
        没有第二种合理解读。"""
        with self.assertRaises(SystemExit):
            vm.parse_size("-1G")

    def test_overflow_does_not_raise(self):
        """app 侧那个同语义的函数原来会 trap（UInt64(Double) 溢出是 SIGTRAP）。
        这边不能跟着犯：必须给个数，不是抛异常。"""
        self.assertGreater(vm.parse_size("99999999999G"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
