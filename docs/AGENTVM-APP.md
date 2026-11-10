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

### 没验证的

- mac1 的 provisioning（建账号、开远程登录）只在安装日志里有记录，装完之后没有
  再复验过一次登录。
- IPSW 全局缓存（`~/.agentvm/images/ipsw`）只在机制层面验证过：硬链同 inode、
  `du` 前后一致、mac1 的 25.6 GB 镜像确实被别的新虚拟机复用。**没有**真正用缓存
  装过第二台 macOS 虚拟机。
- 半成品虚拟机的「重装」按钮在 app 里点过的路径只有命令行等价物
  （`install --detach --force`，Debian 13 实测装到 ready=true）；
  app 内点击本身只做了双实例冒烟（起来了、没崩、列表带 broken 字段）。

---

## 半成品虚拟机与「关不掉的安装页」（2026-11-10 第四轮审计）

用户报的 bug 原话：「一个 bug，VM 不完整，缺 disk.qcow2。重新 install。
一直跳出无法关闭，或者修复，你全面审计，进行修复」。

一句话概括：安装中断后留下一台只有 `vm.json` 的虚拟机，**起点一句死胡同错误，
中途一个没有出口的页面**。两端都修了。

### 起点的死胡同

`start_linux` 报「VM 不完整，缺 disk.qcow2。重新 install。」，而 `install`
紧接着回「xxx 已存在。换个名字，或加 --force 重装。」——两句互相踢皮球。
app 那边连 `--force` 都传不了（新建向导永远不带这个 flag），所以在图形界面里
这条错误是终点。

`spawn_detached_install` 还多一层：app 永远走 `--detach`，而「已存在」这道校验
原来只写在 `cmd_install` 里。父进程一声不响就把子进程起出来、写好 install.pid、
向 app 报「安装已开始」，子进程一秒后死在「已存在」上。app 那时已经切到
「正在安装」页。macOS 那条分支更糟：它会 `save_vm(_macos_vm_stub(...))`，
重装一台装好的 mac 会先把它的 vm.json 覆写成空壳。

修法：`vm_broken()` 算出缺哪些文件并作为 `broken` 字段随 `status` 发出；
两处 `die` 改成能照着做的命令（`_self_cmd()` 给全路径，vmkit.py 不在 PATH 上）；
`_refuse_existing()` 在三个入口都跑。

### 中途没有出口的页面

`NewVMWizard` 的 `.installing` 页脚原来只有「停止」一个按钮，且
`.disabled(!busy)`。`step` 离开 `.installing` 的唯一路径是
`.onChange(of: installFinished)`，而它只在收到一个**能解成 VMInfo 的** `done`
事件时置位。安装失败、被停止、done 形状对不上——这三种情况它永远是 false。
于是页面没有任何关闭按钮，Esc 也没有 `.cancelAction`，「停止」打在一个早就
死掉的进程上什么也不发生。这就是用户说的「无法关闭」。

`handleEvent` 的 `done` 分支原来把两种形状混为一谈：引导进程的
`{"name","log","pid","detached":true}` 只说明「已经甩到后台」，安装进程自己的
`{"vm":{…完整记录…}}` 才是装好了。前者解 VMInfo 必然失败，而失败是静默的
（`try?`）。

修法：页脚永远给「关闭（后台继续装）」；新增 `installEnded`，在
`tailLog` / `resumeTailing` 的 completion 里置位——进程没了就是结束了，
这个信号不依赖任何事件形状。

### 顺带修掉的三个老问题

**`--force` 会把 install.log 一起删掉。** 父进程先打开日志（append）再分叉，
子进程继承那个 fd，而 `--force` 会 `_rmtree` 整个 VM 目录。文件被 unlink，
子进程往一个不在目录树里的 inode 写，用户看到的是日志不存在或停在旧内容，
app 一个事件都读不到。`_reopen_install_log()` 在删除后把 fd 1/2 接回新文件
（`os.dup2`，`sys.stdout` 不用重新赋值），补表头，并按 `os.getpid()` 恢复
install.pid——那一份也被 rmtree 删了，而 app 的「停止」按钮和重启后的
「接上还在跑的安装」都靠它找进程。

