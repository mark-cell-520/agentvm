# Pitfalls

Every entry here cost real time. They are ordered roughly by how surprising they
were. If you only read one section, read "Secrets get mangled in transit" — it
fails silently and looks like a bad API key.

---

## 1. Secrets get mangled in transit

**Symptom.** You write a perfectly good API secret into a config file. The API
says `invalid credentials`. You test the same secret by hand with `curl` and it
works. You conclude the config file is being ignored, and start debugging the
loader.

**Cause.** When a secret is copied through a chat interface, a CI log, or any
pipeline with a redaction layer, that layer may replace the *first characters*
of the key with a placeholder token while leaving the rest intact. The result is
written to disk verbatim. The file looks plausible; only the byte count is wrong.

In this project it turned a 35-character secret into a 44-character string with
an 18-character placeholder glued onto the front — same tail, wrong prefix:

```
intended:  AbCdEf1234567890abcdefghijklmnopqr
written:   <PLACEHOLDER_x7f3a9>abcdefghijklmnopqr
```

(The real values are deliberately not reproduced here. Use your own synthetic
example when documenting this class of bug.)

**How it was found.** A direct API call with the hand-typed secret returned
`code: 0`, while the gateway running the file's contents returned
`app_id or app_secret is invalid`. That contradiction ruled out every
configuration theory and forced a byte-level look at the file:

```sh
grep "^FEISHU_APP_SECRET=" ~/.hermes/.env | od -c | head -4
```

**Fix / prevention.**

- Transport secrets as base64, never as literal text.
- Assert the byte length before and after transfer.
- Prefer reconstructing a value from a file you already have over retyping it.
- Verify by reading the value *back out of the file* and using it, not by
  re-testing the value you think you wrote.

`scripts/50-configure.sh` does all of this: it reads secrets from files, pipes
them as base64, and greps for the expected length on the far side.

---

## 2. `tart exec` fails with exit 125 and no error message

**Symptom.**

```sh
$ tart exec agent -- echo hello
$ echo $?
125
```

No output, no diagnostic. It looks like the VM is dead or the guest agent is not
running.

**Cause.** The syntax is `tart exec <name> <command>...`. The `--` is *not* a
separator that gets stripped — it is passed through as the command name, so Tart
tries to execute a binary called `--`.

**Fix.**

```sh
tart exec agent echo hello        # correct
tart exec agent sh -c 'nproc'     # correct
```

This one is nasty because the failure is silent and the exit code is generic. If
`tart exec` returns 125, try removing `--` before investigating anything else.

---

## 3. `HERMES_YOLO_MODE` is frozen at import time

**Symptom.** You set `HERMES_YOLO_MODE=1` in `.env`, restart the service, and
`is_approval_bypass_active()` still reports `False` when you check it by hand.

**Cause.** `tools/approval.py` reads it once, at module import:

```python
_YOLO_MODE_FROZEN: bool = is_truthy_value(os.getenv("HERMES_YOLO_MODE", ""))
```

`.env` is loaded by python-dotenv *later*, during application startup. Whether
the variable is visible depends on import order, which differs between running
the CLI directly and running under systemd.

The comment in the source says this is deliberate — freezing it closes a
prompt-injection escalation path where a skill flips the flag mid-process.

**Fix.** Use the config path as the primary mechanism and the env var as backup:

```yaml
# ~/.hermes/config.yaml
approvals:
  mode: "off"        # read at call time via load_config()
```

Plus a systemd drop-in so the service definitely has the variable:

```ini
# /etc/systemd/system/hermes-gateway.service.d/override.conf
[Service]
Environment=HERMES_YOLO_MODE=1
```

Verify with:

```sh
./venv/bin/python -c "from tools.approval import is_approval_bypass_active, _get_approval_mode; print(is_approval_bypass_active(), _get_approval_mode())"
```

**Also note:** bypassing approvals is not the same as removing all limits. The
source comments state that hardline blocks are enforced by the caller *before*
this gate, so `--yolo` skips only the recoverable approval layer.

---

## 4. YAML 1.1 turns `off` into a boolean

**Symptom.** You write `approvals: {mode: off}` and approvals still behave as
`manual`.

**Cause.** YAML 1.1 treats bare `off`/`no`/`false` as booleans, so the value
arrives in Python as `False`, not the string `"off"`.

**Fix.** Quote it: `mode: "off"`. The agent's own normaliser handles both, but
quoting removes the ambiguity.

---

## 5. Only one of the two auto-start layers was configured

**Symptom.** Everything works, you reboot, and the bot is silent.

**Cause.** There are two independent things to start and they live in different
operating systems:

| layer | what | mechanism |
|---|---|---|
| host | the VM | launchd, `RunAtLoad` + `KeepAlive` |
| guest | the agent gateway | systemd, `enabled` |

Configuring only the guest service means nothing starts it, because the VM is
not running. Configuring only the host means the VM boots but the gateway does
not.

**Fix.** Do both. `scripts/60-autostart.sh` handles the pair. Verify the whole
chain, not just one hop:

```sh
launchctl list | grep com.agentvm
tart list
tart exec agent systemctl is-active hermes-gateway
```

---

## 6. `tart clone` ignores local blob caches

**Symptom.** You download image layers by hand, drop them into
`~/.tart/cache/content/sha256/`, and `tart clone` still tries to download from
the registry.

**Cause.** `tart clone` streams OCI layers directly into
`~/.tart/tmp/<uuid>/disk.img`. The content-addressed cache is not consulted for
this path.

**Fix.** Serve the blobs over HTTP and let Tart pull normally. That is what
`scripts/lib/oci-registry.py` is for. Note that `--insecure` is mandatory:
Tart refuses a plain-HTTP registry without it.

---

## 7. Disk size can only grow

**Symptom.** You want a 10 GB guest; `tart set --disk-size 10` fails or does
nothing.

**Cause.** Tart's images carry a fixed logical size in the manifest annotation
`org.cirruslabs.tart.uncompressed-disk-size` — 20 GB for `cirruslabs/*`. The
setter refuses to shrink, to avoid data loss.

**Reality check.** Logical size is not disk usage. The image is APFS-sparse, so a
20 GB logical Debian guest occupies 2–3 GB on disk. If you genuinely need a
small *logical* disk, you need `tart create` plus an OS install, not a clone.

---

## 8. Guest takes ~90 seconds before it answers

**Symptom.** `tart list` says `running`, but SSH and `tart exec` both fail. You
conclude the VM is broken, restart it, and get the same thing.

**Cause.** `tart run` returns as soon as Virtualization.framework accepts the
configuration. The guest kernel and userspace are still booting.

**Fix.** Wait. Poll instead of guessing:

```sh
for i in $(seq 1 30); do
  tart exec agent true 2>/dev/null && break
  sleep 10
done
```

---

## 9. Registry throttling looks like a hung download

**Symptom.** A 600 MB image download sits at the same byte count for minutes.

**Cause.** Two different failure modes look identical:

- **Throttling.** Traffic is flowing, just very slowly (measured: 2.7 KB/s on a
  single connection to ghcr.io, ~35 KB/s aggregate with Tart's concurrency).
- **Stall.** Nothing is flowing.

**How to tell them apart.** Sample the same counter twice:

```sh
nettop -P -l 2 -p $(pgrep -f "tart clone" | head -1)
```

`bytes_in` is cumulative, so two samples a second apart give you a real rate.
A `dd` read test is misleading here: the page cache will happily serve 771 MB/s
from a file that was just written, telling you nothing about the disk.

**Fix.** Don't fight it. Download by another route and use the offline import
path (pitfall 6).

---

## 10. `cp` on a sparse disk image fills your disk

**Symptom.** You copy a 64 GiB *logical* disk image to check something, and your
disk fills up.

**Cause.** Plain `cp` materialises holes. A sparse file that occupies 28 GiB
becomes a full 64 GiB.

**Fix.** Use `dd conv=sparse`, or `rsync --sparse`:

```sh
dd if=src.img of=dst.img bs=4m conv=sparse
```

Verify afterwards that the destination is still sparse:

```sh
stat -f "logical %z, allocated %b*512" dst.img
```

Also note that `mv` between APFS volumes does *not* preserve sparseness and
preallocates the full size at the destination, and `ditto` materialised it too.
`dd conv=sparse` was the only tool here that got it right.

---

## 11. Killing a shell wrapper leaves the real child running

**Symptom.** You `kill` a long-running `mv`/`cp`, confirm it is gone, and hours
later find the same process still writing to disk.

**Cause.** `pgrep -f "mv ..."` matches the `zsh -c` wrapper *and* the `mv` child.
Killing the wrapper's PID leaves the child orphaned and reparented to launchd,
where it keeps running.

**Fix.** Match the actual binary and kill everything that matches:

```sh
pkill -9 -f "^mv "          # anchored, matches the real process
pgrep -fl "mv -PRpv"        # verify with the distinctive flags
```

Orphaned copies of large files are invisible in `df` if you deleted the
destination, because the blocks are held by the open file descriptor.

---

## 12. `HTTP 402` is a billing problem, not a config problem

**Symptom.** The gateway reports a generic "the model provider failed after
retries" and hides the upstream error.

**Cause.** The gateway deliberately keeps raw provider errors out of chat. The
real error is only in the logs:

```sh
sudo journalctl -u hermes-gateway -n 50 | grep -iE "error|402"
```

`402 insufficient_balance` means the key authenticated, the endpoint was right,
the model name was accepted — the account is just empty. Changing configuration
cannot fix it.

**Side effect worth knowing.** A 402 also marks the auxiliary client (title
generation) unhealthy for 600 s, so even after topping up you should restart the
service rather than waiting it out.

---

# Windows on ARM 专篇

以下条目是 Windows 侧新增的，macOS 上不存在。按预计耗时排序。
每条都标了验证状态：未验证的意味着尚未在真实 Windows on ARM 设备上确认。

## W1. Hyper-V 要管理员权限，原卖点作废

**症状.** 照着 macOS 的经验预期「不需要管理员密码」，`New-VM` 直接拒绝。

**原因.** 建 VM、挂虚拟磁盘、配 NAT 端口转发都需要管理员权限。这是 Hyper-V 的机制，不是脚本的选择。

**影响.** README 必须改口。原项目把「不走 Homebrew、不需要管理员密码」当卖点，Windows 版这条不成立。

**处置.** `agentvm.py install` 检测当前是否已提权，未提权时给出明确指令而不是让 UAC 弹窗在无人值守时挂住。已实现，未验证。

## W2. 增量盘父盘同时只能服务一个 VM

**症状.** 想同时跑两个 VM，第二个创建失败。

**原因.** Hyper-V 的 differencing disk 要求父盘不被其他 VM 挂载。Tart 的 APFS CoW 没有这个限制，一个基础镜像可以克隆任意多个。

**处置.** 要并发多个 VM 就得复制多份父盘，或用独立 VHDX 换空间。这是与 Tart 的实质差别，文档已写明。

## W3. qcow2 转 VHDX 的块对齐

**症状.** 转换成功但 VM 启动报磁盘错误。

**原因.** VHDX 的块大小与 Hyper-V 默认值不兼容。

**处置.** 用 `qemu-img convert -O vhdx`，块对齐由 qemu-img 处理。转换后必须在目标机上验证能启动，未验证。

## W4. Windows Defender 扫描 vhdx，磁盘 IO 掉一个数量级

**症状.** 客人机里 `apt install build-essential` 慢到不可用，而宿主导入机磁盘看着很闲。

**原因.** 实时保护在客人机每次写盘时扫描 vhdx 文件。

**处置.** 把 `%USERPROFILE%\.agentvm` 加进排除目录。这一步本身需要管理员权限，不能默默失败。未验证。

## W5. ARM64 客人机必须 Gen2

**症状.** VM 创建成功，启动黑屏。

**原因.** Gen1 是 x86 BIOS 专属。ARM64 只能走 UEFI，即 Generation 2。

**处置.** `create_vm` 固定用 `-Generation 2`。已实现。

## W6. Secure Boot 模板

**症状.** Gen2 默认开 Secure Boot，客人机起不来。

**原因.** Debian 镜像的 shim 签名与 Hyper-V 的 Secure Boot 模板不匹配。

**处置.** Debian 12 的 shim 由微软签名，理论上可用，所以默认开；起不来就把 `secure_boot` 配置项关掉。这一项目标机未验证，是首次启动最可能撞上的一条。

## W7. CRLF 换行让 bash 脚本报「bad interpreter」

**症状.** 报错说 `bad interpreter: /usr/bin/env bash`，完全不提换行。

**原因.** git 在 Windows 上把 `.sh` 检出成 CRLF，shebang 后面多一个 `\r`。

**处置.** 仓库加 `.gitattributes` 强制 `*.sh text eol=lf`。已实现。

## W8. 造 cloud-init 种子盘没有现成工具

**症状.** `genisoimage`、`oscdimg` 在 Windows 上都不存在。

**原因.** 前者是 Linux 工具，后者在 Windows ADK 里，都不是默认存在。

**处置.** 用 IMAPI2（Windows 原生 COM 组件）造 ISO，卷标 `cidata`。需要 `powershell -STA`，MTA 模式下会失败且报错不指向根因。已实现，IMAPI2 在 ARM64 上的可用性未验证。

## W9. 非 ASCII 路径

**症状.** 中文 Windows 用户名下，PowerShell 与 Hyper-V 对路径的处理不稳定。

**处置.** 检测到 `%USERPROFILE%` 含非 ASCII 时应明确报错并建议换路径，不要静默失败。未实现。

## W10. SSH host key 在 VM 重建后变化

**症状.** 自动化第二次跑时卡在 host key 校验。

**原因.** `StrictHostKeyChecking` 默认拦截。VM 重建后 host key 必然变。

**处置.** 用 `StrictHostKeyChecking=no` 加 `UserKnownHostsFile=NUL`。沙箱内 VM 的威胁模型下这是刻意的取舍，但要在文档里写明，不能让读者以为这是疏漏。

## W11. NAT 端口占用

**症状.** `Add-NetNatStaticMapping` 失败，错误信息不指向端口被占。

**处置.** `check` 阶段显式探测宿主端口。未实现。

## W12. guest 首启约 90 秒，Start-VM 返回不等于可用

**症状.** `Start-VM` 成功返回，SSH 立刻连不上。

**原因.** 对应 macOS 侧第 8 条：VM 状态变 Running 时客人机内核还在解压。

**处置.** `wait_ready` 先等 Running 再轮询 SSH，默认等 240 秒。已实现。

## W13. Default Switch 的 IP 不稳定

**症状.** 昨天还能 SSH，今天宿主重启后连不上。

**原因.** Hyper-V 默认交换机用 DHCP，guest 地址可能变。

**处置.** 不缓存 IP。每次 `exec_vm` 前重新读 guest 地址并重建 NAT 映射。已实现，未验证。

## W14. `tart exec` 的 `--` 陷阱在 Windows 侧不存在，但换个形态

**症状.** 无。

**说明.** 原第 2 条是 Tart 特有语法坑。Windows 侧走 SSH，命令经 `sh -c` 传，`--` 会被 shell 正常处理。此条列出只为说明它已核对过、不构成 Windows 侧风险。

## W15. base64 不是加密

**症状.** 认为「base64 过了就安全」，把编码后的密钥放进命令行参数。

**原因.** 原项目 `50-configure.sh` 把 base64 后的值放进 `tart exec` 的 argv。base64 只是编码，进程表里读出来还原即可。

**处置.** Windows 侧改成密钥走 stdin，命令行参数里只有 `sh -c 'umask 077; base64 -d > file; chmod 600 file'`，密钥材料不出现在任何 argv。比原项目严一档。已实现。

---

# ARM64 镜像实测篇

这一篇的记录来自在本机（Apple Silicon macOS）实测 Debian 官方 arm64 cloud
镜像的全过程。每条都是撞过才写下来的，不是文档推断。

## M1. qcow2 压缩簇用 raw deflate，不是标准 zlib

**症状.** 自写 qcow2 读取器解压第一个簇就报 `incorrect header check`，
而 `qemu-img` 转同一个文件完全正常。

**原因.** 标准 zlib 流带 2 字节头（`0x78 0x9c` 之类）。这个镜像的压缩簇
是裸 deflate 流，没有头。用 `zlib.decompress()` 必然失败，
用 `zlib.decompressobj(-15)`（wbits 为负表示 raw deflate）立刻解出
正好一个簇的 65536 字节。

**处置.** 两种形态都试，先标准后 raw。已在 `scripts/lib/qcow2_to_raw.py`
的 `_inflate()` 实现。换一个镜像源不至于让转换器失效。

## M2. 压缩簇 offset 掩码不能用 62 位

**症状.** 按文档把 offset 掩码写成 `(1<<62)-1`，第一个簇能解，后面全部
报「读取不完整：期望 11995 字节，实得 0」，偏移大到 4 后面 17 个 0。

**原因.** 压缩簇描述符的位布局是：

```
bit 62                      压缩标志
bit csize_shift .. 61       压缩数据占用的 512 字节扇区数减 1
bit 0 .. csize_shift-1      压缩数据在镜像文件中的字节偏移
```

其中 `csize_shift = 62 - (cluster_bits - 8)`，cluster_bits 为 16 时是 54。
也就是说高位被扇区数占着，offset 只到 bit 53。用 62 位掩码会把扇区数
并进偏移，得到一个远超文件长度的假偏移。

**怎么定位的.** 顺序 raw-deflate 解压文件，记录每个流的真实起始位置，
再拿 L2 表条目的低位去比对，六个样本全部吻合才确认掩码是
`(1 << csize_shift) - 1`。没有靠记忆里的 QEMU 常量。

## M3. cloud-init 的 NoCloud 标签大小写都认

**疑虑.** FAT 与 ISO9660 规范都会把卷标转成大写，`hdiutil` 造出的卷标是
`CIDATA`，而 cloud-init 文档写的是小写 `cidata`。

**结论.** 不用担心。镜像自带的 `DataSourceNoCloud.py` 第 44 到 46 行
同时查三种写法：

```python
label_list = util.find_devs_with("LABEL=%s" % label.upper())
label_list.extend(util.find_devs_with("LABEL=%s" % label.lower()))
label_list.extend(util.find_devs_with("LABEL_FATBOOT=%s" % label))
```

**方法上值得记一笔.** 这个结论不是查文档得到的，是用 `ext4read.py` 从
镜像里把 `DataSourceNoCloud.py` 读出来看的。涉及具体行为的问题，
读目标系统自己的源码比查上游文档可靠。

## M4. Debian cloud 镜像只有一个 ext4 分区，没有 EFI 系统分区

**症状.** 打算用 `VZEFIBootLoader` 走 UEFI 启动，无从下手。

**原因.** `debian-12-genericcloud-arm64.qcow2` 的分区表里只有一个
3.09 GB 的 ext4 root 分区，起始于 LBA 262144（128 MB 处）。`/boot/efi`
目录存在但是空的挂载点，不是 ESP。

**影响.** 只能用 `VZLinuxBootLoader` 直接加载内核与 initrd，而这两个
文件都在 ext4 分区里，所以必须先读 ext4。这是 `scripts/lib/ext4read.py`
存在的全部理由。

## M5. Virtualization.framework 的五个 API 陷阱

写 `scripts/vzrun.swift` 时逐个撞上的，每一个的报错都不指向真正原因：

1. `VZLinuxBootLoader(kernelURL:initialRamdiskURL:)` 不存在。initrd 是
   创建后才能赋的属性 `bootLoader.initialRamdiskURL`。
2. Linux 客人机的 virtio 控制台要放 `config.consoleDevices`，不是
   `serialPorts`。后者只接受 `VZSerialPortConfiguration`，类型不匹配会触发
   编译器内部错误（`failed to produce diagnostic for expression`）而不是
   可读的诊断。
3. `VZVirtioConsoleDeviceConfiguration.ports` 是只读的
   `VZVirtioConsolePortConfigurationArray`，要用下标 `ports[0] = port`，
   没有 `add` 方法。
4. `VZFileHandleSerialPortAttachment` 不接受 `FileHandle.nullDevice` 作读端，
   会抛 `NSInvalidArgumentException`。必须打开真实的 `/dev/null` 拿 fd。
5. `vm.start()` 的 async 形式在本环境一直 pending 不返回，completion
   handler 形式正常同步返回。排障时优先用 completion handler。

## M6. 本环境 VZ 无法真正启动 guest

**现象.** 配置校验通过，`vm.start()` 同步返回，但 VM 状态进入 4
（Pausing）而非 3（Starting）或 1（Running），串口零字节，
start 回调从不触发。最小配置（无盘、无网络、无串口）表现完全相同。

**已排除.** 硬件层 `kern.hv_support` 为 1，支持虚拟化。配置层
`config.validate()` 通过。所以问题不在磁盘镜像、内核、cloud-init 或
启动参数。

**判断.** 这是运行环境对 Virtualization.framework 的限制，不是脚本问题。
在真实可用的 Mac 上 `scripts/vzrun.swift` 的 API 调用是正确的，
但「能在真机启动 guest」这一条未获验证，不能声称已验证。

## M7. YAML 双引号标量后面不能再跟内容

**症状.** `linux_seed()` 生成的是 `fqdn: "d1".local` 和
`instance-id: "d1"-001`。两行都是**语法错误**的 YAML：双引号标量在闭合引号处
结束，后面紧跟一个裸标量，解析器直接报 ParserError。cloud-init 会把整份
user-data 丢掉——账号、密码、SSH 公钥、packages 全部不生效，表象是「VM 起来了
但登不进去」，而宿主机从下载到启动每一步都返回成功。

**怎么发现的.** 给 cloud-init 加 `qemu-guest-agent` 时顺手把生成的 user-data
喂给 `yaml.safe_load`，它炸了。之前没人炸是因为没有人校验过产物——只有密码
注入那一类问题会让人去看生成的文本（见 docs/AGENTVM-APP.md「cloud-init 的
YAML 注入」），而引号外跟内容是同一个病的另一个实例。

**结论.** 后缀必须放进引号里面：`_yaml_scalar(name + '.local')`。生成配置
文件的代码，产物要过一遍解析器再交出去，不能只看「字符串拼对了」。

**影响范围.** 只有 `linux_seed()` 这两行。改完之后 user-data 和 meta-data
都用 `yaml.safe_load` 验过能解析，`passwd` 里的 `p@ss #word` 也还是原值。

## M8. Debian 13 云镜像的 apt 卡住——根因是镜像自带的 security 源地址是错的（已修）

**症状.** 2026-11-10 装 `debian-13`（trixie genericcloud arm64），SSH 12 秒
就通、`vmkit.py install` 打印「已就绪」，但 `guest ping` 报「guest agent 不可达」，
`qemu-guest-agent` 一个字节都没装上。`cloud-init status` 一直是 `running`。

**排查过程与已排除的几条路.** 按顺序查的，每条都有实测证据，记下来免得下次重走：

1. **不是网络慢。** 宿主机 `curl` 从同一个 VM 里下同一个文件 0.91 秒拿到
   9.6 MB（`http://deb.debian.org/debian/dists/trixie/main/binary-arm64/Packages.xz`），
   换 `mirrors.ustc.edu.cn` 是 0.63 秒。https 同样 1.59 秒 13 MB。
2. **不是 IPv6。** `getent hosts deb.debian.org` 只返回 AAAA 记录
   （`2a04:4e42:8c::644`），而 QEMU user networking 没有 IPv6 路由，
   apt 又没有 curl 的 Happy Eyeballs——看着像根因。但 `Acquire::ForceIPv4=true`
   加上之后照样挂。
3. **不是 mirrorlist 机制。** Debian 云镜像用 `/etc/apt/mirrors/debian.list`
   加 `mirror+file` 方法重定向，换成写死的 `sources.list` 后仍挂。
4. **不是 `_apt` 沙箱用户。** `-o APT::Sandbox::User=root` 禁用降权后仍挂。
5. **不是并发。** `-o Acquire::Queue-Mode=access` 串行化之后仍挂。

**真正的位置.** `apt-get -o Debug::Acquire::http=1 update` 的输出停在
`Get:13 ... trixie/main arm64 Packages [9614 kB]`——**下载已经完成了**，
大小和 curl 拿到的一致。此时 `ss -tnp` 显示那条到 Fastly（`199.232.162.132:80`）
的连接是 `ESTAB`、`Recv-Q=0`、`Send-Q=0`，apt 主进程和各个方法进程全部阻塞在
`do_select`，而 `sqv`（apt 2.9 的签名验证器，`/usr/lib/apt/methods/sqv`）
CPU 时间是 `00:00:00`——一个跑了十几分钟却一秒 CPU 都没用过的进程。

**结论（当时）. 挂在 apt 下载完成之后的 sqv 验证上，是 Debian trixie 的 apt 2.9
在这个 VM 环境里的内部问题，与本项目的 QEMU 参数、网络、cloud-init 配置都无关。
没有找到不需要关闭签名验证的规避手段，所以不默认改 seed——为装一个包
全局禁掉 `APT::Get::AllowUnauthenticated` 不划算。

**后来找到了真正的根因（2026-11-10 当天晚上，已修）.** 上面五条排除都对，
但方向找错了：问题不在「下载完成之后」，而在 **security 那个套件的地址根本是
404**。Debian trixie 云镜像的源长这样：

    URIs: mirror+file:///etc/apt/mirrors/debian-security.list
    Suites: trixie-security

而 `/etc/apt/mirrors/debian-security.list` 里写的是
`https://mirrors.tuna.tsinghua.edu.cn/debian/`——**少了 `-security` 后缀**。
Debian 的 security 套件在这些镜像上是一条独立的树。实测四个源：

    <镜像>/debian/dists/trixie-security/Release          全部 404
    <镜像>/debian-security/dists/trixie-security/Release  全部 200
    （清华、阿里、USTC、deb.debian.org 四个源都是这个规律）

apt 拿到 404 会重试，最后报
`E: The repository 'mirror+file:/etc/apt/mirrors/debian-security.list trixie-security Release' does not have a Release file`，
cloud-init 的 `package_update_upgrade_install` 随之失败，`cloud-init status`
变成 `error`。而「下载完 9614 kB 之后卡住」那个表象，是 main/updates/backports
三个套件下完之后、security 那个 404 在重试——`ss` 看到一条 ESTAB 且
Recv-Q=0 的连接、sqv 零 CPU，都是重试间隔里的样子，不是验签卡住。

**为什么之前没看出来.** 手动 `apt-get update` 的报错被 `-o Debug::Acquire::http=1`
的输出淹没了，而当时的排查一直在追「下载完成之后发生了什么」。
`Acquire::ForceIPv4=true` 一度让症状从「挂住」变成「快速报错」，
反而成了定位它的线索。

**修法.** `linux_seed` 对 Debian 走另一条路：`apt: {preserve_sources_list: true}`
让 cloud-init 别重写源，然后自己写一份 deb822，security 指向
`<镜像>/debian-security/`，顺带去掉用不到的 `deb-src`。镜像根由
`pick_apt_mirror` 测速选出来（官方 deb.debian.org 实测 0.0–0.1 MB/s，
清华 5 MB/s、阿里 8–10 MB/s），测不出就用官方地址。
实测效果：`qemu-guest-agent` 从「永远装不上」变成 8 秒上线，
`cloud-init status` 从 `error` 变成 `done`。

**影响与现状.** 已修。修完之后 Debian 13 与 Ubuntu 一样：
`cloud-init status: done`、`qemu-guest-agent` 8 秒上线、`guest` 子命令和
stop 的 agent 优先关机都可用。VM 本身一直完全可用（登得进去、有网、能 SSH），
受影响的只是 guest agent 那一条路。

**怎么让这类问题当场可见.** `start_linux()` 在 SSH 就绪后改成有上限地等
guest agent（`_wait_guest_agent`，180 秒）并报进度：apt 在装包时 VM 早就能
登录了，探一次就 warn 会让「已就绪」把「装好了」和「还在装」混成一句。
到点仍不通才 warn，并给出客人机里的排查命令。

**另一条同类坑.** cloud-init 的 `apt.primary` / `apt.security` 在 Ubuntu 上
直接生效，在 Debian 上**完全无效**——Debian 的源走 `mirror+file:` 重定向，
改 `apt.primary` 之后 `URIs` 原样不动（实测）。两家的源机制不是一套。

## M9. Windows RDP：guest 内完全正常，但宿主机经 hostfwd 连不上

**症状.** 2026-11-10 装通 Windows 11 24H2 ARM64 后，宿主机连
`127.0.0.1:21000`（QEMU hostfwd 到 guest 的 3389）发 RDP X.224 连接请求，
拿到的是 0 字节，补发一次变成 `ECONNRESET`。

**已确认正常的部分.** 四条都查过，都不是问题：

1. hostfwd 参数是对的：`user,id=net0,hostfwd=tcp:127.0.0.1:21000-:3389,
   hostfwd=udp:127.0.0.1:21000-:3389,hostfwd=tcp:127.0.0.1:20000-:22`。
2. guest 里 3389 在 `::` 和 `0.0.0.0` 上都 Listen，`TermService` 是 Running。
3. 防火墙三条 RDP 入站规则全部启用，Profile 覆盖 Domain/Private/Public。
   guest 的网络类别是 Public——临时把 Public 防火墙整个关掉再测，
   宿主机侧依然是 0 字节，所以不是防火墙。
4. **从 guest 内部连自己的 3389 有正常响应**（TPKT/X.224 二进制应答），
   连 `10.0.2.15:3389` 也有。RDP 服务本身是好的。

**卡在哪.** slirp 从宿主机往 guest 的 3389 建连接这一跳。同一个 hostfwd
机制下 SSH（`20000 -> guest:22`）完全正常，说明不是 slirp 整体坏了，
而是这一条链路特有的失败。根因未定位，没有继续挖——`ss -tnp` 只能看到
宿主机侧，guest 侧看不到 slirp 发起的连接最终怎么了。

**现在能用的两条路.**

1. **VNC（推荐）.** `22000`，RFB 3.8，安全类型 `01 01`（即 None，无认证），
   实测握手正常。它是 QEMU 层的 framebuffer 服务，根本不经过 guest 网络，
   所以不受这个问题影响。注意 QEMU 的 VNC 不给新客户端推首帧，
   连上后画面是空的，要等客人机重绘（见 M 系列前面记的 VNC 三个坑）。
2. **SSH.** `20000`，正常，管理员权限。

**判断.** 记成已知限制，不阻塞 Windows 装机。真要 RDP，
在同一台机器上跑 `Microsoft Remote Desktop` 连 `127.0.0.1:21000`
能否绕过 slirp 这一跳，本轮没有实测（会弹 GUI 窗口）。

## M10. cloud-init 的 runcmd 里等 cloud-init 自己，死锁

`linux_seed` 生成的 user-data 末尾原来有这一行：

```yaml
runcmd:
  - [ sh, -c, "cloud-init status --wait >/dev/null 2>&1 || true" ]
```

它跑在 `modules:final/config-scripts_user` 里，而那一整个阶段要等 runcmd
返回才算结束。于是 `cloud-init status --wait` 等的是「cloud-init 结束」，
cloud-init 结束要等 runcmd 返回——互相等，`cloud-init status` 永远停在
`status: running`。2026-11-10 实测：客人机起来 7 分钟后仍然 running，
`/var/log/cloud-init.log` 最后一行是
`Running command ['/var/lib/cloud/instance/scripts/runcmd']`，
`ps` 里能看到一个 `cloud-init status --wait` 子进程在空转。

**怎么发现的.** 给 Linux 装机加「等 guest agent 上线」时计时，发现
`qemu-guest-agent` 已经 active 而 cloud-init 仍是 running，与预期相反——
agent 是 runcmd 之前的模块装的，它 active 说明后面的模块卡住了。

**修法.** 删掉那一行。宿主机这边要等 cloud-init 完，就在宿主机等
（`_wait_guest_agent`），没有理由让客人机里再起一个自己等自己的进程。

**顺带.** 这一行还解释了另一件事：`qemu-guest-agent` 装上但
`cloud-init status` 不结束，会让「cloud-init status --wait」成为
排障时的红鲱鱼——看着像没装完，其实只差这一个自等待。

## M11. `guest exec "整条命令"` 被 shlex.quote 成一个命令名

`qga_exec` 收到列表时，一律 `shlex.join(argv)` 后套 `sh -c`。单个元素且
含空格的列表会被整体加引号：

```
argv = ["uname -a && df -h /"]
shlex.join -> "'uname -a && df -h /'"
/bin/sh -c "'uname -a && df -h /'"
-> /bin/sh: 1: uname -a && df -h /: not found
```

2026-11-10 实测。而 `vmkit.py guest --name d1 exec -- uname -a`（多个参数）
一直是好的，所以只有「用户把整条命令引成一个参数」这一种写法坏——
而那恰恰是带管道、`&&`、重定向时最自然的写法，也正是排障时最常用的。

**修法.** 单个元素且匹配 `[\s|&;<>()$\`\\"'*?\[\]#~%]` 时，直接把它当
`sh -c` 的命令行，不 quote；多个元素才逐个 quote 再拼回去（那样
`echo "a b"` 才能还原）。

## M12. VNC 显示编号分配：端口那一侧的偏移量算错了

`vm.json` 存的是**显示编号**（16100 起），QEMU 实际监听 **5900 + 编号**
（22000 起）。分配函数要比对两件事：别的 VM 占用的编号，和宿主机上已占用的
端口。原来端口那一侧写成

```python
if PORT_VNC_BASE + (d - VNC_DISPLAY_BASE) in used:
```

`PORT_VNC_BASE` 是 5900、`VNC_DISPLAY_BASE` 是 16100，对 `d = 16100` 就是
`5900 + 0 = 5900`——它去比对的是 5900 到 9899 这一整段，跟 QEMU 真正会绑的
22000 到 25999 毫无关系。宿主机上已经有东西占着 22000 时这里看不出来，
照样把 16100 发出去，QEMU 起来才报

```
-vnc 127.0.0.1:16100: Failed to find an available port: Address already in use
```

那句话里的 16100 是显示编号，用户照着去找端口会找到空气。

2026-11-10 实测：一台跑着的 QEMU 占着 22000，`alloc_vnc_display(set())`
仍返回 16100；改成 `PORT_VNC_BASE + d` 后返回 16101（端口 22001）。

**教训.** 修「两个编号空间混用」这类 bug 时，两边都要修。这里第一次只修了
`taken` 那一侧（VM 之间的编号比较），端口那一侧的偏移量留着，症状从
「每台 VM 都拿 16100」变成「只在宿主机端口真的被占时才炸」，更难复现。

## M13. 装到一半关掉终端，QEMU 作为孤儿继续跑

`_install_signal_guard` 原来只挂 SIGTERM 和 SIGINT。关掉终端窗口发的是
SIGHUP，`vmkit install ... | head` 让管道先关发的是 SIGPIPE（Python 默认把
SIGPIPE 设成 SIG_IGN，往关闭的管道写时抛 BrokenPipeError，那个异常没人接）。
两条路的结果一样：vmkit 静默消失，而 QEMU 是 `start_new_session` 出去的另一个
进程组，继续跑着——占 2 GB 内存、SSH 端口、VNC 端口，而用户以为「取消了就
什么都没了」。2026-11-10 实测复现：`nohup vmkit install ... &` 起来 20 秒后
`kill -HUP`，vmkit 没了，`ps` 里 QEMU 还在，`-name pipetest`。

pid 文件还在，所以 `vmkit stop --name X` 事后能停；但没有任何东西告诉用户
这儿还挂着一台，而内存和端口是真实占着的。

**修法.** 把 SIGHUP 和 SIGPIPE 加进同一个 handler，和 SIGTERM 走同一条
「读本次安装的 pid 文件、SIGTERM 它、自己 os._exit(130)」的路。

**注意别把 SIGPIPE 和「写管道失败」混为一谈。** 挂上 handler 之后 Python
不再把 SIGPIPE 转成 BrokenPipeError，`vmkit install | head` 这类用法会安静地
把客人机一起带走——那正是想要的行为，但意味着别指望能靠管道中途断开来
「只看看前几行」而不影响安装。

## M14. macOS/HVF 上几条「调优手段」其实是无效的，别浪费时间

2026-11-10 逐条查了 QEMU 11.1.3 源码加本机实测，结论都带证据。记下来是为了
下次别再被网上的说法带跑。

**`-global virtio-blk-pci.cache-size=2M` 会让 QEMU 起不来。** `cache-size`
从来不是 virtio-blk 的设备属性——`-device virtio-blk-pci,help` 列出的 75 个
属性里只有 `write-cache`，写成 `-global` 直接
`Property 'virtio-blk-pci.cache-size' not found` 然后 exit 1。它是 **qcow2
镜像格式**的选项（L2/refcount 块缓存）。而且 `-global` 是懒生效的：命令行里
有它但没创建对应设备时 QEMU 不报错，等到某天加了块盘才炸。
macOS 上 qcow2 的 L2 缓存默认只有 8 MiB（Linux 是 32 MiB），`cache-size=2M`
是把 8 MiB 砍到 2 MiB，方向是反的。

**`cache=none` 在 macOS 上不绕开宿主 page cache。** macOS 没有 `O_DIRECT`
（clang 探测确认，`os.O_DIRECT` 也不存在），QEMU 的 `block/file-posix.c` 把
`O_DIRECT` 定义成 `O_DSYNC`。于是四种 cache 模式在 macOS 上的真实行为是：
`writeback` 无附加标志、`writethrough` 无、`none` O_DSYNC、`directsync`
O_DSYNC——**没有一种是真 O_DIRECT**。`writeback`（默认）就是对的。

**`aio=native` / `aio=io_uring` 在 macOS 上直接拒绝。** 实测
`aio=native was specified, but is not supported in this build.` 和
`invalid aio option`。macOS 上默认就是 `aio=threads`，而且没有别的可选。

**iothread 在 HVF 下拿不到主要收益。** `accel/hvf/hvf-all.c` 的 memory
listener 没有 `eventfd_add`/`eventfd_del`（整个 `accel/hvf/` 目录 grep
`eventfd` 零命中），而 `hw/virtio/virtio-pci.c` 的
`memory_region_add_eventfd()` 是无条件调的——只是没人接。所以 virtio 门铃在
HVF 上永远走 data abort → VM exit → 用户态 `virtio_queue_notify()`，
iothread 只影响 `handle_output` 之后跑在哪个 AioContext。UTM 的 QEMU 配置里
也没有 iothread / num-queues / vectors（`UTMQemuConfiguration+Arguments.swift`
全文 grep 零命中）。

**`num-queues` 的默认值是 1，而且上限是 vCPU 数。**
`include/hw/virtio/virtio-blk.h` 的 `VIRTIO_BLK_AUTO_NUM_QUEUES = UINT16_MAX`
被解析成 `conf->num_queues = 1`；实测 2 vCPU 下 `num-queues=4/8` 都只建出
2 个队列，写吞吐 2.0–2.4 GB/s 与默认无差别。

**`virtio-balloon` 在 macOS 上是结构性失效。** QEMU issue #4170：
`ram_block_discard_range()` 在 macOS 上返回成功但不真的把内存还给宿主机，
于是 balloon 充气、free page reporting、virtio-mem 全部变成 no-op，
「没有 error、没有 warning，trace 里每次调用都是 `ret: 0`」。这不是配置问题。

**`gic-version=3` 设不设都一样。** HVF 下 GICv3 的可用条件是
`!kernel_irqchip || !vms->virt`，而 `-machine virt,virtualization=` 默认 false，
所以任何组合下 GICv3 都成立。真正起作用的 `kernel-irqchip` 在 virt-11.1 及以后
**默认就是 on**（`hw/arm/virt.c` 的 `virt_machine_11_1_options()` 不设
`hvf_no_kernel_irqchip_default`），运行时 `info qtree` 能看到
`dev: hvf-arm-gicv3`。UTM 那个 in-kernel GICv3 补丁已经被上游吸收，
我们用 11.1.3 等于白拿到了。

**qcow2 `cluster_size` 越大越占空间。** 实测首启后实际占用：
默认 64K 是 27 MB，1M 是 131 MB，2M 是 182 MB。L2 缓存上限只有 8 MiB，
大 cluster 不会提升命中率，只会让 `max_l2_cache` 更大、更占空间。

**窗口不可见时 QEMU 慢 5 到 10 倍。** QEMU issue #3190，macOS App Nap 导致。
我们是 launchd 无人值守跑的，如果哪天发现「同配置前台跑就是快」，先查这条。
`-display none` 不起 Cocoa 前端，理论不受影响，但没有实测过。

## M15. Apple CDN 加连接不会更快，反而更慢

2026-11-10 实测 `updates.cdn-apple.com`（UniversalMac IPSW）：

    串行单连接，4 个 100 MB 段   2.8 / 3.1 / 3.0 / 5.5 秒（19–36 MB/s）
    6 个 curl 并发，每段 100 MB  段耗时 8.8–16.2 秒，聚合 37 MB/s
    2 个 curl 并发              段耗时 12.2–17.9 秒，比串行还慢

CDN 是按客户端限速的，加连接只是把每条的份额摊薄，聚合不涨。
所以下载就是一条 curl 连接加 `-C -` 续传。

顺带记一个反例：早前一版的 `SegmentedDownloader`（URLSession 多连接分段）
文档里写「单连接 1 MB/s、6 连接聚合 18.8 MB/s」，那个数与这次实测不符，
而且那条路在 Apple CDN 上每次都稳定卡在同一个段、根本下不完
（四种修法全部实测排除，见 MacVMEngine.swift 里 `macFetchWithCurl` 的注释）。
那个类已经删掉。

## M16. 「VM 不完整，缺 disk.qcow2。重新 install。」是个死胡同（已修）

2026-11-10 用户原话：「一个 bug，VM 不完整，缺 disk.qcow2。重新 install。
一直跳出无法关闭」。

这句话是 `start_linux` 里的一句 `die`。问题不在它报错了，在于**它指的路走不通**：

    start   → 「VM 不完整，缺 disk.qcow2。重新 install。」
    install → 「xxx 已存在。换个名字，或加 --force 重装。」

两句互相踢皮球。用户既起不动这台机器，也没法原地修好它，只能自己去终端
删目录。而 app 那边连 `--force` 都传不了（新建向导永远不带这个 flag），
所以在图形界面里这条错误是终点了。

更糟的是 `spawn_detached_install` 那条路：app 永远走 `--detach`，而
「已存在」这道校验原来只写在 `cmd_install` 里。父进程一声不响就把子进程
起出来、写好 install.pid、向 app 报「安装已开始」，子进程一秒后死在
「已存在」上。app 那时已经切到「正在安装」页，用户看到的是一句错误
钉在一个没有出口的页面上（M17）。

修法三件事：

1. `vm_broken(vm)` 显式算出「缺哪些文件」，`vm_record` 把它作为 `broken`
   字段发出去。界面据此显示「没装完」并给「重装」按钮，而不是等用户点
   「启动」去撞一句死胡同错误。
2. 两处 `die` 都改成能照着做的命令（`install --name X --force` 或
   `delete --name X`），并且用 `_self_cmd()` 给全路径——vmkit.py 没有装到
   PATH 上，写 `vmkit.py` 用户照抄会 command not found。
3. `_refuse_existing()` 在 `cmd_install`、本地安装包路和
   `spawn_detached_install` 三处都跑一遍。半成品时那句错误要特别说明
   「它没有可用数据，--force 正是给它准备的」——不说清楚用户以为会丢东西，
   不敢按。

## M17. 新建向导的「正在安装」页没有出口（已修）

同一个 bug 的后半截。`NewVMWizard` 的 `.installing` 页脚原来只有一个按钮：

    Button("停止") { state.backend.cancel() }
        .disabled(!state.backend.busy)

而 `step` 离开 `.installing` 的唯一路径是 `.onChange(of: state.installFinished)`，
`installFinished` 只在 `handleEvent` 收到一个**能解成 VMInfo 的** `done`
事件时置位。安装失败、被用户停止、后端发来的 done 形状对不上——这三种情况
它永远是 false。于是：

- 「停止」按钮在安装失败后虽然能点，但 `cancel()` 打在一个早就死掉的进程上，
  什么也不发生；
- 页面没有任何关闭按钮，Esc 也没有 `.cancelAction`；
- 用户被关在这一页上，只能强杀 app。

这就是用户说的「无法关闭」。

修法：

- 页脚永远给「关闭（后台继续装）」——安装是脱离 app 的后台任务，顶部横幅
  会继续显示进度，这句话必须写在按钮上，不然用户以为一关就没了。
- 新增 `installEnded`：`tailLog` / `resumeTailing` 的 completion 里置位。
  进程没了就是结束了，这个信号不依赖任何事件形状，失败时也一定来。
  到点就让 `step` 回 `.config`（能改名再装一次），没有选过系统时回 `.pick`。
- `handleEvent` 的 `done` 分支区分两种形状：引导进程的
  `{"name","log","pid","detached":true}` 只说明「已经甩到后台」，
  安装进程自己的 `{"vm":{…完整记录…}}` 才是装好了。原来两个都按后者解，
  引导那个必然静默失败（`try?`），`installFinished` 永远不来。

## M18. `--force` 重装会把 install.log 一起删掉，app 看到的是一个空页面（已修）

`spawn_detached_install` 先打开 `<vm>/install.log`（append）再把子进程
起出来，子进程继承那个 fd；而 `--force` 会 `_rmtree` 整个 VM 目录。文件被
unlink，子进程还在往一个已经不在目录树里的 inode 写。

用户看到的是 install.log 不存在、或者停在上一轮的内容；app 的 `tailLog`
一个事件都读不到，界面是一个完全不动的「正在安装」——比报错更难查。

这条在「给半成品虚拟机加重装按钮」之前一直没人踩到，因为 `--force` 原来
只有命令行会用。修法：`_reopen_install_log()` 在 `_rmtree` 之后把 fd 1/2
重新指回新文件（`os.dup2`，`sys.stdout` 不用重新赋值），补一行表头，
并把 install.pid 按 `os.getpid()` 写回去——那一份也被 rmtree 删了，
而 app 的「停止」按钮和重启后的「接上还在跑的安装」都靠它找进程。

只在确实是后台子进程时调用（用 install.pid 的存在判断）：命令行前台跑的
用户 stdout 是终端，不能把它改道进文件里。

## M19. install.pid 里的死进程号会被复用，app 以为安装永远在跑（已修）

app 的 `installAlive` 原来只判「这个 pid 活着吗、是不是僵尸」。macOS 的
pid 会回绕复用，一个早就结束的安装留下的号随时可能被派给毫无关系的进程。
这时候 app 认为安装还在跑：

- 顶部横幅钉在「正在安装」；
- `running` 不归零 → `busy` 恒为真 → 所有按钮全灰；
- `resumeWatching` 会去尾一份早就写完的日志，`tailLog` 的定时器永远不取消。

用户什么都做不了，只能强杀 app。

两件事一起修：

1. vmkit 侧：安装结束就删 install.pid。放在 `main()` 里 install 那段
   `finally`（`die()` 走 `sys.exit`，正常返回也会走到），信号 handler 里
   再补一次——那边用 `os._exit`，不走 finally。窗口从「永久」缩到
   「安装进行中」。
2. app 侧：`installAlive` 用 `ps -o state=,command=` 连进程名一起核对，
   命令行里没有 `vmkit.py` / `vmz` 就当作没在跑。只取 `comm` 不够——
   python3 的 comm 是 "Python"，任何 Python 程序都算。对不上时返回 nil
   是安全的方向：最坏是让用户重点一次安装，不会把人锁死在灰屏界面上。

顺带修了 `tailLog` 的一个老问题：`resumeTailing` 已经把整份日志放出去过
一遍（并设好 tailOffset），紧接着调的 `tailLog` 又把 offset 清零重读，
同样的事件触发两遍——同一个错误弹窗连跳两次。加 `fromStart:` 参数，
接续时传 false。

## M20. 全角标点紧跟变量名，bash 把它并进变量名，`set -u` 下整个脚本崩（已修）

2026-11-10 跑 `./install.sh` 时装 app 前一步硬失败：

    ./install.sh: line 100: APP_DST\xef: unbound variable

那一行是 `warn "已存在 $APP_DST，覆盖"`。`，` 是 U+FF0C，UTF-8 三个字节
`EF BC 8C`。bash 的变量名规则本来只认 `[A-Za-z0-9_]`，但在这个 locale 下
（`LC_CTYPE=C.UTF-8`）高位字节被它当成名字的一部分——于是 `$APP_DST，覆盖`
解析成一个叫 `APP_DST\xEF\xBC\x8C` 的变量，`set -u` 判它未绑定，直接退出。
最小复现：

    $ sh -c 'set -eu; X=hi; echo "$X，y"'      # X<0xEF>: unbound variable
    $ sh -c 'set -eu; X=hi; echo "${X}，y"'    # hi，y

十二处同类写法，八处在会执行的代码里，五个脚本全都带 `set -u`：

    app/build.sh          退出码 $vmz_status）。      /  $sign_status）。
    install.sh            详见 $VMZ_LOG）             /  ${APP_DST}，覆盖（这一处直接挡住装机）
                          / 详见 $APP_SIGN_LOG）
    scripts/10-install-tart.sh   用本地压缩包 $TARBALL（  /  退出码 $rc）。
    scripts/agentvm-fetch        删掉了 $out，
    scripts/install-qemu.sh      找不到 $QEMU_BIN。

后果不是「提示语少个值」：脚本在把 AgentVM.app 拷进 /Applications **之前**
退出，而前面 QEMU 检查、vmz 编译全都打了 ok。用户看到的是一个装到一半、
日志一片绿的安装。装机路径上最不该有的形状。

统一改成 `${VAR}`。花括号让名字的边界没有歧义，跟 locale 无关。

lint 加了一道正则检查（`Makefile` 的 `wide-punct` 目标），专门抓
「没有花括号的 `$VAR` 后面紧跟非 ASCII 字节」。注释里的不算——那是文档，
不会执行；但看得见的也顺手加了花括号，免得有人照着抄。

## M21. 自动重连一次都没执行过：`scheduleReconnect` 排的活儿被 `!stopped` 挡在门外（已修）

用户报的现象：app 内嵌控制台显示「虚拟机那边把连接关了」，然后**再也不动**。
而同一时刻 QEMU 好端端在监听（monitor 的 `info vnc` 看得见服务器，用一个
python 裸客户端连上去 30 秒、收 1.9 MB 数据、一个字节都没断）。

代码里本来就有一段「断开时自动重连，带退避」，注释还写着「原来断线之后……
然后再也不动——必须用户自己想起去点『重连』」。也就是说这段代码是专门为了修
这个问题加的，而它自己从来没生效过：

    private func fail(_ msg: String) {
        guard !stopped else { return }
        stopped = true            // ← 为了停掉旧连接的 receive 循环
        ...
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.stopped else { return }   // ← stopped 刚被置 true
            ...
            self.connect()
        }

`fail()` 靠 `stopped` 让旧连接的 receive 循环停下来，而重连恰恰要在那个状态下
发生。两处对同一个标志的期待正好相反，结果就是退避定时器每次到点都直接 return。
**一个从来没有任何执行路径能走到的分支，比没有分支更危险**——它让「已经修过了」
这件事看起来是真的。

修法：把「当前连接还活着吗」和「用户还想连着吗」分开。`stopped` 只管前者；
后者由 `disconnect()` cancel 那个 work item 来表达（它本来就这么做了）。
work 里不再判 `stopped`，`connect()` 第一件事就是 `stopped = false`。

顺带把次数从 5 提到 8：1/2/4/8/16/32/64/128 秒，合计约 4 分钟。虚拟机装完系统
重启、QEMU 重新起来都要这个量级，5 次只有 31 秒不够；而「用户已经关掉面板」
不靠次数兜，靠 cancel。

实测（`/tmp/vncharness` 的 Swift harness + python RFB 服务端）：

    0.048s  已连接
    1.713s  虚拟机那边把连接关了      ← 服务端真的断开
    2.801s  已连接                    ← 1 秒退避后自己连回来

## M22. `.ready` 只说明 TCP 通了，不等于 RFB 握得下来（已修）

`connect()` 的 stateUpdateHandler 在 `.ready` 那一刻就 `publish(connected: true,
status: "已连接")`。而 `.ready` 只是三次握手成了：端口被别的东西占着、listen
backlog 满了、对面是个不收数据的服务，全都会走到这里然后永远等不到 ServerInit。

那时的界面是一盏绿灯加「已连接」，底下没有画面——而状态明明是好的，这是最难查
的一种形状。VNCClient 自己的注释里就记着另一件同类事（「连接是好的、尺寸也对、
敲键盘也有反应，就是看不到变化」），但绿灯这一处没人回头看。

改成：`.ready` 时显示「正在取画面」，真正拿到画面尺寸（serverInit 那一步）才
`connected = true`。`queueConnected`（输入事件的门禁）也跟着移到那一刻，
TCP 一通就往客人机发键鼠事件本来也不对。

同一个坑在测试里藏了一道：我的第一个 RFB 测试服务端把协议顺序写反了（服务端
先 `recv` 等客户端版本，而 RFB 规定服务端先发）。两边互等，握手永远完不成——
但因为那会儿「已连接」在 `.ready` 就置位，测试看起来是通过的。**绿灯本身成了
掩盖测试缺陷的东西。** 修好之后第一次重跑，两个测试同时变红，正是这个改动
把它们照出来的。

## M23. QEMU 的 VNC 默认允许独占，第二个客户端会把 app 的控制台踢掉（已修）

`-vnc 127.0.0.1:16100` 没给 `share` 参数，QEMU 默认是 `allow-exclusive`：
客户端在 ClientInit 里请求独占时，其他客户端全部断开。

这条不是理论风险。「远程桌面」那一行给用户的就是 `open vnc://127.0.0.1:22000`，
用户完全可能一边用 app 内嵌控制台、一边用系统 Screen Sharing 看同一台。那时
app 的画面会被一下踢没，而它只会说「虚拟机那边把连接关了」——指向一个不存在
的客人机故障。

两处 `-vnc` 都加 `,share=force-shared`。强制共享之后谁也踢不了谁，多开几个
客户端只是多几个入口。

注意 `share` 是启动参数，**对已经跑起来的 VM 无效**，要下次启动才生效。

## M24. RFB 的第一行是画面顶部，CGContext 的第 0 行是画面底部——原样 memcpy 就是上下颠倒（已修）

用户报「文字窗口都是倒置的，内部无法选择」。

先证明不是 QEMU：用 python 按 app 的同一套字节（SetPixelFormat 32/24 小端真彩、
位移 16-8-0、SetEncodings raw + DesktopSize、ClientInit shared=1）连上去抓一帧，
画面是正的。所以数据没问题，是 app 把它放错了位置。

RFB 的矩形数据第一行对应画面顶部；而 `CGContext(data:width:height:...)` 的数据
缓冲区第 0 行是画面底部（CG 的原点在左下角）。原样 `memcpy(dst + y * rowBytes, ...)`
等于把顶部数据写进底部，整个画面颠倒。

连带的是鼠标：`VNCScreenView` 的 `isFlipped = true` 只换算鼠标坐标，不管图像方向。
于是用户看到按钮在右下、framebuffer 里它在左上，手指点下去命中另一个控件——
「点哪儿都中不了」。这两句抱怨是同一个根因。

修法：逐行翻转着写，`dst + (fbH - 1 - y - r) * rowBytes`。顺手删掉了原来
「x == 0 && w == fbW 时整块 memcpy」那条快路——它在新方向下是错的。

验证：服务端发一帧「上白下黑」，检查 CG 数据第 0 行（画面底部）是黑、
最后一行（画面顶部）是白。修好后通过；修好前第 0 行是白的。

## M25. 控制台窗口关不掉：Esc 被画面视图无条件吃掉，而窗口里根本没有关闭按钮（已修）

用户报「窗口只能打开，无法关闭」。两条叠在一起：

一、`VNCScreenView.keyDown` 里 `if event.keyCode == 0x35 { ...; return }` 是
无条件消费。这个视图是 first responder，事件到不了 SwiftUI 的 `.cancelAction`。
新建向导的安装页和详情页的控制台都嵌着同一个视图，两边同时中——安装页那个
「关闭（后台继续装）」按钮绑的就是 `.cancelAction`。

二、`ConsoleView` 原来一个关闭控件都没有：没有按钮，sheet 那张 × 在全屏画面上
不一定点得到。于是 Esc 失效之后用户没有任何出路。

修法：Esc 按场景分流——锁鼠标时释放鼠标，没锁时 `window?.performClose(nil)`；
另外加一个看得见的「关闭」按钮，和单独给客人机发 Esc 的「Esc 键」按钮
（键盘上那个 Esc 被占了，而装系统时取消对话框全靠它）。

**顺带踩到一个自己造的回归**：刚加「关闭」按钮时没考虑 `ConsoleView` 是被复用的——
新建向导的安装页嵌着一块 300px 高的，那里的 `dismiss()` 作用的是最外层 sheet，
一按就把整个安装向导关了。加了 `embedded` 参数，嵌入时不显示关闭按钮、Esc 也不
关窗口（改成把 Esc 转给客人机）。

## M26. 非锁定模式下鼠标移动送不到客人机；滚轮垂直方向是反的；切 app 时修饰键卡住（已修）

三条都藏在 `VNCScreenView` 里，都是「代码看着对、路径从来没走到」。

一、`mouseMoved` 里有段「不锁鼠标时也要把移动发下去」的修复，但
**`NSWindow.acceptsMouseMovedEvents` 只在 `acquireLock()` 里置过**。从没锁过鼠标
的用户在画面上滑动，客人机光标一动不动——只有点下去那一下才跳过去。安装器里的
悬停高亮、Tooltip、菜单展开全部无效，而用户完全看不出是客户端的问题。修法：
`viewWillMove(toWindow:)` 里置上，`releaseLock()` 故意不复位（解锁后也该收到移动）。

二、滚轮垂直方向反了。`scrollingDeltaY > 0` 是往上滚，而 RFB 的 button 4（掩码 8）
才是往上滚——原来把正的发给 16（button 5）。手指往上推、内容往下跑，每次都得
反向操作。水平方向本来是对的，不要一起动。

三、修饰键只在视图内部维护 `prevFlags`。按住 Shift 去点标题栏按钮，或者 Cmd+Tab
切走再松开，视图都收不到 flagsChanged，而客人机那边的 Shift 一直按着——切回来
打字全是 Shift+a。装系统要敲大写字母和符号，正好卡在这。修法：
`resignFirstResponder()` 和窗口失焦通知里补发释放。

## M27. 面板里的 VMInfo 是过期快照；cancel 的兜底打的是 status 子进程（已修）

一、`settingsVM` / `storageVM` / `consoleVM` 都是点按钮时赋一次值的快照，
`refreshStatus()` 之后不同步。症状：加完一块盘，列表还写着「还没有附加盘」；
改完 CPU 内存，面板里还是旧值——用户以为没生效，会反复点同一个按钮。
「保存」按钮拿旧 vm 当 dirty 基线，还会出现「什么都没改却可点」的反向情况。
`macVM` 那边本来就是同步的，这三个漏了。

二、`Backend.cancel()` 的兜底 `process?.terminate()` 打的是「最近启动的那条命令」，
而安装期间每个 stage 都会 `refreshStatus()` 把 `process` 覆盖成一条 status 子进程。
install.pid 丢失、pid 被复用、ps 起不来导致 `installAlive` 返回 nil 时，用户点
「停止」→ SIGTERM 发不到真正的安装进程，界面说停了、横幅消失了、向导跳回配置页，
而 4 GB 还在后台继续下载。兜底改成给一句能照着做的提示，不去 terminate 那条
本来就该退的 status。

## M28. 同一次状态更新里 dismiss 一个 sheet 又 present 另一个；启动时三条命令的 error 被整个丢掉（已修）

一、`state.consoleVM = vm; dismiss()` 写在一起。SwiftUI 对「同一事务里撤销 A、
呈现 B」的处理不可靠：结果是 consoleVM 留着非 nil、控制台 sheet 却不显示，
而此后在详情页点「控制台」也不会再弹（item 没变，SwiftUI 认为已经呈现过了）。
用户完全没有入口。修法：先 `dismiss()`，再用 `DispatchQueue.main.async` 隔一帧设
`consoleVM`。

二、`init()` 里 catalog / status / check 三条命令，各自的 handler 都 guard 了一个
特定的事件类型，`error` 事件被一起挡掉，terminationHandler 也不看退出码。于是：
catalog.json 不在 → 向导第一步永远转「正在读镜像目录…」，不给任何原因；
status 失败 → 主窗口显示「还没有虚拟机」，而 ~/.agentvm/vms 下明明有东西，
用户会以为自己记错，去点新建装出第二份；check 失败 → 每个详情页顶一条
「宿主机检查没全过」而下面一条明细都没有。操作类命令早就用 handleCommandEvent
堵过这一类，只是这三条读命令漏了。

## M29. mouseMoved 只送给 first responder——「鼠标移不动」和上一轮的修法为什么都没用（已修）

**这条是用户报「进入控制台还是无法进行鼠标移动」的根因，2026-11-10 在本机实测定位。**

一、上一轮（M26）的修法是 `viewWillMove(toWindow:)` 里
`newWindow?.acceptsMouseMovedEvents = true`。端到端实测（真 ConsoleView + 假 RFB
服务端 + 合成鼠标事件）证明那条路有一个没说出口的前提：**它只把 mouseMoved 送给
first responder**。而点击之前这个视图不是 first responder（SwiftUI 里通常是
hosting view 或窗口自己），于是 5 次鼠标移动送到服务端是 0 个 PointerEvent。
点击、拖动、滚轮、键盘全都正常，唯独移动整个不工作——因为那些走 hit-test，只有
mouseMoved 走 first responder。点过一下之后这条通路才通，而「先移动、再点」正是
人的操作顺序，所以现象是「完全没法用鼠标」。

二、真正的主通道是 `NSTrackingArea`：owner 直接收，不依赖 first responder，也不
依赖 hit-test。`biztalk72/VNCClient` 的 VNCView.swift 和 `loomhand/swift-rfb` 的
RFBView.swift 两个独立实现都只用它，全程不碰 acceptsMouseMovedEvents。
SwiftTerm 的测试注释把第一条路写得很直白：「acceptsMouseMovedEvents delivers
mouseMoved to the first responder regardless of the pointer's location」。

三、**选项必须是 .activeAlways。** 本机 SDK 里 `.activeInKeyWindow` 的位值是 32
（经典头文件里那是 ActiveWhenFirstResponderOnly），实测它在这个视图上不投递——
窗口明明是 key 的、`isKeyWindow == true`，点击前的移动还是一个都不来。换成
`.activeAlways`（128）立刻就好。`kanka-jp/konnyaku` 的 PR #25 也是被同类问题逼着
从「依赖激活态」改成 .activeAlways 的。代价：控制台不在前台时鼠标划过画面也会带动
客人机光标——对远程桌面窗口来说是合理行为。

四、**两条通路不能同时开。** 跟踪区域加上窗口标志之后，点击后视图成了 first
responder，两条路都通，每次移动往客人机送两次一模一样的 PointerEvent。修法：
窗口那个标志只在锁定模式下才置（那时候走窗口级局部监视器，而局部监视器要这个
标志才看得见 mouseMoved——不置的话 AppKit 压根不生成这类事件），并且存原值、
解锁时还原，不无条件写 true。

五、顺带在这条路上修掉的：`NSTrackingArea.Options` 的位值在 macOS 27 的 SDK 里
和经典头文件不一样（activeInKeyWindow 32 / activeInActiveApp 64 / activeAlways 128，
ActiveWhenFirstResponderOnly 整个被删了），照老教程写的选项组合会 silently 变成
另一回事。判断某个选项到底生不生效，只能实测。

## M30. 延迟按压把拖动整个吃掉；keysym 对不上就卡键；锁定模式两条路又同时开了（已修）

**2026-11-10 第八轮。这一轮是在 M29 修完之后，用同一个端到端 harness 把输入
通路整条跑一遍跑出来的——三个真毛病，全都是「协议字节对得上、语义是反的」那一类，
只看代码看不出来。**

一、**120ms 延迟按压把拖动整个吃掉了。** `mouseDown` 为了迁就图形安装器（要看到
指针先移到按钮上才认这一下点击），按下之后憋 120ms 才真发 button-1。问题在于
`buttons |= 1` 是立刻置上的，于是这 120ms 里每次拖动都带着 button1 的掩码发出去了，
而客人机还没收到过按下。harness 实测到的完整顺序是：

    移动@A(无键) → 拖动@B(带键) → 按下@A(带键) → 松开@A(无键)

中间那些移动全白费，松开还回到按下点——拖选文字、拖安装器的滚动条整个不工作，
而单击是好的（单击根本碰不到这条路径），所以极难归因。修法：`send()` 是所有指针
事件的唯一出口，在那儿放一个「只要掩码非零就先把憋着的按下发掉」。**不能只堵
`mouseDragged`**：实测合成事件在按住左键移动时是以 `mouseMoved`（不是
mouseDragged）送到视图的，只堵 dragged 堵不住。松开那一侧也要改：按下补在按下点，
松开放开手指的位置，原来两下都发在按下点上。

二、**keysym 对不上就是永久卡键。** 原来用 `Set<UInt32>` 记「送下去过的 keysym」，
keyUp 时再按 keysym 找回来发释放。可 keysym 是按下那一刻按修饰键算出来的：
Shift+a 送下去的是 'A'(0x41)，而用户常常先松 Shift 再松 a——那一刻 keyUp 的
modifierFlags 里已经没有 Shift，照 `characters` 算出来是 'a'(0x61)，对不上，
释放永远发不出去。harness 实测：Shift 按下、A 按下、Shift 松开、a 松开，服务端
只收到 A 的按下，没有松开；全局配对检查抓到 `0x41 剩 1 个按下没释放`。
修法：**按虚拟键码记账**（`[UInt16: UInt32]`，键码 → keysym）。按下和松开必须是
同一个物理键，而键码才是那个在修饰键变了之后仍然稳定不变的东西。同一个改动顺带
修掉了「同一个 keysym 按两次只释放一次」。

三、**锁定模式下两条通路又同时开了，而且是我自己刚引入的。** M29 第四点记着
「两条通路不能同时开」，当时只拆了跟踪区域。这一轮为了「按 Esc 能释放鼠标」在
`acquireLock()` 里加了 `win.makeFirstResponder(self)`（点「锁定鼠标」按钮之后
first responder 是那个按钮，Esc 送不到画面视图上，而按钮的提示文字写着「按 Esc
释放」）——于是一锁定，视图既是 first responder、窗口标志又置着，局部监视器转发的
事件顺着响应链又进一次 `mouseMoved`。实测同一点连发三次，客人机收到 7 个
PointerEvent，每步位移翻倍，指针像脱缰一样冲到屏幕边缘被夹住。
修法：局部监视器 `return nil` 把事件吃掉，不再往下传。

四、顺带修掉的：`viewWillMove(toWindow:)` 每次都用新的 observer 覆盖
`keyObserver`，老窗口的 token 一直到 deinit 才移除，回调还会打过来。

五、这一轮同时立下了 harness 自己的三条规矩（都是这一轮踩出来的，不遵守就会
把 harness 的假象当成 app 的毛病）：
1. **主线程一秒都不能堵。** 原来用 `usleep` 分隔合成事件，而 harness 自己就是那个
   app——主线程睡着时 run loop 不转，AppKit 会把排队的 mouseMoved / flagsChanged
   合并掉：5 个移动只剩最后 1 个，Shift+a 变成裸 a。改成挂主队列的重复定时器。
2. **合成修饰键不能设空 flags。** `CGEvent(keyboardEventSource:virtualKey:keyDown:)`
   之后设 `e.flags = []` 会把事件的修饰符字段抹掉，app 那边一个修饰键都看不到。
   只在非空时才设。
3. **合成 Command 要用真的 Command 键事件。** 在 'a' 那个事件上设
   `flags = .maskCommand` 会把系统里的 Command 弄成粘滞——实测下一个测试段落才
   收到 0xffeb 按下，而用户根本没按过 Command。按键码 0x37 真的按下去再起来。

## M31. 关机状态下「用系统窗口打开」是个死胡同对话框（已修）

**2026-11-10，用户截图报的：选中一台关着机的虚拟机，点「用系统窗口打开」，
弹出一个只有「好」的对话框，写着「VM 没在运行，先 start」。**

一、按钮在关机状态和热运行状态下长得一模一样、也都可点，而它跑的那条命令
（`vmkit window`）第一件事就是查进程，不在就 die。于是用户每一次点击都必然
得到同一个对话框，除了点「好」什么都不能做——而他想要的是看画面，这个意图
根本没被回应。同一个死胡同在 `open`（显示窗口）和 `screenshot` 里也有。

二、根因不在后端在界面：**摆了一个必然失败的按钮**。修法是让关机状态下这个
按钮变成「启动并用系统窗口打开」，走 `start --window`（就是把 headless 置成
false 再启动，一次 QEMU 就带着原生窗口起来）。`start_linux` 看到 pid 活着会
回一句「VM 已在运行」然后返回，所以 VMInfo 快照过期（见 M27）时也不会起第二个
QEMU，刷新之后按钮自己变回「用系统窗口打开」。

三、后端那三句话也一起改了。原来写「VM 没在运行，先 start」——那是命令行口气，
而 app 把后端的 stderr 原样弹成对话框，用户看到的就是一句英文味儿的指令。
现在写成能照着做的一条路（带 `_self_cmd()` 拼出的真实命令，以及 app 里对应的
按钮名）。

四、顺带修掉一个同类的误导：`screenshot` 原来只看 `monitor.sock` 文件在不在，
而 QEMU 退出时不清理它（实测 linux1 关着机，monitor.sock 和 qemu.pid 都还留着），
于是那条检查永远通过，一路走到 screendump 才失败，报的是「没抓到画面，VM 可能
刚退出或没配 framebuffer 设备」——把人往 framebuffer 配置上引，而真相是它根本没
起动过。改成先查进程（`qemu_pid`，它同时回答「在不在」「是不是僵尸」「是不是
QEMU」），再查套接字。
