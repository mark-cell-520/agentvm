# agentvm

在 Mac 上一键装一个完整的虚拟机，Windows 11、macOS 或者 Linux 都行。免费，开源，
M 芯片原生虚拟化，装系统全程看得见也点得动。给 AI agent 当安全沙箱，也给你自己当
随删随建的试验场。

[English below](#english)

```sh
./install.sh
```

装的是 `/Applications/AgentVM.app`。打开点左上角的 + ，选系统，点开始安装，
剩下的它自己跑完——下载、校验、建盘、无人值守配置、启动、等就绪，全程不用管。
Windows 约 10 到 15 分钟，Linux 约 1 到 3 分钟。装完虚拟机是一个原生 macOS
窗口，就在本机，不用开远程桌面。

## 四件事

**1. 可视化安装。** 装系统那十几分钟里，app 内嵌的控制台是一个真 VNC 客户端——
不是隔几秒抓一张图，是真画面，能拖动滚动条、能在输入框里打字、能点「下一步」。
实测用它在 app 里把 fnOS（飞牛）ARM64 从 ISO 一步步装到了能用的系统，宿主机上
直接打开它的 Web 界面。也支持自己的安装包：向导第一步下面有「用自己的安装包装」，
选一个本地 ISO，或者贴一个下载网址（后端带断点续传）。

**2. 可视化配置。** 装完之后改 CPU、内存、磁盘、显示模式、三个端口、挂不挂安装
光盘、共享目录、端口转发，都在「配置」面板里。改完点「保存并重启」立刻生效——不用重新安装系统，
系统里的东西全留着。原来改内存只能删了重装，而删了重装意味着系统里的一切全没。

**3. 存储四件套。** 「存储」面板里能加附加盘、拍快照、挂共享目录、克隆：
- **附加盘**：填个大小就加，raw 或 qcow2 都行，重启挂上，系统里分区格式化就能用。
- **快照**：qcow2 内部快照，拍一张，之后想退回这个状态就回滚，不用重装。秒级。
- **共享目录**：virtio-9p 把宿主机文件夹挂进虚拟机，传文件不用再走 SSH scp。
- **克隆**：照一个装好的虚拟机复制一份。想试什么就克隆一份去折腾，折腾坏了删掉，
  原来那份还在。qcow2 overlay 是秒级的，和源共用底册、不额外占空间。
- macOS 虚拟机上快照和克隆同样能用（走 APFS 的 clonefile，秒级、不占额外空间），
  附加盘和共享目录不行——原因写在 app 的「存储」面板里，不是忘了做。

**4. 一条命令的等价命令行。** 同一个后端，不想用 app 也行：

```sh
scripts/vmkit.py install --distro debian-13 --name d1
scripts/vmkit.py disk add --name d1 --size 64G --format qcow2
scripts/vmkit.py snapshot create --name d1 --tag clean
scripts/vmkit.py clone --name d1 --new-name d2
```

## 能装什么

Windows 11 24H2 Enterprise Evaluation ARM64、macOS 27.2、Ubuntu Server 24.04 /
22.04 LTS ARM64、Debian 13 / 12 ARM64，加上任何你自己的 ISO（fnOS、各种发行版、
各种 NAS 系统都行）。

## 它是完整的虚拟机，不是容器

有独立的系统、独立的用户、独立的文件系统和独立的 systemd。agent 在里面装什么、
改什么、删什么，都碰不到你的真实环境。不想要了，一条命令连虚拟机带数据一起删干净，
宿主不留痕迹。

## 快速开始

需要 Apple Silicon 的 Mac（macOS 14 以上）和 macOS 自带的 Command Line Tools
（里面有 `swiftc`，不需要装 Xcode）。QEMU 由 `install.sh` 自动装，装不上会明确
告诉你卡在哪一条。磁盘：Windows/Linux 留 12 GB 以上，macOS 虚拟机要 60 GB 以上。

```sh
git clone https://github.com/mark-cell-520/agentvm.git
cd agentvm
./install.sh          # 装 QEMU（如果需要）+ 编 app + 装到 /Applications
open /Applications/AgentVM.app
```

app 里点 + ，选系统，点开始安装。装机的全过程都在 app 里，包括用自己安装包时
那个要你手动点的安装器。

## 四条不同的安装路径

装不同的系统走的是四条完全不同的路，这不是没统一好，是它们本来就不一样：

| | Windows 11 | macOS | Linux 云镜像 | 自己的安装包 |
|---|---|---|---|---|
| 系统盘 | raw，Setup 真写块设备 | raw，恢复镜像写入 | qcow2 overlay，共用一份底册 | raw |
| 怎么装 | 注驱动 + autounattend 应答文件 + 真跑 Setup | 从 Apple 官方 IPSW 恢复，全自动 | cloud-init 从种子盘喂账号，没有安装程序 | 图形安装器，用户自己点 |
| 能不能全自动 | 能 | 能，装完自动建账号并打开 SSH | 能 | **不能，也不装成能** |
| 磁盘后端 | QEMU + HVF | **Virtualization.framework** | QEMU + HVF | QEMU + HVF |
| 画面 | 内嵌 VNC / 原生窗口 | **app 内 VZ 原生画面** | 内嵌 VNC / 原生窗口 | 内嵌 VNC |
| 下载量 | 4.3 GB | **25.6 GB** | 0.4–0.7 GB | 你自己给 |

Windows 为什么必须用 QEMU 而不是 Apple 的 Virtualization.framework：VZ 没有
vTPM、virtio 设备集固定而 Windows 无对应驱动、EFI 启动链有缺口。这三条都是
实测撞过的，不是推测。

macOS 反过来，必须用 VZ 而不是 QEMU：QEMU 的 `vmapple` 机型是专为 macOS 客人机
设计的，但 Homebrew 的 11.1.3 没注册它（`strings` 能看到 VMApple 符号，
`-machine vmapple` 报 unsupported）；自己打补丁编一份要连带 Reims vGPU、
OpenAVB ROM、idevicerestore 补丁一整套。而 VZ 的 `VZMacOSRestoreImage` /
`VZMacOSInstaller` / `VZMacOSBootLoader` 是公开 API，宿主机上直接拿到
macOS 27.2.1 的 IPSW 地址。详见 [docs/AGENTVM-APP.md](docs/AGENTVM-APP.md)。

## 装机速度：把 apt 那段从几分钟压到几十秒

Linux 云镜像「下载完就能用」，但 `install` 报「已就绪」之后还有一段在跑：
cloud-init 要现装 `qemu-guest-agent`，而那之前有一次 `apt-get update`。
2026-11-10 实测这一段要 25 到 140 秒（Debian 13 更是永远完不成），
占掉整个装机时间的大头。三件事把它压下来：

**1. 装之前先给 apt 镜像测速。** 官方源实测只有 0.3 MB/s，国内镜像 5 到 10 MB/s。
`install` 会并发探四个源、取最快的写进种子盘，全程约 4 秒。
不想测就指定：`AGENTVM_APT_MIRROR=https://...` 环境变量，或 vm.json 里的
`apt_mirror` 字段。海外用户会测出官方源最快，不会被迫走国内镜像。

**2. 裁掉用不到的索引。** 种子盘里写一份 `/etc/apt/apt.conf.d/99zz-agentvm-fastindex`：
不下载 Translation（每个 suite×component 一份）、索引就地压缩不展开、
只用 IPv4、关掉 DEP-11 和 CNF 两个索引目标。Ubuntu 24.04 上实测
`apt-get update` 从 155 秒降到 46 秒，落盘从 152 MB 降到 57 MB。
代价写在那份文件的注释里（`apt show` 没有本地化描述、`command-not-found` 失效），
删掉文件就回默认行为。

**3. Debian 的 security 源地址是错的，得换。** Debian trixie 云镜像自带的
`/etc/apt/mirrors/debian-security.list` 指向 `<镜像>/debian/`，而 Debian 的
security 套件在这些镜像上是**另一条树** `<镜像>/debian-security/`——四个源
实测前者全 404、后者全 200。apt 反复重试之后报
`does not have a Release file`，cloud-init 的装包模块失败，
`cloud-init status` 停在 `error`。这就是 docs/PITFALLS.md M8 记了半天的
「Debian 13 apt 卡住」的真正原因。现在对 Debian 走
`preserve_sources_list` 加自写 deb822，实测 `qemu-guest-agent` 从永远装不上
变成 8 秒上线。

三件加起来，三个系统等 guest agent 的时间：

    Ubuntu 24.04   169 秒 → 38 秒
    Ubuntu 22.04   132 秒 → 72 秒
    Debian 13      永远卡住 → 8 秒

SSH 就绪之后会显示「等 guest agent 上线」并报进度，不再是干等。
到 180 秒还不通才 warn，VM 照样能用（SSH 和窗口不受影响）。

## 客人机代理与共享目录

Linux 云镜像那条路从 install 起就带着 QEMU guest agent：cloud-init 装
`qemu-guest-agent`，QEMU 挂上 `virtio-serial`，宿主机经 `<vm>/qga.sock` 讲
JSON-RPC。它给的是一条**不依赖客人机网络**的通道——sshd 没起来、IP 变了、
防火墙把 22 挡了，SSH 都用不了，它照样通。

```sh
python3 scripts/vmkit.py guest --name d1 ping              # 探活
python3 scripts/vmkit.py guest --name d1 ip                # 客人机的 IPv4
python3 scripts/vmkit.py guest --name d1 disks             # 客人机能看到的块设备
python3 scripts/vmkit.py guest --name d1 exec -- uname -a  # 在客人机里执行
```

关机顺序也因此变了：guest agent → SSH → ACPI 电源键 → 强杀。qcow2 被强杀有丢
数据风险，所以前面每一层都值得试。加 agent 之前就起着的老 VM 没有这个 socket，
那一步自动跳过，不报错。

**Windows on ARM 侧没有它，也不硬上。** qemu-ga 的 Windows ARM64 安装包
（`qemu-ga-arm64.msi`）目前只有 virtio-win-guest-tools-installer PR #100 /
#104 的 CI artifact，没有正式 release 产物；UTM #5134 说 guest agent 未移植到
ARM64 Windows。两条证据对不上，所以等正式包。宿主机侧的代码是「socket 存在就
试」：自己装了社区版 agent、并按 Linux 那边的约定把 socket 放到 `<vm>/qga.sock`
就能用上，不用改代码。详见 [docs/WINDOWS-ARM-ON-MACOS.md](docs/WINDOWS-ARM-ON-MACOS.md)。

### tart 的目录共享、VNC 与端口暴露

macOS 上 Linux 走 tart 的那条路（`agentvm.py`）把 `tart run` 的三个能力透传出来：

```sh
python3 agentvm.py start --dir ~/Shared:host --dir ~/Code:code \
                         --vnc 127.0.0.1:5901 \
                         --expose 8080:80
```

客人机里的挂载点是 `/Volumes/My Shared Files/<标签>`。`--dir` 和 `--expose`
可以重复给。也可以写进 `.agentvmrc`（`tart_dirs=~/Shared:host,~/Code:code`、
`tart_vnc=127.0.0.1:5901`、`tart_expose=8080:80`），或用环境变量
`AGENTVM_TART_DIRS` / `AGENTVM_TART_VNC` / `AGENTVM_TART_EXPOSE`。端口必须是
1-65535 的整数，路径要真实存在——配错了在启动前就报出来，不会起一半再吐一句
看不懂的 usage。

端口暴露走 tart 的 `--net-softnet-expose`，这是 tart 官方的端口转发方案：比
自己在宿主机上起一个 socat 稳，多一个进程就多一个会静默死掉的东西，而它死了
之后的表象是「端口突然连不上」，跟 VM 本身没关系。这三个参数只对 tart 后端
有意义，Hyper-V 那边给了也不生效（不报错）。

## macOS 虚拟机：能装，但有三条要知道的限制

在 Apple Silicon 的 Mac 上再装一个 macOS，用的是 Apple 官方的
Virtualization.framework——这条路能走通，但有三条限制是框架定的，不是这个
项目偷懒：

**1. 它跟着 app 的寿命。** VZ 的虚拟机不能跨进程传递，所以 app 里启动的
macOS 虚拟机，app 一退出它就停。Windows/Linux 那套「安装甩后台、app 可以关」
的做法在这儿做不到。命令行 `vmkit.py start --name m1` 也是前台跑——那个终端
就是虚拟机的寿命，Ctrl-C 优雅关机。要长期挂着就用 app，或者用开机自启
（launchd 那条路会在每次登录后把它拉起来）。

**2. 它没有 VNC。** 所以 app 里那套自研 RFB 控制台对 macOS 虚拟机用不上，
画面是 app 内嵌的原生视图——比 VNC 清楚也快，但只能用 app 看。命令行也
没有窗口可切。

**3. 磁盘要留够。** 恢复镜像 25.6 GB，装完之后系统盘实际写三十多 GB。
下载是单连接的（实测并发反而更慢，见下），中断了重跑会接着下。

恢复镜像有全局缓存：`~/.agentvm/images/ipsw/`，和 Linux 云镜像、Windows ISO、
virtio 驱动同一个待遇——一个文件全机器共用，随虚拟机的创建与删除生灭无关。
装第二台 macOS 虚拟机时它直接被硬链过来，0 字节新占空间，那 25.6 GB 的下载
也整段跳过（实测省 20 分钟以上）。缓存命中时 `install` 一开头就会说
「复用本地恢复镜像 … 预计 5 到 10 分钟（镜像已在本地，不含下载）」，
磁盘预估也跟着从 60 GB 降到 28 GB。

**关于下载速度，别再往上加连接。** 2026-11-10 在 Apple CDN 上复测：
串行单连接下四个 100 MB 段，2.8 / 3.1 / 3.0 / 5.5 秒（19 到 36 MB/s）；
6 个 curl 并发、每段 100 MB，段耗时 8.8 到 16.2 秒，聚合 37 MB/s；
2 个并发时每段 12.2 到 17.9 秒，比串行还慢。CDN 是按客户端限速的，
加连接只是把每条的份额摊薄。所以下载就是一条 curl 连接加 `-C -` 续传。

装完第一次启动会自动建账号、打开远程登录（SSH），不用进 Setup Assistant 手动点。

宿主机连不进客人机这件事，现在有一条公开路径能走一半：**端口转发（vsock）**。
VZ 的 NAT 附件没有端口映射 API（运行时实测 `VZNATNetworkDeviceAttachment` 只有
5 个方法，方法名里带 port 的有 0 个），vsock 是唯一公开的进客路径，所以宿主机上
开一个只绑 127.0.0.1 的 TCP 监听，每个连接换一条到客人机 vsock 端点的连接：

```sh
./scripts/vmz/vmz forward add --name m1 --host 2222 --guest 22   # 之后重启生效
# 安装时一起给也行（可重复）：
#   vmz install --name m1 --forward 2222:22 --forward 8022:22
```

**但要说清楚它通到哪一步**：宿主机这边（监听、转发、重试）是通的，
`VZNATNetworkDeviceAttachment` 不给的端口映射由它补上。客人机那边需要有个程序
监听对应的 vsock 端口，而 **macOS 客人机默认没有这样的服务**——它的 sshd 只听
TCP，系统里没有现成的 vsock 监听者（vzvm 那条路能通，是因为它的客人机是 Linux，
nix-daemon/sshd 直接听在 vsock 端口上）。所以「宿主机不再对客人机完全封闭」这
件事做到了，「`ssh -p 2222 agent@127.0.0.1` 直接能进」还没有——那需要客人机里
有个听 vsock 的东西。界面上（配置面板 → 端口转发）和启动日志里都写着这句话，
不装作已经通了。

共享目录是另一件事，那个是真通了：**virtiofs**，宿主机目录自动挂到客人机的
`/Volumes/<挂载名>`，不需要在客人机里敲 mount（Linux 那边的 9p 要自己挂，
macOS 这边用的是 VZ 的自动挂载 tag， lima 的源码注释明确警告不能用自己起的 tag，
否则客人机里能看见卷但一访问就是 Operation not permitted）。

```sh
./scripts/vmz/vmz share add --name m1 --path ~/code --mount code   # 之后重启生效
# 安装时一起给也行（可重复）：
#   vmz install --name m1 --share ~/code:code --share ~/data:data:ro
```

```sh
./install.sh
# app 里点 + → 选 macOS 27.2 → 开始安装（约 40 到 60 分钟）
```

或者命令行（一样的东西，只是没有画面）：

```sh
python3 scripts/vmkit.py install --distro macos-27 --name m1 --disk 80G
python3 scripts/vmkit.py start --name m1      # 前台跑，Ctrl-C 关机
```

macOS 的许可只允许在 Apple 硬件上跑 macOS 虚拟机，这个项目不包含任何 Apple
软件——IPSW 由 Apple 的服务器按你的机器现取。

## Windows on ARM 上装

Windows 版走 Hyper-V，客人机是 ARM64 原生 Linux，不是模拟。

```sh
python agentvm.py check        # 检查宿主机，缺什么一次说清
python agentvm.py install      # 启用 Hyper-V 组件，需管理员
python agentvm.py fetch        # 下载 arm64 qcow2，校验 SHA512，转 VHDX
python agentvm.py start        # 启动并等就绪
python agentvm.py provision    # 客人机内换 apt 源、装编译工具链
python agentvm.py configure    # 推送 LLM / 飞书 配置，密钥不进命令行
python agentvm.py autostart    # 开机任务加客人机 systemd，两层
```

三条硬性前置条件：Windows 11 on ARM 专业版（Hyper-V 家庭版没有）、管理员权限
（这一条和 macOS 版不同，是 Hyper-V 的机制决定的）、磁盘留 15 GB 以上并建议把
`%USERPROFILE%\.agentvm` 加进 Defender 排除目录。

关于验证状态：Windows 侧代码是按 Hyper-V 命令行语义写的，**尚未在真实 Windows
on ARM 设备上跑过**。未验证项清单见 `docs/WINDOWS-PORT.md` 第 10 节。如果你在
目标机上跑通了或撞了墙，那是最有价值的信息。

## 离线导入（给「拉不动镜像」准备的一条备路）

网络正常时不需要这一套，直接从 ghcr.io 克隆就行。它的存在是因为实测这台机器到
ghcr.io 单连接 2.7 KB/s、并发聚合 35 KB/s，一个 600 MB 的 Debian 镜像要下 60 小时；
同一条网络下 `deb.debian.org` 38 KB/s，而清华源 12.5 MB/s，差 320 倍——不换源，
`build-essential` 要下 2.5 小时。而重启不丢的环境需要**两段**自启链，分属两个
操作系统，只配一段，重启后必静默。

```sh
make check && make install-tart && make fetch
make serve          # 另一个终端
make create-vm && make provision && make configure && make autostart
```

## 文档

- [docs/AGENTVM-APP.md](docs/AGENTVM-APP.md) — app 的实现：三条安装路径、内嵌 VNC
  控制台、JSON 事件协议、端口分配、可视化配置、存储四件套，以及每一处踩过的坑。
- [docs/WINDOWS-ARM-ON-MACOS.md](docs/WINDOWS-ARM-ON-MACOS.md) — 在 Apple Silicon
  上装 Windows 11 ARM64 的完整记录：官方镜像从哪来（免账号）、依赖怎么装（无 sudo）、
  autounattend 的 LogonCount 陷阱、原生窗口与 VNC 的坑。
- [docs/PITFALLS.md](docs/PITFALLS.md) — 12 个踩过的坑，按「有多反直觉」排序。
  Secrets 在传输中被替换、`tart exec` 的 `--` 陷阱、稀疏盘被 `cp` 写实……
  每一条都是实际花掉时间才定位的。
- [docs/OFFLINE-IMPORT.md](docs/OFFLINE-IMPORT.md) — 离线导入的原理：为什么不能往
  Tart 缓存目录塞文件，以及最小 OCI 注册表做了什么。
- [docs/WINDOWS-PORT.md](docs/WINDOWS-PORT.md) — Windows 版的未验证项清单。

## 安全

`config/env.template` 里默认**关闭**「允许所有飞书用户」和「绕过审批」。两个都开
意味着同租户任何人都能让你的 agent 在 VM 里跑任意命令。要开的话请先读那两段注释。

密钥一律从文件读取，经 base64 传输，落地 0600，用完即删，全程不经过命令行参数。
VM 名做白名单校验——所有 PowerShell 语句都是单引号插值，一个单引号就能闭合字符串
接任意代码，这个面在入口处挡掉。

虚拟机只绑 127.0.0.1，外面连不进来。macOS 虚拟机的网络是 VZ 的 NAT，
宿主机上默认一个端口都不开（配了 vsock 端口转发的那些也只绑 127.0.0.1，
见「macOS 虚拟机」那一节）。密码一律不进命令行参数：macOS 那条路会把密码写进
VM 目录里一个 0600 文件再传给后端，`ps` 看不到。

## 免费与开源

MIT 许可。没有付费版，没有功能锁定，没有账号，没有遥测。所有代码都在仓库里，
看得见也改得动。

---

<a id="english"></a>

## English

`agentvm` installs a full VM on your Mac — Windows 11, macOS, or Linux — with the
entire installation visible and clickable. Free, open source, Apple Silicon native.
A real sandbox for AI agents, and a throwaway lab for yourself.

```sh
./install.sh
open /Applications/AgentVM.app
```

Four things it does that most one-click VM installers do not:

1. **Visual installation.** A real RFB/VNC client is embedded in the app — not a
   screenshot every few seconds. You can drag scrollbars, type into text fields,
   and click "Next" inside the guest. It was used to install fnOS ARM64 from an
   ISO to a working system entirely inside the app, then reach its web UI from
   the host. Bring your own ISO, or paste a download URL (resumable).
2. **Visual configuration.** After install, change CPU, RAM, disk, display mode,
   three ports, and whether the install ISO is attached — then hit "Save and
   Restart" and it takes effect. No reinstall, and nothing inside the guest is
   lost. (macOS VMs have no ports to change — their network is NAT.)
3. **Storage.** Add data disks, take qcow2 snapshots and roll back to them in
   seconds, share a host directory into the guest over virtio-9p, and clone a
   working VM into a scratch copy that shares its backing file. macOS VMs get
   snapshots and clones too (APFS clonefile, instant, no extra space) but not
   data disks or directory shares — the reason is in the Storage panel, not
   forgotten.
4. **The same backend from the command line**, if you would rather not open an app.

Four genuinely different install paths sit underneath: Windows gets drivers
injected plus an autounattend answer file and really runs Setup; macOS is
restored from Apple's own IPSW through Virtualization.framework and creates its
account automatically; Linux cloud images are fed accounts through cloud-init
and have no installer at all; your own ISO has a graphical installer that only
you can click through. Windows cannot use Apple's Virtualization.framework at
all — no vTPM, a fixed virtio device set with no matching Windows drivers, and a
gap in the EFI boot chain. macOS cannot use QEMU: its `vmapple` machine type is
not registered in Homebrew's QEMU 11.1.3, and building one takes the whole Reims
vGPU / OpenAVB / idevicerestore patch stack. So each guest gets the engine that
actually works for it.

A macOS guest runs on Virtualization.framework, which comes with three limits
the framework imposes, not this project choosing: it lives and dies with the app
process (a VZ machine cannot be handed across processes, so closing the window
shuts it down); it has no VNC, so its picture is a native view inside the app
rather than the embedded RFB client; and its restore image is 25.6 GB with
another thirty-odd written by the installed system. The image is cached once in
`~/.agentvm/images/ipsw/` and hard-linked into each VM directory, so a second
macOS guest costs 0 bytes and skips the download entirely. The download is a
single curl connection on purpose — measured 2026-11-10 on Apple's CDN, six
parallel range requests aggregate no more than one serial connection, and two
are slower than one. The first boot creates the account and turns on remote
login, so there is no Setup Assistant to click through. macOS licensing permits
macOS VMs only on Apple hardware, and this repo ships no Apple software — the
IPSW is fetched from Apple's servers for your specific machine.

A Windows-on-ARM port runs on Hyper-V with an ARM64-native Linux guest. It is
written against Hyper-V's command-line semantics but **has not been run on real
Windows on ARM hardware**; the unverified list is in `docs/WINDOWS-PORT.md`.

An offline import path exists for networks that cannot reach container registries
at a usable speed — on one measured host, a 600 MB Debian image from `ghcr.io`
would have taken about 60 hours, while a Tsinghua mirror gave 12.5 MB/s, a 320x
difference. On a healthy network you will hit none of this.

MIT licensed. No paid tier, no feature gating, no account, no telemetry. Every
script is in the repo, readable and editable. VMs bind to 127.0.0.1 only; VM names
are whitelist-validated at every entry point, because every PowerShell statement
is a single-quoted interpolation and one stray quote would close the string.