这条在加重装按钮之前一直没人踩到：`--force` 原来只有命令行会用。

**install.pid 里的死进程号会被复用。** app 的 `installAlive` 原来只判「活着吗、
是不是僵尸」。macOS 的 pid 会回绕复用，死号随时可能被派给无关进程，这时 app
认为安装永远在跑：横幅钉住、`running` 不归零于是 `busy` 恒真、所有按钮全灰、
`tailLog` 的定时器永远不取消。两件事一起修：vmkit 侧安装结束就删 install.pid
（`main()` 的 `finally` + 信号 handler，那边用 `os._exit` 不走 finally）；
app 侧用 `ps -o state=,command=` 连进程名核对，没有 `vmkit.py` / `vmz`
就当作没在跑。只取 `comm` 不够——python3 的 comm 是 "Python"。

**`tailLog` 把 `resumeTailing` 读过的日志又重读一遍。** `resumeTailing` 设好
tailOffset 之后调的 `tailLog` 又把它清零，同样的事件触发两遍，同一个错误弹窗
连跳两次。加 `fromStart:` 参数。

### 验证

Swift harness（编译进 `Backend.swift` + `Models.swift`，`/tmp/swifttest`）四场景：

    install.pid 是死 pid     -> nil
    pid 被 /bin/sleep 复用   -> nil
    活着的无关 python        -> nil
    命令行带 vmkit.py 的进程 -> 返回该 pid
    VMInfo 解码 broken 字段   -> isBroken=true，stateText=没装完

端到端：造半成品（装到一半 SIGKILL + 删盘）→ `start` 报可执行的修复命令；
app 重装路径 `install --detach --force` 日志可读、install.pid 在位、
Debian 13 装到 `ready=true`；对 mac1 走 app 路径被拒且 vm.json md5 前后一致；
新构建的 app 双实例冒烟（起来、没崩、bundle 后端 status 带 broken 字段）。

`make lint` 现在也覆盖 `app/Sources/*.swift` 了——原来只有
`MacVMEngine.swift` 被 parse，而这轮改的 `ContentView` / `AppState` /
`NewVMWizard` 从来没被检查过。实测踩过：同一个 `confirmDelete` 写了两遍，
`make lint` 全绿，直到 `app/build.sh` 才炸。

---

## 控制台「连接关了」之后再也不会自己回来（2026-11-10 第五轮）

用户报：虚拟机那边把连接关了，关不掉，也没法继续操作。

排查顺序和结论：

一、VM 本身没病。`vmkit.py status` 报 running、monitor 的 `info vnc` 看得见
服务器在 127.0.0.1:22000 LISTEN、`VM status: running`。用一个 python 裸 RFB
客户端按 app 的字节序列连上去，30 秒收到 1.9 MB 数据，**一个字节都没断**。
QEMU 的 stderr 全都进了 qemu.log，而那个文件是 0 字节——服务端侧没有任何
异常记录。所以不是 QEMU 踢客户端。

二、app 侧两处缺陷合谋。见 PITFALLS M21、M22。`VNCClient` 里：

    generation      每条连接一个代次，旧连接的迟到 EOF 按代次丢掉
    scheduleReconnect 不再判 !stopped（那正是它从来不执行的原因），次数 5 -> 8
    .ready          只显示「正在取画面」；connected 改到 serverInit 之后才置位

三、`ConsoleView.onAppear` 加了 `didConnect` 一次性保护。SwiftUI 不保证 onAppear
只走一遍，而 `connect()` 每次都 cancel 上一条连接再建新的。

四、vmkit 两处 `-vnc` 加 `share=force-shared`（PITFALLS M23）。默认的
allow-exclusive 会让用系统 Screen Sharing 看同一台 VM 的用户把 app 控制台踢掉。
这是启动参数，对已在跑的 VM 无效。

### 测试怎么写的

