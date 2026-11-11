#!/usr/bin/env python3
"""winarm-vm — 在 Apple Silicon Mac 上用 QEMU+HVF 无人值守安装 Windows 11 ARM64。

为什么需要它
------------
Windows 在 Apple Silicon 上不能用 Virtualization.framework 跑：VZ 没有
vTPM，virtio 设备集是固定的而 Windows 没有对应驱动，EFI 启动链也有缺口。
所以 tart / lume / apple/container 全都跑不了 Windows。唯一可行的是
QEMU 加 HVF（Hypervisor.framework），这也是 UTM 对 Windows 采用的引擎。

本脚本把三件事做成一条命令：
    1. 把官方 Windows 11 ARM64 ISO 改造成无人值守安装介质
       （注入 virtio 驱动到 boot.wim 与 install.wim，写 autounattend.xml，
        暂存 OEM 驱动与首次登录脚本，重建成 UEFI 可启动 ISO）
    2. 建虚拟机（稀疏 raw 盘 + 私有 EFI NVRAM）
    3. 用 QEMU+HVF 启动，并等到 SSH 就绪

用法
----
    winarm-vm.py install                 # 全流程：准备介质、建 VM、装系统、等 SSH
    winarm-vm.py prepare                 # 只改造安装介质
    winarm-vm.py create                  # 只建磁盘与 EFI vars
    winarm-vm.py start                   # 启动 QEMU
    winarm-vm.py status                  # 看状态与 SSH 是否可达
    winarm-vm.py stop                    # 关机：guest agent / SSH / ACPI，超时则强杀
    winarm-vm.py logs [-f]               # 看 QEMU 日志
    winarm-vm.py autostart [--disable]   # 装/卸 launchd 开机自启

依赖（macOS 上）：
    qemu-system-aarch64   带 HVF 的 QEMU（brew install qemu）
    wimlib-imagex         改 WIM 用
    mkisofs 或 xorrisofs  重建 ISO 用
    bsdtar / hdiutil      macOS 自带，解包 UDF 用

配置（环境变量）：
    ISO_PATH      原始 Windows ARM64 ISO
    VM_DIR        虚拟机目录，默认 ~/.winarm/win11
    DISK_SIZE     虚拟盘大小，默认 64G
    RAM_SIZE      内存，默认 4G（不要贪大，见下）
    CPU_CORES     vCPU 数，默认 2（不要贪多，见下）
    USERNAME      Windows 本地管理员，默认 Docker
    PASSWORD      密码，默认 admin
    SSH_KEY       宿主机 SSH 私钥，默认 ~/.winarm/ci_key
    SSH_PORT      转发到客人机 22 的宿主端口，默认 12222
    RDP_PORT      转发到客人机 3389 的宿主端口，默认 13389
    VNC_DISPLAY   VNC display 号，端口 = 5900 + 号
    VIRTIO_PATH   virtio 驱动包解压后的目录
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape_raw


def xml_escape(s: str) -> str:
    """XML 文本节点转义（& < >）。

    autounattend.xml 里的用户名、密码、区域、键盘布局都是用户可给的任意字符串。
    一个 & 就让整份 XML 不合法，而 Windows Setup 对不合法的应答文件的反应是
    **静默忽略**——退回全手动界面，安装挂死十几分钟后超时，日志里一个字都不提。
    默认值碰不到这条，用户一设复杂密码就踩。
    """
    return _xml_escape_raw(str(s))


def _ps_single_quoted(s: str) -> str:
    """把任意字符串包成 PowerShell 的单引号字面量。

    PowerShell 单引号串里唯一的转义方式就是把单引号写成两个。原样插入时密码
    `a'b` 会产出 `-Value 'a'b'`——语法错误，自动登录钉不死，重启停在登录界面，
    sshd 起不来。$ 和反引号在单引号串里不会被展开，所以不用管。
    """
    return "'" + str(s).replace("'", "''") + "'"

HOME = Path.home()
DEFAULT_VM_DIR = HOME / ".winarm" / "win11"

# virtio 驱动清单。存储与网卡是关键：viostor / vioscsi 让 Setup 看见盘，
# NetKVM 让装好的系统有网。缺了这两个，安装直接卡死。
VIRTIO_DRIVERS = [
    "viostor", "vioscsi", "NetKVM", "viorng", "viomem", "Balloon",
    "vioserial", "qemupciserial", "vioinput", "viogpudo", "qxldod",
    "qxl", "viofs", "pvpanic", "smbus",
]


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"ok  {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"warn {msg}", flush=True)


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        timeout: int | None = None, **kw) -> subprocess.CompletedProcess:
    """跑外部命令。默认把输出接过来，出错时把 stdout/stderr 一起抛出来。"""
    try:
        return subprocess.run(cmd, check=check, capture_output=capture,
                              text=True, timeout=timeout, **kw)
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "") + (e.stderr or "")
        die(f"命令失败：{' '.join(cmd[:6])}\n{out.strip()[:2000]}")
    except subprocess.TimeoutExpired:
        die(f"命令超时：{' '.join(cmd[:6])}")
    raise AssertionError


def which(name: str) -> str | None:
    """按名字找工具，PATH 里没有也去 Homebrew 常见前缀下找。

    不能只 shutil.which：QEMU 装在 ~/hb/bin，而那个目录只在交互 shell 的
    PATH 里。launchd、cron、app 拉起的进程都可能不带它，只认 PATH 会把明明
    装好了的工具报成「缺少依赖」。实测踩过：从命令行后台起一次安装，PATH 里
    没有 ~/hb/bin，require_tools 一次报缺 qemu/wimlib/mkisofs 三样，其实三个
    都在 ~/hb/bin 下。上面 find_tool 的注释说的就是同一个坑，以前只有它兜了底。
    """
    hit = shutil.which(name)
    if hit:
        return hit
    env = os.environ.get("HOMEBREW_PREFIX")
    cands = [Path(env) / "bin" / name] if env else []
    cands += [HOME / "hb" / "bin" / name,
              Path("/opt/homebrew/bin") / name,
              Path("/usr/local/bin") / name,
              # hdiutil、osascript 这些 macOS 自带的在 /usr/bin，PATH 被削得
              # 很干净时（cron、某些 launchd 任务）也会找不到
              Path("/usr/bin") / name,
              Path("/bin") / name]
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def find_tool(name: str) -> str:
    """把工具名解析成绝对路径，找不到就说清楚去哪找。

    兜底逻辑在 which 里，这里只负责「找不到的时候怎么说」。以前两处各写了
    一份相同的兜底，而 require_tools 用的又是没有兜底的 which，三者不一致
    才会出现「工具明明装着却报缺少依赖」。
    """
    hit = which(name)
    if hit:
        return hit
    die(f"找不到 {name}。PATH 里没有，~/hb、/opt/homebrew、/usr/local 下也没有。"
        f"如果 QEMU 装在别处，设 HOMEBREW_PREFIX 指过去。")


def wimlib() -> str:
    """wimlib-imagex 的绝对路径。

    必须解析成绝对路径再用：这个工具在 ~/hb/bin 下，而 PATH 只在交互 shell 里
    才有。以前这里是 `run(["wimlib-imagex", ...])` 裸名字，从 launchd、cron 或
    一个 PATH 干净的 shell 里起安装，改介质改到一半才抛
    FileNotFoundError: 'wimlib-imagex'——4 GB 镜像白下一遍。实测踩过。
    """
    return find_tool("wimlib-imagex")


# ---------------------------------------------------------------- 环境检查 --

def require_tools() -> dict[str, str]:
    """检查依赖，缺什么一次说清，不要等到跑一半才崩。"""
    need = {
        "qemu-system-aarch64": "brew install qemu",
        "wimlib-imagex": "brew install wimlib",
    }
    have = {k: which(k) for k in need}
    missing = [f"{k}（{v}）" for k, v in need.items() if not have[k]]
    if not (which("mkisofs") or which("xorrisofs")):
        missing.append("mkisofs 或 xorrisofs（brew install cdrtools 或 brew install xorriso）")
    if not which("hdiutil"):
        missing.append("hdiutil（macOS 自带，缺失说明不是 macOS）")
    if missing:
        die("缺少依赖：\n  " + "\n  ".join(missing))
    return {k: v for k, v in have.items() if v}


# ------------------------------------------------------------ 无人值守配置 --

AUTOUNATTEND_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowspe">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>@REGION@</UILanguage></SetupUILanguage>
      <InputLocale>@KEYBOARD@</InputLocale>
      <SystemLocale>@REGION@</SystemLocale>
      <UILanguage>@REGION@</UILanguage>
      <UserLocale>@REGION@</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>256</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>128</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Label>System</Label><Format>FAT32</Format></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Label>Windows</Label><Letter>C</Letter><Format>NTFS</Format></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add">
              <Key>/IMAGE/NAME</Key>
              <Value>@IMAGE@</Value>
            </MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
          <InstallToAvailablePartition>false</InstallToAvailablePartition>
        </OSImage>
      </ImageInstall>
      <DynamicUpdate><Enable>false</Enable><WillShowUI>Never</WillShowUI></DynamicUpdate>
      <UpgradeData><Upgrade>false</Upgrade><WillShowUI>Never</WillShowUI></UpgradeData>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <FullName>@USERNAME@</FullName>
        <Organization>winarm</Organization>
        <ProductKey><Key /><WillShowUI>OnError</WillShowUI></ProductKey>
      </UserData>
      <EnableFirewall>false</EnableFirewall>
      <Diagnostics><OptIn>false</OptIn></Diagnostics>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg.exe add "HKLM\\SYSTEM\\Setup\\MoSetup" /v AllowUpgradesWithUnsupportedTPMOrCPU /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="offlineServicing">
    <component name="Microsoft-Windows-LUA-Settings" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <EnableLUA>false</EnableLUA>
    </component>
  </settings>
  <settings pass="generalize">
    <component name="Microsoft-Windows-PnPSysprep" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <PersistAllDeviceInstalls>true</PersistAllDeviceInstalls>
    </component>
    <component name="Microsoft-Windows-Security-SPP" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SkipRearm>1</SkipRearm>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Security-SPP-UX" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SkipAutoActivation>true</SkipAutoActivation>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>*</ComputerName>
    </component>
    <component name="Microsoft-Windows-International-Core" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>@KEYBOARD@</InputLocale>
      <SystemLocale>@REGION@</SystemLocale>
      <UILanguage>@REGION@</UILanguage>
      <UserLocale>@REGION@</UserLocale>
    </component>
    <component name="Microsoft-Windows-Deployment" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg.exe add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg.exe add "HKLM\\Software\\Policies\\Microsoft\\Windows\\CloudContent" /v "DisableConsumerAccountStateContent" /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg.exe add "HKLM\\Software\\Policies\\Microsoft\\Windows\\CloudContent" /v "DisableWindowsConsumerFeatures" /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg.exe add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\RunOnce" /v winarmSetup /t REG_SZ /d "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\OEM\\winarm-setup.ps1" /f</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
    <component name="Microsoft-Windows-TerminalServices-LocalSessionManager" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <fDenyTSConnections>false</fDenyTSConnections>
    </component>
    <component name="Microsoft-Windows-TerminalServices-RDP-WinStationExtensions" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <UserAuthentication>0</UserAuthentication>
    </component>
    <component name="Networking-MPSSVC-Svc" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <FirewallGroups>
        <FirewallGroup wcm:action="add" wcm:keyValue="RemoteDesktop">
          <Active>true</Active><Profile>all</Profile><Group>@FirewallAPI.dll,-28752</Group>
        </FirewallGroup>
      </FirewallGroups>
    </component>
  </settings>
  <settings pass="auditSystem" />
  <settings pass="auditUser" />
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>@USERNAME@</Name>
            <Group>Administrators</Group>
            <Password><Value>@PASSWORD@</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <!-- 不能写 LogonCount。写了 1 就意味着自动登录只生效一次，
             Setup 那次用完，Windows 就把 HKLM 下的 AutoAdminLogon 重置回 0，
             之后每次重启都停在登录界面，SSH 也跟着起不来（sshd 靠自动登录
             那个交互会话挂着）。这里不写 LogonCount，并在 GUEST_SETUP_PS1
             里把注册表显式钉成 AutoAdminLogon=1。 -->
        <Username>@USERNAME@</Username>
        <Password><Value>@PASSWORD@</Value><PlainText>true</PlainText></Password>
      </AutoLogon>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
    </component>
  </settings>
</unattend>
"""

