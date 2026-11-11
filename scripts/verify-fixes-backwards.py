#!/usr/bin/env python3
"""反向验证：把每一处修复逐个退回原样，确认对应的测试真的会红。

为什么值得花这一遍：测试通过只说明「现在的代码过了现在的测试」，不说明
「这条测试能抓住它声称抓住的那个 bug」。退回原样还红，才证明这条测试真的
焊在那儿。全绿的一条测试可能只是一个永远为真的断言。

2026-11-11 第二轮补进 scripts/agentvm-fetch 的三处。它是 bash 不是 Python，
但机制一样：替换一段原文、跑指定测试、要求红、再恢复。
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VMKIT = HERE / "vmkit.py"
WINARM = HERE / "winarm-vm.py"
FETCH = HERE / "agentvm-fetch"
AI = HERE / "autoinstall.py"
TART = HERE.parent / "backends" / "tart.py"
TEST_CMDS = HERE / "test-vmkit-commands.py"
TEST_FETCH = HERE / "test-agentvm-fetch.py"
TEST_TART = HERE / "test-tart-backend.py"
TEST_AI = HERE / "test-autoinstall.py"

# (说明, 文件, 退回用的替换对, 测试文件, 测试名)
CASES = [
    ("delete 不给 Linux VM 卸 plist",
     VMKIT,
     [("\n    _remove_autostart_plist(name)\n    _rmtree(d)", "\n    _rmtree(d)")],
     TEST_CMDS,
     "TestDeleteAutostart.test_delete_unloads_the_plist_for_a_linux_vm"),

    ("clone 不看附加盘的返回码",
     VMKIT,
     [('                made = r.returncode == 0 and Path(nd["file"]).is_file()\n'
       '        if not made:\n            # 不能带着一份记着不存在文件',
       '                made = r.returncode == 0 and Path(nd["file"]).is_file()\n'
       '        if False:\n            # 不能带着一份记着不存在文件')],
     TEST_CMDS,
     "TestCloneExtraDisk.test_an_extra_disk_that_fails_to_copy_stops_the_clone"),

    ("clone macOS 不写 vm.json",
     VMKIT,
     [("        save_vm(stub)\n        ok(f\"已在 {vm_dir(new)} 写好配置",
       "        ok(f\"已在 {vm_dir(new)} 写好配置")],
     TEST_CMDS,
     "TestCloneMacos.test_cloning_a_macos_vm_writes_a_vm_json"),

    ("Windows snapshot list 不发结构化事件",
     VMKIT,
     [("        if args.action == \"list\":\n            snaps = _winarm_snapshot_list(vm)",
       "        if False and args.action == \"list\":\n            snaps = _winarm_snapshot_list(vm)")],
     TEST_CMDS,
     "TestWindowsSnapshotList.test_windows_snapshot_list_emits_the_shape_the_app_wants"),

    ("configure macos 不同步 vm.json",
     VMKIT,
     [("            save_vm(rec)\n            ok(\"vm.json 里的同名项也一起改了",
       "            ok(\"vm.json 里的同名项也一起改了")],
     TEST_CMDS,
     "TestConfigureMacos.test_configure_macos_also_updates_vm_json"),

    ("stop 不看 SSH 关机的返回码",
     VMKIT,
     [("        if r.returncode == 0:\n            for _ in range(120):",
       "        if True:\n            for _ in range(120):")],
     TEST_CMDS,
     "TestStopSSHShutdown.test_a_failed_ssh_shutdown_is_reported_not_waited_out"),

    ("all_vms 不防「合法 JSON 但不是对象」",
     VMKIT,
     [("            if not isinstance(data, dict):\n                continue\n", "")],
     TEST_CMDS,
     "TestAllVmsHardening.test_a_vm_json_that_is_not_an_object_does_not_kill_the_listing"),

    ("缺字段直接下标",
     VMKIT,
     [('        "RAM_SIZE": str(_vm_field(vm, "ram")),',
       '        "RAM_SIZE": vm["ram"],')],
     TEST_CMDS,
     "TestVmField.test_a_missing_field_gets_a_sentence_not_a_stack_trace"),

    ("_VmWriteLock 不真的加锁",
     VMKIT,
     [("        self.fh = open(self.path, \"w\")\n        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)\n        return self",
       "        self.fh = open(self.path, \"w\")\n        return self")],
     TEST_CMDS,
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
     TEST_CMDS,
     "TestDiskAddLeftover.test_the_leftover_file_error_says_how_to_remove_it"),

    ("winarm 的 status 回退路径会写盘",
     WINARM,
     [("        return int(cfg.vnc_display)", "        return free_vnc_display(cfg)")],
     TEST_CMDS,
     "TestWinarmVncReadOnly.test_status_does_not_write_the_vnc_display_back"),

    ("winarm 的 autostart 不转义路径",
     WINARM,
     [("    q_vm_dir = shlex.quote(str(cfg.vm_dir))", "    q_vm_dir = str(cfg.vm_dir)")],
     TEST_CMDS,
     "TestWinarmAutostartQuoting.test_the_wrapper_quotes_the_vm_dir"),

    # ---- 第二轮：scripts/agentvm-fetch ----
    ("fetch 的 --concurrency 不校验值",
     FETCH,
     [("      case \"$2\" in\n"
       "        ''|*[!0-9]*) die \"--concurrency 要一个正整数，得到：$2\" ;;\n"
       "      esac\n"
       "      [ \"$2\" -ge 1 ] || die \"--concurrency 至少是 1，得到：$2\"\n"
       "      [ \"$2\" -le 64 ] || die \"--concurrency 至多 64（再多 registry 会开始返回 429），得到：$2\"\n"
       "      CONCURRENCY=\"$2\"; shift 2 ;;",
       "      CONCURRENCY=\"$2\"; shift 2 ;;")],
     TEST_FETCH,
     "TestConcurrencyArg.test_a_non_number_is_refused_up_front"),

    ("fetch 把失败的 HTTP 码接成 000000",
     FETCH,
     [("    if ! code=\"$(curl -sL -C - \\\n"
       "        -H \"Authorization: Bearer $tok\" \\\n"
       "        --connect-timeout 20 --max-time 86400 \\\n"
       "        --retry 5 --retry-delay 3 --retry-all-errors \\\n"
       "        -o \"$out\" -w '%{http_code}' \\\n"
       "        \"https://$REG_HOST/v2/$REPO_PATH/blobs/sha256:$digest\" 2>/dev/null)\"; then\n"
       "      [ -n \"$code\" ] || code=000\n"
       "    fi",
       "    code=\"$(curl -sL -C - \\\n"
       "        -H \"Authorization: Bearer $tok\" \\\n"
       "        --connect-timeout 20 --max-time 86400 \\\n"
       "        --retry 5 --retry-delay 3 --retry-all-errors \\\n"
       "        -o \"$out\" -w '%{http_code}' \\\n"
       "        \"https://$REG_HOST/v2/$REPO_PATH/blobs/sha256:$digest\" || echo 000)\"")],
     TEST_FETCH,
     "TestHttpCodeCapture.test_a_failed_curl_reports_000_not_000000"),

    ("fetch 的下载退回串行",
     FETCH,
     [("xargs_rc=0\n"
       "awk 'NF>=3 && $2 != \"\" {print $2, $3}' \"$DEST/blobs.txt\" \\\n"
       "  | xargs -P \"$CONCURRENCY\" -n 2 bash -c 'fetch_one \"$1\" \"$2\"' _ || xargs_rc=$?",
       "while read -r _kind dgs _sz; do\n"
       "  [ -n \"${dgs:-}\" ] || continue\n"
       "  fetch_one \"$dgs\" \"$_sz\" || true\n"
       "done < \"$DEST/blobs.txt\"")],
     TEST_FETCH,
     "TestActuallyParallel.test_four_blobs_download_concurrently"),

    # ---- 第三轮：vmkit / winarm / tart 的启动链路 ----
    ("vm_record 的 ssh_ok 退回完整 SSH",
     VMKIT,
     [('        # 轮询接口上的轻量探测，不跑完整 SSH（见 linux_ssh_banner_ok 的说明）。\n'
       '        rec["ssh_ok"] = linux_ssh_banner_ok(vm)',
       '        rec["ssh_ok"] = linux_ssh_ready(vm, timeout=4)')],
     TEST_CMDS,
     "TestSshBannerProbe.test_vm_record_uses_the_light_probe"),

    ("start_linux 在已运行时裸 return",
     VMKIT,
     [('        return True\n    d = vm_dir(vm["name"])',
       '        return\n    d = vm_dir(vm["name"])')],
     TEST_CMDS,
     "TestStartLinuxReturnContract.test_already_running_returns_true"),

    ("winarm 的 stop 强杀后不等待",
     WINARM,
     [('    for _ in range(50):\n'
       '        if not qemu_running(cfg):\n'
       '            break\n'
       '        time.sleep(0.2)\n'
       '    ok("已强杀")',
       '    ok("已强杀")')],
     TEST_CMDS,
     "TestWinarmStopWaitsForDeath.test_stop_returns_only_after_the_process_is_gone"),

    ("tart 的 wait_ready 退回固定 10 秒",
     TART,
     [("                time.sleep(min(interval, left))\n"
       "                interval = min(interval * 2, 10)",
       "                time.sleep(10)")],
     TEST_TART,
     "TestWaitReadyBackoff.test_the_interval_backs_off_instead_of_sitting_at_ten"),

    ("tart 的 run_vm 把输出丢进 DEVNULL",
     TART,
     [("            stdout=log, stderr=subprocess.STDOUT,",
       "            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,")],
     TEST_TART,
     "TestRunVmKeepsTartsOwnOutput.test_the_log_file_lands_in_the_vm_dir"),

    ("_tart 的 capture=False 失败消息是空的",
     TART,
     [('            if not detail:\n'
       '                # capture=False 时两条流都没被接住，detail 必然是空的。这时不能\n'
       '                # 甩一句空消息——告诉用户 tart 自己的输出已经打到终端上面去了。\n'
       '                detail = ("没有捕获到输出（这条命令的输出直接写在了终端里，"\n'
       '                          "往上面看）" if not capture else "tart 没有输出")\n',
       '')],
     TEST_TART,
     "TestTartCaptureFalse.test_a_failing_command_with_no_capture_still_names_the_problem"),
    # ---- 第四轮：autoinstall 的无人值守稳定性 ----
    ("就绪探测退回每轮一次",
     AI,
     [('                if time.time() >= next_probe:\n'
       '                    next_probe = time.time() + PROBE_EVERY_SECONDS\n'
       '                    if service_ready(vm, timeout=PROBE_TIMEOUT):',
       '                if True:\n'
       '                    if service_ready(vm, timeout=1.0):')],
     TEST_AI,
     "TestProbeRateLimit.test_the_probe_is_rate_limited_not_per_round"),

    ("画面稳不下来时没有熔断",
     AI,
     [('                    if unstable == UNSTABLE_WARN_ROUNDS:\n'
       '                        say(f"画面已经连续 {unstable} 轮在变，还在静默等它稳下来"\n'
       '                            f"（进度条一直动的安装器就是这样，属正常）", "warn")\n'
       '                    if unstable >= UNSTABLE_GIVE_UP:\n'
       '                        save_frame(vdir, acts, got, rfb.w, rfb.h)\n'
       '                        say(f"画面连续 {unstable} 轮没稳过，判断不出该点哪里。"\n'
       '                            f"自动安装停下来，剩下的请您在画面里点完", "warn")\n'
       '                        return "manual"\n'
       '                    continue',
       '                    continue')],
     TEST_AI,
     "TestUnstableScreen.test_a_perpetually_changing_screen_gives_up"),

    ("帧编号不从现有帧后面接",
     AI,
     [('    m = -1\n'
       '    for p in (*vdir.glob(f"auto-*{FRAME_SUFFIX}"), *vdir.glob("auto-*.ppm")):\n'
       '        n = _frame_no(p)\n'
       '        if n < (1 << 30):        # 读不出编号的那些不算\n'
       '            m = max(m, n)\n'
       '    return m + 1',
       '    return 0')],
     TEST_AI,
     "TestNextFrameNo.test_continues_after_the_highest_existing_frame"),

    ("同一帧不去重，照样连存三张",
     AI,
     [('        nonlocal last_saved\n'
       '        if frame is None or frame is last_saved:\n'
       '            return\n'
       '        last_saved = frame\n'
       '        save_frame(vdir, acts, frame, rfb.w, rfb.h)',
       '        save_frame(vdir, acts, frame, rfb.w, rfb.h)')],
     TEST_AI,
     "TestStashSavesEachFrameOnce.test_one_screen_is_saved_once"),
]


def run_one(test_file: Path, test: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(test_file), test],
                          capture_output=True, text=True, cwd=str(HERE))


def main() -> int:
    targets = [VMKIT, WINARM, FETCH, TART, AI]
    backups = {p: p.read_text() for p in targets}
    bad = []
    try:
        for i, (what, path, subs, test_file, test) in enumerate(CASES, 1):
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
                r = run_one(test_file, test)
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
