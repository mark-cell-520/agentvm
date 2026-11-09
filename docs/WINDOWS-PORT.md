# Windows 移植方案（实施态）

把 agentvm 从「macOS + Tart」移植到「Windows on ARM + Hyper-V」。

本文的设计决策已由心虫（HeartFlow 6.8.145，`src/core/decision.js` 的 `HeartFlowDecision.decide`）逐项裁决，裁决记录见第 12 节。心虫判定不了的输入（开放式技术问答）它明确弃权，本文所有结论均来自可量化判据的结构化裁决，不是它的自由发挥。

目标读者：要动手改这个仓库的人。读完应该能直接开始写代码。

---

## 1. 结论摘要

流水线的六步结构原样保留，替换的是虚拟化后端、宿主管家层、镜像来源三块。客人机（guest）侧脚本一行不改。

已裁决的五项：

1. 后端：Hyper-V，不用 QEMU。
2. 客人机架构：ARM64 原生，不做跨架构模拟。
3. 编排语言：Python 3 跨平台层，bash 只留 guest 侧。
4. 代码组织：同仓库加 backend 抽象层，不新建平行目录。
5. 离线 OCI 导入路径：移除，主路径改为单文件 qcow2 下载加 SHA256 校验。
6. 宿主导入客户机执行命令的通道：SSH，不用 PowerShell Direct。

---

## 2. 为什么 ARM Windows 上必须选 Hyper-V

这一节是裁决的输入，也是整个方案的地基。

Windows on ARM（骁龙 X 系列）上三个候选的实际处境：

QEMU 官方在 Windows 平台只发布 x86_64 构建，没有原生 ARM64 Windows 构建。要 ARM64 原生客户机就得用第三方非官方构建或自行交叉编译，这是硬性分发障碍：用户装不上，或者装上一个来源不明、无安全更新通道的二进制。

若退而用 x86_64 版 QEMU，它自身在 WoA 上依赖 Prism 模拟层运行，其内部再对 x86_64 客户机做全系统模拟，等于模拟之上叠模拟。沙箱的用途是跑编译工具链，这个场景下性能劣化一个数量级就直接不可用。

Hyper-V 在 WoA 上是一等公民，原生支持 ARM64 Linux 客户机，WSL2 就跑在这条路径上。Windows 11 on ARM 预装专业版自带该组件，无需额外下载虚拟化软件。Gen2 VM 走 UEFI，ARM64 客户机必须 Gen2，这与 Debian cloud 镜像的 UEFI 启动方式天然匹配。

代价有三条，都要在实现中消化：创建与管理 VM 需要管理员权限，与原项目「不需要管理员密码」的卖点直接冲突；镜像需从 qcow2 转换为 VHDX；Secure Boot 签名情况需确认。

---

## 3. PowerShell Direct 为什么被否

直觉上 PowerShell Direct 最像 `tart exec`：Hyper-V 原生、免网络、免端口、经 VMBus 直达。心虫仍判它出局，理由是可行性只有 0.45。

PowerShell Direct 对 Linux 客户机的支持依赖 guest 内的 hv_socks 代理组件，Debian cloud 镜像默认不含。要让它工作需要额外安装代理、配置 socket，而这条路径在 ARM64 Debian 上是否顺畅未经验证。相比之下 SSH 在 Debian cloud 镜像里是默认启用且由 cloud-init 直接注入公钥的，开箱可用，且与现有 `50-configure.sh` 的 base64 传输机制天然衔接。

选 SSH 换来的是三件要管的事：guest 内 SSH 服务、host key 在 VM 重建后变化、hostfwd 端口占用。

---

## 4. 架构与代码组织

```
agentvm.py                     入口，解析子命令，选 backend
backends/
  __init__.py
  base.py                      Backend 抽象接口
  tart.py                      macOS 现有行为，包装成同一接口
  hyperv.py                    Windows 新增
guest/
  40-provision-guest.sh        从 scripts/ 移来，一行不改
  patch-agent-config.py        从 scripts/lib/ 移来，一行不改
scripts/                       macOS 侧现有脚本保留，逐步改为调 agentvm.py
lib/
  common.sh                    macOS 侧保留
  oci-registry.py              按裁决移除
  patch-agent-config.py        移往 guest/
config/
  agentvmrc.example            更新
  env.template                 保留
docs/
  WINDOWS-PORT.md              本文
  PITFALLS.md                  追加 Windows 篇
```