harness 在 `/tmp/vncharness`：一个 python RFB 服务端（自己实现握手和回帧），
两个 Swift 入口各测一件事，都不定时器触发而用轮询，避开主线程延迟把时序打乱。

    a/  连上后立刻第二次 connect()：旧连接被 cancel，它的迟到 EOF 不能把
        新连接也判成断开。判据：全程 connected 且从未出现断开类状态。
        结果：探针一次都没触发，通过。
    b/  服务端 2 秒时真的断开：客户端必须自己连回来。
        结果 1.713s 断开 -> 2.801s 已连接，服务端日志印证连接 1 重新握手。

第一个版本的测试服务端把 RFB 顺序写反了（服务端先 recv 后 send），两边互等，
握手永远完不成——但因为那会儿「已连接」在 TCP `.ready` 就置位，测试看起来是
通过的。**是「已连接」诚实化这个改动把测试缺陷照出来的**，修好服务端后两个测试
同时从假绿变成真绿。

### 没验证的

- 真实 app 里点开控制台看 fnOS 安装界面：修复已装进 /Applications
  （二进制 md5 与本地构建一致，含「正在取画面」「正在重连（第」字样），
  VM 全程未受影响，但没有在 GUI 里实际操作过。
- 多客户端共存（app 控制台 + 系统 Screen Sharing）：`share=force-shared` 只对
  新启动的 VM 生效，linux1 这次启动时还没有它。

---

## 画面是倒的、窗口关不掉、鼠标点不中（2026-11-10 第六轮）

用户报：「窗口只能打开，无法关闭，文字窗口都是倒置的，内部无法选择，很多问题」。

### 先分清是谁的问题

倒置这条先做了排除法：用 python 按 app 的同一套 RFB 字节连 QEMU 抓一帧，
画面是正的；QEMU 的 stderr 全在 qemu.log 里且是 0 字节。所以数据没错，是 app
把它放错了位置——RFB 矩形数据第一行是画面顶部，而 CGContext 数据缓冲区第 0 行
是画面底部，原样 memcpy 就是上下颠倒。

同一个根因还解释了「内部无法选择」：`VNCScreenView.isFlipped` 只换算鼠标坐标、
不管图像方向，于是用户看到按钮在右下、framebuffer 里它在左上，点下去命中别的
控件。**两句抱怨是一个 bug。**

### 改了什么

    VNCClient.blit        逐行翻转写（CG 第 0 行是画面底部），删掉原来的整块
                          memcpy 快路——它在新方向下是错的
    VNCScreenView.keyDown Esc 按场景分流：锁鼠标时释放鼠标，没锁时关窗口
    ConsoleView           加「关闭」和「Esc 键」按钮；embedded 参数
    VNCScreenView         viewWillMove 里置窗口的 acceptsMouseMovedEvents
    VNCScreenView.scrollWheel  垂直方向掩码对调（往上滚是 8 不是 16）
    VNCScreenView         resignFirstResponder + 窗口失焦通知里补发修饰键释放
    ConsoleView.keyUp     Esc 不再发一次孤立的释放
    ConsoleView           不再自动抢 first responder（原来抢走后向导的 Tab/Enter 全废）
    AppState.refreshStatus  settingsVM / storageVM / consoleVM 换成新快照
    Backend.cancel        兜底不再 terminate 共享的 process 字段
    NewVMWizard           dismiss 与 consoleVM 赋值隔一帧
    AppState              catalog / status / check 三个 handler 认 error 事件
    MacVMWindow           加「关闭窗口」按钮（原来一个关闭控件都没有）

### 一个自己造的回归

刚给独立控制台加「关闭」按钮时没考虑 `ConsoleView` 是被复用的：新建向导的安装页
嵌着一块 300px 高的，那里的 `dismiss()` 作用的是最外层 sheet，一按就把整个安装
向导关了——而用户只是想收起那块画面。加了 `embedded` 参数才堵住。
**给复用组件加控件之前，先数一遍它有几处调用点。**

### 验证

- 翻转：服务端发「上白下黑」一帧，CG 第 0 行（画面底部）必须黑、最后一行
  （画面顶部）必须白。修好后通过。
