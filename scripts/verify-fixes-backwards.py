#!/usr/bin/env python3
"""反向验证：把每一处修复逐个退回原样，确认对应的测试真的会红。

为什么值得花这一遍：测试通过只说明「现在的代码过了现在的测试」，不说明
「这条测试能抓住它声称抓住的那个 bug」。退回原样还红，才证明这条测试真的
焊在那儿。全绿的一条测试可能只是一个永远为真的断言。
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VMKIT = HERE / "vmkit.py"
WINARM = HERE / "winarm-vm.py"
TEST = HERE / "test-vmkit-commands.py"

# (说明, 文件, 退回用的替换对, 测试名)
CASES = [
    ("delete 不给 Linux VM 卸 plist",
     VMKIT,
     [("\n    _remove_autostart_plist(name)\n    _rmtree(d)", "\n    _rmtree(d)")],
     "TestDeleteAutostart.test_delete_unloads_the_plist_for_a_linux_vm"),

    ("clone 不看附加盘的返回码",
     VMKIT,
     [('                made = r.returncode == 0 and Path(nd["file"]).is_file()\n'
       '        if not made:\n            # 不能带着一份记着不存在文件',
       '                made = r.returncode == 0 and Path(nd["file"]).is_file()\n'
       '        if False:\n            # 不能带着一份记着不存在文件')],
     "TestCloneExtraDisk.test_an_extra_disk_that_fails_to_copy_stops_the_clone"),

    ("clone macOS 不写 vm.json",
     VMKIT,
     [("        save_vm(stub)\n        ok(f\"已在 {vm_dir(new)} 写好配置",
       "        ok(f\"已在 {vm_dir(new)} 写好配置")],
     "TestCloneMacos.test_cloning_a_macos_vm_writes_a_vm_json"),

    ("Windows snapshot list 不发结构化事件",
     VMKIT,
     [("        if args.action == \"list\":\n            snaps = _winarm_snapshot_list(vm)",
       "        if False and args.action == \"list\":\n            snaps = _winarm_snapshot_list(vm)")],
     "TestWindowsSnapshotList.test_windows_snapshot_list_emits_the_shape_the_app_wants"),

    ("configure macos 不同步 vm.json",
     VMKIT,
     [("            save_vm(rec)\n            ok(\"vm.json 里的同名项也一起改了",
       "            ok(\"vm.json 里的同名项也一起改了")],
     "TestConfigureMacos.test_configure_macos_also_updates_vm_json"),

    ("stop 不看 SSH 关机的返回码",
     VMKIT,
     [("        if r.returncode == 0:\n            for _ in range(120):",
       "        if True:\n            for _ in range(120):")],
     "TestStopSSHShutdown.test_a_failed_ssh_shutdown_is_reported_not_waited_out"),

    ("all_vms 不防「合法 JSON 但不是对象」",
     VMKIT,
     [("            if not isinstance(data, dict):\n                continue\n", "")],
     "TestAllVmsHardening.test_a_vm_json_that_is_not_an_object_does_not_kill_the_listing"),

    ("缺字段直接下标",
     VMKIT,
     [('        "RAM_SIZE": str(_vm_field(vm, "ram")),',
       '        "RAM_SIZE": vm["ram"],')],
     "TestVmField.test_a_missing_field_gets_a_sentence_not_a_stack_trace"),

    ("_VmWriteLock 不真的加锁",
     VMKIT,
     [("        self.fh = open(self.path, \"w\")\n        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)\n        return self",
       "        self.fh = open(self.path, \"w\")\n        return self")],
     "TestWriteLock.test_two_configure_processes_do_not_swallow_each_other"),

    ("disk add 的残文件报错是死胡同",
     VMKIT,
     [('            die(f"{target} 已经在了。\\n"\n'
       '                f"如果它是上一次加盘失败留下的残文件（vm.json 里没有这一项），"\n'
       '                f"先删掉它再重来：\\n"\n'
       '                f"  rm -f {target}\\n"\n'
       '                f"想先看看现在登记了哪些附加盘："\n'
       '                f"{_self_cmd()} disk list --name {name}")',
       '            die(f"{target} 已存在，换个虚拟机名或先删掉它")')],
     "TestDiskAddLeftover.test_the_leftover_file_error_says_how_to_remove_it"),

    ("winarm 的 status 回退路径会写盘",
     WINARM,
     [("        return int(cfg.vnc_display)", "        return free_vnc_display(cfg)")],
     "TestWinarmVncReadOnly.test_status_does_not_write_the_vnc_display_back"),

    ("winarm 的 autostart 不转义路径",
     WINARM,
     [("    q_vm_dir = shlex.quote(str(cfg.vm_dir))", "    q_vm_dir = str(cfg.vm_dir)")],
     "TestWinarmAutostartQuoting.test_the_wrapper_quotes_the_vm_dir"),
]


def run_one(test: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(TEST), test],
                          capture_output=True, text=True, cwd=str(HERE))


def main() -> int:
    backups = {p: p.read_text() for p in (VMKIT, WINARM)}
    bad = []
    try:
        for i, (what, path, subs, test) in enumerate(CASES, 1):
            text = backups[path]
            for old, new in subs:
                if old not in text:
                    print(f"{i:2}. {what}\n    退回用的原文在 {path.name} 里找不到，"
                          f"这条没法反向验证：\n    {old[:70]!r}")
                    bad.append(what)
                    break
                text = text.replace(old, new, 1)
            else:
                path.write_text(text)
                r = run_one(test)
                red = r.returncode != 0
                tail = [l for l in (r.stderr or "").splitlines() if l.strip()]
                why = tail[-1][:70] if tail else ""
                print(f"{i:2}. {what}\n    {'红（测试抓住了）' if red else '绿（这条测试是假的！）'}"
                      f"  {why}")
                if not red:
                    bad.append(what)
                path.write_text(backups[path])
    finally:
        for p, text in backups.items():
            p.write_text(text)

    print()
    if bad:
        print(f"有 {len(bad)} 条没焊住：")
        for b in bad:
            print(f"  · {b}")
        return 1
    print(f"{len(CASES)} 条全部反向验证通过：退回原样就红，恢复就绿。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
