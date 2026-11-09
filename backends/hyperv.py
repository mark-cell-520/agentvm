"""Hyper-V 后端（Windows on ARM）。

全部 Windows 侧操作经 PowerShell 完成。本文件在 macOS 上可以安全 import：
模块级不执行任何 Windows 命令，只有方法被调用时才检查平台。

验证状态：本文件在 macOS 上开发，未经任何 Windows on ARM 目标机验证。
每个方法的文档字符串标注了验证状态。未验证的方法在首次真实运行前
不应被视为可用，详见 docs/WINDOWS-PORT.md 第 10 节。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .base import Backend, BackendError

# ------------------------------------------------------------------ 常量 --

# Debian 官方 cloud image。arm64 对应 Windows on ARM 的原生客人机架构。
DEFAULT_IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/"
    "debian-12-genericcloud-arm64.qcow2"
)
DEFAULT_IMAGE_SHA_URL = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/SHA512SUMS"
)

# cloud-init NoCloud 数据源的卷标，写错 guest 就找不到配置
CIDATA_LABEL = "cidata"

# 宿主转发端口，exec_vm 的 SSH 端点
DEFAULT_SSH_PORT = 2222

# guest 首启就绪等待上限。PITFALLS 第 8 条：tart run 返回后客人机还要
# 约 90 秒，Hyper-V 侧同理，Start-VM 返回不等于 SSH 可连。
GUEST_BOOT_TIMEOUT = 240


# VM 名的白名单。所有 PowerShell 语句都是 f-string 单引号插值
# （`New-VM -Name '{name}'`），一个单引号就能闭合字符串、后面接任意
# PowerShell 代码、以当前用户权限执行：
#   agentvm.py create-vm --name "x'; Remove-Item -Recurse C:\Users; '"
# 这在 macOS 上碰不到（后端直接拒绝运行），但在 Windows 上是实打实的注入。
# 与其给二十处插值逐个转义（漏一处就前功尽弃），不如在入口挡住。
_VM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def check_vm_name(name: str) -> str:
    if not _VM_NAME_RE.match(name or ""):
        raise BackendError(
            f"VM 名不合法：{name!r}。只能用字母、数字、点、下划线、连字符，"
            f"字母或数字开头，最长 63 字符。"
        )
    return name


class HyperVBackend(Backend):
    name = "hyperv"
    platform_note = "Windows on ARM，Hyper-V 虚拟化，ARM64 原生客人机"

    def __init__(self, cfg=None):
        # cfg 是 dict，键见 agentvm.py 的 load_config。None 时用默认值，
        # 便于单独 import 本类做单元测试。
        cfg = cfg or {}
        self.home = Path(cfg.get("agentvm_home") or Path.home() / ".agentvm")
        self.base_dir = self.home / "base"
        self.vms_dir = self.home / "vms"
        self.keys_dir = self.home / "keys"
        self.ssh_key = self.keys_dir / "agentvm_ed25519"
        self.ssh_port = int(cfg.get("ssh_port") or DEFAULT_SSH_PORT)
        self.ssh_user = cfg.get("ssh_user") or "agent"
        self.image_url = cfg.get("image_url") or DEFAULT_IMAGE_URL
        self.image_sha_url = cfg.get("image_sha_url") or DEFAULT_IMAGE_SHA_URL
        self.apt_mirror = cfg.get(
            "apt_mirror", "https://mirrors.tuna.tsinghua.edu.cn/debian/"
        )
        self.secure_boot = bool(cfg.get("secure_boot", True))
        self.qemu_img = cfg.get("qemu_img") or "qemu-img"

    # ------------------------------------------------------- PowerShell 封装 --

    def _ps(self, script: str, check: bool = True, timeout: int = 300) -> str:
        """执行一段 PowerShell，返回 stdout。

        -STA 是必须的：IMAPI2 的 COM 对象只在单线程单元里工作，
        默认的 MTA 模式下造 ISO 会失败，且报错不指向根因。
        """
        if os.name != "nt":
            raise BackendError(
                "Hyper-V 后端只能在 Windows 上运行，当前平台是 "
                f"{os.name}。在 macOS 上请用 tart 后端。"
            )
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise BackendError(
                "找不到 powershell.exe。Windows on ARM 应自带 PowerShell，"
                "若被精简掉请从微软官方渠道恢复。"
            )
        try:
            proc = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-STA",
                 "-Command", script],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            # 原来让 TimeoutExpired 直接往外抛，而 agentvm.py 的 main 只接
            # BackendError 和 KeyboardInterrupt——用户看到的是完整 traceback。
            raise BackendError(
                f"PowerShell {timeout} 秒超时：{script.strip()[:200]}"
            ) from e
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise BackendError(f"PowerShell 执行失败（退出码 {proc.returncode}）：{err}")
        return (proc.stdout or "").strip()

    def _ps_json(self, script: str) -> object:
        """执行 PowerShell 并把 stdout 按 JSON 解析。

        PowerShell 侧统一用 ConvertTo-Json -Compress 输出，避免表格格式
        的本地化表头把解析搞乱。
        """
        out = self._ps(f"{script} | ConvertTo-Json -Compress -Depth 4")
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise BackendError(f"PowerShell 输出不是合法 JSON：{exc}\n原始输出：{out[:400]}")

    # ------------------------------------------------------------- 探测 --

    @classmethod
    def available(cls) -> bool:
        if os.name != "nt":
            return False
        return bool(shutil.which("powershell"))

    def missing_prerequisites(self) -> list[str]:
        """返回缺失项。空列表表示齐备。

        每一项是「缺什么 + 怎么补」，让用户在一步之内看懂，而不是在
        后面的步骤里撞到 Hyper-V 的原始报错。
        """
        missing: list[str] = []
        if os.name != "nt":
            return [f"当前平台不是 Windows（{os.name}），Hyper-V 后端不可用"]

        # Hyper-V 组件。Get-WindowsOptionalFeature 需要管理员，
        # 非管理员运行时静默跳过，由 install() 负责提权。
        try:
            feat = self._ps_json(
                "Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V "
                "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty State"
            )
            if feat is not None and str(feat).lower() != "enabled":
                missing.append(
                    "Hyper-V 组件未启用。以管理员身份运行："
                    "Enable-WindowsOptionalFeature -Online -FeatureName "
                    "Microsoft-Hyper-V -All -NoRestart，然后重启。"
                    "（或运行 agentvm install）"
                )
        except BackendError:
            # 非管理员或组件查询不可用，不当成缺失，交给 install 报
            pass

        # qemu-img 只承担离线格式转换，不是虚拟化后端
        if not self.have(self.qemu_img):
            missing.append(
                f"找不到 {self.qemu_img}。镜像需要从 qcow2 转成 VHDX，"
                "请安装 QEMU for Windows（仅用其命令行工具，不用于运行 VM）："
                "https://qemu.weilnetz.de/w64/"
            )

        # Windows 自带 OpenSSH 客户端，exec_vm 依赖它
        if not self.have("ssh") or not self.have("ssh-keygen"):
            missing.append(
                "找不到 ssh 或 ssh-keygen。请安装 OpenSSH 客户端："
                "设置 → 应用 → 可选功能 → 添加功能 → OpenSSH 客户端"
            )

        return missing

    # ------------------------------------------------------------- 安装 --

    def install(self) -> None:
        """启用 Hyper-V 组件，校验转换工具与 SSH 客户端。

        Hyper-V 的启用必须管理员权限。这里不尝试静默提权，而是检测当前
        是否已提权并给出明确指令，避免 UAC 弹窗在无人值守时挂住。
        """
        if os.name != "nt":
            raise BackendError("Hyper-V 后端只能在 Windows 上安装")

        admin = self._ps_json(
            "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]"
            "::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
        )
        if not admin:
            raise BackendError(
                "启用 Hyper-V 需要管理员权限。请以管理员身份重新运行：\n"
                "  右键开始菜单 → Windows PowerShell（管理员）→ agentvm install"
            )

        state = self._ps_json(
            "Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V "
            "| Select-Object -ExpandProperty State"
        )
        if str(state).lower() == "enabled":
            print("  ok Hyper-V 组件已启用")
        else:
            print("==> 正在启用 Hyper-V 组件")
            self._ps(
                "Enable-WindowsOptionalFeature -Online -FeatureName "
                "Microsoft-Hyper-V -All -NoRestart"
            )
            print("  ok Hyper-V 组件已启用，需要重启后生效")

        if not self.have(self.qemu_img):
            raise BackendError(
                f"找不到 {self.qemu_img}，无法把 qcow2 转成 Hyper-V 认识的 VHDX。\n"
                "请安装 QEMU for Windows（只用它的命令行转换工具，VM 仍由 Hyper-V 跑）：\n"
                "  https://qemu.weilnetz.de/w64/\n"
                "装完把 qemu-img.exe 所在目录加进 PATH，或设 AGENTVM_QEMU_IMG 指向它。"
            )
        print(f"  ok 找到转换工具 {self.qemu_img}")

        if not (self.have("ssh") and self.have("ssh-keygen")):
            raise BackendError(
                "找不到 ssh / ssh-keygen。exec_vm 走 SSH 通道，请安装 "
                "OpenSSH 客户端（设置 → 应用 → 可选功能 → OpenSSH 客户端）。"
            )
        print("  ok 找到 OpenSSH 客户端")

    # --------------------------------------------------------- 镜像下载 --

    def _download(self, url: str, dest: Path, resume: bool = True) -> None:
        """下载到 dest，支持 Range 续传。

        断点续传是必需的：Debian cloud 镜像数百 MB，慢网下一次下不完，
        而重下整个文件是最常见的浪费。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = dest.stat().st_size if dest.exists() else 0
        headers = {"User-Agent": "agentvm/0.2"}
        mode = "wb"
        if have and resume:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            if have and resume and resp.status == 206:
                total += have
            elif have and resume and resp.status == 200:
                # 服务端不支持 Range，重新开始
                have = 0
                mode = "wb"
            with open(dest, mode) as fh:
                read = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    read += len(chunk)
        if total and dest.stat().st_size != total:
            raise BackendError(
                f"下载不完整：{dest} 现有 {dest.stat().st_size} 字节，"
                f"期望 {total} 字节。重跑可续传。"
            )

    def _sha512(self, path: Path) -> str:
        import hashlib

        h = hashlib.sha512()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def ensure_base_vhdx(self) -> Path:
        """确保父盘 VHDX 就位，返回其路径。

        流程：下载 qcow2 → 校验 SHA512 → qemu-img 转 VHDX。
        三步各自幂等，重复调用只做缺失的那几步。
        """
        self.base_dir.mkdir(parents=True, exist_ok=True)
        qcow2 = self.base_dir / Path(self.image_url).name
        vhdx = qcow2.with_suffix(".vhdx")

        if not qcow2.exists():
            print(f"==> 下载基础镜像 {self.image_url}")
            self._download(self.image_url, qcow2)
            print(f"  ok 已下载 {qcow2.name}（{qcow2.stat().st_size / 1e6:.0f} MB）")
        else:
            print(f"  ok 基础镜像已存在 {qcow2.name}")

        # SHA512 校验。Debian 官方发布 SHA512SUMS，内容寻址本身就是最强的
        # 完整性证明，比任何附带 checksum 都可靠。
        try:
            self._download(self.image_sha_url, self.base_dir / "SHA512SUMS")
            want = ""
            for line in (self.base_dir / "SHA512SUMS").read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == qcow2.name:
                    want = parts[0]
                    break
            if want:
                got = self._sha512(qcow2)
                if got != want:
                    raise BackendError(
                        f"镜像校验失败：{qcow2.name} 的 SHA512 与官方发布值不符。\n"
                        f"  期望 {want}\n  实际 {got}\n"
                        "文件可能在传输中被截断或篡改，请删除后重下。"
                    )
                print("  ok 镜像 SHA512 与官方发布值一致")
            else:
                print("  warn SHA512SUMS 里没有该文件条目，跳过校验")
        except BackendError:
            raise
        except Exception as exc:  # 网络失败不该阻断已有镜像的使用
            print(f"  warn 无法获取官方校验值（{exc}），跳过校验")

        if not vhdx.exists():
            print(f"==> 转换 qcow2 → VHDX（用 {self.qemu_img}，仅离线转换）")
            proc = subprocess.run(
                [self.qemu_img, "convert", "-O", "vhdx", str(qcow2), str(vhdx)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 or not vhdx.exists():
                raise BackendError(
                    f"qemu-img 转换失败：{(proc.stderr or proc.stdout).strip()}\n"
                    "请确认安装的是 QEMU for Windows 且 qemu-img.exe 在 PATH 中。"
                )
            print(f"  ok 父盘就绪 {vhdx.name}（{vhdx.stat().st_size / 1e6:.0f} MB）")
        return vhdx

    # ------------------------------------------------------------ SSH 通道 --

    def ensure_ssh_key(self) -> Path:
        """确保持有 ed25519 密钥对，返回私钥路径。

        私钥落在 keys_dir，权限由 Windows ACL 控制。公钥经 cloud-init
        注入 guest，全程不经过命令行参数。
        """
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        if self.ssh_key.exists():
            return self.ssh_key
        print(f"==> 生成 SSH 密钥 {self.ssh_key}")
        proc = subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(self.ssh_key),
                "-N",
                "",
                "-C",
                "agentvm",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise BackendError(f"ssh-keygen 失败：{(proc.stderr or '').strip()}")
        pub = Path(str(self.ssh_key) + ".pub")
        if not pub.exists():
            raise BackendError("ssh-keygen 未生成公钥文件")
        print(f"  ok 公钥 {pub}")
        return self.ssh_key

    def _pubkey(self) -> str:
        pub = Path(str(self.ssh_key) + ".pub")
        return pub.read_text().strip().split()[1] if pub.exists() else ""

    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.ssh_key),
            "-p",
            str(self.ssh_port),
            "-o",
            "StrictHostKeyChecking=no",
            # 沙箱内 VM 的威胁模型下关掉 known_hosts 是刻意的取舍：
            # VM 重建后 host key 必然变化，硬校验会让自动化每次卡住。
            "-o",
            "UserKnownHostsFile=NUL",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=10",
            f"{self.ssh_user}@127.0.0.1",
        ]

    def _vm_ip(self, name: str) -> str | None:
        """读取 guest 的 IPv4 地址。

        依赖 guest 集成服务上报。Hyper-V 的 Default Switch 用 DHCP，
        宿主重启后地址可能变，所以每次 exec 前都重新读，不缓存。
        """
        check_vm_name(name)
        out = self._ps(
            f"Get-VM -Name '{name}' -ErrorAction SilentlyContinue | "
            "Get-VMNetworkAdapter | Select-Object -ExpandProperty IPAddresses"
        )
        for cand in out.split():
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", cand):
                return cand
        return None

    def _ensure_nat(self, name: str) -> None:
        """确保 NAT 静态映射存在，且指向 guest 当前 IP。

        Default Switch 的 IP 不稳定（坑位 13），所以映射要可重入：
        地址变了就删旧建新。
        """
        check_vm_name(name)
        ip = self._vm_ip(name)
        if not ip:
            return  # guest 还没拿到地址，等 wait_ready 再试

        exists = self._ps(
            "Get-NetNatStaticMapping -ErrorAction SilentlyContinue "
            f"| Where-Object {{$_.ExternalPort -eq {self.ssh_port}}} "
            "| Select-Object -First 1 -ExpandProperty InternalIPAddress"
        )
        if exists and exists.strip() == ip:
            return
        if exists:
            self._ps(
                "Get-NetNatStaticMapping -ErrorAction SilentlyContinue "
                f"| Where-Object {{$_.ExternalPort -eq {self.ssh_port}}} "
                "| Remove-NetNatStaticMapping -Confirm:$false"
            )
        # ExternalIPAddress 0.0.0.0 表示监听宿主所有地址
        self._ps(
            "Get-NetNat -ErrorAction SilentlyContinue | Select-Object -First 1 "
            f"-ExpandProperty Name | ForEach-Object {{ Add-NetNatStaticMapping "
            f"-NatName $_ -ExternalIPAddress '0.0.0.0' -InternalIPAddress '{ip}' "
            f"-InternalPort 22 -ExternalPort {self.ssh_port} -Protocol TCP }}"
        )

    def wait_ready(self, name: str, timeout: int = GUEST_BOOT_TIMEOUT) -> None:
        """轮询等 guest 就绪。

        先等 VM 进入 Running，再等 SSH 端口可连。两步都要等：
        Hyper-V 的 Start-VM 返回时客人机内核还在解压。
        """
        check_vm_name(name)
        deadline = time.time() + timeout
        running = False
        while time.time() < deadline:
            state = self._ps(
                f"(Get-VM -Name '{name}' -ErrorAction SilentlyContinue).State"
            )
            if state.strip().isdigit() and int(state.strip()) == 2:  # 2 = Running
                running = True
                break
            time.sleep(5)
        if not running:
            raise BackendError(
                f"VM '{name}' 在 {timeout} 秒内未进入 Running 状态。"
                "请用 Get-VM 查看状态，并检查 Hyper-V 事件日志。"
            )

        print("==> 等待客人机启动（约 1 到 2 分钟）")
        last_err = ""
        while time.time() < deadline:
            try:
                self._ensure_nat(name)
                proc = subprocess.run(
                    self._ssh_base() + ["true"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    print("  ok 客人机已就绪")
                    return
                last_err = (proc.stderr or "").strip()[:200]
            except subprocess.TimeoutExpired:
                last_err = "SSH 连接超时"
            time.sleep(10)
        raise BackendError(
            f"客人机在 {timeout} 秒内未就绪。最后一次错误：{last_err}\n"
            "常见原因：cloud-init 未注入公钥、guest 未拿到 IP、"
            "NAT 映射端口被占用。"
        )

    # -------------------------------------------------------- VM 生命周期 --

    def vm_exists(self, name: str) -> bool:
        check_vm_name(name)
        out = self._ps(
            f"@(Get-VM -Name '{name}' -ErrorAction SilentlyContinue).Count"
        )
        return out.strip().isdigit() and int(out.strip()) > 0

    def list_vms(self) -> list[dict]:
        data = self._ps_json(
            "Get-VM -ErrorAction SilentlyContinue | "
            "Select-Object Name, State, ProcessorCount, MemoryStartup"
        )
        if not data:
            return []
        if isinstance(data, dict):
            data = [data]
        return [
            {
                "name": vm.get("Name"),
                "state": str(vm.get("State")),
                "cpu": vm.get("ProcessorCount"),
                "memory_mb": int((vm.get("MemoryStartup") or 0) / 1048576),
            }
            for vm in data
        ]

    def _write_seed(self, name: str) -> Path:
        """造 cloud-init NoCloud 种子盘，返回 ISO 路径。

        Windows 没有 genisoimage 也没有 oscdimg，用 IMAPI2（Windows 原生
        COM 组件）造 ISO，不引入额外依赖。-STA 模式是必需的，见 _ps。

        未验证：IMAPI2 在 Windows on ARM 上的可用性未经目标机确认。
        """
        vm_dir = self.vms_dir / name
        vm_dir.mkdir(parents=True, exist_ok=True)
        iso = vm_dir / "seed.iso"
        stage = vm_dir / "seed-src"
        stage.mkdir(exist_ok=True)

        pubkey = self._pubkey()
        if not pubkey:
            raise BackendError("没有公钥，先调 ensure_ssh_key()")

        (stage / "meta-data").write_text(
            f"instance-id: {name}-001\nlocal-hostname: {name}\n",
            encoding="utf-8",
        )
        # packages 与 runcmd 承接原 40-provision-guest.sh 的职责，首启一次完成
        (stage / "user-data").write_text(
            "#cloud-config\n"
            f"hostname: {name}\n"
            "users:\n"
            f"  - name: {self.ssh_user}\n"
            "    shell: /bin/bash\n"
            '    sudo: "ALL=(ALL) NOPASSWD:ALL"\n'
            "    lock_passwd: true\n"
            "    ssh_authorized_keys:\n"
            f"      - {pubkey}\n"
            "ssh_pwauth: false\n"
            "package_update: true\n"
            "packages:\n"
            "  - build-essential\n"
            "  - python3-dev\n"
            "  - libffi-dev\n"
            "  - curl\n"
            "  - ca-certificates\n"
            "runcmd:\n"
            f"  - sed -i 's|deb.debian.org/debian|{self.apt_mirror}|g' /etc/apt/sources.list\n"
            "  - apt-get update\n",
            encoding="utf-8",
        )

        if iso.exists():
            iso.unlink()

        ps = f"""
$ErrorActionPreference = 'Stop'
$image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$image.ChooseImage()
$image.VolumeName = '{CIDATA_LABEL}'
$image.FileSystemsToCreate = 7
$image.Root.AddTree('{stage}', $false)
$stream = $image.CreateResultImage()
$target = [System.IO.File]::Open('{iso}', 'Create')
try {{ $stream.Save($target) }} finally {{ $target.Close() }}
"""
        self._ps(ps)
        if not iso.exists() or iso.stat().st_size == 0:
            raise BackendError(
                "IMAPI2 造 ISO 失败，产物为空。IMAPI2 在 Windows on ARM 上的"
                "可用性未经验证，若确认不可用需改用其他造 ISO 途径。"
            )
        print(f"  ok 种子盘 {iso.name}（卷标 {CIDATA_LABEL}）")
        return iso

    def create_vm(
        self,
        name: str,
        cpu: int,
        memory_mb: int,
        disk_gb: int,
        base_image: str | None = None,
    ) -> None:
        """建 VM：父盘 → differencing disk → Gen2 VM → 挂种子盘 → NAT 映射。

        幂等：VM 已存在则跳过创建，只校正资源规格。
        """
        check_vm_name(name)
        if base_image:
            self.image_url = base_image
        self.ensure_ssh_key()
        parent = self.ensure_base_vhdx()
        if not parent.exists():
            raise BackendError(f"父盘不存在：{parent}")

        if self.vm_exists(name):
            print(f"  ok VM '{name}' 已存在，跳过创建")
        else:
            vm_dir = self.vms_dir / name
            vm_dir.mkdir(parents=True, exist_ok=True)
            disk = vm_dir / f"{name}.vhdx"
            print(f"==> 创建增量盘 {disk.name}（父盘 {parent.name}）")
            # differencing disk：对应 Tart 的 APFS CoW clone。
            # 注意父盘同一时刻只能服务一个 differencing disk，与 Tart 不同。
            self._ps(
                f"New-VHD -Path '{disk}' -ParentPath '{parent}' -Differencing"
            )
            if not disk.exists():
                raise BackendError("New-VHD 未生成增量盘")

            print(f"==> 创建 Gen2 VM '{name}'（{cpu} 核 / {memory_mb} MB）")
            # Generation 2 是必须的：ARM64 客人机只能走 UEFI，Gen1 是 x86 BIOS
            self._ps(
                f"New-VM -Name '{name}' -Generation 2 "
                f"-MemoryStartupBytes {int(memory_mb) * 1048576} "
                f"-VHDPath '{disk}' -SwitchName (Get-VMSwitch | "
                "Select-Object -First 1 -ExpandProperty Name)"
            )
            self._ps(f"Set-VMProcessor -VMName '{name}' -Count {cpu}")
            # Secure Boot 默认开。Debian 12 的 shim 由微软签名，理论上可用；
            # 若启动黑屏则改为 Off。这一项未在目标机验证。
            sb = "On" if self.secure_boot else "Off"
            try:
                self._ps(f"Set-VMFirmware -VMName '{name}' -EnableSecureBoot {sb}")
            except BackendError:
                print(f"  warn 无法设置 Secure Boot {sb}，保持默认值")

            iso = self._write_seed(name)
            self._ps(f"Set-VMDvdDrive -VMName '{name}' -Path '{iso}'")
            print(f"  ok VM '{name}' 已创建")

        self._ps(
            f"Set-VMProcessor -VMName '{name}' -Count {cpu}; "
            f"Set-VMMemory -VMName '{name}' -StartupBytes {int(memory_mb) * 1048576}"
        )

    def run_vm(self, name: str) -> None:
        check_vm_name(name)
        if not self.vm_exists(name):
            raise BackendError(f"VM '{name}' 不存在，先 create_vm")
        state = self._ps(f"(Get-VM -Name '{name}').State")
        if state.strip() == "2":
            print(f"  ok VM '{name}' 已在运行")
            return
        print(f"==> 启动 VM '{name}'")
        self._ps(f"Start-VM -Name '{name}'")

    def stop_vm(self, name: str) -> None:
        check_vm_name(name)
        if not self.vm_exists(name):
            return
        print(f"==> 停止 VM '{name}'")
        self._ps(f"Stop-VM -Name '{name}' -Force -TurnOff -ErrorAction SilentlyContinue")

    def delete_vm(self, name: str) -> None:
        """连 VM 带盘一起删。对应 Tart 的「不想要了一条命令删干净」。

        原来只 unlink `<name>.vhdx`，`<vm>/seed.iso`、`<vm>/seed-src/` 整个留在
        ~/.agentvm/vms/<name>/ 下；而 vm_exists 判 false 时直接 return，残留目录
        越攒越多。现在整棵删掉。
        """
        check_vm_name(name)
        if not self.vm_exists(name):
            print(f"  ok VM '{name}' 不存在")
            shutil.rmtree(self.vms_dir / name, ignore_errors=True)
            return
        print(f"==> 删除 VM '{name}'")
        self._ps(f"Stop-VM -Name '{name}' -Force -TurnOff -ErrorAction SilentlyContinue")
        self._ps(f"Remove-VM -Name '{name}' -Force")
        # 种子盘、seed-src、临时文件都在这个目录下，一起清
        shutil.rmtree(self.vms_dir / name, ignore_errors=True)
        print("  ok 已删除")

    # -------------------------------------------------------- guest 内执行 --

    def exec_vm(self, name: str, command: str, timeout: int = 300) -> str:
        """经 SSH 在 guest 内执行命令。

        密钥与命令都不经过额外文件，但命令本身出现在 SSH 参数里。
        guest 是隔离沙箱，这条通道不承载宿主秘密，所以可接受；
        真正的秘密走 exec_vm_script 的 stdin 通道，见 50-configure 的等价物。
        """
        check_vm_name(name)
        self._ensure_nat(name)
        proc = subprocess.run(
            self._ssh_base() + [command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise BackendError(
                f"guest 内命令失败（退出码 {proc.returncode}）：{err}\n命令：{command[:200]}"
            )
        return (proc.stdout or "").strip()

    def exec_vm_script(self, name: str, script_path: str, timeout: int = 600) -> str:
        """把宿主脚本经 stdin 送进 guest 执行。

        对应原项目 `tart exec vm sh -s < script`：脚本内容不进命令行参数，
        宿主进程表里看不到内容。
        """
        check_vm_name(name)
        script = Path(script_path).read_text(encoding="utf-8")
        self._ensure_nat(name)
        proc = subprocess.run(
            self._ssh_base() + ["sh -s"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise BackendError(
                f"guest 内脚本失败（退出码 {proc.returncode}）：{err}\n脚本：{script_path}"
            )
        return (proc.stdout or "").strip()
