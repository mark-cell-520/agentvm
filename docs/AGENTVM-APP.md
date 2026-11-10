# AgentVM.app — 一键装虚拟机

一个 macOS app，选系统、点安装，剩下的它自己跑完。不用终端，不用记命令。

```
./install.sh          # 一键安装到 /Applications 并打开
```

## 它能装什么

| 系统 | 怎么装 | 体积 | 用时 |
|---|---|---|---|
| Windows 11 24H2 Enterprise Evaluation ARM64 | 真跑一遍 Windows 安装程序，全自动无人值守 | 4.3 GB | 10–15 分钟 |
| Ubuntu Server 24.04 LTS ARM64 | 云镜像，下载完就能登 | 617 MB | 1–3 分钟 |
| Ubuntu Server 22.04 LTS ARM64 | 云镜像，下载完就能登 | 701 MB | 1–3 分钟 |
| Debian 13 (Trixie) ARM64 | 云镜像，下载完就能登 | 412 MB | 1–3 分钟 |
| Debian 12 (Bookworm) ARM64 | 云镜像，下载完就能登 | 438 MB | 1–3 分钟 |

Windows 和 Linux 走的是两条完全不同的装法，不是同一个流程的两个参数。

还有第三条：**用自己的安装包**。向导第一步下面有「用自己的安装包装」，选一个本地
ISO，或者贴一个下载网址。安装过程在 app 内嵌的控制台里自己点完。实测装过 fnOS
（飞牛）ARM64：

```
~/Downloads/fnos_Mainland-PE_arm_1.2.0701_armsr_2881.iso
```

Debian 13 (Trixie) 底、图形安装器、UEFI arm64，3.0 GB，装完 Web UI 在客人机
`5666` 口上。这条路径和另两条的区别见下面「本地安装包」一节。

三类安装全程都在 app 里看得见也点得动，见下面「控制台」一节。

**Windows** 拿到的是原始 ISO，必须真装：把 virtio 驱动注入 `boot.wim` 和
`install.wim`（不注入 Setup 看不见虚拟盘，直接卡死）、写 `autounattend.xml`
（跳过 OOBE 那几十次点击）、暂存 OEM 驱动和首次登录脚本、重建成 UEFI 可启动
ISO，然后跑 Setup。这段逻辑在 `scripts/winarm-vm.py` 里已经实测跑通过一整轮，
`vmkit.py` 直接委托它，没有重写——重写就等于把踩过的坑再踩一次。

**Linux** 拿到的是装好的 qcow2 云镜像，所以只做三件事：qcow2 上叠一层可增长
overlay（多个 VM 共用一份底册镜像，各自写入时才真正占空间）、用 cloud-init 的
NoCloud 配置盘把账号和 SSH 公钥喂进去、启动。没有安装程序要交互，所以一分钟
就能登进去。

## app 里面有什么

```
AgentVM.app/
  Contents/
    MacOS/AgentVM              SwiftUI 写的界面
    Resources/backend/
      vmkit.py                 统一后端，app 和命令行共用同一份
      winarm-vm.py             Windows 介质改造（实测跑通的那份）
      catalog.json             可选系统、下载地址、校验和
```

界面只负责显示和收集参数，所有脏活都在 `vmkit.py`。所以同一套能力命令行也能用：

```sh
scripts/vmkit.py catalog                          # 列出可选系统
scripts/vmkit.py check                            # 检查宿主机能不能跑
scripts/vmkit.py install --distro debian-13 --name d1
scripts/vmkit.py install --source ~/Downloads/xxx.iso --name fnos   # 本地包
scripts/vmkit.py install --source-url https://.../x.iso --name fnos # 网址下载
scripts/vmkit.py list
scripts/vmkit.py status --name d1
scripts/vmkit.py start --name d1                  # 启动（沿用存下来的窗口模式）
scripts/vmkit.py start --name d1 --window         # 强制开原生窗口（会重启）
scripts/vmkit.py window --name d1                 # 已在跑：切到窗口并带到前台
scripts/vmkit.py stop --name d1
scripts/vmkit.py delete --name d1
scripts/vmkit.py configure --name d1 --cpu 4 --mem 8G   # 改配置
scripts/vmkit.py configure --name d1 --disk 64G         # 磁盘只能往大改
scripts/vmkit.py configure --name d1 --window           # 改成开原生窗口
scripts/vmkit.py configure --name d1 --headless         # 改成无窗口（画面走 VNC）
scripts/vmkit.py configure --name d1 --iso              # 重启时挂上安装光盘
scripts/vmkit.py configure --name d1 --ssh-port 20022   # 改端口（当场校验冲突）
scripts/vmkit.py restart    --name d1                   # 改完一键重启生效
scripts/vmkit.py screenshot --name d1             # 抓一帧 png（命令行排障用）
```

`start` 不带 flag 时沿用 `vm.json` 里存的 `headless`，不再是「一定开窗口」。
本地安装包装的时候就是无窗口的（画面走 VNC，在 app 控制台里操作），装完之后点一次
「启动」按老规则会突然弹一个原生窗口出来，而 app 里的控制台还在——同一个画面的两个
入口，很莫名。要窗口就显式 `--window`。

输出协议是 stdout 每行一个 JSON 对象，界面直接解析：

```
{"t":"stage","id":"download","label":"下载镜像"}
{"t":"progress","id":"download","frac":0.42,"bytes":1,"total":2,"speed":3}
{"t":"log","level":"info","msg":"..."}
{"t":"done","vm":{...}}
```

不是 JSON 的行按普通日志显示。这是有意的：被委托的 `winarm-vm.py` 里有几处
直接 `print`，客户端宽容一点比去改那条已经跑通的路径安全。

## 为什么需要 QEMU

macOS 不自带 QEMU，而 **Windows 在 Apple Silicon 上不能用
Virtualization.framework 跑**：VZ 没有 vTPM、virtio 设备集是固定的而 Windows
没有对应驱动、EFI 启动链有缺口。所以 tart / lume / apple/container 全都跑不了
Windows，UTM 对 Windows 也是退回 QEMU 后端。

`install.sh` 会检查 QEMU，没有就装（`scripts/install-qemu.sh`）。它按顺序试
四条路：已经装了、PATH 里有 brew、已知位置有 brew（`~/hb`、`/opt/homebrew`）、
`/opt/homebrew` 可写。四条都不通（典型的是一台既没 Homebrew 又不能 sudo 的
Mac）会明确告诉你卡在哪，不假装成功。

Linux 这一侧同样走 QEMU。这样两套系统共用一个引擎、一套磁盘/网络/显示逻辑，
比「Linux 用 VZ、Windows 用 QEMU」少维护一条路径。

