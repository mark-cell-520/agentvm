"""Tart 后端（macOS，Apple Silicon）。

这是原有行为在新抽象层下的包装，逻辑与 scripts/30-create-vm.sh、
60-autostart.sh 中的 tart 调用保持一致。保留它的目的是让上层流程
不必区分平台，同时让 macOS 侧行为可回归对比。

此路径保持与仓库原有脚本相同的语义，未做功能改动。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Backend, BackendError

DEFAULT_TART = str(Path.home() / ".local" / "bin" / "tart")


class TartBackend(Backend):
    name = "tart"
    platform_note = "macOS，Tart 加 Virtualization.framework，arm64 原生客人机"

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.tart = cfg.get("tart_bin") or DEFAULT_TART
        self.home = Path(cfg.get("agentvm_home") or Path.home() / ".agentvm")
        self.registry_port = int(cfg.get("local_registry_port") or 5000)
        self.repo_name = cfg.get("local_repo_name") or "debian"
        self.source = cfg.get("image_source") or (
            f"127.0.0.1:{self.registry_port}/{self.repo_name}:latest"
        )

    # ------------------------------------------------------------- 探测 --

    @classmethod
    def available(cls) -> bool:
        import platform
        import shutil

        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return False
        return shutil.which("tart") is not None or Path(DEFAULT_TART).exists()

    def missing_prerequisites(self) -> list[str]:
        import platform

        missing: list[str] = []
        if platform.system() != "Darwin":
            return [f"当前平台不是 macOS（{platform.system()}），Tart 后端不可用"]
        if platform.machine() != "arm64":
            missing.append("Tart 后端需要 Apple 芯片")
        if not Path(self.tart).exists():
            missing.append(f"找不到 tart：{self.tart}。请运行 scripts/10-install-tart.sh")
        return missing

    def install(self) -> None:
        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "10-install-tart.sh"
        if not script.exists():
            raise BackendError(f"找不到安装脚本：{script}")
        proc = subprocess.run(["bash", str(script)], text=True)
        if proc.returncode != 0:
            raise BackendError(f"Tart 安装失败，退出码 {proc.returncode}")

    # ------------------------------------------------------------ tart 封装 --

    def _tart(self, *args: str, check: bool = True, stdin: str | None = None) -> str:
        if not Path(self.tart).exists():
            raise BackendError(f"tart 不存在：{self.tart}，请先 install")
        proc = subprocess.run(
            [self.tart, *args],
            capture_output=True,
            text=True,
            input=stdin,
        )
        if check and proc.returncode != 0:
            raise BackendError(
                f"tart {' '.join(args)} 失败（退出码 {proc.returncode}）："
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return (proc.stdout or "").strip()

    # -------------------------------------------------------- VM 生命周期 --

    def vm_exists(self, name: str) -> bool:
        out = self._tart("list")
        return any(line.split()[1:2] == [name] for line in out.splitlines() if len(line.split()) > 1)

    def list_vms(self) -> list[dict]:
        out = self._tart("list")
        vms = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                vms.append({"name": parts[1], "state": parts[2], "cpu": None, "memory_mb": None})
        return vms

    def create_vm(self, name, cpu, memory_mb, disk_gb, base_image=None) -> None:
        if self.vm_exists(name):
            print(f"  ok VM '{name}' 已存在，跳过克隆")
            return
        source = base_image or self.source
        print(f"==> 克隆 {source} -> {name}")
        # --insecure 是必须的：Tart 拒绝明文 HTTP 注册表
        self._tart("clone", source, name, "--insecure", "--concurrency", "16")
        self._tart("set", name, "--cpu", str(cpu), "--memory", str(memory_mb))

    def run_vm(self, name: str) -> None:
        # tart run 是前台阻塞的，而且不能加 timeout：agentvm.py 的 start 会
        # 在这一行等到天荒地老，后面的 wait_ready 和「已就绪」永远到不了，
        # 用户只能 Ctrl-C。所以改成后台起，把 tart 的 pid 交给调用方。
        # check=False 是刻意的：起不来由 wait_ready 那步去报告，这里抢跑
        # 报错只会给出一个和实际状态脱节的退出码。
        import subprocess
        from pathlib import Path

        proc = subprocess.Popen(
            [self.tart, "run", name, "--no-graphics"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        pidfile = self.vms_dir / name / "tart.pid"
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(str(proc.pid))
        except OSError:
            # 写不了 pid 文件不影响 VM 本身在跑，只是以后 stop 要多问一次
            pass

    def stop_vm(self, name: str) -> None:
        self._tart("stop", name, check=False)

    def delete_vm(self, name: str) -> None:
        self._tart("delete", name, check=False)

    # -------------------------------------------------------- guest 内执行 --

    def exec_vm(self, name: str, command: str, timeout: int = 300) -> str:
        # 注意 tart exec 的语法是 tart exec <name> <command>...，
        # 中间的 -- 不会被剥掉，会当成要执行的二进制名。见 PITFALLS 第 2 条。
        if not Path(self.tart).exists():
            raise BackendError(f"tart 不存在：{self.tart}")
        # TimeoutExpired 要转成 BackendError。agentvm.py 的 main 只接
        # BackendError 和 KeyboardInterrupt，超时会以完整 traceback 的形式砸到
        # 用户脸上——而客人机卡住时超时恰恰是最常见的那种失败。
        try:
            proc = subprocess.run(
                [self.tart, "exec", name, "sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"guest 内命令 {timeout} 秒超时：{command[:200]}"
            ) from e
        if proc.returncode != 0:
            raise BackendError(
                f"guest 内命令失败（退出码 {proc.returncode}）：{(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").strip()

    def exec_vm_script(self, name: str, script_path: str, timeout: int = 600) -> str:
        script = Path(script_path).read_text(encoding="utf-8")
        if not Path(self.tart).exists():
            raise BackendError(f"tart 不存在：{self.tart}")
        try:
            proc = subprocess.run(
                [self.tart, "exec", name, "sh", "-s"],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"guest 内脚本 {timeout} 秒超时（{Path(script_path).name}）"
            ) from e
        if proc.returncode != 0:
            raise BackendError(
                f"guest 内脚本失败（退出码 {proc.returncode}）：{(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").strip()

    def wait_ready(self, name: str, timeout: int = 240) -> None:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.exec_vm(name, "true", timeout=20)
                return
            except BackendError:
                time.sleep(10)
        raise BackendError(f"客人机在 {timeout} 秒内未就绪")