Backend 接口，就是现有脚本实际用到的全部能力：

```
name                           -> "tart" | "hyperv"
install()                      -> 装好虚拟化软件（tart 装 .app，hyperv 开启组件）
create_vm(name, cpu, mem)      -> 从基础镜像造 VM（CoW）
run_vm(name)                   -> 启动，无图形
exec_vm(name, cmd)             -> 在 guest 内执行命令
stop_vm(name)                  -> 停机
list_vms()                     -> 列出 VM 及状态
```

`exec_vm` 是唯一有语义落差的一个：Tart 的 guest agent 免登录直达，Hyper-V 后端走 SSH，多出「SSH 密钥注入」和「等 guest 就绪」两件事。这两件放进 backend 内部，对上层透明。

现有调用点与接口的对应：

| 现有调用 | 出现位置 |
|---|---|
| `tart clone SOURCE NAME --insecure` | 30-create-vm.sh |
| `tart set NAME --cpu N --memory M` | 30-create-vm.sh |
| `tart get NAME` / `tart list` | 30-create-vm.sh / Makefile status |
| `tart run NAME --no-graphics` | 60-autostart.sh |
| `tart exec NAME ...` | Makefile provision/status/logs、50-configure.sh、60-autostart.sh |
| `tart stop NAME` | Makefile stop |

---

## 5. 镜像、磁盘与 cloud-init

### 镜像源

Tart 镜像 `ghcr.io/cirruslabs/debian` 是 arm64 Tart 专用 raw 盘格式，Windows 上用不了。换成 Debian 官方 cloud image：

```
https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2
```

实测该 URL 有效（302 到镜像站，amd64 与 arm64 同名不同目录）。这个镜像自带 cloud-init，免交互装机，SSH 默认启用。

### 磁盘布局

对应 Tart 的「只读基础镜像 + APFS CoW 增量」：

```
%USERPROFILE%\.agentvm\
  base\debian-12-genericcloud-arm64.qcow2    只读基础镜像（下载）
  base\debian-12-genericcloud-arm64.vhdx     转换后的父盘
  vms\agent\agent.vhdx                       differencing disk，VM 的真实盘
  vms\agent\seed.iso                         cloud-init NoCloud 种子盘
  vms\agent\console.log                      串口输出
  keys\agentvm_ed25519                       宿主持有的 SSH 私钥
```

创建命令，对应 `tart clone` 加 `tart set`：

```
New-VHD -Path vms\agent\agent.vhdx -ParentPath base\...vhdx -Differencing
Set-VM -Name agent -ProcessorCount 2 -MemoryStartupBytes 1GB
```

Hyper-V 的 differencing disk 必须挂「全部虚拟磁盘挂在线的父盘」，父盘不能同时被其他 VM 使用，这是与 Tart 的 APFS CoW 的差别：Tart 的基础镜像可以同时支撑任意多个克隆，Hyper-V 的父盘同一时刻只能服务一个 differencing disk。要同时跑多个 VM 就需要多份父盘拷贝，或用独立 VHDX 换空间。

### cloud-init

`40-provision-guest.sh` 做的事大部分搬进 cloud-init 的 `packages` 与 `runcmd`，首启一次完成，省掉一次 `exec_vm` 往返。apt 换源放 `runcmd`。

user-data 草图：

```
#cloud-config
hostname: agent
users:
  - name: agent
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - <由宿主持有私钥推导的公钥>
ssh_pwauth: false
package_update: true
packages:
  - build-essential
  - python3-dev
  - libffi-dev
  - curl
  - ca-certificates
runcmd:
  - sed -i 's|deb.debian.org/debian|mirrors.tuna.tsinghua.edu.cn/debian|g' /etc/apt/sources.list
  - apt-get update
```