## 装完怎么用

日常操作走 app 里的「控制台」：一个内嵌的 VNC 画面，看得见也点得动，装系统、进桌面
都在里面，不依赖原生窗口是否存在。

想要一个原生 macOS 窗口就点「用系统窗口打开」（无窗口跑的会重启一次客人机，按钮上
写着）。窗口标题是 `QEMU <名字> - (Press ⌃⌥G to release Mouse)`。

- 原生窗口里鼠标点进去会被虚拟机捕获，按 `Control+Option+G` 释放；控制台里点进去
  就能操作，按 `Esc` 释放，也可以锁定成相对移动（拖得出画面）。
- 窗口尺寸一开始是锁死的，AppleScript 改 `size` 和点绿色缩放钮都没反应。
  要先在 View 菜单点 `Zoom To Fit`，之后才能自由缩放。全屏是同一个菜单里的
  `Enter Fullscreen`。
- launchd 或 app 拉起的窗口不会自动抢前台，回来看不见就在 Dock 点图标，
  或者用 app 里的「显示窗口」。

### Linux 的控制台是黑的？

云镜像默认只把控制台接到串口，framebuffer 上什么都没画，所以窗口是纯黑——
不是没启动，是没人往 `/dev/fb0` 写字。`vmkit.py` 在 cloud-init 配置里做了两
件事把这个问题解决掉：开 `getty@tty0`（有个登录终端在 framebuffer 上），
以及往每个 VT 发 `setterm -blank 0` 关掉控制台空闲黑屏。

顺带一个坑：cloud-init 的 `runcmd` 是按实例跑的，同一个 `instance-id` 重启
不会重放。所以改 user-data 之后要删掉 VM 重装，改了才生效。

## 本地安装包：install --source

catalog 里那五个是「我们替你选好、验过校验和的」。自己挑的包不是——没有校验和、
没有推荐配置，而且**安装过程必须你自己点**。所以向导里它单独放在列表外面，不混在
网格里，免得看着一样可靠。

```sh
scripts/vmkit.py install --source ~/Downloads/xxx.iso --name fnos \
    --cpu 2 --mem 2G --disk 32G
```

也可以只给一个网址，让后端去下：

```sh
scripts/vmkit.py install --source-url https://example.com/system-arm64.iso --name fnos
```

下载落在 `<vm>/iso/`，不是你的下载文件夹——那是你的目录，不该由我们往里写。
断点续传和进度条跟 catalog 那条共用同一套。**没有校验和可对**：你贴的地址我们手里
没有可信的哈希，编一个出来比不验更糟，所以只校验大小，并且明确说一声未校验。

和另两条路的实现区别：

- **不注入 virtio 驱动、不写应答文件。** Debian 系安装器内核自带 virtio 模块，
  实测直接认出盘——fnOS 安装器的磁盘列表里出现 `[vda] HDD 32.00 GB`。
- **不可能全自动，所以不假装。** 原来那套「等 SSH 就绪」的循环在这条路上只会空等
  四十分钟然后报超时，而用户早就装完了。现在 `install` 只负责建盘、建 EFI 变量、
  把安装器跑起来，然后把步骤打出来告诉用户接下来点什么。
- **`start` 时不挂光盘，这是关键。** 见下。

装法本身：盘是新建的 raw 空盘（`truncate`，稀疏，写到了才占空间），virtio-blk，
GPT 由安装器自己分。端口分配和另两条一样，另加一个 `web_port`（23000 起）转客人机
的 5666——fnOS 这类 NAS 的主要界面是 Web UI，实测 `curl http://127.0.0.1:23000/`
得到 HTTP 200 和 `<title>飞牛 fnOS</title>`。80/443 不转，宿主机上大概率已经占了。

### 为什么 start 不能挂着光盘

实测：盘上明明写好了 GPT（BOOT 的 ESP + SYSTEM 的根分区），但只要光盘还挂着，
重启就有可能退回安装器而不是进装好的系统——固件在有多个可启动项时不按 bootindex
选，而是沿用 NVRAM 里上次那一条。用户看到的会是「装完了又回到安装界面」，然后以为
没装成，重点安装一遍。

所以 `local_qemu_args(vm, with_iso)`：`install` 传 `True` 挂光盘进安装器，
`start` 传 `False` 只从盘启。安装中途的重启没关系——那时盘还是空的，固件自然落到
光盘。

顺带一个反直觉的观察：装完之后**不关机直接重启**，这次是能进 fnOS 的（光盘还挂着）。
所以这个坑不是每次必现，而是时灵时不灵——这种 bug 最难查，别因为「我这次没事」就把
`with_iso` 去掉。

## 控制台：装系统全程看得见也点得动

三类安装全都在 app 里内嵌的一个 VNC 画面里完成。这不是「隔几秒抓一张图」，
而是一个真 RFB 3.8 客户端（`app/Sources/VNCClient.swift`）——键盘鼠标真的送进
客人机，拖动滚动条、在输入框里打字都行，而抓屏做不到这两件，装系统必然要用到。

装到哪一步都能看到：Windows 那十几分钟 Setup 的进度、fnOS 安装器的选盘和格式化
确认、Linux 云镜像的启动和登录，全在这个画面里。安装进度页直接内嵌它，装完之后在
详情页点「控制台」还能继续操作。

实测用这个控制台把 fnOS ARM64 从 ISO 装到了能用的系统：点「下一步」、确认格式化、
等它复制完文件、重启，最后 fnOS 起来，宿主机上
`curl http://127.0.0.1:23000/` 返回 HTTP 200 和 `<title>飞牛 fnOS</title>`。

### 为什么安装期间不给原生窗口

这个版本的 QEMU 没有 `-no-quit`，点窗口红按钮或按 ⌘W 会直接把 QEMU 退掉
（`mouseUp → terminate: → NSTerminateNow`），十几分钟的安装跟着没，而 `qemu.log`
是空的、退出码还是 0。所以安装期间 `-display none`，画面只从 VNC 出——这也正是
控制台能看到画面的原因。想要一个原生 macOS 窗口：

```sh
scripts/vmkit.py window --name fnos        # 或 start --name fnos --window
```

**这会重启一次客人机**，因为显示模式（`-display`）是 QEMU 的启动参数，跑起来之后
改不了。`window` 命令会提前把这件事说出来，不静默重启。

### 画面不刷新？先看看光标动没动

`-vga none -device ramfb` 下，QEMU 只在客人机真的重绘 framebuffer 时才发增量更新。
**光标不算 framebuffer 内容**——它是显示层画上去的，动它不会触发更新。所以「光标
在动但画面不动」是正常的，要等安装器自己重绘（进度条、页面切换）才有新帧。

