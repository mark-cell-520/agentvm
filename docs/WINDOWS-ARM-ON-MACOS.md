# 在 Apple Silicon Mac 上装 Windows 11 ARM64 虚拟机

本文件记录 `scripts/winarm-vm.py` 的实现与实测结果。目标：在这台 Mac mini
（Apple Silicon，macOS 27.0.1）上无人值守装好一个 Windows 11 ARM64 虚拟机。

## 为什么不能用 Virtualization.framework

Windows 在 Apple Silicon 上跑不了 VZ，这不是脚本问题，是结构性缺失：

- VZ 没有 vTPM。Windows 11 安装与启动要求 TPM 2.0。
- VZ 的 virtio 设备集是固定的，而 Windows 没有对应驱动。
- 通用 EFI 启动链有缺口，Windows 的 bootmgr 起不来。

所以 tart、lume、apple/container 全都跑不了 Windows。UTM 对 Windows 也是
退回自己的 QEMU 后端。唯一可行的是 QEMU 加 HVF（Hypervisor.framework），
这正是 UTM 对 Windows 采用的引擎。

## 官方镜像从哪来

微软的 `www.microsoft.com/software-download/windows11arm64` 页面确实提供
`3816  Windows 11 (multi-edition ISO for Arm64)`。但它的下载链接由
`software-download-connector` API 动态给出，未登录时返回：

```json
{"Key":"ErrorSettings.GenericError","Value":"Not authorized","Type":0}
```

这个 API 只在页面同源上下文里放行，顶层导航和 curl 都会被 Azure Front Door
的 WAF 挡掉（本机 edge 定位 country_code=CN）。要真拿到得登录微软账号并
加入 Windows Insider。

绕开的办法：用 Enterprise Evaluation 的直连 CDN 地址。这是微软自己服务器上
的文件，不需要账号：

```
https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26100.1.240331-1435.ge_release_CLIENTENTERPRISEEVAL_OEMRET_A64FRE_en-us.iso
sha256 dad633276073f14f3e0373ef7e787569e216d54942ce522b39451c8f2d38ad43
大小 4295096320 字节
```

评估版 90 天免产品密钥，适合当虚拟机用。实测下载完整、SHA-256 逐字一致。
下载速度 16 MB/s（微软 CDN 不限速）。

## 依赖怎么装

本机 `/opt` 不可写、sudo 要密码、没有 brew。所以 QEMU 走「直接装
Homebrew bottle」的路：

1. `codeload.github.com` 拉 Homebrew 本体到 `~/hb`。
2. 从 `ghcr.io` 拉 qemu 及其依赖的 bottle。
3. 手动解压到 Cellar、改写 install name、建符号链接。

关键约束与对应办法：

- **Homebrew bottle 只能重定位到不超过 13 字符的前缀。** `~/hb` 是 12 字符，
  刚好够。`~/.homebrew` 是 19 字符，超了会被迫源码编译。
- **bottle 是可重定位的**：Mach-O 里写的是 `@@HOMEBREW_PREFIX@@` 占位符，
  安装时替换成真实前缀。
- **改写必须用 `install_name_tool`，不能用二进制替换。** 第一版用「替换加
  null 补足等长」原地改，结果 install name 被从中间截断，只剩 `/Users/mm/hb`，
  二进制跑不起来。`-change` 改依赖引用，`-id` 改库自身 ID，两者都要用。
- **改完必须重签。** `install_name_tool` 破坏代码签名，macOS 对签名不匹配的
  二进制直接 SIGKILL，症状是进程能起但零输出、退出码 137。qemu 还要把
  `com.apple.security.hypervisor` entitlement 原样带回去，否则没有 HVF。
- **`openssl@4` 在 ghcr 上的仓库名是 `openssl/4`**：Homebrew 把公式名里的
  `@` 换成 `/`。写 `openssl@4` 会 NAME_INVALID，写 `openssl` 会 DENIED。
- **gnutls 与 nettle 的版本要配对。** gnutls 3.8.13 要 `libhogweed.6`，
  nettle 4.0 给的是 `libhogweed.7`。退回 nettle 3.10.2 的 arm64_tahoe bottle
  （macOS 27 没有这个老版本的 bottle，取最接近的 macOS 版本）就对了。
- **ghcr.io 单连接约 40 KB/s，8 路并行能到 280 KB/s。** 113 MB 的 qemu
  bottle 单连接要一小时，并行八分钟。Range 是支持的（206），但要用
  `curl -L -C -`；Python 手拼 Range 那版没被服务端遵守，文件在 11 MB 和
  23 MB 之间反复，永远下不完。