- 上一轮那两个 harness（a 旧连接 EOF 不误杀新连接、b 断开后自动重连）复跑仍旧通过。
- `make lint` 全绿，`app/build.sh` 完整编译通过。
- ./install.sh 装进 /Applications，二进制 md5 与本地构建一致，VM（linux1 pid 59232）
  全程未受影响。

### 没验证的

- 没有在真实 GUI 里点过：按钮位置、Esc 关窗口、滚轮方向、鼠标锁定/释放。
  这些都要用户实际点一遍才算数。
- 安装向导里那块 300px 高的内嵌画面：缩放后能不能点中小按钮，没实测。
- `acceptsMouseMovedEvents` 那条是照 AppKit 语义改的，没有写 harness 验证
  （需要真实窗口）。

---

## 鼠标还是不动：上一轮的修法只对 first responder 有效（2026-11-10 第七轮）

用户报：「进入控制台，还是无法进行鼠标移动，问题很多啊，去 github 搜索代码，
进行全面修复」。

### 定位方式

先做了个假 RFB 服务端（只做握手、回一帧「上白下黑」、把收到的键盘鼠标事件按
时间顺序写进 JSONL），再把**真实的 ConsoleView.swift + VNCClient.swift** 编进
一个 harness：`NSHostingView(rootView: ConsoleView(...))` 开一个真窗口，用
`CGEvent(...)?.post(tap: .cghidEventTap)` 合成鼠标键盘事件驱动它，最后对着服务端
收到的字节断言。也就是说测的是真代码路径，不是 mock。

跑出来的基线（修复前）：

    事件类型        数量    说明
    PointerEvent    4       其中 3 个是同一次点击（移动/按下/松开）
    KeyEvent        3       普通 a 有按下有松开；Cmd+a 只有一个 0x61 按下

- 点击前发的 5 次鼠标移动 → 服务端收到 **0 个** PointerEvent
- Cmd+a 被当成裸的 0x61 送了进去，而且**永远没有 key-up**（AppKit 在 Command
  按住期间不补发普通键的 keyUp）——客人机从此认为 a 一直按着

### 根因

`window.acceptsMouseMovedEvents = true` 这条路只把 mouseMoved 送给 **first
responder**。点击之前 `VNCScreenView` 不是 first responder（SwiftUI 里通常是
hosting view 或窗口自己），所以移动整个不工作；点击、拖动、滚轮、键盘都正常，
因为那些走 hit-test，只有 mouseMoved 走 first responder。

修法是改走 `NSTrackingArea`（owner 直接收，不依赖 first responder 也不依赖
hit-test），这同时是 GitHub 上两个独立的 Swift VNC 客户端
（biztalk72/VNCClient、loomhand/swift-rfb）唯一使用的方式。细节见 PITFALLS M29。

### 顺带修掉的

    VNCScreenView.updateTrackingAreas  装 .activeAlways 的移动跟踪区域（主修）
    VNCScreenView.acquireLock          锁定模式才置窗口标志，并存原值、解锁还原
    VNCScreenView.keyDown              Command 组合键本地吃掉，不进客人机
    VNCScreenView.keyDown/keyUp        用 held 集合记账，只在真送过按下时才补释放
    VNCScreenView.resignFirstResponder 释放全部（原来只释放修饰键）
    VNCScreenView                      窗口失焦通知 / deinit 也走 releaseAllKeys
    VNCScreenView.scrollWheel          滤掉触控板惯性滚动那一段
    VNCScreenView.keysym               Command 按住时不按 characters 算 keysym
    VNCScreenView.viewDidMoveToWindow  补装一次跟踪区域（窗口刚拿到时）

### 两条通路不能同时开

这是实测照出来的一个隐藏坑：跟踪区域加上窗口标志之后，点击后视图成了 first
responder，两条路都通，**每次移动往客人机送两次一模一样的 PointerEvent**
（同一份 harness 里点击从 3 个事件变成 6 个）。所以窗口那个标志只在锁定模式下
才置——锁定模式走窗口级局部监视器，而局部监视器要这个标志才看得见 mouseMoved
（不置的话 AppKit 压根不生成这类事件，单独测过：只装监视器 = 0 个事件）。