反过来也踩过：一开始以为画面是冻结的，反复查协议查请求管道，最后发现是当时屏幕上
真的什么都没变。判断方法是用 QEMU 监控套接字 `screendump` 取一张和 VNC 画面比对，
或者敲一个肯定改变界面的键。

### 点不动的话，先让指针停一会儿

实测：同样三个事件（移动、按下、松开），中间隔 0.3 秒点不动安装器的「下一步」，
隔 0.8 秒就点了。图形安装器那类界面要看到指针先移到按钮上才认这一下点击。所以
控制台的 `mouseDown` 会先送一次移动、隔 120 毫秒再送按下——真实用户本来就是先移动
再点，不赌客人机的脾气。

### 一个服务端行为：请求发出去不一定有回应

RFB 里服务端只在收到 `FramebufferUpdateRequest` 时才发画面，而「发过一次请求」并不
等于「之后会持续推」。实测 QEMU 会在某些情况下不再回应增量请求：字节一个不错、
framebuffer 也确实在变，但客户端一个字节都收不到。所以客户端有个保活定时器，250 毫秒
没有新帧就补一个请求。代价是空闲时每 250 毫秒一个 10 字节的小包，可以忽略。

协议实现上另外三个坑（都花了时间才定位）：

1. **握手里漏 `ClientInit`**：安全结果之后必须发那 1 字节，否则服务端一直等，
   `ServerInit` 永远不来。症状是「连上了但没有画面」，而连接状态明明是好的。
2. **`SetPixelFormat` 少一个补齐字节**：消息头是 类型1 + 补齐3 + 格式16 = 20 字节，
   少发一个 QEMU 直接判协议错误断开。症状是「ServerInit 都收到了，紧接着连接被关」。
3. **「这一帧收完没有」必须在数完矩形时判断**，不能等解析循环转回来：最后一个矩形
   往往正好把缓冲区吃到空，那时循环直接退出，下一个更新请求就不发了，画面死死停在
   第一帧——而键盘鼠标客人机都收到了，就是看不到变化。

## 安装跑在后台，不跟着界面走

装一个 Windows 要十几分钟。第一版把安装当作 app 的子进程，结果踩了一次：
4.3 GB 镜像下完、SHA-256 校验通过、正在改造介质的时候，用户关掉了 app 窗口，
app 退出，子进程跟着没，十几分钟的下载和安装全部作废，日志里一个字都没留。

现在的做法是**安装跑在脱离 app 的后台进程里**，app 只负责起它和尾它的日志：

```
vmkit.py install --detach ...
        │
        ├─ 父进程：起一个 setsid 的兄弟进程，把日志路径和 pid 交出来，立刻返回
        │
        └─ 后台进程：真正干活，stdout 重定向到 <vm>/install.log
                     同时把 pid 写进 <vm>/install.pid
```

app 重新打开时会扫 `~/.agentvm/vms/*/`，看到 `ready: false` 且有活着的
`install.pid` 就自动接上，把已有日志整段放出来，然后继续尾。界面上表现为
顶部一条进度横幅，点「查看详情」能看完整日志。

三个实现上的坑：

1. **`Emit.done()` 是把内容包在 `vm` 键里的**。`Emit.done({"log": ...})`
   输出的是 `{"t":"done","vm":{"log":...}}`，从顶层取 `ev["log"]` 什么都拿
   不到，而失败是静默的——界面只是显示「安装没起来」。
2. **不能用 `VMInfo` 解 `vm.json`**。`VMInfo` 对应的是 `status` 命令的输出，
   带 `ssh_command`、`disk_used_bytes` 这些 `vm.json` 里没有的非可选字段。
   只读一个字段的时候直接解字典，别走 Codable。
3. **判断安装结束要看进程消不消失，不要看日志里有没有某个标记**。进程没了
   一定结束了，而标记可能因为崩溃根本没写出来。

## PATH 干净时会谎报「缺少依赖」

QEMU、wimlib、cdrtools 都装在 `~/hb/bin`，而那个目录**只在交互 shell 的 PATH
里**。从 launchd、cron、或者一个没继承 PATH 的进程里起安装，`shutil.which`
什么也找不到。

原来这里有三份互相不一致的实现，凑出一个很难看的故障：

- `find_tool()` 有兜底（PATH → `~/hb/bin` → `/opt/homebrew/bin` → `/usr/local/bin`）
- `which()` 就是光秃秃的 `shutil.which`，没有兜底
- `require_tools()` 依赖检查用的正是没有兜底的 `which()`
- 而 `inject_drivers()` 里有 5 处 `run(["wimlib-imagex", ...])` 裸名字调用

于是从命令行后台起一次安装，先被 `require_tools` 一次报缺 qemu/wimlib/mkisofs
三样（其实全都在 `~/hb/bin` 下）；把 `which` 修好之后，又会在 4 GB 镜像下完、
校验通过、改造介质改到一半时才抛
`FileNotFoundError: 'wimlib-imagex'`——4 GB 白下一遍。

现在兜底逻辑只在 `which()` 里一份，`find_tool()` 复用它，5 处裸调用改成走
`wimlib()`。兜底目录也补上了 `/usr/bin` 和 `/bin`，因为 `hdiutil`、`osascript`
这些 macOS 自带的在那儿，PATH 被削干净时同样找不到。实测 `PATH=/bin` 下六个
工具全能解析。

## 稀疏盘的「已用空间」不是 st_size

`status` 报的 `disk_used_bytes` 原来取 `st_size`。64G 的稀疏盘 `st_size` 永远
是 68.7e9，实际可能只写了 6 GB——界面会显示「磁盘已用 64 GB」，用户据此判断
磁盘快满了纯粹是误导。正确的是 `st_blocks * 512`，即真正分配出去的块。
`winarm-vm.py` 里本来就有个注释把这坑写清楚了，`vmkit.py` 这边没照做。

## 端口分配

每个 VM 拿三个端口，从固定基数往上找第一个没被占用的：

```
SSH        20000 起
远程桌面    21000 起（只有 Windows 有）
VNC        22000 起
Web UI     23000 起（只有本地包装的 NAS 类系统有，转客人机 5666）
```

**不要用 `nc -z` 判断虚拟机起来没有。** QEMU 的 `hostfwd` 在 VM 启动那一刻就把
宿主机侧端口绑好了，探端口必然通，跟客人机起没起来无关。`vmkit.py` 的
`ssh_ready()` 是真发一条 SSH 命令拿结果。

### VNC 有两个编号空间，混着比就会全都一样

QEMU 的 `-vnc` 收的是**显示编号**，实际监听 `5900 + 编号`。`vm.json` 里存编号
（16100 起），界面和 app 用的是端口（22000 起）。