## 安装介质怎么改

官方 ARM64 ISO 是 UDF 为主（install.wim 3.58 GB，ISO9660 放不下），
macOS 的 `hdiutil` 能挂载，`bsdtar` 不能。所以用挂载加 `ditto` 复制。

改三处：

1. **virtio 驱动注入 WIM。** 盘走 virtio-scsi，Windows Setup 不认识，不注入
   驱动连盘都看不见。用 `wimlib-imagex update` 把驱动加到 boot.wim 与
   install.wim 的每个镜像索引，路径 `/$WinPEDriver$`。这个 ISO 里是
   install.wim 而不是 install.esd，所以两个都能改；若是 esd 就只能改
   boot.wim，装好的系统要靠 OEM 目录补驱动。
2. **`$OEM$` 暂存。** 驱动与首次登录脚本必须放 `sources/$OEM$/$1/`，
   放 ISO 根目录会被 Setup 静默忽略，结果 C:\OEM 和 C:\Drivers 都不出现。
3. **autounattend.xml。** 跳过 TPM/SecureBoot/RAM 检查、跳过 OOBE、建本地
   管理员、自动登录，并用 `RunOnce` 挂首次登录脚本。

重建 ISO 必须带 `-udf`，且 El Torito 的 `boot-load-size` 要覆盖整个 efisys
镜像，否则 edk2 加载到被截断的 FAT，卡在 "Start boot option"。

镜像版本名从 install.wim 里用 `wimlib-imagex info` 读，实测是
`Windows 11 Enterprise Evaluation`。autounattend 里指定 `/IMAGE/NAME`
才能跳过「选择映像」那一步。

## QEMU 配置

```
-accel hvf -cpu host -M virt,highmem=on
-drive if=pflash,...,readonly=on,file=edk2-aarch64-code.fd
-drive if=pflash,...,file=efi-vars.fd          # 每个 VM 私有副本
-device qemu-xhci -device usb-kbd -device usb-tablet
-device virtio-scsi-pci,id=scsi0,iothread=io0 -object iothread,id=io0
-drive file=data.img,format=raw,if=none,id=hd,cache=writeback,discard=on,detect-zeroes=on
-device scsi-hd,drive=hd,bus=scsi0.0,bootindex=1
-rtc base=localtime -device virtio-rng-pci
-netdev user,id=net0,hostfwd=tcp:127.0.0.1:12222-:22,hostfwd=tcp:127.0.0.1:13389-:3389
-device virtio-net-pci,netdev=net0
```

光盘的 bootindex 要**低于**磁盘。空盘没有 EFI 启动项，固件自然落到光盘；
等 Setup 把引导写进磁盘后，后续重启就走磁盘，安装过程的中途重启因此能接着
走而不是从头再来。反过来（光盘优先）会在 Setup 里死循环。

### 显示：本机就用原生窗口

`-vga none -device ramfb` 两种模式都要有，只切换显示后端：

```
# 无人值守（launchd 开机自启走这条，否则每次登录 Mac 都弹窗口）
-display none -vnc 127.0.0.1:1
# 交互（VM 就在本机，直接开窗口，不要绕远程桌面）
-display cocoa
```

由 `HEADLESS` 环境变量控制，默认写在 `~/.winarm/winarm-launchd.sh` 里。

**framebuffer 不能省**：`-vga none` 下没有显示设备，`screendump` 抓不到帧，
排障时看不见客人机停在哪一步；而 aarch64 的默认 VGA 设备 Windows ARM 没有
驱动，直接用必黑屏。ramfb 是纯内存 framebuffer，Windows 靠 UEFI framebuffer
交接驱动它，代价是分辨率锁死 800x600。

Cocoa 窗口标题是 `QEMU winarm - (Press ⌃⌥G to release Mouse)`。View 菜单有
`Zoom To Fit`、`Zoom Interpolation`、`Enter Fullscreen`。**不开 Zoom To Fit
时窗口尺寸是锁死的**，AppleScript 改 `size` 和点绿色缩放钮都没反应，开了才能
缩放。launchd 拉起的窗口不会自动抢前台，要 `activate`。

## 实测结果

全部在本机跑通，不是推断：