# 首次登录时在客人机里跑：装 virtio 驱动、开 OpenSSH、装 SSH 公钥。
GUEST_SETUP_PS1 = r"""# winarm guest setup -- runs once at first logon (staged to C:\OEM via \sources\$OEM$).
$ErrorActionPreference = 'SilentlyContinue'
if (Test-Path C:\OEM\winarm-ready) { exit 0 }
Start-Transcript -Path C:\OEM\winarm-setup.log -Append | Out-Null
function Log($m){ Write-Output ("[{0}] {1}" -f (Get-Date -Format o), $m) }
Log "winarm guest setup starting"

# --- install staged virtio drivers into the driver store -------------------
if (Test-Path C:\Drivers) {
  Log "installing staged virtio drivers from C:\Drivers"
  & pnputil.exe /add-driver C:\Drivers\*\*.inf /subdirs /install 2>&1 | Out-Null
} else { Log "C:\Drivers not present" }

# --- OpenSSH Server -------------------------------------------------------
Log "installing OpenSSH Server"
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  Log "FoD unavailable, fetching Win32-OpenSSH ARM64 from GitHub"
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $u = 'https://github.com/PowerShell/Win32-OpenSSH/releases/latest/download/OpenSSH-ARM64.zip'
    $z = "$env:TEMP\openssh.zip"
    Invoke-WebRequest -Uri $u -OutFile $z -UseBasicParsing
    Expand-Archive -Path $z -DestinationPath 'C:\Program Files' -Force
    & 'C:\Program Files\OpenSSH-ARM64\install-sshd.ps1'
  } catch { Log "OpenSSH GitHub fallback failed: $_" }
}

$sshDir = @('C:\Windows\System32\OpenSSH','C:\Program Files\OpenSSH-ARM64','C:\Program Files\OpenSSH') |
  Where-Object { Test-Path (Join-Path $_ 'sshd.exe') } | Select-Object -First 1
Log "OpenSSH dir: $sshDir"
if ($sshDir) { & (Join-Path $sshDir 'ssh-keygen.exe') -A 2>&1 | Out-Null }

# sshd 的 service 模式对密钥文件属主与 ACL 很严格：必须是 Administrators 或
# SYSTEM 属主且其他人无权访问，否则 sshd 会接受 TCP 连接然后断开。
function Lock-SshFile($p){
  if (Test-Path $p) {
    icacls $p /setowner Administrators 2>&1 | Out-Null
    icacls $p /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' 2>&1 | Out-Null
  }
}
Get-ChildItem 'C:\ProgramData\ssh\ssh_host_*_key' | ForEach-Object { Lock-SshFile $_.FullName }

if (Test-Path C:\OEM\authorized_keys) {
  Log "installing administrators_authorized_keys"
  $ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
  New-Item -Path 'C:\ProgramData\ssh' -ItemType Directory -Force | Out-Null
  Copy-Item C:\OEM\authorized_keys $ak -Force
  Lock-SshFile $ak
}

New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
  -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force | Out-Null

New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Enabled True `
  -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd

# 实测 Win32-OpenSSH ARM64 上 SCM 服务会崩（错误 1067），非交互上下文里
# sshd.exe 也起不来。唯一可靠的是在自动登录用户的高权限交互会话里跑
# sshd.exe -D。虚拟机配了自动登录，所以这个任务每次开机都会把 SSH 拉起来。
Start-Sleep 3
if ((Get-Service sshd).Status -ne 'Running' -and $sshDir) {
  Log "sshd service not running -- installing interactive 'sshd -D' logon task"
  $exe = Join-Path $sshDir 'sshd.exe'
  $a = New-ScheduledTaskAction -Execute $exe -Argument '-D'
  $t = New-ScheduledTaskTrigger -AtLogOn
  $p = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest
  $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
  Register-ScheduledTask -TaskName 'winarm-sshd' -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null
  Start-ScheduledTask -TaskName 'winarm-sshd'
}

# 自动登录必须钉死。autounattend 里的 AutoLogon 只管 Setup 那一次，之后
# Windows 会把 AutoAdminLogon 重置回 0，重启就停在登录界面，而 sshd 是靠
# 自动登录那个交互会话拉起来的，登录界面进不去 SSH 就起不来。
# AutoLogonCount 也要删：它在的时候自动登录会递减，用完就停。
$w = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $w -Name AutoAdminLogon -Value '1' -Type String -Force
Set-ItemProperty -Path $w -Name DefaultUserName -Value '@USERNAME@' -Type String -Force
Set-ItemProperty -Path $w -Name DefaultPassword -Value '@PASSWORD@' -Type String -Force
Set-ItemProperty -Path $w -Name DefaultDomainName -Value $env:COMPUTERNAME -Type String -Force
Remove-ItemProperty -Path $w -Name AutoLogonCount -ErrorAction SilentlyContinue
Log "autologon pinned: AutoAdminLogon=1 user=@USERNAME@"

Set-Content -Path C:\OEM\winarm-ready -Value (Get-Date -Format o)
Log "winarm guest setup done"
Stop-Transcript | Out-Null
"""


# ------------------------------------------------------------------- 配置 --