原来的写法是 `alloc_port(22000, taken) - 5900`，而 `taken` 里收的是别的 VM 存下来的
显示编号（16100）。拿 16100 去和 22000 比，永远比不中——**结果是每个虚拟机拿到的都是
16100**，第二个虚拟机一起动 VNC 就绑不上，控制台全是黑的。实测踩过：fnos 和 win11
的 `vm.json` 里 `vnc_display` 都是 16100，两个都没法同时用控制台。

所以 `alloc_vnc_display()` 把两边分开比：显示编号和显示编号比，端口和端口比。
**已经建好的 VM 不会自动修**，`vnc_display` 撞了的要手改 `vm.json`。

## 两个「进程还在吗」的判断错误

1. **僵尸进程也算在跑。** `qemu_pid()` 原来用 `os.kill(pid, 0)`，而对僵尸发 0 号
   信号一样成功。QEMU 是 `start_new_session` 出去的孤儿进程，回收要靠 launchd，强杀
   之后有几十毫秒到几秒它还是僵尸。实测踩过：`window` 命令强杀之后 `start_local`
   看到 pid 还「活着」，直接返回「VM 已在运行」——结果既没有窗口也没重启，用户看到的是
   虚拟机凭空没了，而日志说它还在跑。现在用 `ps` 一次同时回答「在不在」「是不是
   僵尸」「是不是 QEMU」（连进程名核对，pid 会被复用）。
2. **`install` 的 done 事件不能直接发 `vm.json`。** 里面是 `vnc_display`、没有
   `ssh_command` 和 `disk_used_bytes`，app 那边解不出 `VMInfo`——而 Swift 的解码失败
   是静默的，界面表现为装完了却一直停在「正在安装」页，单独在终端里跑同样的命令却是
   好的。所以 `vm_record()` 一份形状，`install` 和 `status` 共用。

## 可视化配置

装的时候和装完之后都能配，两处是同一套预设和同一条资源条。

装的时候在向导第二步：三档预设（轻量 / 均衡 / 高性能）只是起点，点完照样能改；
下面每一样都能调，并且有一条资源条实时显示「这个虚拟机要占掉机器多少」。

装完之后在详情页点「配置」：改 CPU、内存、磁盘、显示模式、挂不挂安装光盘、四个端口、
登录名、密码。原来根本没有这个面板——想加内存只能删了重装，而删了重装意味着系统里的
东西全没。

```sh
scripts/vmkit.py configure --name d1 --cpu 4 --mem 8G
scripts/vmkit.py restart    --name d1        # 改完一键重启，让改动生效
```

改完有两档：「保存」（下次重启生效）和「保存并重启」（立刻生效）。后者就是
「不用重新安装系统，重启一下就切换」——正在运行的点它等于关机再开机，关机状态点它
等于保存后直接启动。

五条规则，界面和报错里都写明了，不静默做：

1. **CPU、内存、显示模式、端口、挂不挂光盘，改完都要重启才生效。** 它们全是 QEMU 的
   启动参数，跑起来之后改不了。后端不偷偷重启——虚拟机可能正装着系统、跑着东西，
   重启该由用户决定，要么他自己点，要么显式走 `restart`。
2. **磁盘只能往大改。** 改小不会让文件系统跟着缩，而 `truncate` / `qemu-img resize`
   都会把超出去的部分截掉，系统里的数据直接对不上。往大改也要系统里再扩一次分区和
   文件系统（Linux 用 `growpart` 加 `resize2fs`，Windows 用 `diskpart extend`）。
   实测 Debian 云镜像的 cloud-init 会在下次开机自己扩，别的系统不一定。
3. **改磁盘大小时虚拟机必须关机。** qcow2 镜像被 QEMU 占着写锁，`resize` 会失败。
4. **端口改完就比一遍冲突。** 和别的虚拟机撞、和宿主机上别的程序撞，都在保存这一下
   就报错。不检查的后果是下次启动 QEMU 绑不上直接退出，而 `qemu.log` 里那句话很容易
   被忽略。VNC 端口的下限是 5900——QEMU 按 `5900 + 显示编号` 监听，小于它编号成负数。
5. **「挂载安装光盘」默认关，而且装完之后就该保持关。** 挂了光盘重启会进安装器而不是
   进装好的系统。只有想重装或重新配置系统时才开，界面上会橙色警告。这个坑时灵时不灵
   （见下面「为什么 start 不能挂着光盘」），别因为「这次没事」就一直开着。

资源条存在的理由：QEMU 的客人机线程是真实线程，内存也是真金白银。把 12 核全部分给
一个虚拟机，macOS 自己的界面线程会被挤下去，表象是键盘鼠标失灵，而用户完全看不出
这两件事有关系。所以分配的时候就把占比画出来，超过 45% 变橙、超过 70% 变红。
内存上限也按宿主机实际大小算，并且固定留 2 GB 给 macOS。

## 存储：附加盘、快照、共享目录、克隆

详情页点「存储」。这四样都是「装完之后还想动这个虚拟机」才会用到的，原来一件都没有
——想加一块盘存数据只能删了重装，想退回干净状态也只能删了重装，而删了重装系统里的
东西全没。

```sh
scripts/vmkit.py disk add --name d1 --size 64G            # 加一块盘
scripts/vmkit.py disk remove --name d1 --disk data1       # 摘掉（连文件删）
scripts/vmkit.py snapshot create --name d1 --tag clean    # 拍快照
scripts/vmkit.py snapshot apply   --name d1 --tag clean   # 回滚
scripts/vmkit.py share add --name d1 --tag host --path ~/Shared   # 共享目录
scripts/vmkit.py clone --name d1 --new-name d2            # 克隆一份
```

### 附加盘

系统盘是安装时建的那一个，这儿加的是另外的。新盘是空的，系统里还要分区格式化
（Linux `mkfs.ext4` 再 mount，Windows 用磁盘管理初始化）。raw 格式省事，qcow2 格式
能拍快照。文件是稀疏的，写到了才真占空间。

**加盘必须给 bootindex，而且系统盘也要有。** 这是实测踩出来的：Linux 那条路的系统盘
原来用 `-drive if=virtio` 自动创建设备，没有启动序号，固件只能自己枚举——加一块空盘
就被插到前面，启动时先去试那块没有引导记录的盘，试完往下走直接落到 HTTP 启动。表象是
「加了一块盘，系统起不来了」，而盘本身一点问题都没有。所以现在系统盘显式 `bootindex=1`，
附加盘从 10 起排后面。

### 快照

qcow2 内部快照，秒级，不额外占空间（增量）。「随删随建的试验场」真正需要的东西：
装了一堆东西之后想退回干净状态，不用重装。

