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

## M8. Debian 13 云镜像的 apt 在 QEMU 里下载完就挂住，cloud-init 永远不完成

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

**结论.** 挂在 apt 下载完成之后的 sqv 验证上，是 Debian trixie 的 apt 2.9
在这个 VM 环境里的内部问题，与本项目的 QEMU 参数、网络、cloud-init 配置都无关。
没有找到不需要关闭签名验证的规避手段，所以不默认改 seed——为装一个包
全局禁掉 `APT::Get::AllowUnauthenticated` 不划算。

**影响与现状.** 只影响 Debian 系：`qemu-guest-agent` 装不上，于是
`vmkit.py guest` 子命令和 stop 的「agent 优先」关机在 Debian 上不可用，
stop 会走 SSH 那条路（照样能优雅关机）。Ubuntu 24.04 与 22.04 实测不受影响，
cloud-init 十几秒内就把包装好了。VM 本身完全可用，登得进去、有网、能 SSH。

**怎么让这件事当场可见.** `start_linux()` 在 SSH 就绪后多探一次 guest agent
（`_warn_if_no_guest_agent`）：不通就明说「cloud-init 可能还在装包或装失败了」
并给出客人机里的排查命令，而不是让「已就绪」把两种情况混成一句。装好包之后
`guest ping` 会自己通，不需要重装 VM。

**待验证.** apt 挂起是否只出现在 trixie 的 apt 2.9。bookworm（debian-12）
用的是 apt 2.6，没有 sqv，理论上不受影响——但 debian-12 本轮没有完整装过，
这条是推断不是实测。

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
