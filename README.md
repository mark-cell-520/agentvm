# agentvm

在 Apple Silicon Mac 上跑一个 1 GB 的 Linux 虚拟机给 AI agent 用，**并且能在访问不了镜像仓库的网络下完成**。

[English below](#english)

## 它解决什么问题

在 Mac 上用 [Tart](https://tart.run) 跑 Linux 虚拟机给 agent 当沙箱，流程本身不复杂。真正花时间的是这三件事：

1. **拉不到镜像。** 实测这台机器到 ghcr.io 单连接 2.7 KB/s、并发聚合 35 KB/s，一个 600 MB 的 Debian 镜像要下 60 小时。
2. **apt 装不动。** `deb.debian.org` 同一条网络下 38 KB/s，而清华源 12.5 MB/s，差 320 倍。不换源，`build-essential` 要下 2.5 小时。
3. **自动重启链是两段。** 宿主 launchd 起 VM，客人机 systemd 起网关。只配一段，重启后必静默。

所以这个项目把「网络很差的情况下，从零搭一个能用的 agent 虚拟机」做成可复现的脚本，并且把踩过的坑全部记录下来。

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

## 实测数据

在同一台 12 核 / 16 GB 的 Mac mini 上，VM 内 Hermes 网关 vs 本机直接运行：

| 维度 | VM 内 | 本机直接运行 |
|---|---|---|
| 网关进程树内存 | 231 MB | 480 MB |
| 生态总内存 | 231 MB | 2650 MB（含 chromium 2007 MB，按需） |
| CPU（15 秒窗口） | 0.5% | 1.9% |
| 磁盘 | 585 MB 客人机内 / 2.8 GB 宿主视角 | 4.6 GB |

VM 空闲时省约 250 MB，但天花板从 16 GB 掉到 1 GB。**要不要为隔离付这个代价，取决于你信不信任 agent 跑的东西**——这是选 VM 的唯一理由，省资源不是。

## 文档

- [docs/PITFALLS.md](docs/PITFALLS.md) — 12 个踩过的坑，按「有多反直觉」排序。 Secrets 在传输中被替换、`tart exec` 的 `--` 陷阱、`HERMES_YOLO_MODE` 的 import 时冻结、稀疏盘被 `cp` 写实……每一条都是实际花掉时间才定位的。
- [docs/OFFLINE-IMPORT.md](docs/OFFLINE-IMPORT.md) — 离线导入的原理：为什么不能往 Tart 缓存目录塞文件，以及最小 OCI 注册表做了什么。

## 安全

`config/env.template` 里默认**关闭**「允许所有飞书用户」和「绕过审批」。两个都开意味着同租户任何人都能让你的 agent 在 VM 里跑任意命令。要开的话请先读那两段注释。

密钥一律从文件读取，经 base64 传输，落地 0600，用完即删。详见 PITFALLS 第 1 条。

## 许可

MIT

---

<a id="english"></a>

## English

`agentvm` bootstraps a ~1 GB Linux VM on Apple Silicon for running AI agents,
and works on networks that cannot reach container registries at usable speed.

The hard parts are not the VM itself: pulling a 600 MB image at 2.7 KB/s,
installing `build-essential` at 38 KB/s, and the fact that a working
reboot-surviving setup needs *two* auto-start layers in two different operating
systems. This project makes all three reproducible and documents the traps in
[docs/PITFALLS.md](docs/PITFALLS.md).

```sh
make check && make install-tart && make fetch
make serve          # in a second terminal
make create-vm && make provision && make configure && make autostart
```

MIT licensed.