两个限制都不掩饰：

1. **只有 qcow2 盘能拍。** 自己挑安装包那条路用的是 raw 盘（安装器直接写块设备，
   qcow2 它不认），拍不了；Linux 云镜像那条可以，附加盘里加了 qcow2 格式的也可以。
2. **拍和回滚时虚拟机必须关机。** 运行中拍出来的快照，磁盘和内存状态不一致，回滚等于
   强制断电加丢写入。

快照列表用 `qemu-img info --output=json` 拿，不要解析 `snapshot -l` 那张表：那张表
按空格切会错位（`VM_SIZE` 是「0 B」两个词、`DATE` 也是两个词），按列头位置切片也会错
（表头的列位置和数据的列位置对不齐，实测这个版本就不齐）。

### 共享目录（virtio-9p）

把宿主机上的一个文件夹挂进虚拟机，传文件不用再走 SSH scp。用 9p 而不是 samba：
QEMU 内置，宿主机不用跑 smbd，客人机也不用装驱动（Linux 内核自带，Windows 要
virtio-win 里那个）。

客人机里要自己挂一次，这一步替不了——挂载点是客人机里的事，每个系统做法不一样：

```
Linux   mkdir -p /mnt/host && mount -t 9p -o trans=virtio,version=9p2000.L host /mnt/host
```

### 克隆

照着一个装好的虚拟机复制一份。想试什么就克隆一份去折腾，折腾坏了删掉，原来那份还在。

Linux 那条路是 qcow2 overlay，克隆只要再叠一层，底册共用，所以是秒级的、不额外占空间。
自己挑安装包那条是 raw 盘，用 APFS clonefile 复制（瞬时且不占空间），失败了才退回普通
拷贝。端口会重新分，不会和源撞——`ssh_command` 也按新端口重算，否则用户复制那条命令
去连，连上的是源而不是克隆。

## 卸载

```sh
rm -rf /Applications/AgentVM.app        # 删 app
rm -rf ~/.agentvm                       # 连虚拟机数据一起删
```

没有往别处写东西，不碰 sudo，不改 shell 配置。

app 里的「删除」走 `vmkit.py delete`，它删整个 VM 目录。这里有个坑：
`winarm-vm.py` 准备介质时会把驱动暂存目录建成 555（只读），
`shutil.rmtree(..., ignore_errors=True)` 删不掉里面的条目就整棵留下，而
`ignore_errors` 把失败吞掉——结果是 delete 报告「已删除」，磁盘上却还留着几百
MB。所以 `_rmtree()` 现在先把目录放权再删，删不掉就明说。另外如果上次删除半途
失败（目录还在但 `vm.json` 已经没了），`load_vm` 会报「没有这个虚拟机」，
用户从此清不掉这个残留，所以 `delete` 改成先看目录在不在。

## 全局审计（2026-11-10）

三个子代理分别审 Python 后端、Swift app、shell 脚本，每条结论都复核过才改。
下面是真正值得记的那部分——都是「两边都说成功了对不上」或者「用户第一步就卡死」的
类型，不是风格问题。

### shell：README 主推的那条链一步都跑不通

`source common.sh` 的路径写错七处：`HERE` 已经等于 `scripts/`，却写
`"$HERE/../lib/common.sh"`，解析到仓库根的 `lib/common.sh`，而文件在
`scripts/lib/common.sh`。于是 `make check`、`install-tart`、`fetch`、`serve`、
`create-vm`、`provision`、`configure`、`autostart` 八个目标全部死在第一句。
加重情节是 `make lint` 用 `bash -n` 只查语法，查不出 source 失败，会打印一片
`ok` 给出虚假绿灯。

同时 Makefile 里 `VM_NAME`、`LLM_KEY_FILE` 等变量一个都没定义，展开出来是
`tart stop `（VM 名是空的）和 `--llm-key-file  --llm-base-url`（空串让参数错位，
`--llm-base-url` 把下一个参数当成自己的值，报出的 `unknown argument: https://...`
和真实原因毫无关系）。

### 客人机里 dash 吃不了 pipefail

`make provision` 是 `tart exec VM sh -s < 40-provision-guest.sh`，而脚本第 18 行是
`set -euo pipefail`。Debian 的 `/bin/sh` 是 dash，dash 不支持 pipefail，整段脚本
第一行就退——换 apt 源和装编译器一样都没做，要到后面某个包编译失败才间接暴露。
改成 `bash -s`。顺带：宿主机环境变量不会透传进 `tart exec`，原来读
`${AGENTVM_APT_MIRROR:-...}` 永远拿到默认值，现在显式拼一行 export 送进去。

### Python：一个写了一半的 vm.json 能拖垮所有命令

`vm_record` 原来直接取 `vm["cpus"]`、`vm["ssh_port"]` 这些字段。vm.json 是会被
写一半的（安装中断、用户手改、旧版本留下的），一个缺 `ssh_port` 的文件能让
`status`（不带 `--name` 时遍历全部 VM）整条命令 KeyError 崩掉——用户看不到任何
虚拟机的状态。现在一律 `.get()` 加默认值，缺键的 VM 照样显示。

同源的还有 `cmd_list`：它把 `all_vms()` 的原文直接发给 app，而 vm.json 里有
`password` 和 `ssh_key` 的明文。`vm_record` 特意不含这两项，`list` 这条路原来
直接漏出去——app 内存和日志里都躺着一份明文口令。现在 `list` 也走 `vm_record`。

### parse_size 同一个输入三种命运

`'64MiB'` 变成 `'64MIB'`，`float()` 直接 ValueError 裸栈。而 `'64GB'` 在这里返回
68719476736，catalog 安装路把这个串原样交给 qemu-img，qemu-img 11.1.3 报
`Invalid image size specified: '64GB'`。同一个写法三条路两种结果：
`install --source x.iso --disk 64GB`（走 truncate）成功、
`install --distro ubuntu --disk 64GB`（走 qemu-img）失败、
`configure --disk 64GB`（走字节数）又成功。现在用正则白名单，非法输入给一句人话，
vmkit 内部一律以字节数下发。

### 克隆出来的 Linux 虚拟机永远起不来

`cmd_clone` 只建 overlay 盘和 efi-vars.fd，没复制 `seed.iso`。而 `start_linux()`
同时要求 `disk.qcow2` 和 `seed.iso` 都在，于是克隆「成功」之后一启动就
`die("VM 不完整，缺 seed.iso。重新 install。")`——用户拿到一个起不来的 VM，
提示还让他重装，等于这个克隆白做。Linux 是主力机型，这个坑必踩。

### 改密码不生效

