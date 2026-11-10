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

    bool 要单独一条：`secure_boot=false` 原来原样返回字符串，而
    `bool("false")` 是 True——用户在 .agentvmrc 里关不掉 Secure Boot，
    改完一点变化都没有，而配置明明是那么写的。
    """
    if isinstance(default, bool):
        low = str(value).strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise BackendError(
            f"{where}：{key} 要给 true/false，你给了 {value!r}"
        ) from None
    if not isinstance(default, int):
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
        # tart run 的三个透传参数，只有 tart 后端用得到。写成字符串而不是
        # 列表：.agentvmrc 和环境变量天生是字符串，_coerce 也只认 bool/int
        # 两种默认值类型，列表会让它原样返回一个 str，反而要到处判类型。
        # 逗号分隔多条：tart_dirs=~/Shared:host,~/Code:code
        #              tart_expose=8080:80,9090:443
        "tart_dirs": "",
        "tart_vnc": "",
        "tart_expose": "",
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
        "AGENTVM_TART_DIRS": "tart_dirs",
        "AGENTVM_TART_VNC": "tart_vnc",
        "AGENTVM_TART_EXPOSE": "tart_expose",
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


def warn(msg: str) -> None:
    print(f"  warn {msg}")


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
    # --dir / --vnc / --expose 只对 tart 后端有意义，Hyper-V 那边没有对应物，
    # 给了也不生效。不报错：用户两边切换着用时不该为一条用不上的参数卡住。
    if backend.name == "tart":
        backend.set_share_options(
            dirs=args.dir, vnc=args.vnc, expose=args.expose)
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
    import subprocess

    if backend.name == "tart":
        argv = [backend.tart, "exec", name, "sh", "-c", cmd]
    else:
        argv = backend._ssh_base() + ["sh", "-c", cmd]
    # timeout 必须给：guest 卡住时这条命令会挂到天荒地老，而 main 只接
    # BackendError 和 KeyboardInterrupt，用户除了 Ctrl-C 没有任何出路。
    try:
        proc = subprocess.run(
            argv,
            input=b64,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"密钥写入 guest 超时（{dest}）") from e
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
    # 配置值不走环境变量传参，也不走命令行：全部以 Python 赋值的形式拼在
    # 脚本开头，随脚本一起经 stdin 进 guest。原来是 `export K=V` 的 sh 写法，
    # 因为整段是经 `sh -s` 喂进去的；解释器换成 python3 之后那几行就是
    # SyntaxError。每一行都要让子进程读到，赋值给 os.environ 正是干这个的。
    env_pairs = [
        ("AGENTVM_LLM_BASE_URL", args.llm_base_url or ""),
        ("AGENTVM_LLM_MODEL", args.llm_model or ""),
        ("AGENTVM_FEISHU_APP_ID", args.feishu_app_id or ""),
        ("AGENTVM_FEISHU_DOMAIN", args.feishu_domain),
        ("AGENTVM_YOLO", "1" if args.yolo else "0"),
    ]
    script = _with_env(Path(patch).read_text(encoding="utf-8"), env_pairs)
    out = _stdin_run(backend, name, script)
    if out:
        print(out)
    ok("配置已应用")

    # 兜底删掉客人机里的明文密钥，并且真的去看一眼。
    # 清理动作原本只存在于 patch 脚本末尾，而那个脚本经 `sh -s` 永远跑不到
    # 最后一行（见 _stdin_run）——guest /tmp 里两份明文密钥永久滞留。
    # 解释器修好之后脚本会自己删，这里再补一刀：patch 中途失败时密钥照样
    # 不留。「删完看一眼」不能省，rm 的退出码只说明命令跑没跑，不说明文件
    # 真的没了。
    info("清理客人机里的明文密钥")
    backend.exec_vm(name, "rm -f /tmp/.llmkey /tmp/.fssecret")
    left = backend.exec_vm(
        name, "ls /tmp/.llmkey /tmp/.fssecret 2>/dev/null | wc -l"
    ).strip()
    if left not in ("0", ""):
        raise BackendError(
            f"客人机里还留着 {left} 个明文密钥文件，请登录 guest 手动删除"
        )
    ok("客人机 /tmp 已无明文密钥")

    print()
    print("下一步：")
    print(f"  1. 确认 LLM 账户有余额（402 是计费问题，不是配置问题）")
    print(f"  2. 重启客人机服务：agentvm.py exec-vm --name {name} -- sudo systemctl restart hermes-gateway")
    print(f"  3. 看日志：agentvm.py logs --name {name}")
    return 0


def _with_env(script: str, pairs: list[tuple[str, str]]) -> str:
    """把配置值以 Python 赋值的形式注入脚本开头。

    值来自命令行参数或配置文件，是任意字符串。用 repr() 而不是手拼引号：
    repr 对换行、引号、反斜杠都给出合法字面量，而手拼的转义漏一个就是一次
    注入——和 hyperv 那边 PowerShell 单引号插值是同一类面。

    插入点必须在 `from __future__ import annotations` 之后：future import
    只准出现在模块最开头（文档串之后），前面多一行 `import os` 就是
    SyntaxError，而 patch-agent-config.py 正带着这一行。
    """
    block = "import os\n" + "".join(
        f"os.environ[{key!r}] = {value!r}\n" for key, value in pairs
    )
    lines = script.splitlines(keepends=True)
    cut = 0
    for i, line in enumerate(lines):
        if line.startswith("from __future__"):
            cut = i + 1
    if not cut:
        return block + "\n" + script
    return "".join(lines[:cut]) + "\n" + block + "\n" + "".join(lines[cut:])


def _stdin_run(backend, name: str, script: str, timeout: int = 300) -> str:
    """把整段脚本经 stdin 送进 guest 的 python3 -。

    这是唯一不把内容放进任何命令行的通道。exec_vm_script 从文件读，
    这里直接从字符串来，因为密钥来自内存而非文件。

    解释器只能是 python3，不能是 sh：喂进来的是 patch-agent-config.py，
    一个 Python 源码文件。原来走 `sh -s`，sh 拿它当 shell 逐行解释，
    第一行 `#!/usr/bin/env python3` 就被当成命令执行，必然失败——而清理
    guest /tmp 里两份明文密钥的代码正在那个跑不起来的脚本末尾，于是密钥
    永久留在客人机里。cmd_configure 里另有兜底删除，但根子在这儿。
    """
    import subprocess

    if backend.name == "tart":
        argv = [backend.tart, "exec", name, "python3", "-"]
    else:
        argv = backend._ssh_base() + ["python3", "-"]
    try:
        proc = subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"guest 内执行 {timeout} 秒超时") from e
    if proc.returncode != 0:
        raise BackendError(f"guest 内执行失败：{(proc.stderr or '').strip()}")
    return (proc.stdout or "").strip()


def cmd_autostart(args, cfg, backend) -> int:
    """两层自启。PITFALLS 第 5 条：只配一层，重启后必静默。"""
    name = args.name or cfg["vm_name"]

    if backend.name == "hyperv":
        # 名字必须过白名单再拼 PowerShell。hyperv.py 里专门写了
        # check_vm_name，因为每一条 PowerShell 语句都是单引号插值——
        # 一个单引号就能闭合字符串接任意代码。而这条路（autostart）
        # 原来没走那道校验，名字可以来自 .agentvmrc 的 vm_name 或环境变量，
        # 用户自己写的配置就能以当前用户权限执行任意命令。
        # check_vm_name 是 hyperv 模块上的函数，不是 backend 的方法。
        from backends.hyperv import check_vm_name as _cvm
        _cvm(name)
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
    # tart 的三个透传参数。action="append" 是为了让 --dir 能重复给；
    # 默认 None（而不是空列表）才能区分「没给」和「给了空」——前者用配置里
    # 的值，后者是显式关掉。
    st = add("start", cmd_start, "启动 VM 并等它就绪")
    st.add_argument("--dir", action="append", default=None,
                    help="共享目录「宿主机路径:标签」，可重复。仅 tart")
    st.add_argument("--vnc", default=None,
                    help="VNC 监听「地址:端口」，如 127.0.0.1:5901。仅 tart")
    st.add_argument("--expose", action="append", default=None,
                    help="端口暴露「宿主端口:客人端口」，可重复。仅 tart")
    cv = add("create-vm", cmd_create_vm, "造 VM（CoW 增量盘）")
    # cmd_create_vm 读 args.cpu / args.memory / args.disk / args.from_image，
    # 而原来这四个参数一个都没注册：在 Mac 上跑 create-vm 就是 AttributeError，
    # 报错指向一个跟用户输入毫无关系的内部行。默认值一律 None，让
    # `args.cpu or cfg["vm_cpu"]` 的回退生效——给 0 会把配置里的值顶掉。
    cv.add_argument("--cpu", type=int, help="CPU 核心数，默认取配置 vm_cpu")
    cv.add_argument("--memory", type=int, help="内存 MB，默认取配置 vm_memory_mb")
    cv.add_argument("--disk", type=int, help="磁盘 GB，默认取配置 vm_disk_gb")
    cv.add_argument("--from-image", dest="from_image",
                    help="基础镜像地址，默认用后端自己的默认源")
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