NoCloud 种子盘（卷标必须 `cidata`）需要 `meta-data` 与 `user-data` 两个文件。Windows 上无 `genisoimage` 也无 `oscdimg`（后者在 Windows ADK 里），用 Python 造 ISO 是唯一不引入外部依赖的路。

### 网络

Hyper-V 默认交换机（Default Switch）提供 NAT 出网，无需管理员权限建网桥，对应 Tart 的默认 NAT。宿主导入客户机用 PowerShell Direct 之外的方式：SSH 需要网络可达，所以要么给 VM 配静态 IP 走 Default Switch，要么加一个 NAT 端口转发。

`Add-NetNatStaticMapping` 可以把宿主某个端口转发到 VM 的 22 端口，这就是 Hyper-V 版的 hostfwd，也是 `exec_vm` 的 SSH 端点。

---

## 6. 宿主管家层替换表

`lib/common.sh` 里 macOS 命令的对应物。左列现有用法，右列 Windows 侧。

| 现有 | 用途 | Windows 侧 |
|---|---|---|
| `uname -s` / `uname -m` | 平台与架构判定 | `$env:PROCESSOR_ARCHITECTURE` |
| `sw_vers -productVersion` | 版本门槛 | `(Get-CimInstance Win32_OperatingSystem).BuildNumber` |
| `sysctl -n kern.hv_support` | 虚拟化可用性 | `(Get-VMHost).VirtualMachinePlatformEnabled` 或 `Get-WindowsOptionalFeature` |
| `sysctl -n hw.memsize` | 内存容量 | `(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory` |
| `df -k /` | 磁盘余量 | `Get-PSDrive C` / `Get-Volume` |
| `stat -f%z` | 文件大小（断点续传） | `(Get-Item $p).Length` |
| `shasum -a 256` | 校验 | `Get-FileHash -Algorithm SHA256` |
| `base64` | secrets 传输 | `[Convert]::ToBase64String([IO.File]::ReadAllBytes($p))` |
| `readlink -f` | 解析真实路径 | `(Resolve-Path $p).Path` |
| `codesign --verify` | Tart bundle 签名 | 不需要，改为校验 qcow2 的 SHA256 |
| `plutil -lint` | plist 语法检查 | 改为校验 schtasks XML |
| `launchctl load/unload/list` | 自启管理 | `schtasks` 或 `New-Service` |
| `~/Library/LaunchAgents/*.plist` | 自启配置落盘 | 任务计划程序 XML |

---

## 7. 自启

两层必须都配，PITFALLS 第 5 条的结论在 Windows 上一样成立：

- 宿主：VM 进程
- guest：hermes-gateway（systemd enabled，`60-autostart.sh` 的 guest 段原样可用）

宿主侧 Hyper-V 有个 Tart 没有的优势：VM 由 Hyper-V 虚拟机工作进程托管，宿主上没有一个需要保活的前台进程。所以「KeepAlive」这半件事由 Hyper-V 自己解决，任务计划程序只需负责「开机把 VM 拉起来」，不需要进程守护。

```
schtasks /Create /TN "agentvm-agent" /SC ONSTART /RL HIGHEST /TR "Start-VM -Name agent"
```

`/RL HIGHEST` 对应管理员权限。guest 侧 systemd 段原样保留。

---

## 8. 新增的坑位清单

macOS 侧不存在、Windows 侧一定会踩的。按预计耗时排序，实现时逐条验证。