class Cfg:
    def __init__(self) -> None:
        self.vm_dir = Path(os.environ.get("VM_DIR", str(DEFAULT_VM_DIR)))
        self.iso = Path(os.environ.get("ISO_PATH", "/tmp/winarm-iso/win11-arm64-eval.iso"))
        self.disk_size = os.environ.get("DISK_SIZE", "64G")
        # 默认值按「不能拖垮宿主机」定，不是按性能最大化定。这台 Mac mini
        # 12 核 16G，原先 4 vCPU + 8G 的配置实测把 QEMU 顶到 105% CPU，
        # 叠加 Lark 的重占用后系统负载到 12.64，WindowServer 抢不到 CPU，
        # 表象是键盘输入失灵。2 vCPU + 4G 跑这个用途完全够，且留得出余量。
        self.ram = os.environ.get("RAM_SIZE", "4G")
        self.cpus = os.environ.get("CPU_CORES", "2")
        self.username = os.environ.get("USERNAME", "Docker")
        self.password = os.environ.get("PASSWORD", "admin")
        self.ssh_key = Path(os.environ.get("SSH_KEY", str(HOME / ".winarm" / "ci_key")))
        self.ssh_port = os.environ.get("SSH_PORT", "12222")
        self.rdp_port = os.environ.get("RDP_PORT", "13389")
        self.vnc_display = os.environ.get("VNC_DISPLAY", "0")
        self.virtio = Path(os.environ.get("VIRTIO_PATH", "/tmp/winarm-virtio/drv"))
        self.headless = os.environ.get("HEADLESS", "1") != "0"

    # 派生路径
    @property
    def disk(self) -> Path:
        return self.vm_dir / "data.img"

    @property
    def vars_fd(self) -> Path:
        return self.vm_dir / "efi-vars.fd"

    @property
    def prepared_iso(self) -> Path:
        return self.vm_dir / "install.iso"

    @property
    def pidfile(self) -> Path:
        return self.vm_dir / "qemu.pid"

    @property
    def monitor(self) -> Path:
        return self.vm_dir / "monitor.sock"

    @property
    def qga_sock(self) -> Path:
        """QEMU guest agent 套接字。**默认不存在**，原因见 qga_shutdown_wait。"""
        return self.vm_dir / "qga.sock"

    @property
    def vnc_display_file(self) -> Path:
        return self.vm_dir / "vnc.display"

    @property
    def qemu_log(self) -> Path:
        return self.vm_dir / "qemu.log"

    @property
    def snapshots(self) -> Path:
        # 每台 VM 自己一份。原来是 vm_dir.parent / "snapshots"，也就是
        # ~/.agentvm/vms/snapshots，所有 Windows VM 共用一个根、只按快照名
        # 区分。两个后果：第二台 VM 拍一个同名快照报「已存在」；更严重的是
        # restore 会把 VM a 的 data.img 覆盖到 VM b 上——用户以为在还原自己
        # 的机器，实际拿到的是另一台的盘，而这一步没有任何提示。
        # macOS 那边（MacVMPaths.snapshots）本来就是每 VM 一份，这边对齐。
        # 已经存在的共用目录由 _migrate_shared_snapshots 负责搬。
        return self.vm_dir / "snapshots"

    @property
    def pubkey(self) -> Path:
        return Path(str(self.ssh_key) + ".pub")


# ------------------------------------------------------------- 介质准备 --

def mount_iso(iso: Path, mnt: Path) -> None:
    mnt.mkdir(parents=True, exist_ok=True)
    run(["hdiutil", "detach", str(mnt)], check=False, capture=True)
    r = run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify",
             "-mountpoint", str(mnt), str(iso)], capture=True, check=False)
    if r.returncode != 0:
        die(f"挂载 ISO 失败：{iso}\n{r.stderr}")


def umount_iso(mnt: Path) -> None:
    run(["hdiutil", "detach", str(mnt)], check=False, capture=True)


def iso_has_autounattend(iso: Path, mnt: Path) -> bool:
    mount_iso(iso, mnt)
    try:
        for n in ("autounattend.xml", "Autounattend.xml", "AUTOUNATTEND.XML"):
            if (mnt / n).is_file():
                return True
        return False
    finally:
        umount_iso(mnt)


def wim_edition_names(wim: Path, mnt: Path) -> list[str]:
    """从 install.wim 里读出镜像版本名，autounattend 要用它跳过选镜像那一步。

    wim 是挂载点里 sources/install.wim 的路径。版本名形如
    "Windows 11 Enterprise" 或 "Windows 11 Pro"。
    """
    r = run([wimlib(), "info", str(wim)], capture=True, check=False)
    names = []
    for line in r.stdout.splitlines():
        if ":" in line and "Name" in line:
            v = line.split(":", 1)[1].strip()
            if v:
                names.append(v)
    return names


def _force_rmtree(path: Path) -> None:
    """删目录树，先放开权限再删。

    bsdtar 解出来的驱动是只读的，直接 rmtree 会失败；用 ignore_errors=True
    又会静默留下半个目录，下一次 mkdir 就 FileExistsError。所以先统一放权。
    """
    if not path.exists():
        return
    for p in path.rglob("*"):
        try:
            os.chmod(p, 0o755 if p.is_dir() else 0o644)
        except OSError:
            pass
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _copy_writable(src: Path, dst: Path) -> None:
    """拷贝后统一放开写权限。

    bsdtar 从 tar 包里解出来的驱动保留原始只读权限，后续 wimlib 打包和
    $OEM$ 暂存都要读它们，只读本身没问题，但往已存在的只读文件上再拷一次
    会 Permission denied，所以这里拷贝完就补写权限。
    """
    shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=_copy_and_chmod)
    for p in dst.rglob("*"):
        try:
            os.chmod(p, 0o644 if p.is_file() else 0o755)
        except OSError:
            pass


def _copy_and_chmod(src: str, dst: str) -> None:
    shutil.copy2(src, dst)
    os.chmod(dst, 0o644)


def collect_drivers(cfg: Cfg, dest: Path) -> Path:
    """把 virtio 包里这个架构要用的驱动归拢到一棵树。

    包里有两个来源，都取，按驱动名去重：
      * ARM64/w11/  合集目录，但只放了存储驱动（viostor、vioscsi）
      * <驱动名>/w11/ARM64/  per-driver 目录，驱动齐全
    只用合集目录会缺 NetKVM，客人机没网，SSH 起不来，所以两者必须合并。
    合集目录优先，per-driver 目录补缺。
    """
    stage = cfg.vm_dir / "prep" / "_stage"
    _force_rmtree(stage)
    stage.mkdir(parents=True)

    seen: set[str] = set()

    def take(src: Path, name: str) -> None:
        if name in seen or not src.is_dir():
            return
        _copy_writable(src, stage / name)
        seen.add(name)

    # 1. 合集目录：里面直接是各驱动的 inf/sys 文件，按文件名前缀归组
    unified = cfg.virtio / "ARM64" / "w11"
    if unified.is_dir():
        for item in unified.iterdir():
            if not item.is_file():
                continue
            stem = item.name.split(".")[0].lower()
            d = stage / stem
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, d / item.name)
            os.chmod(d / item.name, 0o644)
            seen.add(stem)

    # 2. per-driver 目录补齐
    for name in VIRTIO_DRIVERS:
        for sub in ("w11", "2k25"):
            src = cfg.virtio / name / sub / "ARM64"
            if src.is_dir():
                take(src, name)
                break

    if not seen:
        die(f"在 {cfg.virtio} 下找不到 ARM64 virtio 驱动。"
            f"请设 VIRTIO_PATH 指向 virtio-win 包解压后的目录。")

    missing = [d for d in ("viostor", "vioscsi", "NetKVM") if d not in seen]
    if missing:
        warn(f"关键驱动缺失：{missing}。装系统可能失败。")

    inf_count = sum(1 for _ in stage.rglob("*.inf"))
    ok(f"virtio 驱动归拢完成：{len(seen)} 个驱动，{inf_count} 个 inf"
       f"（{', '.join(sorted(seen))}）")
    return stage


def inject_drivers(wim: Path, stage: Path) -> None:
    """把驱动加到 WIM 的每个镜像索引里，路径 $WinPEDriver$。"""
    r = run([wimlib(), "info", str(wim)], capture=True, check=False)
    n = 1
    for line in r.stdout.splitlines():
        if "Image Count" in line and ":" in line:
            try:
                n = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            break
    for i in range(1, n + 1):
        run([wimlib(), "update", str(wim), str(i),
             "--command", "delete --force --recursive /$WinPEDriver$"],
            check=False, capture=True)
        r = run([wimlib(), "update", str(wim), str(i),
                 "--command", f"add {stage} /$WinPEDriver$"],
                capture=True, check=False)
        if r.returncode != 0:
            warn(f"驱动注入镜像 {i} 失败：{(r.stderr or '').strip()[:200]}")
        else:
            ok(f"驱动注入 {wim.name} 镜像 {i}/{n}")