| 项 | 结果 |
|---|---|
| ISO | 官方 Enterprise Evaluation ARM64，4.3 GB，SHA-256 一致 |
| 安装 | 无人值守，约 16 分钟，中途重启正常续上 |
| 系统 | Microsoft Windows 11 Enterprise Evaluation，ARM64 |
| 虚拟化 | HVF 硬件加速，`com.apple.security.hypervisor` entitlement 生效 |
| 盘 | virtio-scsi，QEMU HARDDISK / SAS，64G 稀疏实际占 6.1 GB |
| 网 | Red Hat VirtIO Ethernet Adapter，10 Gbps，客人机 10.0.2.15，可 ping 通 1.1.1.1 |
| 驱动库 | 9 个 Red Hat virtio 驱动 |
| SSH | 127.0.0.1:12222，密钥登录，hostname 返回 WIN-SED9FKTVSIQ |
| RDP | 127.0.0.1:13389 |
| VNC | 127.0.0.1:5905 |

## 踩过的坑

### QEMU monitor 的 quit 会杀掉 VM

排障时给 monitor 发 `screendump xxx` 习惯性带上 `quit` 收尾。monitor 的
`quit` 是让 QEMU 退出，于是每抓一次屏就把 VM 杀了，日志里只留下一行
`terminating on signal 15`，看起来像被外部打死。为这个误判了好几轮，
一度以为是环境在杀进程。**给 monitor 发命令永远不要带 quit。**

### 就绪判断不能只探 TCP 端口

QEMU 的 hostfwd 在 VM 一启动时就开始监听宿主端口，跟客人机里有没有 sshd
无关。只探端口会在安装刚开始就误报「SSH 就绪」，而那时连 banner 都换不来。
必须真发一次 SSH 命令、拿到输出才算通。

### OpenSSH Server 的 capability 装不上，但服务能用

`Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0` 返回
`Online : True` 却什么都没装，`Get-WindowsCapability` 仍是 NotPresent。
退路是从 GitHub 装 Win32-OpenSSH ARM64，这条路能成。

而 Win32-OpenSSH ARM64 的 SCM 服务会崩（错误 1067），非交互上下文里
`sshd.exe` 也起不来。唯一可靠的是在自动登录用户的高权限交互会话里跑
`sshd.exe -D`，用一个 AtLogOn 的计划任务挂着。虚拟机配了自动登录，
所以每次开机 SSH 都会起来。最终 `Get-Service sshd` 显示 Stopped，
但 SSH 实际是通的。

### 首次登录可能被弹窗打断

OneDrive 的「Turn On Windows Backup」弹窗会抢焦点，把后续按键送到别处，
表现为命令打出来缺字符。按 Esc 关掉再操作。

### 后台进程要 nohup 包一层

`nohup cmd &` 直接起在工具调用的 shell 里，调用结束会被收掉。套一层
`nohup wrapper.sh &`，由 wrapper 再起目标进程，才能跨调用存活。

### autounattend 里的 LogonCount 会让自动登录只生效一次

autounattend.xml 的 `<AutoLogon>` 写了 `<LogonCount>1</LogonCount>`，
意思是自动登录只算一次。Setup 那次用完，Windows 就把注册表里的
`AutoAdminLogon` 重置回 `0`，之后每次重启都停在登录界面。

这个坑阴在 SSH 上：sshd 是靠「自动登录用户的高权限交互会话」拉起来的
（见上一节），登录界面进不去，交互会话就没有，SSH 就起不来。表象是
「VM 明明在跑，SSH 却连不上」，很容易误判成 sshd 或网络的问题。

两个修法要一起上：模板里删掉 `LogonCount`，再在客人机首次登录脚本里
把 `AutoAdminLogon=1`、`DefaultUserName`、`DefaultPassword`、
`DefaultDomainName`（填计算机名）显式写进注册表，并删掉
`AutoLogonCount`——它在的时候自动登录会递减，用完就停。

### 关机优先走 SSH，不要只发 ACPI 电源键

`system_powerdown` 是「电源键事件」，要客人机里有交互会话去处理它。
停在登录界面时没人接这个事件，等到超时也关不掉，最后只能强杀，而
raw 盘被强杀有丢数据的风险。

实测同一台机器：登录状态下 `system_powerdown` 8 秒退出；停在登录界面时
60 秒等不到，只能 `kill -9`。所以 `stop` 的顺序改成：SSH 可达就先
`shutdown /s`，再退到 ACPI，最后才强杀。

现在最前面还多了一条 guest agent（`qga_shutdown_wait`），但它对 Windows
ARM64 默认不走，原因见下一节。正常情况下那一步是零成本直接跳过。

### Windows ARM64 的 guest agent：等正式包，不硬上

`cmd_stop` 里那条 guest agent 的路是通用的，Windows 侧也留着，但
`qemu_args` 里**没有** `virtio-serial` 和 `org.qemu.guest_agent.0`。两条
证据对不上，不能赌：