### 验证

端到端 harness 五条断言全过（服务端收到的真实 RFB 字节）：

1. 点击前的移动到达客人机：1 个 PointerEvent（x=646 y=121）——修复前是 0
2. 一次点击 = 3 个事件（移动/按下/松开），无重复——修复前重复时是 6 个
3. 滚轮 -3 → 3 次 button 5（掩码 16）+ 3 次释放，方向正确
4. Cmd+a 完全不进客人机：0 个 KeyEvent——修复前是一个没有 key-up 的 0x61
5. 普通键 a 有按下有松开：2 个，配对

另外：`make lint` 全绿；./install.sh 装进 /Applications，二进制 md5 与本地构建
一致；画面方向复测仍是上白下黑（上一轮的 blit 翻转没被破坏）。

顺带查清一件不用担心的事：`CGContext.makeImage()` 是**快照**——实测把上下文
从纯白改写成纯黑之后，之前取出的图像第 0 字节仍是 255。所以解析线程边画边
memcpy 不会改写主线程正在显示的图像，不需要为它加每帧拷贝。

### 没验证的

- 没有在真实 GUI 里点过任何一个按钮。Esc 关窗口、按钮位置、滚轮方向、鼠标
  锁定/释放，都要用户实际点一遍才算数。
- 安装向导里那块 300px 高的内嵌画面：缩放后能不能点中小按钮，没实测。
- `.activeAlways` 的代价（控制台不在前台时鼠标划过画面也会带动客人机光标）
  没有单独测，是按远程桌面窗口的常规行为接受的。

### 环境变化（如实说明）

上一轮结尾还能看到的 fnOS 安装虚拟机（linux1，pid 59232）**现在不在了**，
`~/.agentvm/vms/` 下只剩 linux2（一个已就绪的 Debian 云镜像，没在跑），
机器上没有任何 QEMU 进程。这不是本轮造成的：本轮只用过 127.0.0.1:5911 上的
假 RFB 服务端和自己的测试窗口；`install.sh` 只删 `/Applications/AgentVM.app`，
`make lint` 不删任何东西。它是在本轮开始前就被移除的，我不知道是谁移除的。

---

## 把输入通路整条跑一遍：拖动被延迟按压吃掉，松手顺序不对就卡键（2026-11-10 第八轮）

第七轮修完「鼠标移不动」之后，用户让「继续修复」。这一轮没有新症状，是把整条
输入通路用第七轮那个端到端 harness 从头跑一遍，跑出三个真毛病。三个都属于
「协议字节对得上、语义是反的」——只看代码看不出来，必须对着服务端实际收到的
字节看。

### 定位方式

还是第七轮那套：假 RFB 服务端收字节 + 把真实的 ConsoleView.swift 和
VNCClient.swift 编进 harness + NSHostingView 开真窗口 + 合成事件驱动。
这一轮把断言从 5 条加到 14 条，新增的都在拖动、缩小后的坐标映射、锁定模式
这三段上。修复前的基线数字都记在 PITFALLS M30 里。

### 修了什么

**一、拖动被 120ms 的延迟按压整个吃掉。**

`mouseDown` 为了迁就图形安装器，按下之后憋 120ms 才真发 button-1。可
`buttons |= 1` 是立刻置上的，于是这 120ms 里每次拖动都带着 button1 的掩码发出去，
而客人机还没收到过按下。harness 抓到的完整顺序：

    移动@A(无键) → 拖动@B(带键) → 按下@A(带键) → 松开@A(无键)

中间那些移动全白费，松开还回到按下点。单击碰不到这条路径，所以现象是
「点击都好使，拖选文字和拖滚动条没反应」，极难归因。

修法是把「先把憋着的按下发掉」放在 `send()` 上——那是所有指针事件的唯一出口。
**不能只堵 `mouseDragged`**：实测合成事件在按住左键移动时是以 `mouseMoved`
送到视图的。松开那一侧也改了：按下补在按下点，松开放开手指的位置。