def build_iso(src_dir: Path, out: Path, label: str) -> None:
    """重建成 UEFI 可启动 ISO。

    两个必须点对：
      * -udf。Windows ARM 的 ISO 是 UDF 为主，install.wim 超过 4 GB，
        ISO9660 放不下，没有 UDF 层启动会失败。
      * El Torito 的 boot-load-size 要覆盖整个 efisys 镜像，否则 edk2
        加载到被截断的 FAT，卡在 "Start boot option"。
    """
    efisys_rel = "efi/microsoft/boot/efisys_noprompt.bin"
    efisys = src_dir / efisys_rel
    if not efisys.is_file():
        efisys_rel = "efi/microsoft/boot/efisys.bin"
        efisys = src_dir / efisys_rel
    if not efisys.is_file():
        die("ISO 里找不到 efisys 启动镜像，无法做成可启动")

    size = efisys.stat().st_size
    load_size = (size + 511) // 512

    if out.exists():
        out.unlink()

    mkisofs = which("mkisofs")
    if mkisofs:
        cmd = [mkisofs, "-iso-level", "4", "-J", "-joliet-long", "-l", "-udf",
               "-V", label[:30],
               "-eltorito-platform", "efi", "-eltorito-boot", efisys_rel,
               "-no-emul-boot", "-boot-load-size", str(load_size),
               "-eltorito-catalog", "boot.catalog", "-o", str(out), str(src_dir)]
    else:
        xorrisofs = which("xorrisofs")
        if not xorrisofs:
            die("mkisofs 与 xorrisofs 都没有，无法重建 ISO")
        warn("没有 mkisofs，退回 xorrisofs。xorriso 也能写 UDF，但启动性未经实测。")
        cmd = [xorrisofs, "-iso-level", "4", "-J", "-joliet-long", "-l", "-udf",
               "-V", label[:30],
               "-eltorito-platform", "efi", "-eltorito-boot", efisys_rel,
               "-no-emul-boot", "-boot-load-size", str(load_size),
               "-eltorito-catalog", "boot.catalog", "-o", str(out), str(src_dir)]

    log(f"重建 ISO（efisys {size} 字节，load-size {load_size} 扇区）")
    run(cmd, capture=True)
    if not out.is_file() or out.stat().st_size < 1_000_000:
        die("ISO 重建失败，输出文件异常")
    ok(f"安装介质就绪：{out}（{out.stat().st_size / 1e9:.2f} GB）")


def cmd_prepare(cfg: Cfg) -> None:
    require_tools()
    # 先建密钥对。公钥要在这一步就打进 ISO 的 $OEM$，等 create/start 再建
    # 就晚了，装完系统 ISO 里没有 authorized_keys，SSH 登不进去。
    ensure_ssh_key(cfg)
    if not cfg.iso.is_file():
        die(f"找不到 ISO：{cfg.iso}。用 ISO_PATH 指定 Windows 11 ARM64 镜像。")
    if not cfg.virtio.is_dir():
        die(f"找不到 virtio 驱动目录：{cfg.virtio}。用 VIRTIO_PATH 指定。")

    if iso_has_autounattend(cfg.prepared_iso if cfg.prepared_iso.exists() else cfg.iso,
                            cfg.vm_dir / "prep" / "mnt"):
        ok("介质里已有 autounattend.xml，跳过改造")
        return

    work = cfg.vm_dir / "prep"
    iso_dir = work / "iso"
    mnt = work / "mnt"
    _force_rmtree(work)
    iso_dir.mkdir(parents=True)
    mnt.mkdir(parents=True)

    log("挂载并复制 ISO 内容（约 4 GB，需要一两分钟）")
    mount_iso(cfg.iso, mnt)
    try:
        run(["ditto", str(mnt), str(iso_dir)])
    finally:
        umount_iso(mnt)
    # ISO 是只读挂载复制来的，权限要放开才能改
    subprocess.run(["chmod", "-R", "u+w", str(iso_dir)], check=False)
    ok(f"已复制到 {iso_dir}")

    stage = collect_drivers(cfg, iso_dir.parent)

    boot = iso_dir / "sources" / "boot.wim"
    inst = iso_dir / "sources" / "install.wim"
    if not boot.is_file():
        die("sources/boot.wim 不存在，ISO 结构异常")

    inject_drivers(boot, stage)
    if inst.is_file():
        inject_drivers(inst, stage)
    else:
        warn("sources/install.wim 不存在（可能是 install.esd）。"
             "只注入了 boot.wim，装好的系统要靠 OEM 驱动目录补 virtio 驱动。")

    # $OEM$ 必须放在 sources\$OEM$ 下，放在 ISO 根目录会被 Setup 静默忽略，
    # 结果 C:\OEM / C:\Drivers 都不出现，首次登录脚本也不跑。
    oem = iso_dir / "sources" / "$OEM$" / "$1"
    (oem / "Drivers").mkdir(parents=True, exist_ok=True)
    _copy_writable(stage, oem / "Drivers")
    (oem / "OEM").mkdir(parents=True, exist_ok=True)
    # 客人机脚本里也要替换 @USERNAME@ / @PASSWORD@，否则自动登录那段会把
    # 字面量 '@USERNAME@' 当账号写进注册表，登录一样进不去。
    #
    # 替换之前必须转义，原来是原样字符串替换：
    #   * 密码含单引号（如 a'b）→ `Set-ItemProperty -Value 'a'b'` 是 PowerShell
    #     语法错误，自动登录钉不死 → 重启停在登录界面 → SSH 起不来。
    #   * 用户名/密码含 `$` 或反引号 → PowerShell 会当变量展开，写进注册表的是
    #     另一个值。
    # XML 侧用 saxutils.escape（& < > 会让 Setup 直接忽略整个应答文件），
    # PowerShell 侧把单引号翻倍（这是 PowerShell 字符串字面量里唯一的转义方式）。
    guest_setup = (GUEST_SETUP_PS1
                   .replace("@USERNAME@", _ps_single_quoted(cfg.username))
                   .replace("@PASSWORD@", _ps_single_quoted(cfg.password)))
    (oem / "OEM" / "winarm-setup.ps1").write_text(guest_setup, encoding="utf-8")
    if cfg.pubkey.is_file():
        shutil.copy(cfg.pubkey, oem / "OEM" / "authorized_keys")
        ok(f"已打包 SSH 公钥 {cfg.pubkey}")
    else:
        warn(f"没有 SSH 公钥 {cfg.pubkey}，先跑 ensure-ssh-key")

    # 镜像版本名：autounattend 里指定 /IMAGE/NAME 才能跳过"选择映像"界面
    image_name = os.environ.get("IMAGE_NAME", "")
    if not image_name:
        mount_iso(cfg.iso, mnt)
        try:
            names = wim_edition_names(inst if inst.is_file() else boot, mnt)
        finally:
            umount_iso(mnt)
        if names:
            image_name = names[0]
            ok(f"install.wim 里的镜像版本：{names}")
        else:
            image_name = "Windows 11 Enterprise"
            warn(f"读不到镜像版本名，先用 {image_name}。装错就用 IMAGE_NAME 指定。")

    region = os.environ.get("REGION", "en-US")
    kbd = os.environ.get("KEYBOARD", "0409:00000409")
    # XML 侧：用户名和密码必须 XML 转义。密码 `p@ss&word` 原样插进
    # `<Value>p@ss&word</Value>` 不是合法 XML，Windows Setup 会**直接忽略整个
    # autounattend.xml**，退回全手动界面，安装挂死十几分钟后超时，而日志里
    # 没有任何线索。默认密码 agentvm 碰不到，用户一设复杂密码就踩。
    xml = (AUTOUNATTEND_TMPL
           .replace("@USERNAME@", xml_escape(cfg.username))
           .replace("@PASSWORD@", xml_escape(cfg.password))
           .replace("@REGION@", xml_escape(region))
           .replace("@KEYBOARD@", xml_escape(kbd))
           .replace("@IMAGE@", xml_escape(image_name)))
    (iso_dir / "autounattend.xml").write_text(xml, encoding="utf-8")
    ok(f"已写 autounattend.xml（用户 {cfg.username}，映像 {image_name}）")

    build_iso(iso_dir, cfg.prepared_iso, "WINARM")

    _force_rmtree(iso_dir)
    ok("中间文件已清理")


# --------------------------------------------------------------- 虚拟机 --

def ensure_ssh_key(cfg: Cfg) -> None:
    if cfg.ssh_key.is_file() and cfg.pubkey.is_file():
        return
    cfg.ssh_key.parent.mkdir(parents=True, exist_ok=True)
    run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "winarm-ci",
         "-f", str(cfg.ssh_key)], check=False, capture=True)
    os.chmod(cfg.ssh_key, 0o600)
    ok(f"已生成 SSH 密钥对 {cfg.ssh_key}")


def find_edk2() -> tuple[Path, Path]:
    """找 edk2 的 code 与 vars。code 只读，vars 每个 VM 要私有副本。"""
    cands = [
        Path(os.environ.get("HOMEBREW_PREFIX", str(HOME / "hb"))),
        Path("/opt/homebrew"), Path("/usr/local"),
    ]
    for base in cands:
        code = base / "share" / "qemu" / "edk2-aarch64-code.fd"
        vars_ = base / "share" / "qemu" / "edk2-arm-vars.fd"
        if code.is_file() and vars_.is_file():
            return code, vars_
    die("找不到 edk2-aarch64-code.fd / edk2-arm-vars.fd。"
        "它们随 QEMU 一起安装（brew install qemu）。")


def cmd_create(cfg: Cfg) -> None:
    cfg.vm_dir.mkdir(parents=True, exist_ok=True)
    ensure_ssh_key(cfg)

    if cfg.disk.exists():
        ok(f"磁盘已存在：{cfg.disk}")
    else:
        run([find_tool("qemu-img"), "create", "-f", "raw",
             str(cfg.disk), cfg.disk_size],
            capture=True)
        ok(f"已建 {cfg.disk_size} 可增长磁盘 {cfg.disk}")

    code, vars_tmpl = find_edk2()
    if not cfg.vars_fd.exists():
        shutil.copy(vars_tmpl, cfg.vars_fd)
        ok(f"已初始化 EFI NVRAM {cfg.vars_fd}")
    cfg._edk2_code = code  # type: ignore[attr-defined]