`configure --password` 只改 vm.json 就完事，输出写着「已改 密码 / 下次启动生效」。
但 Linux 那条路的密码是 cloud-init 从 `seed.iso` 里的 NoCloud user-data 读的，
而 `linux_seed()` 只在 install 时调用过一次。重启后读到的还是旧 user-data，
用户拿新密码登不进去，而界面说已改。现在改密码会重新生成种子盘。

### Windows 关机必然演变成 90 秒后 kill -9

`cmd_stop` 的 Windows 分支只往环境里设了 `VM_DIR`，winarm 的 `Cfg()` 于是回落到
默认的 `~/.winarm/ci_key`——而这个 VM 安装时用的是 `~/.agentvm/id_ed25519`，
两把不是同一把钥匙。`ssh_ready()` 永远认证失败，跳过优雅关机，ACPI 等 90 秒
（登录界面没人理电源键事件），最后 `kill -9` 强杀 raw NTFS 盘。每次 stop 都固定
复现：先卡 90 秒，再丢客人机未落盘数据。抽一个 `winarm_env()` 四处共用。

### cloud-init 的 YAML 注入

`linux_seed` 用 f-string 把用户名和密码原样插进 user-data。密码 `p@ss #word` 生成
`passwd: p@ss #word`，YAML 把 `#word` 当注释，**实际密码变成 `p@ss`**——用户拿原
密码登不进去，而两边都说成功了。以 `{`、`[`、`*` 开头的密码会让 cloud-init 整体
解析失败，连账号都建不出来。现在用 `json.dumps` 产出合法 YAML 双引号标量，
用户名另做一道 `[a-z_][a-z0-9_-]*` 白名单。

autounattend.xml 是同一个病的另一个实例：密码 `p@ss&word` 原样插进
`<Value>p@ss&word</Value>` 不是合法 XML，而 Windows Setup 对不合法的应答文件的
反应是**静默忽略**——退回全手动界面，安装挂死十几分钟后超时，日志里一个字都不提。
XML 侧 `saxutils.escape`，PowerShell 侧把单引号翻倍。

### Swift：快速点击整个丢掉

`mouseDown` 送一次移动，然后延迟 120ms 发按下，延迟块里
`guard self.buttons & 1 == 1`。而 `mouseUp` 已经把按钮位清成 0 了——按下到松开短于
120ms 时（手点、触控板轻点，几乎人人如此）那个 guard 直接 return，**客人机从头到尾
没收到过 button-1 按下**，只看到两次原地移动。快速点安装器的「下一步」整个没反应，
而慢点就好，极难归因。改成用单独的 `pendingPress` 标志，mouseUp 时若按压还没发出去
就立刻补发 down+up。

### Swift：客人机一改分辨率连接就永久卡死

DesktopSize 伪编码的矩形**不带像素数据**，只有 12 字节头。代码却按 raw 算
`bytes = w*h*4`（1920×1080 就是 8.3 MB 的期望长度），guard 永远不满足，那 12 字节
死死卡在缓冲区头部，之后每次 receive 都先撞上同一段字节，后续所有消息全部解析不动。
表现是画面冻结、尺寸不更新，而 keepAlive 还在每 250ms 补请求，QEMU 持续回数据，
`buf` 无上限增长。装 Windows 时 Setup 改分辨率必触发。改成伪编码只消耗 12 字节。

### Swift：端口哨兵 0 和 vmkit 的 None 语义冲突

`VMSettings.init` 把 `vm.rdpPort`（Linux 虚拟机是 nil）存成 0，configure 里
`0 != nil` 成立，于是每次打开配置面板保存都传一个 `--rdp-port 0`，vmkit 的
`take_port(0)` 直接 die「远程桌面 端口要给 1024 到 65535 之间，你给了 0」——
整条 configure 什么都还没改就退出。改成 `Int?`，只有真的改过才拼参数。

### Swift：整个安装期间的反馈界面是死的

`backend` 是 AppState 里的一个普通 `let`，不是 `@Published`。视图写
`state.backend.progress` 时读得到值，但 SwiftUI 对「嵌套 ObservableObject 的属性
访问」的依赖追踪不可靠——实测症状是阶段标签停在第一句、进度条钉在 0、日志区空白、
红色错误不出现，只有装完那一刻 `installFinished` 变化界面才瞬间跳变。
加一个 `@Published private(set) var revision`，handleEvent 每写一次 backend 就碰它
一下，objectWillChange 一定发得出去。

同一类问题的另一个实例：所有操作类命令的事件处理器都写 `{ _ in }`，把后端发来的
error 事件整个丢掉。后果是界面显示「已保存」而实际什么都没改——运行时把磁盘从 32G
改到 64G，后端 die「虚拟机正在运行，镜像被占着写锁」，app 收不到，绿色「已保存」
亮起，重启后磁盘还是 32G。

### Swift：开机自启永远卸不掉

plist 路径写成 `.applicationSupportDirectory`（= `~/Library/Application Support`）
→ `.deletingLastPathComponent()`（= `~/Library`）→
`.appendingPathComponent("Library/LaunchAgents/…")`，解析出来是
`~/Library/Library/LaunchAgents/…`——多套了一层 Library。而 vmkit 写的是
`~/Library/LaunchAgents/…`。于是 `installed` 恒为 false，每次点「开机自启」都只传
`--name` 不传 `--disable`：自启只会被反复装上，永远卸不掉，按钮文案也永远不反映
当前状态。

### Swift：第二个虚拟机必然卡死在安装页

新建向导不重置 `vmName`，装完 win11 再点 + ，名称框里还是「win11」。点「开始安装」
→ vmkit die「win11 已存在」→ 错误事件进了 handleEvent 但界面不刷新（见上），
`installFinished` 永远不来，向导停在「正在安装」，而「停止」停不掉真正的安装进程
（`process` 指的是那条写完 install.pid 就退出的引导父进程）。现在打开向导就清表单，
`cancel()` 读 `install.pid` 对真正的进程发信号。

### 命令行注入面

`50-configure.sh` 的 here-doc 不加引号，`${LLM_BASE_URL}` 原样进客人机脚本：
`--llm-base-url 'http://x"; touch /tmp/pwn; echo "'` 就能在客人机里执行任意命令。
机密走文件是对的，偏偏这三个「不是秘密」的值是缺口。现在 `printf %q` 转义后再插值。

`backends/hyperv.py` 里约二十处 PowerShell 单引号插值
（`New-VM -Name '{name}'`），一个单引号就能闭合字符串接任意 PowerShell 代码、
以当前用户权限执行：`create-vm --name "x'; Remove-Item -Recurse C:\Users; '"`。
逐个转义二十处必然漏，所以在入口做 `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$` 白名单。

### QEMU 参数里的逗号