修完 harness 抓到的顺序：

    移动@A(无键) → 按下@A(带键) → 拖动@B(带键) → 松开@B(无键)

**二、keysym 对不上就是永久卡键。**

原来用 `Set<UInt32>` 记送下去过的 keysym，keyUp 时按 keysym 找回来发释放。
Shift+a 送下去的是 'A'(0x41)，用户先松 Shift 再松 a 时，keyUp 的 modifierFlags
里已经没有 Shift，算出来是 'a'(0x61)——对不上，释放永远发不出去。

改成按虚拟键码记账（`[UInt16: UInt32]`）。按下和松开必须是同一个物理键，
键码才是修饰键变了之后仍然稳定不变的东西。同一个改动顺带修掉「同一个
keysym 按两次只释放一次」。

**三、锁定模式两条通路又同时开了——而且是我自己上一轮引入的。**

第七轮记着「两条通路不能同时开」，当时只拆了跟踪区域。这一轮为了让 Esc 能
释放鼠标，在 `acquireLock()` 里加了 `win.makeFirstResponder(self)`：点「锁定鼠标」
按钮之后 first responder 是那个按钮，Esc 送不到画面视图上，而按钮的提示文字
写着「按 Esc 释放」。于是一锁定，视图既是 first responder、窗口标志又置着，
局部监视器转发的事件顺着响应链又进一次 `mouseMoved`。

实测同一点连发三次，客人机收到 7 个 PointerEvent，每步位移翻倍，指针像脱缰一样
冲到屏幕边缘被夹住。修法是局部监视器 `return nil` 把事件吃掉。

顺带修掉：`viewWillMove(toWindow:)` 每次覆盖 `keyObserver` 而不移除旧的，
老窗口的 token 一直到 deinit 才清理。

### harness 自己的三条规矩（这一轮踩出来的）

不遵守就会把 harness 的假象当成 app 的毛病，很危险：

1. **主线程一秒都不能堵。** 原来用 `usleep` 分隔合成事件，而 harness 自己就是
   那个 app——主线程睡着时 run loop 不转，AppKit 会把排队的 mouseMoved /
   flagsChanged 合并掉：5 个移动只剩最后 1 个，Shift+a 变成裸 a。
2. **合成修饰键不能设空 flags。** `CGEvent` 之后设 `e.flags = []` 会把修饰符
   字段抹掉，app 那边一个修饰键都看不到。
3. **合成 Command 要用真的 Command 键事件**（键码 0x37）。在 'a' 那个事件上设
   `flags = .maskCommand` 会把系统里的 Command 弄成粘滞，下一个段落才收到按下。

还有一条是关于测试资产的：假 RFB 服务端必须在主线程里先 bind 再启动写文件的
循环。放在线程里 bind，失败只是那条线程死掉，写文件的循环照跑——它会用一个空
列表把上一次的真实结果覆盖掉，测试就会「断言全过但其实是空数据」。这个坑真的
踩到过。

### 验证

两种窗口尺寸各跑一遍，14 条断言全过。新加的几条：

- 快速拖动：按下@A → 拖动@B → 松开@B，顺序和落点都对（修复前是拖动白做）
- 先松 Shift 再松字母：A 的释放补上了，全局配对检查 0 个卡键
  （修复前 `0x41 剩 1 个按下没释放`）
- 缩小到 700×449 和 620×359（就是向导里那块 300px 高的画面那个量级）之后，
  点下去的坐标换算仍然正确，且缩小之后鼠标移动照旧到达客人机
  （顺带验证 bounds 变化时 AppKit 重装跟踪区域这条路）
- 锁定模式：同一点连发三次，客人机一格一格往前走（467/534/600，每步 67 像素），
  指针移到画面外面事件仍然送得到，Esc 能释放，解锁后点击回到绝对坐标映射

### 没验证的