def qemu_args(cfg: Cfg, edk2_code: Path, media: Path | None) -> list[str]:
    """组装 qemu-system-aarch64 参数。这套配置是实测能启动并装完 Win11 ARM 的。"""
    args = [
        "-name", "winarm",
        "-accel", "hvf",
        "-cpu", "host",
        "-M", "virt,highmem=on",
        "-m", cfg.ram,
        "-smp", cfg.cpus,
        # UEFI：code 只读，vars 每个 VM 私有可写（存启动项与 EFI NVRAM）
        "-drive", f"if=pflash,format=raw,readonly=on,file={edk2_code}",
        "-drive", f"if=pflash,format=raw,file={cfg.vars_fd}",
        # 输入
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd",
        "-device", "usb-tablet",
        # 系统盘走 virtio-scsi，驱动已注入安装介质
        "-device", "virtio-scsi-pci,id=scsi0,iothread=io0",
        "-object", "iothread,id=io0",
        "-drive", (f"file={cfg.disk},format=raw,if=none,id=hd,"
                   f"cache=writeback,discard=on,detect-zeroes=on"),
        "-device", "scsi-hd,drive=hd,bus=scsi0.0,bootindex=1",
        "-rtc", "base=localtime",
        "-device", "virtio-rng-pci",
        # 网络：QEMU 自带 user 模式，客人机可直接出网；端口只绑 127.0.0.1
        "-netdev", (f"user,id=net0,"
                    f"hostfwd=tcp:127.0.0.1:{cfg.rdp_port}-:3389,"
                    f"hostfwd=udp:127.0.0.1:{cfg.rdp_port}-:3389,"
                    f"hostfwd=tcp:127.0.0.1:{cfg.ssh_port}-:22"),
        "-device", "virtio-net-pci,netdev=net0",
        # 控制
        "-monitor", f"unix:{cfg.monitor},server,nowait",
        "-pidfile", str(cfg.pidfile),
    ]

    if media is not None:
        # 光盘 bootindex 比盘高也没关系：空盘没有 EFI 启动项，固件自然落到
        # 光盘；等 Setup 把引导写入磁盘后，后续重启就走磁盘，安装过程的中途
        # 重启因此能接着走而不是从头再来。
        args += [
            "-drive", f"file={media},format=raw,if=none,id=cd,media=cdrom,readonly=on",
            "-device", "usb-storage,drive=cd,bootindex=2",
        ]

    # framebuffer 设备两种模式都要有。-vga none 下没有任何显示设备，QEMU 的
    # screendump 会因为没有 framebuffer 而失败，排障时看不见客人机停在哪一步；
    # 而 aarch64 的默认 VGA 设备 Windows ARM 没有驱动，直接用会黑屏。ramfb 是
    # 纯内存 framebuffer，不占资源，Windows 靠 UEFI framebuffer 交接能驱动它。
    args += ["-vga", "none", "-device", "ramfb"]

    if cfg.headless:
        # 无人值守：没有窗口，只留 VNC 备用通道。launchd 开机自启走这条，
        # 否则每次登录 Mac 都会弹一个虚拟机窗口。
        args += ["-display", "none"]
    else:
        # 本机直接开窗口。VM 就跑在宿主机上，没必要绕 VNC 或远程桌面。
        # Cocoa 前端会起一个原生 macOS 窗口，标题栏有电源键和缩放。
        args += ["-display", "cocoa"]

    # VNC 备用通道，headless 时也能看画面。display 号不写死：宿主机上
    # 5900 很可能已经被别的东西占了（macOS 屏幕共享就常用这个段），
    # QEMU 会直接以 "Failed to find an available port" 退出。
    args += ["-vnc", f"127.0.0.1:{free_vnc_display(cfg)}"]
    return args


def free_vnc_display(cfg: Cfg) -> int:
    """从配置的 display 号起往上找第一个 127.0.0.1 上没人占的端口，并把选中
    的号写进 vm_dir/vnc.display。

    必须写盘：启动时探到空闲只是「那一刻」空闲，VM 跑起来后这个号才是真相。
    status 是在 VM 已经跑着的时候再探一次，端口占用情况变了，很可能探出另一个
    号。原先 status 直接重新探，报出去的 VNC 端口和 QEMU 实际监听的不是一个，
    用户照 status 给的端口连必然连不上（实测报 5902、实际监听 5901）。
    """
    import socket
    base = int(cfg.vnc_display)
    for d in range(base, base + 64):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sk.bind(("127.0.0.1", 5900 + d))
            except OSError:
                continue
            try:
                cfg.vnc_display_file.write_text(str(d))
            except OSError:
                pass
            return d
    die("找不到空闲的 VNC 端口（试了 64 个 display）")


def recorded_vnc_display(cfg: Cfg) -> int:
    """QEMU 实际在用的 display 号。没有记录时退回配置里那个号，**不重新探**。

    原来回退去调 free_vnc_display()，而它是会写盘的（它自己的注释就写着：
    启动时探到的空闲只是「那一刻」的空闲，写盘才是为了让 status 报的和 QEMU
    实际监听的一致）。于是 snapshot restore 刚删掉的 vnc.display——它注释里
    写明「留着会让 status 报一个已经不监听的端口」——被下一次**只读**的 status
    又写回来，用户照 status 给的端口连上去什么都没有。一次查询把一次清理
    撤掉了，2026-11-11 审计实测复现。只读的路径不能写盘。
    """
    try:
        return int(cfg.vnc_display_file.read_text().strip())
    except (OSError, ValueError):
        return int(cfg.vnc_display)