`-drive file=…,format=…` 和 `-virtfs local,path=…,mount_tag=…` 都用逗号分隔键值对，
路径里一个逗号就把参数切成两半（`~/My,Dir` 被当成 `path=~/My` 加一个叫 `Dir` 的键），
挂载失败而报错不指回真实原因。加盘和加共享目录时都挡掉。

### 并发安装

`spawn_detached_install` 原来没有任何「已有安装在跑」的检查。连点两次安装就会起
第二个安装进程，两边同时 `qemu-img create` 同一个 `disk.qcow2`、同时 `save_vm` 写
同一个 `vm.json`、同时起两个 QEMU 抢同一组端口——磁盘和配置都损坏，而两份日志
交错在一起无法复盘。`install.pid` 写了但原来只有 app 读，vmkit 自己从不看。

### 僵尸进程

winarm 的 `qemu_running()` 用 `os.kill(pid, 0)` 判活，僵尸照样返回成功。vmkit 的
`qemu_pid()` 早就用 `ps` 修掉了同一件事（注释里写着踩坑经过），而 vmkit 的 Windows
分支正是委托到 winarm——两边不一致，等于修了又漏。Swift 侧 `installAlive(name:)`
是同一个病：会让 app 永远认为上一次安装还在跑，从此拒绝再接任何安装。

### 删除虚拟机不卸开机自启

`cmd_delete` 只 `_rmtree(d)`，plist 还留在 `~/Library/LaunchAgents/`，而它指向的
wrapper 就在被删掉的 VM 目录里。之后每次登录 launchd 执行一个不存在的脚本 →
非零退出 → `KeepAlive` 加 `ThrottleInterval 15` → 每 15 秒重试一次，永不停止。
用户看到的是「删了的虚拟机还在每隔一会儿折腾一下」。

### 修完之后验证过什么

- 三个 shell 脚本目录、七条 Python 文件全部语法通过；`make lint` 全绿（现在它真的
  覆盖 `agentvm-fetch` / `agentvm-serve` / `install.sh` / `app/build.sh`，并且用
  `ast.parse` 而不是 `py_compile`，不在仓库里留 `__pycache__`）。
- Swift 全量编译通过，app 构建、ad-hoc 签名、部署到 `/Applications` 均正常，
  部署的 `vmkit.py` / `winarm-vm.py` / `catalog.json` 与仓库逐字节一致。
- 用残缺 vm.json 实测 `status` 不再崩；`parse_size` 的边界（`64MiB`、`64GB`、空串、
  `abc`）逐個试过；`_yaml_scalar` 对 `p@ss #word`、`{a: b}` 产出合法 YAML；
  `set_env` 对含 `\1`、`\g<0>` 的密钥不再改写内容。
- qcow2 簇数上界用合成案例复现并修好（virtual=3,453,002,496 时需要 52,689 簇，
  旧逻辑给 57,344，现在给 52,689）。
- VM 名白名单实测拒绝 `x'; Remove-Item -Recurse C:\Users; '` 而放行 `agent`、
  `a.b-c_d`。
- `vmkit.py check` / `status` / `list` 实跑正常，`list` 输出里不再有 password 键。

### 没修的

- `scripts/install-qemu.sh` 的路径 2 到 4（真装 Homebrew 或自建一个）仍未实测，
  这台机器上 QEMU 已经在 `~/hb/bin`。已在注释里标明是未验证而非已确认的问题。
- Windows 侧（`backends/hyperv.py`）整体未在真实 Windows on ARM 设备上跑过，
  见 `docs/WINDOWS-PORT.md` 第 10 节。
- QEMU Guest Agent、USB 直通（Quickemu 有）按这个沙箱的定位没做。

---

## macOS 客人机（2026-11-10 新增）

### 为什么 macOS 走 VZ，另外两种走 QEMU

三条路都试过，只有一条通：

1. **QEMU 的 `vmapple` 机型**是专为 macOS 客人机设计的（模拟 Apple 硬件、从
   IPSW 恢复系统）。Homebrew 的 QEMU 11.1.3 **没有注册它**：`strings` 能看到
   `VMAppleVirtioBlkVariant` 之类的符号，而 `-machine vmapple` 报
   `unsupported machine type`。代码在，机型没开。自己打补丁编一份要连带
   Reims vGPU、OpenAVB ROM、idevicerestore 补丁一整套——参考
   `Lakr233/vmapple-cli` 的 `Patches/` 目录，光 QEMU 补丁就 17 个，而且那是
   给 arm64 Linux + KVM 用的，macOS 宿主的 HVF 路径更不完整。
2. **裸 `virt` 机型 + OpenCore**：arm64 的 OpenCore 没有成熟发行物，
   `KVM-Opencore` 那些 release 全是 x86 的。
3. **VZ 官方 API**：`VZMacOSRestoreImage` / `VZMacOSInstaller` /
   `VZMacOSBootLoader` / `VZMacPlatformConfiguration` / `VZMacGuestProvisioningOptions`
   全套公开。宿主机上 `fetchLatestSupported` 直接拿到 macOS 27.2.1 (26B131) 的
   IPSW 地址，`mostFeaturefulSupportedConfiguration` 给出最少 2 核 / 4 GB。

所以 `app/Sources/MacVMEngine.swift` + `scripts/vmz/main.swift` 是一份 Swift
代码、两个入口（app 内嵌与 CLI），和 `vzrun.swift` 那条实验线是同一条思路的成品。

### 架构：VZ 的虚拟机不能跨进程

这是整条设计的地基，也是三条限制的来源：

- `VZVirtualMachineView` 必须和 `VZVirtualMachine` 在同一个进程里，没有公开的
  跨进程共享路径。所以 macOS 客人机的寿命跟持有它的进程绑定：app 里启动的随
  app 退出而停，`vmz start` 起的随那个终端。Windows/Linux 那套「安装甩后台、
  app 可以关」的做法在这儿做不到。
- 没有 VNC server，所以 app 的自研 RFB 客户端对 macOS 客人机用不上，画面是
  `VZVirtualMachineView`（原生 NSView，比 VNC 清楚也快）。
- 宿主机不转发端口：`VZNATNetworkDeviceAttachment` 不对外监听，SSH 在 NAT
  内部的 192.168.64.x。这一点现在有一条公开路径能走一半：vsock 端口转发
  （`MacVsockProxy`）在宿主机上开只绑 127.0.0.1 的 TCP 监听，接到客人机的
  vsock 端口上——宿主机这边是通的，但客人机里得有程序监听那个 vsock 端口，
  而 macOS 客人机默认没有。详见 README「macOS 虚拟机」那一节。

### 安装流程

