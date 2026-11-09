#!/usr/bin/env python3
"""agentvm — 一条命令从零搭一个完整的 Linux 虚拟机。

跨平台入口。macOS 走 Tart 后端，Windows on ARM 走 Hyper-V 后端，
上层流程完全一致。原有 scripts/ 下的 bash 流程保留不动，本入口是
它们在同一套抽象下的等价物，不替换 macOS 现有用法。

用法：
    agentvm.py check                  检查宿主机是否满足条件
    agentvm.py install                装虚拟化后端及其依赖
    agentvm.py fetch                  下载并校验基础镜像
    agentvm.py create-vm              造 VM（CoW 增量盘）
    agentvm.py provision              客人机内换 apt 源、装编译工具链
    agentvm.py configure              推送 LLM / 飞书配置（密钥不落地明文）
    agentvm.py autostart              宿主加客人机两层开机自启
    agentvm.py status / logs / stop / delete

每个子命令都可重复执行。
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from backends import BackendError, get_backend  # noqa: E402

AGENTVM_VERSION = "0.2.0"

# guest 侧脚本的查找顺序。guest/ 是新位置，scripts/ 是 macOS 原有位置，
# 两个都认，保证迁移期间 macOS 侧零回归。
GUEST_SCRIPT_CANDIDATES = [
    REPO_ROOT / "guest" / "40-provision-guest.sh",
    REPO_ROOT / "scripts" / "40-provision-guest.sh",
]
PATCH_SCRIPT_CANDIDATES = [
    REPO_ROOT / "guest" / "patch-agent-config.py",
    REPO_ROOT / "scripts" / "lib" / "patch-agent-config.py",
]


# ------------------------------------------------------------------ 配置 --

def _coerce(key: str, value: str, default, where: str):
    """把配置值转成默认值那个类型，转不过就明说哪一行。

    原来是 `int(value) if isinstance(default, int) and value.isdigit() else value`：
    默认值是 int 而用户写了 `vm_memory_mb=abc` 时，isdigit() 为假，字符串原样留下，
    一路流到 create_vm 里的 `int(memory_mb) * 1048576` 才 ValueError 裸栈——
    报错指向一个跟配置毫无关系的内部行。VM_CPU=x 同理。
    """
    if not isinstance(default, int) or isinstance(default, bool):
        return value
    try:
        return int(value)
    except ValueError:
        raise BackendError(
            f"{where}：{key} 要给数字，你给了 {value!r}"
        ) from None


def load_config() -> dict:
    """汇总配置：默认值 < .agentvmrc < 环境变量。

    与原 common.sh 的口径一致：环境变量优先，仓库根的 .agentvmrc 次之。
    """
    cfg: dict = {
        "agentvm_home": str(Path.home() / ".agentvm"),
        "vm_name": "agent",
        "vm_cpu": 2,
        "vm_memory_mb": 1024,
        "vm_disk_gb": 20,
        "backend": None,
        "apt_mirror": "https://mirrors.tuna.tsinghua.edu.cn/debian/",
        "ssh_port": 2222,
        "ssh_user": "agent",
        "secure_boot": True,
        "tart_bin": str(Path.home() / ".local" / "bin" / "tart"),
        "qemu_img": "qemu-img",
    }

    rc = REPO_ROOT / ".agentvmrc"
    if rc.is_file():
        for lineno, line in enumerate(rc.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in cfg:
                cfg[key] = _coerce(key, value, cfg[key], f".agentvmrc 第 {lineno} 行")

    # 环境变量覆盖
    env_map = {
        "AGENTVM_HOME": "agentvm_home",
        "VM_NAME": "vm_name",
        "VM_CPU": "vm_cpu",
        "VM_MEMORY_MB": "vm_memory_mb",
        "AGENTVM_BACKEND": "backend",
        "AGENTVM_APT_MIRROR": "apt_mirror",
        "AGENTVM_SSH_PORT": "ssh_port",
        "AGENTVM_QEMU_IMG": "qemu_img",
        "AGENTVM_TART_BIN": "tart_bin",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            val = os.environ[env_key]
            cfg[cfg_key] = _coerce(cfg_key, val, cfg.get(cfg_key),
                                   f"环境变量 {env_key}")

    return cfg


def find_script(candidates: list[Path]) -> Path:
    for cand in candidates:
        if cand.is_file():
            return cand
    raise BackendError(
        "找不到客人机脚本，已查找：\n  " + "\n  ".join(str(c) for c in candidates)
    )


def info(msg: str) -> None:
    print(f"==> {msg}")


def ok(msg: str) -> None:
    print(f"  ok {msg}")


# ---------------------------------------------------------------- 子命令 --

def cmd_check(args, cfg, backend) -> int:
    """宿主机检查。对应原 00-check.sh，失败要早且响。"""
    fail = 0
    info(f"后端：{backend.name}（{backend.platform_note}）")

    missing = backend.missing_prerequisites()
    for item in missing:
        print(f"  warn {item}")
    if missing:
        fail += len(missing)

    # 磁盘余量。guest 盘是稀疏的，但镜像缓存与安装的 agent 不是。
    try:
        import shutil

        total, used, free = shutil.disk_usage(cfg["agentvm_home"].split(os.sep)[0] or os.sep)
        free_gib = free / (1024 ** 3)
        if free_gib < 10:
            print(f"  warn 仅剩 {free_gib:.1f} GiB 可用空间，建议 15 GiB 以上")
            fail += 1
        else:
            ok(f"可用磁盘 {free_gib:.1f} GiB")
    except OSError:
        pass

    if not backend.available():
        print(f"  warn 后端 {backend.name} 在当前平台不可用")

    print()
    if fail:
        print(f"{fail} 项检查未通过")
        return 1
    ok("宿主机检查全部通过")
    return 0


def cmd_install(args, cfg, backend) -> int:
    backend.install()
    ok("后端安装完成")
    return 0


def cmd_fetch(args, cfg, backend) -> int:
    """下载并校验基础镜像。幂等，可重复执行续传。

    只有 Hyper-V 后端走这条路。macOS 的 Tart 后端恒返回 1，而 --help 写的还是
    「下载并校验基础镜像」——在 Mac 上照文档跑 agentvm.py fetch 看到返回码 1，
    以为失败了。现在返回 0 并说清该去哪，不是错误路径。
    """
    if backend.name != "hyperv":
        info("Tart 后端不用这条，请用 scripts/agentvm-fetch（OCI 逐层下载）")
        return 0
    backend.ensure_base_vhdx()
    ok("基础镜像就绪")
    return 0


def cmd_start(args, cfg, backend) -> int:
    """启动 VM 并等它就绪。

    为什么必须有这条：原来整套流程是 create-vm → provision，而 create_vm 只做
    New-VM/克隆，provision 直接 SSH 连 127.0.0.1:2222——VM 从来没被启动过，
    连接必然被拒。报错是 SSH 连接失败，完全不指向「VM 没启动」这个根因，
    照文档从零搭的用户卡在第二步。Backend.run_vm() 两个后端都实现了，
    只是 args.func 里没有任何子命令调用它。
    """
    name = args.name or cfg["vm_name"]
    backend.run_vm(name)
    try:
        backend.wait_ready(name)
        ok(f"VM '{name}' 已就绪")
    except BackendError as e:
        # 起都起来了但没就绪，别用 traceback 吓人。真实原因通常是客人机还在跑
        # cloud-init（docs/PITFALLS.md 记着首次要 90 秒左右）。
        warn(f"VM '{name}' 启动了但还没就绪：{e}")
        warn("客人机首次启动要 90 秒左右，等一会儿再跑 provision。")
    return 0


def cmd_create_vm(args, cfg, backend) -> int:
    name = args.name or cfg["vm_name"]
    cpu = args.cpu or cfg["vm_cpu"]
    mem = args.memory or cfg["vm_memory_mb"]
    disk = args.disk or cfg["vm_disk_gb"]
    backend.create_vm(name, cpu, mem, disk, base_image=args.from_image)
    ok(f"VM '{name}' 就绪")
    return 0


def cmd_provision(args, cfg, backend) -> int:
    """客人机内 provisioning。

    cloud-init 已在首启时装好编译工具链，这一步是幂等补跑：
    换 apt 源、装包、验证工具链。脚本内容经 stdin 送入，不进命令行参数。
    """
    name = args.name or cfg["vm_name"]
    script = find_script(GUEST_SCRIPT_CANDIDATES)
    info(f"推送并执行 {script.name}")
    out = backend.exec_vm_script(name, str(script))
    if out:
        print(out)
    ok("客人机 provisioning 完成")
    return 0


def _push_secret(backend, name: str, b64: str, dest: str) -> None:
    """把 base64 编码的秘密经 stdin 写进 guest，落地 0600。

    关键点：base64 值走 stdin，不出现在任何命令行参数里。原项目把
    base64 后的值放进 `tart exec` 的 argv，而 base64 只是编码不是加密，
    进程表里仍可还原，所以这里严一档。

    不能用 `sh -s` 收 stdin：那会把整个 stdin 当脚本读完，数据就没法
    同时送达。所以这里用 `sh -c` 收一个固定命令，让 base64 内容独占 stdin。
    """
    cmd = f"umask 077; base64 -d > {dest}; chmod 600 {dest}"
    if backend.name == "tart":
        import subprocess

        proc = subprocess.run(
            [backend.tart, "exec", name, "sh", "-c", cmd],
            input=b64,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise BackendError(f"密钥写入 guest 失败：{(proc.stderr or '').strip()}")
        return

    import subprocess

    proc = subprocess.run(
        backend._ssh_base() + ["sh", "-c", cmd],
        input=b64,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise BackendError(f"密钥写入 guest 失败：{(proc.stderr or '').strip()}")


def cmd_configure(args, cfg, backend) -> int:
    """推送 LLM / 飞书配置。密钥从文件读，经 stdin 传输，落地 0600。

    三条规则，全部来自 PITFALLS 第 1 条的教训：
      1. 密钥不进命令行参数。
      2. 不重敲聊天或 CI 日志里给出的密钥——那些值可能被脱敏层改过前缀，
         文件看着正常，API 只说 invalid credentials。
      3. 传输前后各断言一次字节长度。
    """
    name = args.vm or cfg["vm_name"]
    patch = find_script(PATCH_SCRIPT_CANDIDATES)

    if not (args.llm_key_file or args.feishu_app_id):
        print("错误：至少提供 --llm-key-file 或 --feishu-app-id")
        return 1

    if args.llm_key_file:
        key_path = Path(args.llm_key_file)
        if not key_path.is_file():
            print(f"错误：--llm-key-file 不存在：{key_path}")
            return 1
        key_bytes = key_path.read_bytes()
        if len(key_bytes) < 20:
            print(f"错误：LLM 密钥只有 {len(key_bytes)} 字节，短得不合理")
            return 1
        ok(f"LLM 密钥 {len(key_bytes)} 字节，来自 {key_path}")

    fs_len = 0
    if args.feishu_app_secret_file:
        fs_path = Path(args.feishu_app_secret_file)
        if not fs_path.is_file():
            print(f"错误：--feishu-app-secret-file 不存在：{fs_path}")
            return 1
        fs_bytes = fs_path.read_bytes()
        if len(fs_bytes) < 16:
            print(f"错误：飞书密钥只有 {len(fs_bytes)} 字节，短得不合理")
            return 1
        fs_len = len(fs_bytes)
        ok(f"飞书密钥 {fs_len} 字节")

    backend.wait_ready(name)

    # 经 stdin 推送，宿主导入机进程表不出现任何密钥材料
    if args.llm_key_file:
        info("推送 LLM 密钥进客人机")
        b64 = base64.b64encode(key_bytes).decode()
        _push_secret(backend, name, b64, "/tmp/.llmkey")
        got = backend.exec_vm(name, "wc -c < /tmp/.llmkey").strip()
        if got != str(len(key_bytes)):
            raise BackendError(f"LLM 密钥传输后长度不符：{got} != {len(key_bytes)}")
        ok("LLM 密钥已传输并校验长度")

    if args.feishu_app_secret_file:
        info("推送飞书密钥进客人机")
        b64 = base64.b64encode(fs_bytes).decode()
        _push_secret(backend, name, b64, "/tmp/.fssecret")
        got = backend.exec_vm(name, "wc -c < /tmp/.fssecret").strip()
        if got != str(fs_len):
            raise BackendError(f"飞书密钥传输后长度不符：{got} != {fs_len}")
        ok("飞书密钥已传输并校验长度")

    info("在客人机内应用配置")
    env_lines = [
        f"AGENTVM_LLM_BASE_URL={args.llm_base_url or ''}",
        f"AGENTVM_LLM_MODEL={args.llm_model or ''}",
        f"AGENTVM_FEISHU_APP_ID={args.feishu_app_id or ''}",
        f"AGENTVM_FEISHU_DOMAIN={args.feishu_domain}",
        f"AGENTVM_YOLO={'1' if args.yolo else '0'}",
    ]
    script = Path(patch).read_text(encoding="utf-8")
    combined = "\n".join(env_lines) + "\n" + script
    out = _stdin_run(backend, name, combined)
    if out:
        print(out)
    ok("配置已应用")
    print()
    print("下一步：")
    print(f"  1. 确认 LLM 账户有余额（402 是计费问题，不是配置问题）")
    print(f"  2. 重启客人机服务：agentvm.py exec-vm --name {name} -- sudo systemctl restart hermes-gateway")
    print(f"  3. 看日志：agentvm.py logs --name {name}")
    return 0


def _stdin_run(backend, name: str, script: str) -> str:
    """把整段脚本经 stdin 送进 guest 的 sh -s。

    这是唯一不把内容放进任何命令行的通道。exec_vm_script 从文件读，
    这里直接从字符串来，因为密钥来自内存而非文件。
    """
    if backend.name == "tart":
        import subprocess

        proc = subprocess.run(
            [backend.tart, "exec", name, "sh", "-s"],
            input=script,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise BackendError(f"guest 内执行失败：{(proc.stderr or '').strip()}")
        return (proc.stdout or "").strip()

    # Hyper-V：SSH 的 stdin 通道
    import subprocess

    proc = subprocess.run(
        backend._ssh_base() + ["sh -s"],
        input=script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise BackendError(f"guest 内执行失败：{(proc.stderr or '').strip()}")
    return (proc.stdout or "").strip()


def cmd_autostart(args, cfg, backend) -> int:
    """两层自启。PITFALLS 第 5 条：只配一层，重启后必静默。"""
    name = args.name or cfg["vm_name"]

    if backend.name == "hyperv":
        # Hyper-V 的 VM 由宿主的工作进程托管，不需要保活进程，
        # 任务计划程序只负责开机把 VM 拉起来。
        info(f"创建开机任务 agentvm-{name}")
        backend._ps(
            f"schtasks /Create /TN 'agentvm-{name}' /SC ONSTART /RL HIGHEST /F "
            f"/TR \"powershell -NoProfile -Command Start-VM -Name '{name}'\""
        )
        ok(f"开机任务已创建（管理员权限，开机启动 VM '{name}'）")
    else:
        info("Tart 后端请用 scripts/60-autostart.sh（launchd LaunchAgent）")
        return 1

    # guest 侧：systemd。这一段两个后端通用，因为它在客人机内跑。
    try:
        out = backend.exec_vm(
            name,
            "cd ${HERMES_HOME:-$HOME/.hermes}/hermes-agent 2>/dev/null && "
            "sudo ./venv/bin/hermes gateway install --system --start-now "
            "--start-on-login && sudo systemctl enable hermes-gateway || true",
        )
        if out:
            print(out)
        ok("客人机 systemd 服务已启用")
    except BackendError as exc:
        print(f"  warn 客人机服务未配置：{exc}")

    print()
    print("自启链：")
    print(f"  开机 → 任务计划程序 → Hyper-V 拉起 VM '{name}'")
    print("        → 客人机启动 → systemd hermes-gateway → 飞书")
    return 0


def cmd_status(args, cfg, backend) -> int:
    name = args.name or cfg["vm_name"]
    for vm in backend.list_vms():
        if vm["name"] == name:
            print(f"VM '{name}'：状态 {vm['state']}")
            try:
                out = backend.exec_vm(name, "free -m | head -2; df -h / | tail -1")
                print(out)
            except BackendError:
                print("  （客人机暂不可达，可能仍在启动）")
            return 0
    print(f"VM '{name}' 不存在")
    return 1


def cmd_logs(args, cfg, backend) -> int:
    name = args.name or cfg["vm_name"]
    print(backend.exec_vm(name, "sudo journalctl -u hermes-gateway -n 50 --no-pager"))
    return 0


def cmd_stop(args, cfg, backend) -> int:
    name = args.name or cfg["vm_name"]
    backend.stop_vm(name)
    ok(f"VM '{name}' 已停止")
    return 0


def cmd_delete(args, cfg, backend) -> int:
    name = args.name or cfg["vm_name"]
    backend.delete_vm(name)
    ok(f"VM '{name}' 已删除")
    return 0


def cmd_exec(args, cfg, backend) -> int:
    name = args.name or cfg["vm_name"]
    print(backend.exec_vm(name, " ".join(args.command)))
    return 0


# ------------------------------------------------------------------ 入口 --

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentvm.py",
        description="一条命令从零搭一个完整的 Linux 虚拟机（macOS Tart / Windows Hyper-V）",
    )
    p.add_argument("--version", action="version", version=f"agentvm {AGENTVM_VERSION}")
    p.add_argument("--backend", choices=["tart", "hyperv"], help="强制指定后端，默认按平台选")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_text: str):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--name", help="VM 名，默认取配置 vm_name")
        sp.set_defaults(func=fn)
        return sp

    add("check", cmd_check, "检查宿主机是否满足条件")
    add("install", cmd_install, "装虚拟化后端及其依赖")
    add("fetch", cmd_fetch, "下载并校验基础镜像")
    add("start", cmd_start, "启动 VM 并等它就绪")
    add("create-vm", cmd_create_vm, "造 VM（CoW 增量盘）")
    add("provision", cmd_provision, "客人机内 provisioning")
    add("status", cmd_status, "VM 与客人机服务状态")
    add("logs", cmd_logs, "客人机网关日志")
    add("stop", cmd_stop, "停止 VM")
    add("delete", cmd_delete, "连 VM 带盘删除")

    cv = add("configure", cmd_configure, "推送 LLM / 飞书配置")
    cv.add_argument("--vm", help="VM 名")
    cv.add_argument("--llm-key-file", help="LLM 密钥文件，绝不从命令行给值")
    cv.add_argument("--llm-base-url")
    cv.add_argument("--llm-model")
    cv.add_argument("--feishu-app-id")
    cv.add_argument("--feishu-app-secret-file", help="飞书密钥文件")
    cv.add_argument("--feishu-domain", default="feishu", choices=["feishu", "lark"])
    cv.add_argument("--yolo", action="store_true", help="绕过审批（先读 PITFALLS 第 3 条）")

    au = add("autostart", cmd_autostart, "宿主加客人机两层开机自启")

    ev = sub.add_parser("exec-vm", help="在客人机内执行命令")
    ev.add_argument("--name", help="VM 名")
    ev.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令")
    ev.set_defaults(func=cmd_exec)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if getattr(args, "backend", None):
        cfg["backend"] = args.backend

    try:
        backend = get_backend(cfg.get("backend"), cfg)
        return args.func(args, cfg, backend)
    except BackendError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