def qemu_running(cfg: Cfg) -> int | None:
    """QEMU 进程号，没在跑就返回 None。

    原来用 `os.kill(pid, 0)`，两个坑：
      1. **僵尸进程也算「在跑」。** 对僵尸发 0 号信号一样成功。QEMU 被强杀之后
         有几十毫秒到几秒是僵尸，这段时间 cmd_start 会返回「VM 已在运行」而
         不重启，cmd_stop 则认为还在跑、走去关机流程再等一轮超时。
      2. **pid 会被复用。** 只认 pidfile 里的数字，不核对是不是我们的 QEMU，
         别的进程占了同一个号就会被当成 VM 在跑——然后被 kill -9。
    scripts/vmkit.py 的 qemu_pid() 已经用 ps 修掉了同一件事（注释里写着踩坑
    经过），而 vmkit 的 Windows 分支正是委托到这儿，所以两边必须一致。
    """
    if not cfg.pidfile.is_file():
        return None
    try:
        pid = int(cfg.pidfile.read_text().strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    r = subprocess.run(["ps", "-p", str(pid), "-o", "state=,comm="],
                       capture_output=True, text=True)
    line = r.stdout.strip()
    if not line:
        return None
    parts = line.split(None, 1)
    if parts[0].startswith("Z"):
        return None
    if len(parts) < 2 or "qemu" not in parts[1]:
        return None
    return pid


def cmd_start(cfg: Cfg, install: bool = False,
              foreground: bool = False) -> int:
    if qemu_running(cfg):
        ok(f"VM 已在运行（pid {qemu_running(cfg)}）")
        return 0
    if not cfg.disk.exists() or not cfg.vars_fd.exists():
        die("VM 还没建。先跑 create。")
    code, _ = find_edk2()
    media = cfg.prepared_iso if (install and cfg.prepared_iso.exists()) else None
    if install and media is None:
        die(f"找不到安装介质 {cfg.prepared_iso}。先跑 prepare。")

    args = qemu_args(cfg, code, media)
    log("启动 QEMU（HVF 硬件加速）")
    with open(cfg.qemu_log, "ab") as lf:
        proc = subprocess.Popen([find_tool("qemu-system-aarch64"), *args],
                                stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        if qemu_running(cfg):
            ok(f"已启动 pid {qemu_running(cfg)}，日志 {cfg.qemu_log}")
            if not foreground:
                return 0
            # 前台模式：堵在这里等 QEMU 退出，并把它的退出码原样交出去。
            # launchd 的 KeepAlive 监护的是它拉起的那个进程，如果这里直接
            # 返回，包装脚本秒退 0，QEMU 变成没人管的孤儿，崩了也不会被
            # 拉起来，KeepAlive 等于白配。
            log("前台模式：等 QEMU 退出（launchd 会按退出码决定是否重启）")
            rc = proc.wait()
            if rc == 0:
                ok("QEMU 正常退出")
                return 0
            # wait() 对信号死亡返回负数（-9 是 SIGKILL）。直接把 -9 交给
            # SystemExit 会得到 247 这种看不出所以然的码，launchd 的
            # LastExitStatus 更是变成 63232。按 shell 的惯例编码成
            # 128+信号号，至少非零、能反推出是被什么信号打死的。
            code = 128 - rc if rc < 0 else rc
            warn(f"QEMU 非正常退出（{code}），launchd 应该会重启它")
            return code
    die(f"QEMU 启动失败，看 {cfg.qemu_log}")


_qga = None


def qga():
    """惰性加载 scripts/lib/qga.py（QEMU guest agent 客户端）。

    和 vmkit.py 那边同一份代码，两个入口各装一次：vmkit 的 Windows 分支是
    把本模块 import 进来用的，而本模块也要能单独跑（`winarm-vm.py stop`），
    所以不能依赖调用方先把它装上。用 importlib 按路径装而不是
    `from lib import qga`：lib/ 不是包，那样得把 scripts/ 塞进 sys.path，而
    lib 是个太通用的名字，会和别处同名模块撞。
    """
    global _qga
    if _qga is None:
        spec = importlib.util.spec_from_file_location(
            "qga", Path(__file__).resolve().parent / "lib" / "qga.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        _qga = mod
    return _qga


def qga_shutdown_wait(cfg: Cfg, wait: int = 60) -> bool:
    """客人机里有 qemu-guest-agent 就走它关机，返回是否已经停下。

    **Windows ARM64 侧默认走不到这儿。** qemu-ga 的 Windows ARM64 安装包
    （qemu-ga-arm64.msi）目前只有 virtio-win-guest-tools-installer PR #100 /
    #104 的 CI artifact，没有正式 release 产物；UTM #5134 那边说 guest agent
    未移植到 ARM64 Windows。两条证据对不上，所以我们**不**在 qemu_args 里加
    virtio-serial 和这个 chardev：加了也只是建一个没人接的 socket，反而让
    这里每次 stop 都白等一轮超时。

    那为什么还留着这段：用户自己装了社区版的 ARM64 agent、并按 Linux 那边
    的约定把 socket 放到 <vm>/qga.sock，这里就会自动用上，不需要改代码。
    判断依据只有「文件在不在」，没有任何隐式探测。
    """
    if not cfg.qga_sock.exists():
        return False
    try:
        qa = qga()
    except OSError:
        # 只有 scripts/lib/qga.py 真的丢了才会走到这儿。VM 还在跑，不能为
        # 这个 die，后面的 SSH、ACPI、强杀照样能停。
        return False
    try:
        if not qa.qga_ping(cfg.qga_sock, timeout=5):
            return False
        log("guest agent 可达，让它走自己的关机流程")
        qa.qga_shutdown(cfg.qga_sock)
    except qa.QGAError as e:
        warn(f"guest agent 关机没成：{e}")
        return False
    for _ in range(wait):
        time.sleep(1)
        if not qemu_running(cfg):
            ok("已优雅关机")
            return True
    warn(f"等了 {wait} 秒 guest agent 还没关掉，试下一条路")
    return False


def cmd_stop(cfg: Cfg) -> None:
    """关机。四条路按可靠性排序，前几条失败了才强杀。

    为什么先试 SSH：ACPI 的 system_powerdown 是「电源键事件」，要客人机里
    有交互会话去处理它。停在登录界面时没人接这个事件，等到超时也关不掉，
    最后只能强杀，而 raw 盘被强杀有丢数据的风险。SSH 的 shutdown 是让
    Windows 自己走完整关机流程，最稳。

    guest agent 排在 SSH 前面，但它对 Windows ARM64 默认不存在，见
    qga_shutdown_wait 的说明——正常情况下这一步是零成本直接跳过。
    """
    pid = qemu_running(cfg)
    if not pid:
        ok("VM 没在运行")
        return

    if qga_shutdown_wait(cfg):
        return

    if ssh_ready(cfg, timeout=8):
        log("SSH 可达，走客人机自己的关机流程")
        r = run(["ssh", "-i", str(cfg.ssh_key), "-p", cfg.ssh_port,
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                 f"{cfg.username}@127.0.0.1",
                 "shutdown /s /t 5 /f"], check=False, capture=True)
        if r.returncode == 0:
            log("已发 shutdown，等 120 秒")
            for _ in range(120):
                time.sleep(1)
                if not qemu_running(cfg):
                    ok("已优雅关机")
                    return
        else:
            warn(f"SSH 关机命令失败：{(r.stderr or '').strip()[:120]}")

    if cfg.monitor.exists():
        try:
            import socket as s
            with s.socket(s.AF_UNIX, s.SOCK_STREAM) as sk:
                sk.settimeout(5)
                sk.connect(str(cfg.monitor))
                sk.sendall(b"system_powerdown\n")
            log("已发 ACPI 电源键事件，等 90 秒")
            for _ in range(90):
                time.sleep(1)
                if not qemu_running(cfg):
                    ok("已优雅关机")
                    return
        except OSError:
            pass

    warn("优雅关机超时，强杀。raw 盘可能留下脏数据，下次启动 Windows 会自愈")
    run(["kill", "-9", str(pid)], check=False, capture=True)
    # 发完 -9 必须等它真的退干净。
    #
    # kill 是异步的：信号出去到进程变成僵尸/被回收之间有一段窗口，这时候 ps 还
    # 看得见它。紧接着任何「起虚拟机」都会走 qemu_running → 看到「VM 已在运行」
    # → 什么都不做就返回。用户改的 CPU/内存/显示模式一个都没生效，而日志说停过了。
    #
    # vmkit.py 的 Windows 分支早就给这条路包了一层轮询兜底（3547 行起，注释写的
    # 就是这件事），但那是外层：winarm 作为独立子命令被调用、launchd 自启、以及
    # snapshot save --clean --restart 这条不经过 vmkit 的路，都不被覆盖。
    # 2026-11-11 审计从根上对齐两边语义，而不是只在外层包。
    for _ in range(50):
        if not qemu_running(cfg):
            break
        time.sleep(0.2)
    ok("已强杀")


def ssh_ready(cfg: Cfg, timeout: float = 12) -> bool:
    """真的发一条 SSH 命令看能不能通。

    不能只探 TCP 端口：QEMU 的 hostfwd 在 VM 一启动时就开始监听宿主端口，
    跟客人机里有没有 sshd 完全无关。只探端口会在安装刚开始就误报「就绪」，
    而那时 SSH 连 banner 都换不来。必须真发一次命令，拿到输出才算通。
    """
    cmd = [
        "ssh", "-i", str(cfg.ssh_key),
        "-p", cfg.ssh_port,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={int(timeout)}",
        "-o", "BatchMode=yes",
        f"{cfg.username}@127.0.0.1",
        "echo winarm-ok",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "winarm-ok" in r.stdout


def disk_used(p: Path) -> float:
    """磁盘真实占用字节数。

    不能用 st_size：稀疏盘的 st_size 是逻辑大小（64G 的盘就一直是
    68.7e9），实际只占了 6 GB。st_blocks * 512 才是真正分配出去的块。
    """
    st = p.stat()
    return st.st_blocks * 512


def cmd_status(cfg: Cfg) -> None:
    pid = qemu_running(cfg)
    print(f"VM 目录    : {cfg.vm_dir}")
    print(f"运行状态   : {'运行中 pid ' + str(pid) if pid else '未运行'}")
    if cfg.disk.exists():
        print(f"磁盘       : {cfg.disk} "
              f"（逻辑 {cfg.disk.stat().st_size / 1e9:.1f} GB，"
              f"实际占用 {disk_used(cfg.disk) / 1e9:.2f} GB）")
    else:
        print("磁盘       : 未创建")
    print(f"安装介质   : {cfg.prepared_iso}" if cfg.prepared_iso.exists() else "安装介质   : 未准备")
    print(f"SSH        : 127.0.0.1:{cfg.ssh_port} "
          f"{'可达' if ssh_ready(cfg) else '不可达'}")
    print(f"RDP        : 127.0.0.1:{cfg.rdp_port}")
    print(f"VNC        : 127.0.0.1:{5900 + recorded_vnc_display(cfg)}")
    if cfg.qemu_log.exists():
        tail = cfg.qemu_log.read_text(errors="replace").strip().splitlines()[-6:]
        print("QEMU 日志尾部:")
        for l in tail:
            print("   " + l[:160])


def cmd_install(cfg: Cfg, wait_min: int = 30) -> None:
    if platform.machine() != "arm64" or platform.system() != "Darwin":
        die("只在 Apple Silicon 的 macOS 上有效。")
    cmd_prepare(cfg)
    cmd_create(cfg)
    cmd_start(cfg, install=True)

    log(f"等 SSH 就绪（最多 {wait_min} 分钟）。安装本身约 10 到 15 分钟。")
    deadline = time.time() + wait_min * 60
    last = 0.0
    while time.time() < deadline:
        if ssh_ready(cfg):
            ok(f"SSH 已就绪：ssh -i {cfg.ssh_key} -p {cfg.ssh_port} {cfg.username}@127.0.0.1")
            return
        if not qemu_running(cfg):
            die(f"QEMU 中途退出了。看 {cfg.qemu_log}")
        now = time.time()
        if now - last > 60:
            last = now
            grew = ""
            if cfg.disk.exists():
                grew = f"，磁盘已写 {disk_used(cfg.disk) / 1e9:.1f} GB"
            print(f"  .. 已等 {int((now - (deadline - wait_min * 60)) / 60)} 分钟，"
                  f"SSH 未就绪{grew}", flush=True)
        time.sleep(5)
    warn(f"等了 {wait_min} 分钟 SSH 仍未就绪。VM 还在跑，"
         f"可用 VNC 127.0.0.1:{5900 + recorded_vnc_display(cfg)} 看画面，"
         f"或看 {cfg.qemu_log}")


def cmd_screenshot(cfg: Cfg, out: str = "/tmp/winarm-screen.ppm") -> None:
    """通过 QEMU monitor 抓一帧客人机画面。

    只有配了 framebuffer 设备才抓得到。-vga none 且没有 ramfb 时
    screendump 会静默失败，什么都不生成。
    """
    if not cfg.monitor.exists():
        die(f"monitor 套接字不存在：{cfg.monitor}。VM 没在运行？")
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(10)
            sk.connect(str(cfg.monitor))
            # 绝对不能带 quit。monitor 的 quit 是让 QEMU 退出，抓一次屏
            # 就把 VM 杀了，而且日志里只会留下一行 terminating on signal 15，
            # 看起来像被外部打死，极易误判成环境问题。
            sk.sendall(f"screendump {out}\n".encode())
            # 原来是固定 sleep(2)。screendump 是 QEMU 收到命令后异步做的：画面
            # 简单时几十毫秒就落盘了，白等两秒；画面复杂时又可能还没写完。
            # 改成轮询文件出现——文件一出现说明 QEMU 已经开始写了，再给 0.2 秒
            # 收尾。带 3 秒上限，超了就走下面那句「没抓到画面」，那本来就是这条
            # 路的失败出口。2026-11-11 审计补上。
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    if Path(out).stat().st_size > 0:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            time.sleep(0.2)
    except OSError as e:
        die(f"连 monitor 失败：{e}")
    p = Path(out)
    if not p.is_file() or p.stat().st_size < 1024:
        die("没抓到画面。可能 VM 没配 framebuffer 设备，或 VM 没在运行。")
    ok(f"画面已存 {out}（{p.stat().st_size / 1024:.0f} KB）")


def _clone_file(src: Path, dst: Path) -> None:
    """APFS 克隆复制，瞬间完成且几乎不占额外空间。

    64G 稀疏盘（实际 5.9G）实测 0.003 秒完成，克隆后两者共享数据块，
    各自写入时才真正复制。所以快照可以随便建，不用担心把磁盘占满。

    先写临时名再 os.replace，避免还原到一半被打断留下半个盘。
    """
    tmp = dst.with_name(dst.name + ".tmp-clone")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    r = subprocess.run(["cp", "-c", str(src), str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # 不是 APFS（或克隆失败）就退回普通复制，慢但至少能用
        r = subprocess.run(["cp", str(src), str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die(f"克隆 {src} 失败：{(r.stderr or '').strip()[:200]}")
    os.replace(tmp, dst)


def _snap_dir(cfg: Cfg, name: str) -> Path:
    """快照目录，同时挡住路径穿越。

    名字里带 / 或 .. 会跳出快照根目录，写到哪里去都不受控，必须拦掉。
    """
    if not name or "/" in name or name in (".", "..") or name.startswith("-"):
        die(f"快照名不合法：{name!r}。用字母、数字、- 和 _。")
    return cfg.snapshots / name


def _migrate_shared_snapshots(cfg: Cfg) -> None:
    """把老版本共用的快照根搬进这个 VM 自己的目录。

    见 Cfg.snapshots：共用根让第二台 VM 拍不了同名快照，还会把 A 的盘还原
    到 B 上。搬之前要能说清这些快照是谁的——共用目录里没有任何 VM 信息，
    同目录下有两台以上虚拟机就分不出来，那种情况下让用户自己决定，不猜。
    """
    old = cfg.vm_dir.parent / "snapshots"
    new = cfg.snapshots
    if old == new or not old.is_dir():
        return
    entries = [p for p in old.iterdir()]
    if not entries:
        # 空目录：清掉它，别留一个以后又被误当成快照根的共用目录
        try:
            old.rmdir()
        except OSError:
            pass
        return
    if new.is_dir() and any(new.iterdir()):
        die(f"同时存在共用的 {old}（{len(entries)} 个快照）和我自己的 {new}"
            f"（已有内容），不知道该用哪个。请手动确认后删掉其中一个。")
    siblings = [p for p in cfg.vm_dir.parent.iterdir()
                if p.is_dir() and p.name != "snapshots"]
    if len(siblings) > 1:
        die(f"快照目录原来是所有虚拟机共用的 {old}，里面有 {len(entries)} 个快照，"
            f"而同目录下有 {len(siblings)} 台虚拟机，分不出这些快照属于谁。\n"
            f"请手动把它们移到各自的 <VM 目录>/snapshots/ 下，然后删掉 {old}。")
    log(f"快照目录从共用的 {old} 搬到我自己的 {new}")
    new.parent.mkdir(parents=True, exist_ok=True)
    # 用 rename 不用 shutil.move：后者遇到「目标是已存在目录」会把源目录
    # 整体搬进目标里面去，出来的是 new/snapshots/...，下一次又回到共用根。
    # POSIX 的 rename 允许把目录换成同名的空目录，正好覆盖 new 刚被
    # mkdir(parents=True) 建出来的情况。
    try:
        os.rename(str(old), str(new))
    except OSError as e:
        die(f"搬不动 {old} -> {new}：{e}。请手动处理后再试。")


def cmd_snapshot(cfg: Cfg, action: str, name: str = "",
                 clean: bool = False, restart: bool = False) -> None:
    """快照与还原。底层是 APFS 的 copy-on-write 克隆，不是 QEMU 内部快照。

    为什么不用 QEMU 的 savevm/loadvm：那两个只支持 qcow2，而这块盘是
    raw（稀疏、QEMU 直接读写，装系统时就是这么建的）。改格式要重建磁盘，
    成本比重构安装还高。

    一致性要说清楚：VM 在跑时存的快照，等于把宿主机的电源拔了。客人机
    自己还没落盘的写缓存会丢，NTFS 有日志能自愈，但正在跑的程序状态不
    保证。要一个保证干净的点，就加 --clean：它会先关机、存完再拉起来。
    """
    cfg.snapshots.mkdir(parents=True, exist_ok=True)
    _migrate_shared_snapshots(cfg)

    if action == "save":
        if not name:
            die("save 要带快照名")
        if not cfg.disk.exists():
            die(f"磁盘不存在 {cfg.disk}")
        d = _snap_dir(cfg, name)
        if d.exists():
            die(f"快照 {name} 已存在。先删或换个名字。")
        was_running = bool(qemu_running(cfg))
        if was_running and clean:
            log("--clean：先关机再存快照")
            cmd_stop(cfg)
        elif was_running:
            warn("VM 正在运行，这个快照等价于拔宿主电源（崩溃一致，非干净快照）。"
                 "要干净快照加 --clean。")
        d.mkdir(parents=True)
        t0 = time.time()
        _clone_file(cfg.disk, d / "data.img")
        if cfg.vars_fd.exists():
            _clone_file(cfg.vars_fd, d / "efi-vars.fd")
        size = sum(p.stat().st_blocks * 512 for p in d.iterdir())
        ok(f"快照 {name} 已存（{size / 1e9:.2f} GB 实际占用，"
           f"耗时 {time.time() - t0:.2f} 秒）")
        if was_running and clean and restart:
            cmd_start(cfg)

    elif action == "list":
        snaps = sorted(p for p in cfg.snapshots.iterdir() if p.is_dir()) \
            if cfg.snapshots.is_dir() else []
        if not snaps:
            print("没有快照")
            return
        print(f"快照目录 {cfg.snapshots}")
        for p in snaps:
            size = sum(f.stat().st_blocks * 512 for f in p.iterdir())
            has_disk = (p / "data.img").is_file()
            print(f"  {p.name:24s} {size / 1e9:7.2f} GB  "
                  f"{'含磁盘' if has_disk else '缺磁盘(坏)'}  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}")

    elif action == "restore":
        if not name:
            die("restore 要带快照名")
        d = _snap_dir(cfg, name)
        if not (d / "data.img").is_file():
            die(f"快照 {name} 不存在或没有磁盘文件。用 snapshot list 看有哪些。")
        if qemu_running(cfg):
            log("VM 在运行，先关机再还原")
            cmd_stop(cfg)
        for fn in ("data.img", "efi-vars.fd"):
            src = d / fn
            dst = cfg.disk if fn == "data.img" else cfg.vars_fd
            if not src.is_file():
                continue
            _clone_file(src, dst)
            ok(f"已还原 {fn}")
        # 还原后 pid 文件指向的是已经死掉的进程，不清掉 status 会误报在跑。
        # vnc.display 同理，留着会让 status 报一个已经不监听的端口。
        if cfg.pidfile.exists():
            cfg.pidfile.unlink()
        if cfg.vnc_display_file.exists():
            cfg.vnc_display_file.unlink()
        ok(f"已还原到快照 {name}")
        if restart:
            cmd_start(cfg)

    elif action == "delete":
        if not name:
            die("delete 要带快照名")
        d = _snap_dir(cfg, name)
        if not d.is_dir():
            die(f"快照 {name} 不存在")
        _force_rmtree(d)
        ok(f"已删除快照 {name}")

    else:
        die(f"不认识的快照动作：{action}。用 save / list / restore / delete。")


def cmd_reset(cfg: Cfg, name: str, restart: bool = False) -> None:
    """重置：还原到指定快照并重新启动。

    单独一个子命令而不是塞进 snapshot，是因为「重置」的语义就是
    还原加重启一条龙，而 snapshot restore 默认不该自己拉起来。
    """
    cmd_snapshot(cfg, "restore", name, restart=False)
    if restart:
        cmd_start(cfg)


def cmd_autostart(cfg: Cfg, disable: bool = False) -> None:
    """装或卸 launchd 开机自启。

    两个不显然的点：

    一、必须是 LaunchAgent，不能是 LaunchDaemon。VM 住在用户主目录下，
    用 root daemon 跑会在 ~/.winarm 里造出 root 属主的 data.img 和
    efi-vars.fd，之后用户自己那条 `winarm-vm.py stop` 反而写不进去。
    代价是 LaunchAgent 绑在登录会话上，登出就会被收掉，VM 跟着停。

    二、KeepAlive 只接非零退出。`stop` 走的是 ACPI 关机，QEMU 会正常
    退 0；如果 KeepAlive 无脑拉起所有退出的进程，那就永远关不掉 VM，
    每次 stop 完一秒后又起来了。写成 SuccessfulExit=false，QEMU 真崩
    （非零退出）才重启，正常关机不重启。
    """
    label = "com.agentvm.winarm"
    agents = Path.home() / "Library" / "LaunchAgents"
    plist = agents / f"{label}.plist"
    wrapper = Path.home() / ".winarm" / "winarm-launchd.sh"
    logfile = Path.home() / ".winarm" / "winarm-launchd.log"

    if disable:
        if plist.exists():
            run(["launchctl", "unload", str(plist)], check=False, capture=True)
            plist.unlink()
            ok(f"已卸载并删除 {plist}")
        else:
            ok("本来就没装开机自启")
        if wrapper.exists():
            wrapper.unlink()
        return

    if not cfg.disk.exists() or not cfg.vars_fd.exists():
        die("VM 还没建。先跑 create，再装开机自启。")

    hb = _qemu_prefix()
    # sys.executable 可能是 ~/.local/bin/python3 这种符号链接，launchd 给的
    # PATH 里没有那个目录。解析成真实路径，免得启动时 exec 失败。
    py = Path(sys.executable).resolve()
    script = Path(__file__).resolve()

    wrapper.parent.mkdir(parents=True, exist_ok=True)
    # **三个插值点全部要 shlex.quote。** 原来原样写进 wrapper，而 wrapper 是
    # 被 launchd 当 /bin/sh 脚本执行的：vm_dir 里带一个双引号就能把后面的
    # export 行闭合并接上任意命令，每次登录都会跑一遍。危害仅限命令行自伤
    # （只有本机用户自己能传 --vm-dir），但同一个仓库的 vmkit.cmd_autostart
    # 对这一类是挡的（见它 4922 行的注释：「命令行自伤面，但仍然要挡」），
    # 所以这是漏挡，不是取舍。2026-11-11 审计补上。
    q_vm_dir = shlex.quote(str(cfg.vm_dir))
    q_py = shlex.quote(str(py))
    q_script = shlex.quote(str(script))
    wrapper.write_text(f"""#!/bin/sh
# 由 launchd 调用的 winarm VM 启动包装。不要手工改，
# `winarm-vm.py autostart` 会重写它。
#
# 为什么需要这一层：launchd 不继承交互 shell 的 PATH，而
# qemu-system-aarch64 在 {hb}/bin。不显式给 PATH 的话 QEMU 起不来，
# 日志里只剩一行 exec 失败，看不出是路径问题。
export PATH="{hb}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOMEBREW_PREFIX="{hb}"
export VM_DIR={q_vm_dir}
# HEADLESS=0 让 QEMU 用 Cocoa 前端开一个原生 macOS 窗口。VM 就在本机，没必要
# 绕 VNC 或远程桌面。ramfb 仍然保留，Windows 靠它出图；不想每次登录都弹窗口
# 就把这行改成 1，或直接用 `winarm-vm.py start` 手工起。
export HEADLESS="0"
# stdin 必须是 /dev/null。launchd 给的是关掉的管道，QEMU 的 monitor 往
# stdin 读会直接 EOF 退出。
# --foreground 让这个进程堵到 QEMU 退出为止，launchd 的 KeepAlive 才监护
# 得到真正干活的那个进程，而不是监护一个早就退出的启动器。
exec {q_py} {q_script} start --foreground < /dev/null
""")
    wrapper.chmod(0o755)

    agents.mkdir(parents=True, exist_ok=True)
    if plist.exists():
        run(["launchctl", "unload", str(plist)], check=False, capture=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{wrapper}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <!-- Nice 是关键。QEMU 的客人机线程是真实线程，不降优先级的话它会把
         WindowServer 挤下去，表象是键盘鼠标失灵——不是坏了，是输入事件
         排不上 CPU。正值 = 更低优先级，10 足够让交互进程永远先走。 -->
    <key>Nice</key>
    <integer>10</integer>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>{logfile}</string>
    <key>StandardErrorPath</key>
    <string>{logfile}</string>
</dict>
</plist>
""")

    r = run(["launchctl", "load", str(plist)], check=False, capture=True)
    if r.returncode != 0:
        die(f"launchctl load 失败：{(r.stderr or r.stdout or '').strip()}")

    ok(f"开机自启已装：{plist}")
    print(f"  登录时启动，QEMU 非零退出才自动重启（正常关机不拉起来）")
    print(f"  启动日志 {logfile}")
    print(f"  卸掉：winarm-vm.py autostart --disable")


def _qemu_prefix() -> Path:
    """定位 QEMU 所在的 Homebrew 前缀，装 launchd 时给 PATH 用。"""
    env = os.environ.get("HOMEBREW_PREFIX")
    if env and (Path(env) / "bin" / "qemu-system-aarch64").is_file():
        return Path(env)
    for base in (HOME / "hb", Path("/opt/homebrew"), Path("/usr/local")):
        if (base / "bin" / "qemu-system-aarch64").is_file():
            return base
    return HOME / "hb"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Apple Silicon 上 QEMU+HVF 无人值守安装 Windows 11 ARM64")
    ap.add_argument("--vm-dir", default=None, help="虚拟机目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("prepare", help="改造安装介质（注入驱动、写应答文件、重建 ISO）")
    sub.add_parser("create", help="建磁盘与 EFI NVRAM")
    sub.add_parser("start", help="启动 VM")
    p_start = None
    for name, sp in sub.choices.items():
        if name == "start":
            p_start = sp
    if p_start is not None:
        p_start.add_argument("--foreground", action="store_true",
                             help="前台等 QEMU 退出并透传退出码（launchd 自启用）")
        p_start.add_argument("--install", action="store_true",
                             help="带上准备好的安装介质启动")
    sub.add_parser("install", help="全流程：准备介质、建 VM、装系统、等 SSH")
    sub.add_parser("status", help="看状态")
    p_shot = sub.add_parser("screenshot", help="抓客人机画面到 PPM（排障用）")
    p_shot.add_argument("out", nargs="?", default="/tmp/winarm-screen.ppm")
    sub.add_parser("stop", help="关机")
    p_logs = sub.add_parser("logs", help="看 QEMU 日志")
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_auto = sub.add_parser("autostart", help="装/卸 launchd 开机自启")
    p_auto.add_argument("--disable", action="store_true", help="卸掉开机自启")
    p_snap = sub.add_parser("snapshot", help="快照：save / list / restore / delete")
    p_snap.add_argument("action", choices=["save", "list", "restore", "delete"])
    p_snap.add_argument("name", nargs="?", default="", help="快照名")
    p_snap.add_argument("--clean", action="store_true",
                        help="save 时先关机，得到干净快照（默认是崩溃一致的）")
    p_snap.add_argument("--restart", action="store_true",
                        help="restore 后重新启动 VM")
    p_reset = sub.add_parser("reset", help="还原到快照并重新启动")
    p_reset.add_argument("name", nargs="?", default="pristine", help="快照名，默认 pristine")
    p_reset.add_argument("--no-start", action="store_true", help="还原后不启动")
    p_inst = None
    for name, sp in sub.choices.items():
        if name == "install":
            p_inst = sp
    if p_inst is not None:
        p_inst.add_argument("--wait", type=int, default=30, help="等 SSH 的分钟数")

    args = ap.parse_args()
    if args.vm_dir:
        os.environ["VM_DIR"] = args.vm_dir
    cfg = Cfg()

    if args.cmd == "prepare":
        cmd_prepare(cfg)
    elif args.cmd == "create":
        cmd_create(cfg)
    elif args.cmd == "start":
        return cmd_start(cfg, install=args.install, foreground=args.foreground)
    elif args.cmd == "install":
        cmd_install(cfg, args.wait)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "screenshot":
        cmd_screenshot(cfg, args.out)
    elif args.cmd == "stop":
        cmd_stop(cfg)
    elif args.cmd == "logs":
        if not cfg.qemu_log.exists():
            die(f"没有日志 {cfg.qemu_log}")
        if args.follow:
            run(["tail", "-f", str(cfg.qemu_log)], check=False)
        else:
            run(["tail", "-n", "40", str(cfg.qemu_log)], check=False)
    elif args.cmd == "autostart":
        cmd_autostart(cfg, args.disable)
    elif args.cmd == "snapshot":
        cmd_snapshot(cfg, args.action, args.name,
                     clean=args.clean, restart=args.restart)
    elif args.cmd == "reset":
        cmd_reset(cfg, args.name, restart=not args.no_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