1. qemu-ga 的 Windows ARM64 安装包（`qemu-ga-arm64.msi`）目前只有
   virtio-win-guest-tools-installer PR #100 / #104 的 CI artifact，没有正式
   release 产物（PR #100 还被关了）。
2. UTM #5134 说 guest agent 未移植到 ARM64 Windows。

硬加设备的后果不是「没效果」而是「有反效果」：QEMU 只要带了这个 chardev 就会
把 `<vm>/qga.sock` 建出来，于是每次 `stop` 都先去连一个没人接的 socket，白等
一轮超时——本来 8 秒能关掉的机器变成每次都多等一段。

宿主机侧的判断只有「socket 在不在」，没有任何隐式探测：自己装了社区版的
ARM64 agent、并按 Linux 那边的约定把 socket 放到 `<vm>/qga.sock`，代码自动
用上，不用改。Linux 云镜像那条路是齐全的（README「客人机代理与共享目录」），
因为 `qemu-guest-agent` 在 Debian/Ubuntu 源里就有。

### 按名字调 QEMU 会在没有 PATH 的地方炸

`Popen("qemu-system-aarch64")` 依赖调用方的 PATH 里有 `~/hb/bin`。从
launchd、cron 或者任何没继承交互 shell 环境的进程里调，抛的是一个
光秃秃的 `FileNotFoundError`，看不出是路径问题——排查时极易往
「QEMU 坏了」的方向跑偏。所有外部工具一律先解析成绝对路径再用。

### VM 会把宿主机键盘吃到失灵（最贵的一个坑）

配置成 `-smp 4 -m 8G` 时，QEMU 实测稳定吃掉 105% CPU。这台 Mac mini
12 核 16G，叠加 Lark 一类重占用应用后，系统 15 分钟负载均值被顶到
**12.64**——等于 12 个核全满。

**表象极具误导性：宿主机键盘输入失灵。** 不是键盘坏了，也不是 QEMU 抓了
输入，是 WindowServer 抢不到 CPU，输入事件排不上队。排查时第一个念头
会往 HID、驱动、权限方向跑，全都错。

三个措施一起上才治住：

1. **vCPU 从 4 降到 2，内存从 8G 降到 4G。** 这个用途 2 核 4G 完全够，
   而且留得出余量。默认值就按「不拖垮宿主机」定，不按性能最大化定。
2. **launchd 任务加 `Nice = 10`。** QEMU 的客人机线程是真实线程，不降
   优先级它就会把交互进程挤下去。正值 = 更低优先级。加完实测 QEMU 稳态
   1.2% CPU、启动峰值 22.8%，宿主机负载稳在 1.1。
3. **开机自启不要默认 armed。** 出过事之后，先确认配置安全再开。

紧急处置的顺序有讲究：**必须先 `launchctl bootout` 再杀 QEMU。**
`KeepAlive` 配的是非零退出才重启，直接 `kill` QEMU 会算非零退出，
15 秒后（`ThrottleInterval`）又被拉起来，等于白杀。

## 复现步骤

```bash
# 1. 装 QEMU 工具链（首次，约 20 分钟，主要是 ghcr.io 下载）
/tmp/winarm-probe/fetch-bottles.py       # 拉 bottle，支持断点续传与并行
python3 /tmp/winarm-probe/install-bottles.py --prefix ~/hb

# 2. 下载 Windows 11 ARM64 评估版 ISO（4.3 GB，约 5 分钟）
curl -L -o /tmp/winarm-iso/win11-arm64-eval.iso \
  "https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26100.1.240331-1435.ge_release_CLIENTENTERPRISEEVAL_OEMRET_A64FRE_en-us.iso"

# 3. 下载 virtio ARM64 驱动包
curl -L -o /tmp/winarm-virtio/virtio.tar.xz \
  "https://github.com/qemus/virtiso-arm/releases/download/v0.1.285-1/virtio-win-0.1.285.tar.xz"
bsdtar -C /tmp/winarm-virtio/drv -xf /tmp/winarm-virtio/virtio.tar.xz

# 4. 一键安装
export PATH="$HOME/hb/bin:$PATH"
export ISO_PATH=/tmp/winarm-iso/win11-arm64-eval.iso
export VIRTIO_PATH=/tmp/winarm-virtio/drv
python3 scripts/winarm-vm.py install --wait 45

# 5. 连上去
ssh -i ~/.winarm/ci_key -p 12222 Docker@127.0.0.1
```

`install` 会做：改造介质、建盘、起 QEMU、等 SSH。等 SSH 用真实命令探测，
不是探端口。

安装完想再起来：

```bash
python3 scripts/winarm-vm.py start
```

