# agentvm

在 Mac 上一键装一个完整的虚拟机，Windows 11 或者 Linux 都行。免费，开源，M 芯片
原生虚拟化，装系统全程看得见也点得动。给 AI agent 当安全沙箱，也给你自己当
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
光盘，都在「配置」面板里。改完点「保存并重启」立刻生效——不用重新安装系统，
系统里的东西全留着。原来改内存只能删了重装，而删了重装意味着系统里的一切全没。

**3. 存储四件套。** 「存储」面板里能加附加盘、拍快照、挂共享目录、克隆：
- **附加盘**：填个大小就加，raw 或 qcow2 都行，重启挂上，系统里分区格式化就能用。
- **快照**：qcow2 内部快照，拍一张，之后想退回这个状态就回滚，不用重装。秒级。
- **共享目录**：virtio-9p 把宿主机文件夹挂进虚拟机，传文件不用再走 SSH scp。
- **克隆**：照一个装好的虚拟机复制一份。想试什么就克隆一份去折腾，折腾坏了删掉，
  原来那份还在。qcow2 overlay 是秒级的，和源共用底册、不额外占空间。

**4. 一条命令的等价命令行。** 同一个后端，不想用 app 也行：

```sh
scripts/vmkit.py install --distro debian-13 --name d1
scripts/vmkit.py disk add --name d1 --size 64G --format qcow2
scripts/vmkit.py snapshot create --name d1 --tag clean
scripts/vmkit.py clone --name d1 --new-name d2
```

## 能装什么

Windows 11 24H2 Enterprise Evaluation ARM64、Ubuntu Server 24.04 / 22.04 LTS
ARM64、Debian 13 / 12 ARM64，加上任何你自己的 ISO（fnOS、各种发行版、各种
NAS 系统都行）。

## 它是完整的虚拟机，不是容器

有独立的系统、独立的用户、独立的文件系统和独立的 systemd。agent 在里面装什么、
改什么、删什么，都碰不到你的真实环境。不想要了，一条命令连虚拟机带数据一起删干净，
宿主不留痕迹。

## 快速开始

需要 Apple Silicon 的 Mac（macOS 14 以上）和 macOS 自带的 Command Line Tools
（里面有 `swiftc`，不需要装 Xcode）。QEMU 由 `install.sh` 自动装，装不上会明确
告诉你卡在哪一条。

```sh
git clone https://github.com/mark-cell-520/agentvm.git
cd agentvm
./install.sh          # 装 QEMU（如果需要）+ 编 app + 装到 /Applications
open /Applications/AgentVM.app
```

app 里点 + ，选系统，点开始安装。装机的全过程都在 app 里，包括用自己安装包时
那个要你手动点的安装器。

## 三条不同的安装路径

装不同的系统走的是三条完全不同的路，这不是没统一好，是它们本来就不一样：

| | Windows 11 | Linux 云镜像 | 自己的安装包 |
|---|---|---|---|
| 系统盘 | raw，Setup 真写块设备 | qcow2 overlay，共用一份底册 | raw |
| 怎么装 | 注驱动 + autounattend 应答文件 + 真跑 Setup | cloud-init 从种子盘喂账号，没有安装程序 | 图形安装器，用户自己点 |
| 能不能全自动 | 能 | 能 | **不能，也不装成能** |
| 磁盘后端 | QEMU + HVF | QEMU + HVF | QEMU + HVF |

Windows 为什么必须用 QEMU 而不是 Apple 的 Virtualization.framework：VZ 没有
vTPM、virtio 设备集固定而 Windows 无对应驱动、EFI 启动链有缺口。这三条都是
实测撞过的，不是推测。

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

虚拟机只绑 127.0.0.1，外面连不进来。

## 免费与开源

MIT 许可。没有付费版，没有功能锁定，没有账号，没有遥测。所有代码都在仓库里，
看得见也改得动。

---

<a id="english"></a>

## English

`agentvm` installs a full VM on your Mac — Windows 11 or Linux — with the entire
installation visible and clickable. Free, open source, Apple Silicon native.
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
   lost.
3. **Storage.** Add data disks, take qcow2 snapshots and roll back to them in
   seconds, share a host directory into the guest over virtio-9p, and clone a
   working VM into a scratch copy that shares its backing file.
4. **The same backend from the command line**, if you would rather not open an app.

Three genuinely different install paths sit underneath: Windows gets drivers
injected plus an autounattend answer file and really runs Setup; Linux cloud
images are fed accounts through cloud-init and have no installer at all; your own
ISO has a graphical installer that only you can click through. Windows cannot use
Apple's Virtualization.framework at all — no vTPM, a fixed virtio device set with
no matching Windows drivers, and a gap in the EFI boot chain — so QEMU with HVF
is the engine for all three.

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