- **锁定模式是白盒驱动的。** 「锁定鼠标」那个按钮点不到：这个 macOS 上 SwiftUI
  的按钮在视图树里根本不是 NSButton（整棵树只有画面视图），辅助功能树里 7 个
  AXButton 的标题也全是空的。所以 harness 是先把焦点挪走（模拟按钮拿着焦点），
  再直接置 `sv.locked = true`——走的还是 `VNCScreenView.locked` 的 didSet，
  也就是按钮真正会走的那条路，但按钮那一层本身的接线没有实测。
- 锁定模式的「手感」没验：位移比例、回中速度这些只能人试。
- 第七轮列的那三条依然没验：没有在真实 GUI 里点过任何一个按钮；安装向导里
  300px 高的内嵌画面缩放后能不能点中小按钮（这一轮验了坐标映射是对的，但一个
  20 客人机像素的按钮在 300px 高的画面里只有 11 屏幕像素，点不点得中是人手的事）；
  `.activeAlways` 的代价没单独测。
- Esc 在小窗口变体里偶发一次没生效（重试第二次才生效）。合成键盘事件在 app 忙着
  处理鼠标事件时会被系统丢掉，这是环境噪声；但如果用户在实际使用中发现
  「按 Esc 释放鼠标有时要按两下」，那是同一个现象，值得反馈。

### 环境变化（如实说明）

这一轮全程只用 127.0.0.1:5911 上的假 RFB 服务端和自己的测试窗口，没有碰任何
虚拟机。机器上仍然只有 linux2 一个已就绪的 Debian 云镜像目录，没有 QEMU 进程。

---

## 关机状态下点「用系统窗口打开」是个死胡同（2026-11-10 第九轮）

用户发来一张截图：选中一台关着机的虚拟机，点「用系统窗口打开」，弹出一个只有
「好」按钮的对话框，写着「VM 没在运行，先 start」。

### 根因不在后端，在界面

按钮在关机状态和热运行状态下长得一模一样、也都可点，而它跑的那条命令
（`vmkit window`）第一件事就是查进程，不在就 die。所以每次点击必然得到同一个
对话框，用户除了点「好」什么都不能做——而他想要的是看画面，这个意图根本没被
回应。同一个死胡同在 `open`（显示窗口）和 `screenshot` 里也有，只是那两个的
入口被 `vm.running` 挡着，正常情况下点不到。

### 改了什么

**一、关机状态下这个按钮变成「启动并用系统窗口打开」**，走 `start --window`
（就是把 headless 置成 false 再启动，一次 QEMU 就带着原生窗口起来）。
在 app 里是新增的 `AppState.startWithWindow`。

VMInfo 是过期快照（见 M27），所以顺手确认了反方向：快照说没在跑、其实在跑时，
`start_linux` 看到 pid 活着会回一句「VM 已在运行」然后返回，不会起第二个 QEMU，
刷新之后按钮自己变回「用系统窗口打开」。

**二、后端那三句话改成能照着做的一条路。** 原来写「VM 没在运行，先 start」，
那是命令行口气，而 app 把后端的 stderr 原样弹成对话框，用户看到的就是一句
英文味儿的指令。现在带 `_self_cmd()` 拼出的真实命令，以及 app 里对应的按钮名。

**三、顺带修掉一个同类的误导。** `screenshot` 原来只看 `monitor.sock` 文件在不在，
而 QEMU 退出时不清理它（实测 linux1 关着机，monitor.sock 和 qemu.pid 都还留着），
于是那条检查永远通过，一路走到 screendump 才失败，报「没抓到画面，VM 可能刚退出
或没配 framebuffer 设备」——把人往 framebuffer 配置上引，而真相是它根本没起动过。
改成先查进程（`qemu_pid`，它同时回答「在不在」「是不是僵尸」「是不是 QEMU」），
再查套接字。

### 验证

三条命令在 linux1（关机）下实跑，各自给出新提示：

    window     → linux1 没在运行，没有窗口可打开。要带着原生窗口起来：vmkit start --name linux1 --window
    open       → linux1 没在运行，没有窗口可切到前台。在 app 里选中它，点「启动并用系统窗口打开」。
    screenshot → linux1 没在运行，没有画面可截。（原来这儿报的是「没抓到画面……framebuffer」）

