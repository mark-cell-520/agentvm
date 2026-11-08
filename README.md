# agentvm

在 Mac 上装一个完整的 Linux 虚拟机。免费，开源，M 芯片原生虚拟化，一条命令从零跑起来。
给 AI agent 当安全沙箱，也给你自己当随删随建的试验场。

[English below](#english)

## 它是完整的虚拟机，不是容器

有独立的 Debian 系统、独立的用户、独立的文件系统和独立的 systemd。agent 在里面装什么、改什么、删什么，都碰不到你的真实环境。不想要了，一条命令连虚拟机带数据一起删干净，宿主不留痕迹。

## 你会拿到什么

- 一个 2 核 1 GB 起步、内存和 CPU 随时可调、磁盘可扩的 Debian 虚拟机，跑在 Apple 自己的 Virtualization 框架上，不是模拟器，性能接近原生。
- 完整可用的 Linux 环境：apt 装包、编译工具链、systemd 服务、SSH 登录。你能在 Linux 上做的事这里都能做，不存在沙箱里什么都干不了的问题。
- 重启不丢：宿主 launchd 负责把虚拟机拉起来，客人机 systemd 负责把服务拉起来，两层都配好，重启之后不用人工介入。
- 完全离线可用：镜像的每一层都从你自己机器上的本地注册表拉，不依赖外网，也不怕镜像仓库被墙或被限速到不能用。
- 密钥不落地：LLM 和飞书的密钥从文件读、经 base64 传、落地权限 0600、用完即删，全程不经过命令行参数，不进 shell 历史。
- 十二个已经替你踩过的坑，按有多反直觉排序写成文档。

## 多快

`make check` 三十秒确认机器能跑，`make install-tart` 两分钟装 Tart（不走 Homebrew，不需要管理员密码），之后按 `fetch`、`serve`、`create-vm`、`provision`、`configure`、`autostart` 六个目标各跑一遍，二十分钟左右拿到一个开机自启的完整沙箱。

## 快速开始

```sh
git clone https://github.com/mark-cell-520/agentvm.git
cd agentvm

make check          # 检查宿主机是否满足条件
make install-tart   # 装 Tart（不走 Homebrew）
make fetch          # 下载镜像的所有 OCI 层（支持断点续传）
```

`make fetch` 完成后，在**另一个终端**起本地注册表：

```sh
make serve
```

然后回第一个终端：

```sh
make create-vm      # 从本地注册表克隆，配 2 核 / 1 GB
make provision      # 客人机内换 apt 源 + 装编译工具链
make configure      # 推送 LLM / 飞书配置（密钥不经过命令行）
make autostart      # launchd + systemd 双层开机自启
```

## 如果镜像能直接拉

网络正常时不需要离线导入那套：

```sh
tart clone ghcr.io/cirruslabs/debian:latest agent
scripts/30-create-vm.sh --name agent --from ghcr.io/cirruslabs/debian:latest
```

`make fetch` / `make serve` 只是给「拉不动」的准备的一条备路。

## 为什么需要离线这条路

在 Mac 上用 [Tart](https://tart.run) 跑 Linux 虚拟机给 agent 当沙箱，流程本身不复杂。真正花时间的是这三件事：

1. **拉不到镜像。** 实测这台机器到 ghcr.io 单连接 2.7 KB/s、并发聚合 35 KB/s，一个 600 MB 的 Debian 镜像要下 60 小时。
2. **apt 装不动。** `deb.debian.org` 同一条网络下 38 KB/s，而清华源 12.5 MB/s，差 320 倍。不换源，`build-essential` 要下 2.5 小时。
3. **自动重启链是两段。** 宿主 launchd 起 VM，客人机 systemd 起网关。只配一段，重启后必静默。

这个项目把这三件事做成可复现的脚本，并且把踩过的坑全部记录下来。网络正常的话，上面三件事你一条都不会遇到。

## 谁适合用

1. 想让 AI agent 在隔离环境里执行命令、安装软件、修改文件，又不放心它直接碰你宿主机的系统和资料。
2. 手上只有 Mac，想要一个随时能删掉的 Linux 环境做编译、调试、试错。
3. 网络到不了镜像仓库，或者慢到不可用，仍然想从零把环境搭起来。

## 隔离值不值这个代价

在同一台 12 核 / 16 GB 的 Mac mini 上，VM 内 Hermes 网关 vs 本机直接运行：

| 维度 | VM 内 | 本机直接运行 |
|---|---|---|
| 网关进程树内存 | 231 MB | 480 MB |
| 生态总内存 | 231 MB | 2650 MB（含 chromium 2007 MB，按需） |
| CPU（15 秒窗口） | 0.5% | 1.9% |
| 磁盘 | 585 MB 客人机内 / 2.8 GB 宿主视角 | 4.6 GB |

VM 空闲时省约 250 MB，但天花板从 16 GB 掉到 1 GB。**要不要为隔离付这个代价，取决于你信不信任 agent 跑的东西**——这是选 VM 的唯一理由，省资源不是。

## 免费与开源

MIT 许可。没有付费版，没有功能锁定，没有账号，没有遥测。所有脚本都在仓库里，看得见也改得动。

## 文档

- [docs/PITFALLS.md](docs/PITFALLS.md) — 12 个踩过的坑，按「有多反直觉」排序。Secrets 在传输中被替换、`tart exec` 的 `--` 陷阱、`HERMES_YOLO_MODE` 的 import 时冻结、稀疏盘被 `cp` 写实……每一条都是实际花掉时间才定位的。
- [docs/OFFLINE-IMPORT.md](docs/OFFLINE-IMPORT.md) — 离线导入的原理：为什么不能往 Tart 缓存目录塞文件，以及最小 OCI 注册表做了什么。

## 安全

`config/env.template` 里默认**关闭**「允许所有飞书用户」和「绕过审批」。两个都开意味着同租户任何人都能让你的 agent 在 VM 里跑任意命令。要开的话请先读那两段注释。

密钥一律从文件读取，经 base64 传输，落地 0600，用完即删。详见 PITFALLS 第 1 条。

## 许可

MIT

---

<a id="english"></a>

## English

`agentvm` installs a full Linux VM on your Mac. Free, open source, Apple Silicon
native, one command from scratch. A real sandbox for AI agents, and a throwaway
lab for yourself.

This is a full VM, not a container: its own Debian system, its own users, its own
filesystem, its own systemd. Whatever an agent installs, edits, or deletes in
there cannot touch your real machine. When you are done, one command removes the
VM and its data, leaving no trace on the host.

You get a 2-core, 1 GB Debian VM you can resize at any time, running on Apple's
own Virtualization framework rather than an emulator, so performance stays close
to native. `apt`, a build toolchain, systemd services, and SSH all work normally,
so nothing is off-limits inside the sandbox. It survives reboots: launchd on the
host brings the VM up, systemd in the guest brings the services up, both layers
configured. It works fully offline, pulling every image layer from a registry
running on your own machine. Secrets are read from files, passed base64-encoded,
landed `0600`, and deleted after use, never on a command line.

MIT licensed. No paid tier, no feature gating, no account, no telemetry. Every
script is in the repo, readable and editable. Twelve hard-won pitfalls are
documented, including secrets mangled in transit, a sparse disk filled solid by
`cp`, and a reboot-surviving setup that silently dies because only one of the two
auto-start layers was configured.

```sh
make check && make install-tart && make fetch
make serve          # in a second terminal
make create-vm && make provision && make configure && make autostart
```

If your network can reach container registries, skip the offline import entirely
and clone straight from `ghcr.io/cirruslabs/debian:latest`.

### Why the offline path exists

The VM itself is not the hard part. On one measured host, pulling a 600 MB Debian
image from `ghcr.io` ran at 2.7 KB/s per connection and 35 KB/s aggregated — about
60 hours. `deb.debian.org` sat at 38 KB/s over the same link while a Tsinghua
mirror gave 12.5 MB/s, a 320x difference, which turns installing
`build-essential` into a 2.5-hour job. And a reboot-surviving setup needs *two*
auto-start layers in two different operating systems; configure one and the bot
goes silent after every restart. This project makes all three reproducible. On a
healthy network you will hit none of them.

### Who this is for

1. Anyone who wants an AI agent to run commands, install packages, and edit files in an isolated environment, without handing it the host machine.
2. Anyone on a Mac who wants a Linux box they can delete and rebuild at will.
3. Anyone whose network cannot reach container registries at a usable speed.

### Isolation is the point, not resource savings

On the same 12-core / 16 GB Mac mini, the Hermes gateway inside the VM versus
running natively:

| | In the VM | Natively |
|---|---|---|
| Gateway process tree | 231 MB | 480 MB |
| Whole ecosystem | 231 MB | 2650 MB (incl. 2007 MB chromium, on demand) |
| CPU (15 s window) | 0.5% | 1.9% |
| Disk | 585 MB in guest / 2.8 GB from the host | 4.6 GB |

Idle, the VM saves about 250 MB — but the ceiling drops from 16 GB to 1 GB.
**Whether that trade is worth it depends on how much you trust what the agent
runs.** That is the only reason to pick a VM. Saving resources is not.