不需要再 `export PATH="$HOME/hb/bin:$PATH"`。脚本会用 `find_tool()`
按 `HOMEBREW_PREFIX`、`~/hb`、`/opt/homebrew`、`/usr/local` 的顺序自己找
QEMU，PATH 里没有也能跑。所以从 launchd、cron 这类不继承交互 shell 环境的
地方调它也没问题。

## 开机自启

```bash
python3 scripts/winarm-vm.py autostart              # 装
python3 scripts/winarm-vm.py autostart --disable    # 卸
```

装的是一个 LaunchAgent（`~/Library/LaunchAgents/com.agentvm.winarm.plist`），
配 `RunAtLoad`，登录时把 VM 拉起来。三个设计要点：

**必须是 LaunchAgent 不能是 LaunchDaemon。** VM 住在用户主目录下，用 root
daemon 跑会在 `~/.winarm` 里造出 root 属主的 `data.img` 和 `efi-vars.fd`，
之后用户自己的 `stop` 反而写不进去。代价是 LaunchAgent 绑在登录会话上，
登出会被收掉，VM 跟着停。

**启动器必须前台等 QEMU 退出。** 第一版包装脚本是 `Popen` 后立即返回，
launchd 拉起的那个进程秒退 0，QEMU 变成没人管的孤儿，`KeepAlive` 配了
也白配——崩了不会有人拉它。所以 `start` 加了 `--foreground`，堵到 QEMU
退出并把退出码透传出去。

**KeepAlive 只接非零退出。** `stop` 走 ACPI/SSH 关机，QEMU 正常退 0；
如果无脑拉起所有退出的进程，那就永远关不掉 VM，每次 `stop` 完 15 秒后又
起来了。写成 `SuccessfulExit=false`，QEMU 真崩（非零退出）才重启。

实测两条分支：`kill -9` QEMU 后 launchd 在 `ThrottleInterval`（15 秒）后
自动重启；正常关机后不重启，`LastExitStatus` 保持 0。

启动链是：Mac 登录 → launchd → 包装脚本（给 PATH、VM_DIR）→
`winarm-vm.py start --foreground` → QEMU → 客人机自动登录 →
AtLogOn 计划任务拉起 `sshd -D` → SSH 可用。冷启动到 SSH 就绪约 20 到 40 秒。

## 快照与重置

```bash
python3 scripts/winarm-vm.py snapshot save pristine --clean   # 存（先关机，干净快照）
python3 scripts/winarm-vm.py snapshot list
python3 scripts/winarm-vm.py snapshot restore pristine        # 还原
python3 scripts/winarm-vm.py snapshot delete pristine
python3 scripts/winarm-vm.py reset pristine                   # 还原 + 重启
```

底层是 APFS 的 copy-on-write 克隆（`cp -c`），不是 QEMU 内部快照。
64G 稀疏盘（实际 6.45 GB）实测 0.02 秒存完，克隆后与原件共享数据块，
各自写入时才真正复制，所以快照可以随便建。快照在 `~/.winarm/snapshots/<名>/`，
含 `data.img` 与 `efi-vars.fd` 两份。

**为什么不用 QEMU 的 `savevm`/`loadvm`**：那两个只支持 qcow2，而这块盘是
raw（稀疏、QEMU 直接读写，装系统时就是这么建的）。改格式要重建磁盘，
成本比重装还高。

**一致性要说清楚。** VM 在跑时存的快照，等价于把宿主机的电源拔了：客人机
自己还没落盘的写缓存会丢，NTFS 有日志能自愈，但正在跑的程序状态不保证。
要一个保证干净的点就加 `--clean`，它会先关机、存完再拉起来。

实测验证过还原真的生效：客人机里写一个 `C:\winarm-marker.txt`，还原到
快照后该文件消失（`Test-Path` 返回 `False`）。快照名做了路径穿越防护，
带 `/`、`..`、`-` 开头的一律拒。

## 当前状态

已装好并在跑。VM 目录 `~/.winarm/win11/`，磁盘 6.1 GB。用
`scripts/winarm-vm.py status` 看状态，`stop` 关机。

未做的部分：

- 没有把 QEMU 接进 agentvm 的 backend 抽象层，目前是独立脚本。
  `backends/base.py`、`backends/tart.py`、`backends/hyperv.py` 定义了它
  没接进去的那套接口。
- Windows 是评估版，90 天后要重新弄或自己授权。
- Windows 侧没有 QEMU guest agent：`qemu-ga-arm64.msi` 只有 PR 的 CI
  artifact，没有正式 release 产物，详见「Windows ARM64 的 guest agent」那一节。
  宿主机侧的代码已经留着位置，包一出来就能接上。