1. `VZMacOSRestoreImage.fetchLatestSupported` 拿 IPSW 地址（**要 entitlement**）
2. 6 连接并发下载 25.6 GB（单连接 1 MB/s → 聚合 18.8 MB/s，7 小时变 23 分钟）
3. `VZMacOSRestoreImage.load(from:)` 从本地读回，取 `hardwareModel`
4. 建 `VZMacMachineIdentifier`（每个 VM 唯一）和 `VZMacAuxiliaryStorage`
5. truncate 一块 raw 系统盘（VZ 只认 raw，qcow2 会 validate 失败）
6. `VZMacOSInstaller` 恢复安装（约 10 到 20 分钟）
7. 第一次启动带 `VZMacGuestProvisioningOptions`（macOS 27+）：建账号、
   自动登录、打开远程登录——不用进 Setup Assistant 手动点

### 踩过的坑

**1. entitlement 缺失是一句看不懂的话。** 没有
`com.apple.security.virtualization` 时，`fetchLatestSupported` 返回
「The restore image catalog failed to load」——完全看不出是权限问题。实测对比过：
签名前后同一份二进制，一个报错一个正常。所以 `app/Resources/entitlements.plist`
必须同时给 app 主二进制**和嵌套的 `Resources/backend/vmz`**：`codesign --deep`
不会把 entitlement 传给嵌套二进制，实测签完 bundle 之后查那个文件只有一行
`Executable=`，没有 entitlement 字典。而 VZ 在缺 entitlement 时抛的是
`NSInvalidArgumentException`（未捕获的 ObjC 异常，进程直接 abort），
vmkit 只会看到 vmz 非零退出、拿不到任何 JSON。

**2. 「断点续传」整个是坏的（第一版）。** 下载器把 dest 预 truncate 成完整大小，
而 `attributesOfItem[.size]` 返回的是逻辑大小：稀疏文件 truncate 到 25.6 GB 之后
`st_size` 就是 25600000000 而 `st_blocks` 是 0，一个字节都没落盘。于是下载中断后
重跑判定「已下完」，直接拿一个全是零洞的 25.6 GB 去装，报一句「读不了这个 IPSW」，
而用户按提示重跑多少次都一样。现在下载写 `.part`、完成才 rename 成正式名，
「正式名存在且大小对」才等于下完；`.part` 旁边一个 checkpoint 文件记录已落盘字节
（每 64 MB 更新一次，25 GB / 8 MB = 3200 段，每段写一次元数据太碎）。

**3. 不带 queue 的 VZVirtualMachine 用主队列，同步等它的 completion 会自死锁。**
Apple 的头文件写明 "uses the main queue ... The completion handlers from the
asynchronous functions are also invoked on that queue"。所以 VZ 必须传一条自己的
串行队列，并且等待的那一方不能占着它（`vzWait` 就是干这个的）。

**4. 服务端忽略 Range 时 dataTask 会把 25 GB 读进内存。** 第一版在 completion 里
才判断状态码——那时候伤害已经发生了，进程被 jetsam 杀而日志里一个字都没有。
现在先用一次 1 KB 的 Range 探测，确认返回 206 才排全部分段；并且改用
`URLSessionDataDelegate` 边收边写，内存峰值只有一段（8 MB）。

**5. pid 文件写的是「承载 VM 的进程」。** app 里启动时那是 AgentVM.app 自己的
pid，而 `vmz stop` 读到它就 `kill(SIGTERM)`——app 当场终止，用户看到「已停」，
实际整个 app 没了，VM 属于非优雅停机。现在 pid 文件写成 `app:<pid>` /
`vmz:<pid>`，`vmz stop` 只对 vmz 宿主发信号。

**6. 快照 tag 没校验白名单。** `snapshot create --tag ../../etc` 被接受，
`URL.appendingPathComponent` 把 `..` 规一路上消解掉，clonefile 把 1 GB 写到了
`~/.agentvm/vms/etc.img`——不在快照列表里，用户看不见，而磁盘少了一个 G。
三个入口（create/apply/delete）现在共用一个 `validateTag`，另外加一道
`assertInside` 纵深防御。

**7. 密码进了命令行参数。** macOS 那条路一开始把 `--password xxx` 直接拼进 argv，
而 `ps` 对所有本地用户可见完整命令行，安装要跑几十分钟。现在走 VM 目录里一个
0600 文件（`--password-file`），vmz 读完即删。

**8. `provisioned` 被无条件置 true。** macOS 26 及以下没有
`VZMacGuestProvisioningOptions`，走的是 warn 分支、根本没建账号，而调用方照样把
`spec.provisioned` 置成 true——于是以后每次启动都不再带，用户每次都看到
Setup Assistant，而「需要自己建账号」这句话只在第一次启动的日志里出现过一次。
现在 `MacVMRuntime` 暴露 `didProvision`，只有真带上了才置位。

### 单元级验证过什么

- `vmz check` / `catalog` / `list` / `status` / `snapshot` / `clone` / `delete`
  全命令冒烟通过；签名前后的 `catalog` 对比证实了 entitlement 是那个错的根因。
- 快照 tag 白名单：`../../etc`、`..`、`.hidden`、`a/b`、空串全部拒绝，
  合法 tag 通过，`vms` 根目录没有游离 img。
- `vmkit install --distro macos-27` 三条路径（前台 / `--detach` / `--source` 本地
  IPSW）用一个假 vmz 脚本验证参数拼装正确，不触发真实下载。
- `vmkit install --detach --distro debian-13` 复现并修好了 UnboundLocalError
  （第一版把通用 argv 分支整段替换掉了，app 装 Windows/Linux 100% 崩）。
- VM 名白名单在 `spawn_detached_install` 入口生效（原来只有 cmd_install 有，
  detach 路径根本不执行）。

### 没验证的

- `VZMacGuestProvisioningOptions` 建账号并打开远程登录：mac1 装完后
  `macvm.json` 里 `provisioned: true`，2026-11-10 那一轮的记录是第一次启动
  自动建了账号、远程登录可用。本轮没有重新开机复核（不为测试重启用户的虚拟机），
  所以这条是「有记录、未复验」。
- app 里 macOS VM 的画面（`VZVirtualMachineView`）没有视觉验证过：装完就走了
  命令行/SSH 那条路，没有截图核对过窗口里真的是桌面。
- 共享目录（virtiofs）在 macOS 客人机上没有可用的驱动，已在 app 的「存储」面板
  里说明，没有实现。
- **恢复镜像的全局缓存只在「第二台及以后」这条路上验证过机制**
  （硬链 0 字节、同 inode、删任一头另一头还在，见 `docs/PITFALLS.md` 的
  IPSW 缓存一节），没有真的装过第二台 macOS 虚拟机。