1. Hyper-V 需要管理员权限。建 VM、改 VM、挂 VHD、配 NAT 全都要提权，这与原项目「不需要管理员密码」的卖点直接冲突，README 必须改口，不能假装没发生。
2. differencing 父盘同时只能服务一个 VM。想同时跑两个 VM 就得两份父盘，Tart 上「一个基础镜像克隆无数个」的体验没了。
3. qcow2 转 VHDX 必须在对齐的块边界上做。`qemu-img convert -O vhdx` 基本可用，但转换后要确认 VHDX 块大小与 Hyper-V 默认值兼容，否则启动报磁盘错误。
4. Windows Defender 实时扫描 vhdx 文件。guest 每次写盘都触发扫描，磁盘 IO 可能劣化一个数量级。必须把 `%USERPROFILE%\.agentvm` 加进排除目录，且这一步本身需要管理员权限。
5. ARM64 客户机必须 Gen2。Gen1 是 BIOS 加 x86 专属，选错启动即黑屏。
6. Secure Boot 模板。Gen2 默认开 Secure Boot，Debian cloud 镜像的 shim 签名情况需确认；不行就关 Secure Boot 或用 Linux 模板，不能默默失败。
7. CRLF 换行。如果任何 bash 脚本在 Windows 上经 Git Bash 运行，而 git 把 `.sh` 转成 CRLF，`#!/usr/bin/env bash` 后多一个 `\r`，报错是「bad interpreter」，不提换行。必须加 `.gitattributes`：`*.sh text eol=lf`。
8. cloud-init 种子盘生成。Windows 无 `genisoimage`，也无 `oscdimg`，用 Python 造 ISO。
9. 非 ASCII 路径。中文 Windows 用户名会让 `%USERPROFILE%` 含非 ASCII，PowerShell 与 Hyper-V 对这类路径的处理不稳定。要么检测并拒绝，要么默认把 VM 放到无特殊字符的路径。
10. SSH host key。VM 重建后 host key 变化，`StrictHostKeyChecking` 会拦住自动化。用 `-o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL`，文档里写一句这是沙箱内 VM 的威胁模型下的取舍。
11. 端口占用。NAT 静态映射的宿主端口被占时错误信息不指向根因，check 要显式探测。
12. guest 首启约 90 秒。PITFALLS 第 8 条，Windows 侧一样，`exec_vm` 必须轮询等待。
13. Default Switch 的 IP 不稳定。Hyper-V 的默认交换机给 VM 的 DHCP 地址在宿主重启后可能变，SSH 端点不能硬编码 IP，要用 NAT 映射的固定宿主端口。

---

## 9. 分阶段实施计划

每个阶段结束都有可验证产物。

阶段 0，确认输入。目标机的 Windows 版本与构建号、是否专业版、是否已有管理员权限、架构确认 ARM64、能否访问 cloud.debian.org、磁盘余量。这些决定镜像 URL 与 Hyper-V 组件状态，缺一项可能白写。

阶段 1，手工最小闭环。在目标机上手工跑通：开 Hyper-V 组件、下载 arm64 qcow2、转 VHDX、建 differencing disk、造 seed.iso、建 VM、启动、SSH 登进去。全部手工命令不写脚本，目的是把第 8 节前几条坑实测一遍，拿到真实的启动参数。

阶段 2，backend 抽象落地。实现 `backends/hyperv.py`，把阶段 1 的手工命令固化。此时应能 `agentvm.py create-vm` 加 `agentvm.py exec-vm -- echo hello`。

阶段 3，配置推送。改造 `50-configure.sh` 的等价物，secrets 走 base64 加长度断言不变，传输端从 `tart exec` 换成 SSH。验证 guest 内 `~/.hermes/.env` 落地 0600。

阶段 4，自启。任务计划程序拉 VM，guest 侧 systemd 段原样保留。重启一次，验证两层都起来。

阶段 5，文档与回归。PITFALLS 追加 Windows 篇，README 加 Windows 快速开始并改口管理员权限，`make lint` 等价物覆盖新代码。

阶段 2 之前不要写 `create-vm` 的正式版，阶段 1 的实测参数会改掉大部分写法。

**当前状态：阶段 1 无法在本机执行。** 本机是 macOS，没有 Hyper-V，也没有 ARM Windows 目标机。所有 Windows 侧代码都是按公开文档与 Hyper-V 命令行语义写的，未经目标机验证。这一点在任何交付说明里都要写明，不能含糊。

---

## 10. 已验证与未验证

### 已在本机实测通过

ARM64 镜像链路（全程实跑，非推断）：

- Debian 官方 arm64 cloud qcow2 可下载，346 MB，用时 73 秒。
- `HyperVBackend._download()` 与 `_sha512()` 代码实测可用，算出的 SHA512
  与官方 `SHA512SUMS` 发布值逐字一致。