`start --name nosuchvm --window` 实跑确认参数解析接受这个组合（走到
「没有这个虚拟机」才停，不是 argparse 报错）。`make lint` 全绿。

### 没验证的

- **没有在 GUI 里点过那个新按钮。** 这个 macOS 上 SwiftUI 的按钮在视图树里不是
  NSButton、辅助功能树里标题也是空的（第八轮记过），harness 点不到。所以
  「点了之后 QEMU 真的带着原生窗口起来」这条只验证了后端命令本身，没验证按钮
  到命令的接线。需要用户实际点一次。
- 没有验证关机状态下其余按钮（配置、存储、开机自启、删除）有没有同类死胡同——
  读代码确认 `configure` 在关机时会说「下次启动生效」而不是报错，但没有实跑。

---

## 「反应很慢」：每帧都过一遍 SwiftUI（2026-11-10 第十一轮）

用户原话：「在虚拟机内，反应很慢，基本没法用鼠标，跟之前最早版本的性能根本没法比，
没法用，彻底重构」。

### 先量再改

做了个假 RFB 服务端全速灌帧，把真实的 ConsoleView + VNCClient 编进 harness，
用 getrusage 数 app 自己烧的 CPU。基线：

    800×600     33fps、39% CPU，折合每帧 12.0 毫秒
    1920×1080   25fps、47% CPU，折合每帧 18.8 毫秒

### 根因

`VNCClient.frame` 是 `@Published`，而 `VNCScreen` 是 `NSViewRepresentable` 且
`@ObservedObject var client`。于是一帧到达就是「objectWillChange → body 重算 →
diff → updateNSView → needsDisplay」，整棵视图树（头部六七个控件、footer、
representable）每秒失效几十次。真正拷字节的活儿只占零头。

### 改了什么

`frame` 改成普通属性，加一个主线程回调 `onFrame`，画面视图在里面直接
`needsDisplay`。SwiftUI 从此不出现在每帧那条路上。只有 guestWidth / connected /
statusText 这些几秒才变一次的还走 @Published。

顺带：点下去的时候如果 200ms 内刚移过鼠标，就不再憋那 120ms 的延迟按压。
（快速点击本来就走不到那条路——mouseUp 会立刻补发 down+up；只有按住不放才
走得到，所以要用「按住 200ms」去测它。）

### 实测

同一个 harness、同一台机器、同样的假服务端：

    800×600     每帧 CPU 12.0ms → 3.6ms    进程总 CPU 39% → 8%
    1920×1080   每帧 CPU 18.8ms → 9.7ms    进程总 CPU 47% → 25%

16 条端到端断言两种窗口尺寸各跑一遍全过（断言从 14 条加到 16 条，新增的两条
专门盯点击延迟）。make lint 全绿。

### 没验证的，以及两个别再优化的方向

**没有验证「用户的卡顿治好了」。** 两条硬阻塞：

1. 没有真虚拟机在跑（linux1 关机），客人机那一侧完全没量过。而 fnOS 装在
   2 核 2G 上，那一侧很可能才是大头。
2. 屏幕锁着（前台是 loginwindow），所有要合成系统输入的测试都跑不了。
   **这一条逼着把端到端 harness 整个改成直接投递 NSEvent**（程序化构造
   NSEvent 送给视图），不再依赖 app 是前台、屏幕锁没锁、有没有别人在用鼠标。
   代价是不再验证「AppKit 会不会把事件路由到这个视图」那一层——那条路第七到
   第九轮实测过，之后没改过。

两个看着像瓶颈其实不是的（都实测过，别再去动）：

1. framebuffer 大小不是瓶颈：1080p 每帧 CPU 只比 800×600 多 6 毫秒，帧率一样。
2. 请求节流不是瓶颈：20ms 下限和 2ms 下限交替跑三次，结果完全一样（24fps、
   1.03 秒 CPU）。中间有一次测出 301fps 是脏数据（服务端没干净重启、旧连接
   还在服务），别信。harness 的帧率上限是那个 Python 假服务端自己，不是 app。

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