- `scripts/lib/qcow2_to_raw.py` 实测转换成功：3.22 GB 虚拟盘，
  18387 个非零簇（379 标准簇 + 18008 压缩簇，与事先统计完全吻合），
  5.9 秒完成，稀疏输出实际占 1.2 GB。
- 转换过程中发现并修掉两个真 bug：压缩簇是 raw deflate 而非标准 zlib；
  offset 掩码必须是 `(1 << csize_shift) - 1` 而非 `(1<<62)-1`。
  详见 PITFALLS 的 ARM64 镜像实测篇 M1、M2。
- `scripts/lib/ext4read.py` 实测可用：列出根目录与 /boot，成功提取
  vmlinuz（27.3 MB）与 initrd（13.9 MB），`file` 确认提取到的是
  `Linux kernel ARM64 boot executable Image`。
- cloud-init 配置经 YAML 解析校验：用户、公钥、5 个包、2 条 runcmd 全部
  正确落地，sudo 规则为 `ALL=(ALL) NOPASSWD:ALL`。
- cloud-init 的 NoCloud 标签匹配逻辑已从镜像自带源码实证：大小写都查，
  所以卷标被规范转成大写 `CIDATA` 不影响识别。见 M3。
- cidata 种子盘已造成并验证卷标与内容（ISO9660 主卷描述符 + 挂载读回）。
- `scripts/vzrun.swift` 编译与 ad-hoc 签名通过，VZ 的五个 API 陷阱
  已逐个定位并修正（见 M5）。

Hyper-V 与 Windows 侧：

- 无。本机是 macOS，没有 Hyper-V，也没有 ARM Windows 目标机。

### 未验证（需要目标机或真机）

- **guest 实际启动**：本环境 VZ 无法真正启动 guest，VM 状态停在 4
  （Pausing），串口零字节，最小配置表现相同，已排除硬件与配置层。
  详见 M6。这是当前最大的未验证项。
- Hyper-V 组件在具体 WoA 设备上的可用性与版本门槛。
- qcow2 转 VHDX 的实际兼容性（`qemu-img convert -O vhdx` 未跑过，
  本机没有 qemu-img）。
- differencing disk 加 ARM64 Gen2 加 Secure Boot 的组合能否启动。
- cloud-init NoCloud 种子盘在 ARM64 guest 内是否真的注入公钥。
- PowerShell Direct 在 ARM64 Debian 上确实不可用（这是判它出局的理由，
  但本机无法证实）。
- NAT 静态映射的端口转发可达性。
- 任务计划程序 `/SC ONSTART` 加 `/RL HIGHEST` 的实际行为。

---

## 11. 心虫裁决记录

全部由 `HeartFlowDecision.decide` 在 mode= `pick_best` 下产出，输入为带可量化判据（feasibility / consequence_value / risk / confidence，取值 0 到 1）的结构化候选，平局会弃权。

| 决策点 | 选中 | 得分 | 次选 | 置信度 | 长期后果值 |
|---|---|---|---|---|---|
| 虚拟化后端 | Hyper-V 加 ARM64 加原生通道 | 0.86 | QEMU ARM64 0.66 | 0.80 | 0.34 |
| 编排语言 | Python 3 跨平台层 | 0.84 | PowerShell 0.81 | 0.80 | 0.34 |
| 代码组织 | 同仓库加 backend 抽象 | 0.82 | 独立目录 0.75 | 0.75 | 0.32 |
| 离线 OCI 路径 | 移除，改单文件 qcow2 加 SHA256 | 0.84 | 保留标记仅 macOS 0.72 | 0.85 | 0.30 |
| 客户机执行通道 | SSH | 0.84 | 双通道 0.71 | 0.80 | 0.32 |

关于长期后果值：五项全部落在 0.30 到 0.34 的窄区间，这是心虫在系统性地表达「这个决策的长期影响我判断不了」。工程决策的长期后果取决于执行质量与后续维护，引擎不给这个保证是诚实的，不是否决信号。读这份记录时不要把 0.3 理解成「这些决策不好」。
