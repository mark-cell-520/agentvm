import Foundation
import Virtualization

/// macOS 客人机的引擎。app 和命令行 vmz 共用这一份代码。
///
/// 为什么 macOS 走 Virtualization.framework，而 Windows / Linux 走 QEMU：
/// 三条路都实测过，只有这一条通。
///
///   1. QEMU 的 `vmapple` 机型是专门为 macOS 客人机设计的（模拟 Apple 硬件，
///      从 IPSW 恢复系统），但 Homebrew 的 QEMU 11.1.3 没有注册它——
///      `qemu-system-aarch64 -machine vmapple` 报 unsupported machine type，
///      而 `strings` 又能看到 VMAppleVirtioBlkVariant 之类的符号：代码在，
///      机型没开。自己打一个开的补丁版要连带 Reims vGPU、OpenAVB ROM、
///      idevicerestore 补丁一整套（见 Lakr233/vmapple-cli 的 Patches 目录，
///      光 QEMU 补丁就 17 个），不是「一键安装」该做的事。
///   2. 裸 `virt` 机型 + OpenCore：arm64 的 OpenCore 没有成熟发行物，
///      KVM-Opencore 那些 release 全是 x86 的。
///   3. VZ 是 Apple 官方 API，VZMacOSRestoreImage / VZMacOSInstaller /
///      VZMacOSBootLoader / VZMacPlatformConfiguration 全套公开，
///      宿主机上 `fetchLatestSupported` 直接拿到 macOS 27.2.1 的 IPSW 地址。
///
/// 代价，必须写清楚：**VZVirtualMachineView 必须和 VZVirtualMachine 在同一个
/// 进程里**，没有跨进程共享的公开路径。所以 macOS 客人机的生命周期跟持有它的
/// 进程绑定——app 里启动的随 app 退出而停，`vmz start` 起的随那个进程。
/// 这不是选择，是框架限制。Windows / Linux 那套「安装甩后台、app 可以关」
/// 的做法在 macOS 客人机上做不到。
///
/// 另一个限制：VZ 没有 VNC。所以 app 的内嵌 RFB 控制台对 macOS VM 不可用，
/// 它的画面走 VZVirtualMachineView（原生 NSView，比 VNC 更清楚也更快）。

// MARK: - 事件输出 --

/// 和 vmkit.py 的 Emit 保持同一套协议：stdout 一行一个 JSON。
/// app 那侧已经有 StreamParser 在解这个形状，复用比新造一套安全。
enum MacEmit {
    static var on = true

    /// 写 stdout / stderr 的锁。
    ///
    /// 为什么必须有：vmz 里 MacEmit.log 会从信号队列、vmQueue、安装进度
    /// 定时器队列上并发调用（SIGTERM handler 打「正在优雅关机」、下载 delegate
    /// 打进度、定时器打「安装中…」）。FileHandle 的 write 不是线程安全的，
    /// 两行 JSON 会交错成半行——而 vmkit 的 _relay 把认不出的碎行当普通日志
    /// 转出去，app 侧看到的就是一堆读不懂的文本，进度条再也不动。
    ///
    /// 只锁「写」这一步，锁内不再调任何会进 MacEmit 的函数（w 是唯一的写出口，
    /// stage/log/progress 都走它），所以不存在递归加锁。
    private static let writeLock = NSLock()

    static func w(_ obj: [String: Any]) {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard on else {
            // 人话模式。命令行不带 --json 时用，事件走同一条路换个印法。
            let t = obj["t"] as? String ?? ""
            switch t {
            case "stage":
                FileHandle.standardError.write("==> \(obj["label"] as? String ?? "")\n".data(using: .utf8)!)
            case "log":
                let lvl = obj["level"] as? String ?? "info"
                FileHandle.standardError.write("[\(lvl)] \(obj["msg"] as? String ?? "")\n".data(using: .utf8)!)
            case "error":
                FileHandle.standardError.write("错误：\(obj["msg"] as? String ?? "")\n".data(using: .utf8)!)
            default:
                if let d = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
                   let s = String(data: d, encoding: .utf8) {
                    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
                }
            }
            return
        }
        // JSON 走 stdout。必须一行一个：app 那侧按换行切，多行 JSON 会让
        // StreamParser 把一段拆成谁也不认识的碎片。
        guard let d = try? JSONSerialization.data(withJSONObject: obj),
              var s = String(data: d, encoding: .utf8) else { return }
        if !s.hasSuffix("\n") { s += "\n" }
        FileHandle.standardOutput.write(s.data(using: .utf8)!)
    }

    static func stage(_ id: String, _ label: String) {
        w(["t": "stage", "id": id, "label": label])
    }

    static func log(_ msg: String, level: String = "info") {
        w(["t": "log", "level": level, "msg": msg])
    }

    static func progress(_ id: String, frac: Double, bytes: Int64, total: Int64, speed: Double) {
        w(["t": "progress", "id": id, "frac": (frac * 10000).rounded() / 10000,
           "bytes": bytes, "total": total, "speed": Int(speed)])
    }

    static func error(_ msg: String) -> Never {
        w(["t": "error", "msg": msg])
        exit(1)
    }
}

/// 所有会以「用户可处理的错误」形态出现的信息都从这儿出。
/// 命令行用它退出，app 用它显示弹窗。
struct MacVMError: LocalizedError {
    let message: String
    init(_ m: String) { message = m }
    var errorDescription: String? { message }
}

// MARK: - 配置 --

/// 一个 macOS 虚拟机的全部状态。落在 <vm>/macvm.json。
///
/// 和 vmkit 的 vm.json 分开存而不是塞进去：vmkit 用 `.get()` 读自己的文件，
/// 加一堆它不认识的键虽然不会崩，但两边同时写一个文件迟早互相覆盖
/// （vmkit 的 save_vm 是整体重写，我们这边改一个字它下一次就整个冲掉）。
struct MacVMSpec: Codable {
    var name: String = ""
    var cpus: Int = 4
    var ram: String = "8G"
    var disk: String = "80G"
    var user: String = "agent"
    var password: String = "agentvm"
    /// 装完之后有没有配过账号。 provisioning 只在第一次启动时生效，
    /// 每次启动都带会让 Setup Assistant 反复出现。
    var provisioned: Bool = false
    /// IPSW 的 build，如 26B131。装完就固定下来，界面显示「macOS 27.2.1」。
    var build: String = ""
    /// 显示分辨率。VZ 的显示尺寸在创建 VM 时定死，跑起来改不了，
    /// 所以跟 cpus/ram 一样属于配置项。
    var width: Int = 1440
    var height: Int = 900
    /// 安装是否已完成。没完成时 start 会拒绝——没有系统的盘启动只会
    /// 卡在 EFI 里，用户看到一个黑屏，不知道要再等多久。
    var installed: Bool = false
    /// virtiofs 共享目录。空数组 = 这次启动不挂共享目录设备。
    /// 见 MacVMShare：macOS 客人机会自动挂到 /Volumes/<挂载名>。
    var shares: [MacVMShare] = []
    /// 端口转发：宿主机 TCP 端口 → 客人机 vsock 端口。空数组 = 不起 vsock 设备。
    /// 见 MacVMForward：这是 VZ 的 NAT 之外唯一的公开进客路径。
    var forwards: [MacVMForward] = []

    enum CodingKeys: String, CodingKey {
        case name, cpus, ram, disk, user, password, provisioned, build
        case width, height, installed
        case shares, forwards
    }

    init() {}

    /// 手写解码：每个字段都 decodeIfPresent，读不到就用默认值。
    ///
    /// 为什么不能用合成的：类里一旦声明了 CodingKeys，合成解码对**非可选**
    /// 字段走的是 decode 而不是 decodeIfPresent。新加的 shares / forwards
    /// 在老 macvm.json 里不存在，整份配置就会读不了——而报错只有一句
    /// 「The data couldn't be read because it is missing」，看不出缺的是哪个
    /// 键，用户看到的是「虚拟机莫名其妙用不了了」。实测踩过，所以手写。
    ///
    /// 每个默认值都和上面属性声明的那一个一致，改一处必须同时改另一处。
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        cpus = try c.decodeIfPresent(Int.self, forKey: .cpus) ?? 4
        ram = try c.decodeIfPresent(String.self, forKey: .ram) ?? "8G"
        disk = try c.decodeIfPresent(String.self, forKey: .disk) ?? "80G"
        user = try c.decodeIfPresent(String.self, forKey: .user) ?? "agent"
        password = try c.decodeIfPresent(String.self, forKey: .password) ?? "agentvm"
        provisioned = try c.decodeIfPresent(Bool.self, forKey: .provisioned) ?? false
        build = try c.decodeIfPresent(String.self, forKey: .build) ?? ""
        width = try c.decodeIfPresent(Int.self, forKey: .width) ?? 1440
        height = try c.decodeIfPresent(Int.self, forKey: .height) ?? 900
        installed = try c.decodeIfPresent(Bool.self, forKey: .installed) ?? false
        shares = try c.decodeIfPresent([MacVMShare].self, forKey: .shares) ?? []
        forwards = try c.decodeIfPresent([MacVMForward].self, forKey: .forwards) ?? []
    }
}

/// 一个 virtiofs 共享目录：把宿主机的一个目录挂进客人机。
///
/// 和 Linux 那边的 VMShare（virtio-9p，客人机里要自己敲 mount）是两套东西：
/// macOS 这边用 VZVirtioFileSystemDeviceConfiguration 加
/// macOSGuestAutomountTag，客人机自己会挂到 /Volumes/<name>，不需要在客人机
/// 里执行任何命令——这是 macOS 客人机和 Linux 客人机在共享目录上最大的区别。
struct MacVMShare: Codable, Hashable, Identifiable {
    /// 宿主机上的目录。绝对路径（~ 会在存之前展开）。启动时会再校验一次：
    /// 不存在或不是目录就直接报错，不让 VZ 拿一份挂不上的配置。
    var hostPath: String
    /// 客人机里的挂载名，/Volumes/ 下显示的就是它。
    ///
    /// 它同时是 VZMultipleDirectoryShare 的字典键，而那个键非法时框架抛的是
    /// **ObjC 异常**（Apple 头文件原话 "The keys must be valid names or an
    /// exception will be raised"）——Swift 接不住，进程直接崩。所以名字必须过
    /// MacVMPaths.validateShareName 的白名单，不能直接塞。
    var name: String
    /// 只读挂载。默认 false：能往里写才是共享目录的意义。
    var readOnly: Bool = false

    var id: String { "\(name)|\(hostPath)" }

    enum CodingKeys: String, CodingKey {
        case hostPath = "host_path"
        case name
        case readOnly = "read_only"
    }
}

/// 一条端口转发：宿主机上的一个 TCP 端口 → 客人机里的一个 vsock 端口。
///
/// 为什么是 vsock 而不是「把 NAT 的端口映射出来」：VZNATNetworkDeviceAttachment
/// 至今没有公开的端口映射 API（运行时实测那个类只有 5 个方法，方法名里带
/// port 的有 0 个），vsock 是唯一公开路径。宿主机侧开 TCP 监听、每个连接换一条
/// 到客人机 vsock 端点的连接，实现见 MacVsockProxy。
struct MacVMForward: Codable, Hashable, Identifiable {
    /// 宿主机上监听的端口。只绑 127.0.0.1——和另外两种客人机一个规矩，
    /// 外面连不进来。
    var hostPort: Int
    /// 客人机里的 vsock 端口。**客人机里得有程序监听这个端口**，
    /// macOS 客人机默认没有（它的 sshd 只听 TCP），见 MacVsockProxy 的注释。
    var guestPort: Int

    var id: String { "\(hostPort)>\(guestPort)" }

    enum CodingKeys: String, CodingKey {
        case hostPort = "host_port"
        case guestPort = "guest_port"
    }
}

/// 目录布局。和 vmkit 共用 ~/.agentvm/vms，macOS VM 混在列表里。
enum MacVMPaths {
    static var root: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".agentvm/vms", isDirectory: true)
    }

    static func dir(_ name: String) -> URL {
        root.appendingPathComponent(name, isDirectory: true)
    }

    static func spec(_ name: String) -> URL { dir(name).appendingPathComponent("macvm.json") }
    static func disk(_ name: String) -> URL { dir(name).appendingPathComponent("disk.img") }
    static func hardwareModel(_ name: String) -> URL { dir(name).appendingPathComponent("hardware.model") }
    static func machineID(_ name: String) -> URL { dir(name).appendingPathComponent("machine.id") }
    static func auxStorage(_ name: String) -> URL { dir(name).appendingPathComponent("aux.nvram") }
    static func ipswDir(_ name: String) -> URL { dir(name).appendingPathComponent("ipsw", isDirectory: true) }
    static func ipsw(_ name: String, file: String) -> URL { ipswDir(name).appendingPathComponent(file) }
    static func pidFile(_ name: String) -> URL { dir(name).appendingPathComponent("macvm.pid") }
    static func snapshots(_ name: String) -> URL { dir(name).appendingPathComponent("snapshots", isDirectory: true) }

    /// 全局镜像缓存。vmkit 的 `IMAGES`（scripts/vmkit.py 的 `ROOT / "images"`）
    /// 就是这一个目录：Linux 云镜像、Windows ISO、virtio 驱动都已经放在这儿，
    /// 一个文件全机器共用。IPSW 原来是个例外——它在每台虚拟机的目录里，于是
    /// 装第二台 macOS 虚拟机就要把 25.6 GB 重新下一遍（实测 19 到 23 分钟），
    /// 删掉第一台就一起没了。现在归到同一处。
    static var imagesRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".agentvm/images", isDirectory: true)
    }

    /// IPSW 的全局缓存位置。
    static func ipswCache(file: String) -> URL {
        imagesRoot.appendingPathComponent("ipsw", isDirectory: true)
            .appendingPathComponent(file)
    }

    /// IPSW 全局缓存目录本身。删虚拟机时要说清「缓存还在」。
    static var ipswCacheDir: URL {
        imagesRoot.appendingPathComponent("ipsw", isDirectory: true)
    }

    /// 两个路径是不是同一个文件（同一个 inode）。判「已经链好了」用。
    static func sameFile(_ a: URL, _ b: URL) -> Bool {
        let fm = FileManager.default
        guard let na = try? fm.attributesOfItem(atPath: a.path)[.systemFileNumber] as? Int,
              let nb = try? fm.attributesOfItem(atPath: b.path)[.systemFileNumber] as? Int
        else { return false }
        return na == nb
    }

    /// VM 名白名单。这个字符串会拼进文件路径，也会进 plist 标签
    /// （开机自启写 LaunchAgent）。`../` 或者带空名字都能跳出 vms 目录。
    static func validateName(_ name: String) throws {
        let ok = !name.isEmpty && name.count <= 48
            && name.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
        if !ok {
            throw MacVMError("虚拟机名字只能含字母、数字、- 和 _，且不超过 48 个字符：\(name)")
        }
    }

    /// 快照名的白名单。它会拼成文件名，而 URL.appendingPathComponent 会把
    /// `..` 规一路上消解掉——所以光禁 `/` 不够，`..` 一样能跳出 snapshots/。
    ///
    /// 实测踩过：create 那一条路原来漏了校验，`--tag ../../etc` 被接受，
    /// clonefile 把 1 GB 写到了 ~/.agentvm/vms/etc.img——不在快照列表里，
    /// 用户看不见，而磁盘少了一个 G。三处（create/apply/delete）现在共用
    /// 这一个函数，漏一处就等于没校验。
    static func validateTag(_ tag: String) throws {
        let ok = !tag.isEmpty && tag.count <= 64
            && tag.allSatisfy { $0.isLetter || $0.isNumber
                || $0 == "-" || $0 == "_" || $0 == "." }
            && !tag.hasPrefix(".")
            && !tag.contains("..")
        if !ok {
            throw MacVMError("快照名字只能含字母、数字、- _ .，"
                             + "不能以点开头、不能含 ..：\(tag)")
        }
    }

    /// 纵深防御：确认一个路径真的在 expected 目录里面。
    ///
    /// 白名单是主要防线，这里是第二道。白名单写漏一个字符（就像 create
    /// 那条路原来漏了整个函数）时，这道还能挡住。resolvingSymlinksInPath
    /// 把两边都解析成真实路径再比前缀，symlink 也一起挡了。
    static func assertInside(_ child: URL, _ parent: URL, what: String) throws {
        let c = child.resolvingSymlinksInPath().path
        let p = parent.resolvingSymlinksInPath().path
        guard c.hasPrefix(p + "/") else {
            throw MacVMError("\(what) 的路径不在该在的目录里：\(c)（应在 \(p) 下）")
        }
    }

    /// 共享目录名的白名单。和 validateTag 同一套字符集：这个名字会拼进
    /// /Volumes/<名字>，也会被用户敲进 shell，只放字母数字和 - _ .
    ///
    /// 为什么不用 VZMultipleDirectoryShare.validateName:error: 那个：实测
    /// 它认为 "my dir"（带空格）是合法名字，而带空格的名字在 /Volumes/ 下
    /// 每一条 shell 命令都要加引号，纯属给用户找事。我们自己拦严一点。
    static func validateShareName(_ name: String) throws {
        let ok = !name.isEmpty && name.count <= 64
            && name.allSatisfy { $0.isLetter || $0.isNumber
                || $0 == "-" || $0 == "_" || $0 == "." }
            && !name.hasPrefix(".")
            && !name.contains("..")
        if !ok {
            throw MacVMError("挂载名只能含字母、数字、- _ .，不能以点开头、不能含 ..，"
                             + "且不超过 64 个字符：\(name)")
        }
    }

    /// 宿主机共享目录：展开 ~、规整路径、确认真的存在而且是个目录。
    ///
    /// 为什么要规整：`URL(fileURLWithPath:)` 不解析 `..`，`~/a/../b` 和 `~/b`
    /// 指向同一个目录——存的时候就存规整过的（见 macParseShare），界面里显示的
    /// 路径才和磁盘上的一致。这里再规整一次是给手改过的配置兜底。
    ///
    /// 只规整、**不解软链接**，用 FileManager.fileExists(isDirectory:) 判目录：
    ///   1. 它会顺着链接看下去，`/tmp`、`/var` 这种系统符号链接能得到正确结果。
    ///   2. URL.resourceValues(isDirectory) 实测对 /tmp 返回 false（它对链接
    ///      本身作答，isSymbolicLink 才是 true），于是「共享 /tmp」会被一句
    ///      「不存在或者不是目录」挡掉，而那个目录明明是好的。
    ///   3. URL.resolvingSymlinksInPath() 也不可靠：实测它解得了自己建的链接，
    ///      却解不了 /tmp → /private/tmp。
    /// 存用户给的路径（规整后）而不是解过的：客人机那边由 VZ 自己去解，
    /// 而用户看到的路径应该和他选的一致。
    static func validateShareDir(_ path: String) throws -> URL {
        let expanded = (path as NSString).expandingTildeInPath
        guard expanded.hasPrefix("/"), !expanded.contains("..") else {
            throw MacVMError("共享目录要给绝对路径，而且不能含 ..：\(path)")
        }
        let url = URL(fileURLWithPath: expanded).standardizedFileURL
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir),
              isDir.boolValue else {
            throw MacVMError("共享目录不存在或者不是目录：\(url.path)")
        }
        return url
    }

    /// 端口范围。1024 以下要 root，而这个项目不要求以 root 跑；
    /// 0 和大于 65535 的压根不是端口。
    ///
    /// **只用于宿主机 TCP 端口。** 客人机的 vsock 端口不归这条管——那是另一套
    /// 端口空间，转发 SSH 要的恰恰就是客人机里的 22（vzvm 的 vsockPort 就是
    /// 22）。见 validateForwards。
    static func validatePort(_ port: Int, what: String) throws {
        guard (1024...65535).contains(port) else {
            throw MacVMError("\(what)要给 1024 到 65535 之间，你给了 \(port)。"
                + "1024 以下要 root 权限，而这个工具不要求以 root 跑。")
        }
    }

    /// 客人机 vsock 端口的范围。vsock 是独立的 32 位端口空间，和宿主机的
    /// 特权端口规矩无关，所以下限是 1 而不是 1024。
    static func validateGuestPort(_ port: Int) throws {
        guard (1...65535).contains(port) else {
            throw MacVMError("客人机 vsock 端口要给 1 到 65535 之间，你给了 \(port)。")
        }
    }

    /// 整份共享目录列表的校验：名字不能重复（VZMultipleDirectoryShare 的
    /// 字典键撞了会静默少挂一个目录），宿主机路径也不能重复（挂两遍同一个
    /// 目录没有意义）。
    static func validateShares(_ shares: [MacVMShare]) throws {
        var names = Set<String>()
        var paths = Set<String>()
        for sh in shares {
            try validateShareName(sh.name)
            let url = try validateShareDir(sh.hostPath)
            if !names.insert(sh.name).inserted {
                throw MacVMError("挂载名重复了：\(sh.name)。每个共享目录要一个不同的名字。")
            }
            if !paths.insert(url.path).inserted {
                throw MacVMError("宿主机目录重复了：\(url.path)。同一个目录挂两遍没有意义。")
            }
        }
    }

    /// 整份转发列表的校验：端口合法、宿主机端口不能重复（两个监听绑同一个
    /// 端口必有一个起不来）。客人机端口不去比对——那是客人机里的事。
    static func validateForwards(_ forwards: [MacVMForward]) throws {
        var hostPorts = Set<Int>()
        for f in forwards {
            try validatePort(f.hostPort, what: "宿主机端口")
            try validateGuestPort(f.guestPort)
            if !hostPorts.insert(f.hostPort).inserted {
                throw MacVMError("宿主机端口重复了：\(f.hostPort)。"
                    + "两条转发不能用同一个宿主机端口。")
            }
        }
    }
}

// MARK: - 共享目录 / 端口转发的命令行解析 --

/// "~/code:code" → MacVMShare。路径里有冒号也不会切错：按最后一个冒号切，
/// 而挂载名过白名单（不含冒号），所以最后一个冒号一定是分隔符。
///
/// 可选的第三段 "ro" 表示只读：`--share ~/code:code:ro`。
func macParseShare(_ s: String) throws -> MacVMShare {
    var readOnly = false
    var text = s
    if let i = text.lastIndex(of: ":"), String(text[i...].dropFirst()) == "ro" {
        readOnly = true
        text = String(text[..<i])
    }
    guard let i = text.lastIndex(of: ":") else {
        throw MacVMError("共享目录要写成 宿主路径:挂载名，例如 ~/code:code。你给了 \(s)")
    }
    let path = String(text[..<i]).trimmingCharacters(in: .whitespaces)
    let name = String(text[i...].dropFirst()).trimmingCharacters(in: .whitespaces)
    if path.isEmpty || name.isEmpty {
        throw MacVMError("共享目录要写成 宿主路径:挂载名，例如 ~/code:code。你给了 \(s)")
    }
    try MacVMPaths.validateShareName(name)
    // 存规整过的绝对路径。启动时还会再校验一次（配置可能被手改），但存的
    // 时候就规整，界面里显示的路径才和磁盘上的一致。
    let url = try MacVMPaths.validateShareDir(path)
    return MacVMShare(hostPath: url.path, name: name, readOnly: readOnly)
}

/// "2222:22" → MacVMForward。宿主机端口:客人机 vsock 端口。
func macParseForward(_ s: String) throws -> MacVMForward {
    let parts = s.split(separator: ":", omittingEmptySubsequences: false)
    guard parts.count == 2,
          let host = Int(parts[0].trimmingCharacters(in: .whitespaces)),
          let guest = Int(parts[1].trimmingCharacters(in: .whitespaces)) else {
        throw MacVMError("端口转发要写成 宿主机端口:客人机端口，例如 2222:22。你给了 \(s)")
    }
    try MacVMPaths.validatePort(host, what: "宿主机端口")
    try MacVMPaths.validateGuestPort(guest)
    return MacVMForward(hostPort: host, guestPort: guest)
}

/// 把一串 "路径:名字" 解析成一份合法列表。命令行用。
func macParseShares(_ list: [String]) throws -> [MacVMShare] {
    let out = try list.map(macParseShare)
    try MacVMPaths.validateShares(out)
    return out
}

/// 把一串 "宿主机:客人机" 解析成一份合法列表。命令行用。
func macParseForwards(_ list: [String]) throws -> [MacVMForward] {
    let out = try list.map(macParseForward)
    try MacVMPaths.validateForwards(out)
    return out
}

// MARK: - 大小解析 --

/// "8G" / "512M" / "80G" → 字节。
/// 和 vmkit 的 parse_size 同一套语义：G 按 1024³，不是 10⁹。
///
/// ** clamping 到 [0, UInt64.max]，不直接 `UInt64(v * mult)`。**
/// `UInt64(Double)` 在值超出表示范围或为负时是 **trap**，不是抛错：负值报
/// "cannot be converted to UInt64 because the result would be less than
/// UInt64.min"，溢出同样，进程以 133（SIGTRAP）退出。用 swiftc 实测过两种。
///
/// macvm.json 是用户手改的文件（加共享目录、改端口转发都要动它），把 ram 写成
/// "99999999999G" 或 "-5G" 是能发生的。原来这里直接转，于是点「启动」就崩，
/// 而这个函数自己上面几行还在重新校验 shares 和 forwards「配置可能被手改过」——
/// ram/disk 这两个会 trap 的漏了。2026-11-11 审计补上。
func macParseSize(_ s: String) -> UInt64 {
    let t = s.trimmingCharacters(in: .whitespaces).uppercased()
    var mult: Double = 1
    var num = t
    if t.hasSuffix("G") { mult = 1024 * 1024 * 1024; num = String(t.dropLast()) }
    else if t.hasSuffix("M") { mult = 1024 * 1024; num = String(t.dropLast()) }
    else if t.hasSuffix("T") { mult = 1024 * 1024 * 1024 * 1024; num = String(t.dropLast()) }
    else if t.hasSuffix("K") { mult = 1024; num = String(t.dropLast()) }
    let v = Double(num.trimmingCharacters(in: .whitespaces)) ?? 0
    let bytes = v * mult
    // NaN 和负数一律当 0：那是「读不出来」，让调用方的后续校验去报错，
    // 比在这儿 trap 掉整个进程好。Inf 夹到 UInt64.max，那之后的值交给
    // macAssertFreeSpace 和 VZConfiguration 自己拒。
    guard bytes.isFinite, bytes > 0 else { return 0 }
    if bytes >= Double(UInt64.max) { return UInt64.max }
    return UInt64(bytes)
}

func macHuman(_ bytes: UInt64) -> String {
    ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
}

// MARK: - spec 读写 --

func macLoadSpec(_ name: String) throws -> MacVMSpec {
    try MacVMPaths.validateName(name)
    let url = MacVMPaths.spec(name)
    guard FileManager.default.fileExists(atPath: url.path) else {
        throw MacVMError("找不到 \(name) 的配置（\(url.path)）。这个虚拟机可能不是 macOS 类型的。")
    }
    do {
        return try JSONDecoder().decode(MacVMSpec.self, from: try Data(contentsOf: url))
    } catch {
        throw MacVMError("读不了 \(url.path)：\(error.localizedDescription)")
    }
}

extension MacVMSpec {
    /// 按名字读配置，读不到返回 nil。
    ///
    /// app 那侧用的是可失败加载而不是 try?：调用点几乎都是「用户点了一个按钮」，
    /// 这时候该显示「配置读不了」而不是崩。CLI 那边用 macLoadSpec 拿错误详情。
    static func load(name: String) -> MacVMSpec? {
        try? macLoadSpec(name)
    }

    func save() throws { try macSaveSpec(self) }
}

func macSaveSpec(_ spec: MacVMSpec) throws {
    let url = MacVMPaths.spec(spec.name)
    let enc = JSONEncoder()
    enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    // 0600：里面有明文密码。vmkit 那边密钥一律 0600 落地，同一个规矩。
    try enc.encode(spec).write(to: url, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
}

// MARK: - 进程存活 --

/// 读 pid 文件并确认进程真的在跑。
///
/// 判活不能用 `kill(pid, 0)`：**僵尸进程照样返回成功**，而被强杀的 VM
/// 最常见的残留状态就是僵尸。用 ps 排除 Z 状态。vmkit 的 installAlive
/// 踩过同一个坑，那边是安装进程，这边是 VM 进程，成因一样。
func macPIDAlive(_ pid: Int32) -> Bool {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/ps")
    p.arguments = ["-p", String(pid), "-o", "state="]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    do {
        try p.run()
        p.waitUntilExit()
    } catch {
        return false
    }
    let out = (try? pipe.fileHandleForReading.readToEnd())
        .flatMap { String(data: $0, encoding: .utf8) } ?? ""
    let state = out.trimmingCharacters(in: .whitespacesAndNewlines)
    return !state.isEmpty && !state.hasPrefix("Z")
}

func macReadPID(_ name: String) -> Int32? {
    macReadPIDFile(name)?.pid
}

/// 写 pid 文件。`host` 区分这个 pid 是谁的。
///
/// 为什么必须有这个区分：**原来写的永远是「承载 VM 的进程」**。app 里启动
/// 时那就是 AgentVM.app 自己的 pid，而 `vmz stop` 读到这个 pid 就
/// `kill(pid, SIGTERM)`——app 没有 SIGTERM 处理，按默认行为当场终止。
/// 用户看到「已停」，实际是整个 app 没了，VM 属于非优雅停机，
/// APFS 留下未干净日志（正是 MacVMRuntime.stop 注释里要避免的那件事）。
/// 「删除」路径更糟：app 被杀的同时 Python 已经把 VM 目录删了。
///
/// 所以 app 侧写 `app:<pid>`，vmz 侧写 `vmz:<pid>`。vmz stop 只对
/// vmz 宿主发信号，见到 app 宿主就说明「请在 app 里关机」。
///
/// 这个值由 `MacVMRuntime.start(spec:provision:host:)` 的调用方给。
/// 原来 start 里硬编码 "app"，于是 `vmz start` 起的机器也记成 app 宿主，
/// 用户想停自己刚起的 VM 只会收到「正在 AgentVM app 里运行」。
func macWritePID(_ name: String, _ pid: Int32, host: String = "vmz") {
    try? Data("\(host):\(pid)\n".utf8).write(to: MacVMPaths.pidFile(name))
}

/// 读 pid 文件，返回 (宿主类型, pid)。文件不存在或进程已死返回 nil。
func macReadPIDFile(_ name: String) -> (host: String, pid: Int32)? {
    guard let text = try? String(contentsOf: MacVMPaths.pidFile(name), encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
    let parts = text.split(separator: ":", maxSplits: 1)
    let host: String
    let pidStr: Substring
    if parts.count == 2 {
        host = String(parts[0])
        pidStr = parts[1]
    } else {
        // 老格式：只有数字。当成 vmz 宿主——那是这个文件的原始语义。
        host = "vmz"
        pidStr = Substring(text)
    }
    guard let pid = Int32(pidStr.trimmingCharacters(in: .whitespaces)),
          pid > 0, macPIDAlive(pid) else { return nil }
    return (host, pid)
}

// MARK: - IPSW 下载 --

/// 正式文件已经下到哪儿了（断点续传的判据）。
///
/// **只看正式名，不看 .part。** 原来把 dest 预 truncate 成完整大小就当成
/// 「已下载」，而 `attributesOfItem[.size]` 返回的是逻辑大小——稀疏文件
/// truncate 到 25.6 GB 之后 st_size 就是 25600000000，而 st_blocks 是 0，
/// 一个字节都没落盘。实测确认过。于是下载中断后重跑，这里返回
/// 「已下完」，直接拿一个全是零洞的 25.6 GB 去装，报一句
/// 「读不了这个 IPSW」，而用户按提示重跑多少次都一样。
///
/// 现在下载写 .part、完成才 rename 成正式名，所以「正式名存在且大小对」
/// 才等于下完了。进行中的下载在 .part 里，由 .part 自己的大小记录进度
/// （见 macPartialBytes）。
func macDownloadedBytes(_ dest: URL) -> Int64 {
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: dest.path),
          let size = attrs[.size] as? UInt64 else { return 0 }
    return Int64(size)
}

/// 进行中的下载写到哪儿了。
///
/// **读 .part 文件自己的大小，不读 checkpoint 文件。** 原来这里读
/// `part.appendingPathExtension("ckpt")`，而全仓库没有任何代码写过那个文件
/// （2026-11-11 grep 确认：只有这一处引用）。于是它恒返回 0，两个后果：
///   · 856 行「接着已下的 X 继续」这句日志永远不会出现；
///   · 865 行 `lastBytes = resume` 从 0 起算，而 .part 可能已经有十几 GB，
///     第一次 0.5 秒采样算出的速度是「已下体积 ÷ 0.5 秒」——界面显示一个
///     几十 GB/s 的假速度和假 ETA，AppState 直接把它印在进度条下面。
///
/// 而 .part 的大小就是这个函数想要的「已确认完整落盘的字节数」：curl 是单连接
/// 顺序写的（见 macFetchWithCurl 的注释），写进去的就是落盘的。唯一的残尾是
/// 中断那一瞬间的最后一段，而调用方已经向下取整到 8 MB 段边界，残尾会被重下
/// 覆盖——比每 64 MB 写一次元数据的方案还简单，且少一个要维护的文件。
func macPartialBytes(_ part: URL) -> Int64 {
    let size = (try? FileManager.default
        .attributesOfItem(atPath: part.path)[.size] as? UInt64) ?? 0
    return Int64(size)
}

/// 装一次同步 HTTP 结果的盒子。Swift 的闭包捕获值语义在多线程下不好使，
/// 用一个类装可变状态最直白。
private final class Box {
    let sem = DispatchSemaphore(value: 0)
    var err: Error?
    var resp: URLResponse?
}

// MARK: - IPSW 全局缓存 --

/// 把 source 硬链到 to。目标所在目录不存在会先建。返回成没成。
///
/// 这三个函数是 internal 而不是 private：vmz 的测试 harness 和 main.swift
/// 编在同一个模块里，要靠它们验证「缓存命中时 0 字节新占空间」。
func macHardlink(_ from: URL, to: URL) -> Bool {
    do {
        try FileManager.default.createDirectory(
            at: to.deletingLastPathComponent(), withIntermediateDirectories: true)
    } catch {
        return false
    }
    return (try? FileManager.default.linkItem(at: from, to: to)) != nil
}

/// 确定这台虚拟机的 IPSW 在哪儿，顺带把全局缓存用上。
///
/// 顺序：VM 自己那份 → 全局缓存 → VM 本地路径（下载目标，由调用方去填）。
///
/// 缓存命中时**不是**直接返回缓存路径，而是先试着硬链一份到 VM 目录：后面
/// 的代码（VZMacOSInstaller、hardwareModel）都只认 VM 本地那个路径，而硬链
/// 不占空间，所以两全。链失败（跨卷之类）就退回直接用缓存路径——VZ 要的只是
/// 一个能读的本地文件，它并不关心这个文件躺在哪个目录下。
func macLocateIPSW(name: String, file: String) -> URL {
    let vmFile = MacVMPaths.ipsw(name, file: file)
    let cache = MacVMPaths.ipswCache(file: file)
    let fm = FileManager.default
    let vmHas = macDownloadedBytes(vmFile) > 0
    let cacheHas = macDownloadedBytes(cache) > 0

    if vmHas && cacheHas {
        // 两份都在。同一份数据（已经是硬链）就没事；不是同一份就把 VM 那份
        // 换成指向缓存的链接，把重复占用的 25.6 GB 还回去。换不成就直接用
        // 缓存那一份——它肯定是完整的。
        if MacVMPaths.sameFile(vmFile, cache) { return vmFile }
        try? fm.removeItem(at: vmFile)
        if macHardlink(cache, to: vmFile) { return vmFile }
        MacEmit.log("VM 目录里的 IPSW 与缓存重复，但没能归并，将直接用缓存那一份。",
                    level: "warn")
        return cache
    }
    if vmHas {
        // VM 自己有一份，缓存里没有。登记进去，让后面的虚拟机也蹭到。
        macRegisterIPSWInCache(from: vmFile)
        return vmFile
    }
    if cacheHas {
        if macHardlink(cache, to: vmFile) {
            MacEmit.log("复用已下载的 \(file)（全局缓存，0 字节新占空间）", level: "info")
            return vmFile
        }
        MacEmit.log("复用已下载的 \(file)（全局缓存）", level: "info")
        return cache
    }
    return vmFile
}

/// 把一份 IPSW 登记进全局缓存（缓存里还没有时）。**不动 source。**
///
/// 为什么用硬链接而不是「VM 直接读缓存路径」：缓存是跨 VM 共享的，删掉其中
/// 一台虚拟机不该把别的虚拟机还要用的 25.6 GB 一起弄没；反过来删缓存也不该
/// 让已经装好的虚拟机失去恢复镜像（`--force` 重装要用它）。硬链接两头指向
/// 同一个 inode，任一头删除数据都还在，而磁盘上始终只有一份。
///
/// 失败不抛异常：这一步纯粹是省磁盘，失败了顶多少省一点，没有理由让整个安装
/// 跟着陪葬。最后的手段是 copy——真的多占一份，所以会 warn 说清多占多少。
func macRegisterIPSWInCache(from source: URL) {
    let cache = MacVMPaths.ipswCache(file: source.lastPathComponent)
    if cache == source || MacVMPaths.sameFile(cache, source) { return }
    let fm = FileManager.default
    do {
        try fm.createDirectory(at: cache.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
    } catch {
        MacEmit.log("建不了 IPSW 缓存目录，这台虚拟机用自己的那一份："
            + error.localizedDescription, level: "warn")
        return
    }
    if fm.fileExists(atPath: cache.path) { return }   // 已经有了，无需再登记
    if (try? fm.linkItem(at: source, to: cache)) != nil { return }
    if (try? fm.copyItem(at: source, to: cache)) != nil {
        let extra = UInt64(max(0, macDownloadedBytes(source)))
        MacEmit.log("硬链接失败（可能跨卷），IPSW 在缓存里是独立一份，"
            + "多占 \(macHuman(extra))。", level: "warn")
        return
    }
    MacEmit.log("没能把 IPSW 登记进缓存，只有这台虚拟机用得上它。", level: "warn")
}

/// 下 IPSW。支持断点续传：重跑会接着已下的部分走。
func macFetchIPSW(_ url: URL, dest: URL,
                  onProgress: @escaping (Double, Int64, Int64, Double) -> Void) throws {
    // 已经下完的就别再下一次。25 GB 白下一遍没人受得了。
    // 只认正式名——.part 是进行中的，见 macDownloadedBytes 的注释。
    let have = macDownloadedBytes(dest)
    let part = dest.appendingPathExtension("part")
    var req = URLRequest(url: url)
    req.httpMethod = "HEAD"
    req.cachePolicy = .reloadIgnoringLocalCacheData
    let config = URLSessionConfiguration.ephemeral
    config.urlCache = nil
    let box = Box()
    let session = URLSession(configuration: config)
    session.dataTask(with: req) { _, resp, err in
        box.err = err
        box.resp = resp
        box.sem.signal()
    }.resume()
    box.sem.wait()

    if let err = box.err {
        throw MacVMError("连不上 IPSW 服务端：\(err.localizedDescription)")
    }
    guard let http = box.resp as? HTTPURLResponse else {
        throw MacVMError("IPSW 服务端没有响应：\(url.absoluteString)")
    }
    let len = http.value(forHTTPHeaderField: "Content-Length").flatMap(Int64.init) ?? 0
    if len <= 0 { throw MacVMError("拿不到 IPSW 大小，无法下载") }
    let total = len

    if have == total {
        onProgress(1.0, total, total, 0)
        return
    }
    if have > total {
        // 服务端换了文件（版本更新）。留着旧的只会校验失败，删掉重下。
        try? FileManager.default.removeItem(at: dest)
    } else if have > 0 {
        // 同名但大小不一样：上一次下到一半被 rename 过，或者换了个版本。
        // 接着写会在头部留出 have 字节的零洞，而 finish() 只校验大小——
        // 得到一个大小正确、头部全零的 IPSW，装到一半才报错。
        try? FileManager.default.removeItem(at: dest)
    }
    // 续传起点必须落在段边界上，否则第一段是短段，段完成后算连续前缀的
    // 步进会错位。向下取整到整段，最多重下一段（8 MB）。
    let seg = 8 * 1024 * 1024
    let ceiling = (Int(total) / seg) * seg
    let aligned = (Int(macPartialBytes(part)) / seg) * seg
    let resume = min(aligned, ceiling)
    try macFetchWithCurl(url: url, part: part, total: total, resume: Int64(resume),
                         onProgress: onProgress)
    // 下完了：校验大小再改成正式名。大小不对就留在 .part 里，
    // 下次重跑接着下——一个不完整的正式文件会被 macDownloadedBytes 当成
    // 「已下完」，那正是这个类要修的那个坑。
    let sz = (try? FileManager.default
        .attributesOfItem(atPath: part.path)[.size] as? UInt64) ?? 0
    guard sz == UInt64(total) else {
        throw MacVMError("下载不完整：落了 \(macHuman(sz))，需要 \(macHuman(UInt64(total)))。"
            + "再跑一次会接着下。")
    }
    try? FileManager.default.removeItem(at: dest)
    do {
        try FileManager.default.moveItem(at: part, to: dest)
    } catch {
        throw MacVMError("下载完了但写不出去：\(error.localizedDescription)")
    }
    onProgress(1.0, total, total, 0)
}

/// 用 curl 下 IPSW，`-C -` 断点续传。
///
/// **为什么是单连接，以及为什么不用 URLSession。** 两条都实测过，
/// 2026-11-10 复测过一次，结论没变：
///
/// 一、并发更慢，不是更快。同日实测 updates.cdn-apple.com：
///   · 串行单连接，4 个 100 MB 段：2.8 / 3.1 / 3.0 / 5.5 秒，即 19 到 36 MB/s；
///   · 6 个 curl 并发、每段 100 MB：段耗时 8.8 到 16.2 秒，聚合 37 MB/s；
///   · 2 个 curl 并发：段耗时 12.2 到 17.9 秒，比串行还慢。
/// Apple 的 CDN 是按客户端限速的，加连接只是把每条的份额摊薄，聚合不涨。
/// 所以这里就是一条连接，没有 `--parallel`、没有自己拼 Range。
///
/// 二、URLSession 那条路根本下不完。原来的 SegmentedDownloader 是「多个
/// data task 并发 + Range 分段」，在 Apple CDN 上每次下载都稳定卡在
/// 「连续前缀的下一个段」：那一个 task 发出去之后再没有任何回调，连接状态
/// 始终 ESTABLISHED，一直挂到 120 秒的 timeoutIntervalForRequest。试过并
/// 排除的四种修法（2026-11-10 全部实测）：
///   1. 降并发（6 → 4）+ httpMaximumConnectionsPerHost —— 卡点只是往后挪一段；
///   2. 每个 task 一个 session（禁连接复用）—— 照样卡，同一个 offset；
///   3. 看门狗 cancel + 拆半重试 —— 重试也卡在同一个 offset；
///   4. URLSession 的 timeoutIntervalForRequest —— 只管「两次数据之间的间隔」，
///      一个不收数据也不断开的连接它不触发。
/// 而 curl 单连接下同一个 range 0.6 秒就到。差别就在 curl 每次新连接、
/// 不复用连接池。所以下载走 curl：进程外、单连接、`-C -` 续传。这条路简单
/// 且已被反复验证，代价（单连接，25.6 GB 约 20 分钟）换来的是「一定能下完」。
///
/// 顺带记下原来那个类踩过的四个坑，免得日后再写一个 URLSession 下载器时
/// 重新踩一遍：urlCache 必须设 nil（25 GB 会被磁盘缓存再存一份，实际吃 51 GB）；
/// 必须写 .part 完成才 rename（truncate 出来的 st_size 是逻辑大小，见
/// macDownloadedBytes）；分段写盘要串行化（FileHandle 的 seek+write 不是
/// 线程安全的）；不能把整个响应读进内存（服务端忽略 Range 时 dataTask 会把
/// 25 GB 当一个 Data 交出来，进程直接被 jetsam 杀）。
private func macFetchWithCurl(url: URL, part: URL, total: Int64, resume: Int64,
                              onProgress: @escaping (Double, Int64, Int64, Double) -> Void) throws {
    let curl = ["/usr/bin/curl", "/usr/local/bin/curl"]
        .first { FileManager.default.isExecutableFile(atPath: $0) }
    guard let curl else {
        throw MacVMError("找不到 curl（/usr/bin/curl）。它是 macOS 自带的，"
                       + "没有它下不了 IPSW。")
    }
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: curl)
    // -f：HTTP 错误时返回非零而不是把错误页写进 .part（那样会得到一个
    //    大小不对的「下载完成」文件）。
    // -S：出错时仍然打印错误信息。
    // -L：跟随重定向。Apple 的镜像地址会 302。
    // --retry 3 --retry-delay 2：瞬时网络抖动自己重试。
    // -C -：从 .part 当前末尾接着写，这是断点续传的全部。
    proc.arguments = ["-fSL", "--retry", "3", "--retry-delay", "2",
                      "-C", "-", "-o", part.path, url.absoluteString]
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = FileHandle.nullDevice
    // 已有 .part 就续传，没有就从零开始（curl 的 -C - 两种情况都认）。
    if resume > 0 {
        MacEmit.log("接着已下的 \(macHuman(UInt64(resume))) 继续", level: "info")
    }
    do { try proc.run() } catch {
        throw MacVMError("起不了 curl：\(error.localizedDescription)")
    }

    // 进度靠轮询文件大小。不解析 curl 的 stderr 进度条：那是给人看的
    // 终端格式，含 \r 和动态宽度，解析它比轮询文件更脆。
    let t0 = Date()
    var lastBytes = resume
    var lastAt = t0
    let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    timer.schedule(deadline: .now() + 0.5, repeating: 0.5)
    timer.setEventHandler {
        let sz = (try? FileManager.default
            .attributesOfItem(atPath: part.path)[.size] as? UInt64) ?? 0
        let now = Date()
        let dt = now.timeIntervalSince(lastAt)
        guard dt >= 0.4 else { return }
        // 分子分母同一个时间窗口，否则算出来的是「增量 ÷ 总耗时」——
        // 那个值随时间趋近 0，会把 30 MB/s 显示成 0.1 MB/s。
        let speed = Double(Int64(sz) - lastBytes) / dt
        lastBytes = Int64(sz)
        lastAt = now
        onProgress(total > 0 ? Double(sz) / Double(total) : 0, Int64(sz), total, speed)
    }
    timer.resume()
    proc.waitUntilExit()
    timer.cancel()

    guard proc.terminationStatus == 0 else {
        // curl 的非零退出码含义见 man curl。最常见的是 22（HTTP 错误，
        // 已由 -f 转过来）、28（超时）、33（Range 不被支持）。
        throw MacVMError("curl 下载失败，退出码 \(proc.terminationStatus)。"
            + "已下的部分都在，重跑会接着下。")
    }
}

// MARK: - VM 配置 --

/// 造一份 VZVirtualMachineConfiguration。
///
/// 单独抽出来是因为安装和启动都要造一份，而两边曾经各写一半：安装那份忘了
/// 挂 aux.nvram 的硬件模型，启动那份忘了键盘——症状是装完之后鼠标键盘全无，
/// 只能强杀。一份代码两条路用，没这个坑。
func macMakeConfiguration(spec: MacVMSpec) throws -> VZVirtualMachineConfiguration {
    let name = spec.name
    let cfg = VZVirtualMachineConfiguration()

    cfg.cpuCount = max(2, spec.cpus)
    cfg.memorySize = macParseSize(spec.ram)

    // macOS 客人机必须用 VZMacOSBootLoader + VZMacPlatformConfiguration，
    // 别的 boot loader 跟它组合会被 validate() 拒掉。
    let boot = VZMacOSBootLoader()
    cfg.bootLoader = boot

    let platform = VZMacPlatformConfiguration()
    // 硬件模型从 IPSW 里取，安装时存盘。启动时必须用同一个：
    // aux.nvram 是和硬件模型配对生成的，换一个 macOS 认不出启动盘。
    guard let hmData = try? Data(contentsOf: MacVMPaths.hardwareModel(name)),
          let hm = VZMacHardwareModel(dataRepresentation: hmData) else {
        throw MacVMError("找不到硬件模型（\(MacVMPaths.hardwareModel(name).lastPathComponent)）。"
            + "这个虚拟机可能还没装完。")
    }
    platform.hardwareModel = hm
    guard let midData = try? Data(contentsOf: MacVMPaths.machineID(name)),
          let mid = VZMacMachineIdentifier(dataRepresentation: midData) else {
        throw MacVMError("找不到机器标识（machine.id）。")
    }
    platform.machineIdentifier = mid
    guard FileManager.default.fileExists(atPath: MacVMPaths.auxStorage(name).path) else {
        throw MacVMError("找不到 NVRAM（aux.nvram）。")
    }
    platform.auxiliaryStorage = VZMacAuxiliaryStorage(url: MacVMPaths.auxStorage(name))
    cfg.platform = platform

    // 系统盘。VZ 只认 raw 格式的镜像，qcow2 会直接 validate 失败——
    // 和 QEMU 那边正相反，别把 Linux 那套磁盘格式搬过来。
    guard FileManager.default.fileExists(atPath: MacVMPaths.disk(name).path) else {
        throw MacVMError("找不到系统盘（\(MacVMPaths.disk(name).lastPathComponent)）。")
    }
    let sysAtt = try VZDiskImageStorageDeviceAttachment(url: MacVMPaths.disk(name), readOnly: false)
    cfg.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: sysAtt)]

    // 图形。macOS 客人机必须用 VZMacGraphicsDeviceConfiguration，
    // VZ 没有 vga/cirrus 那些给 BIOS 客人机用的设备。
    let gfx = VZMacGraphicsDeviceConfiguration()
    gfx.displays = [VZMacGraphicsDisplayConfiguration(
        widthInPixels: max(800, spec.width),
        heightInPixels: max(600, spec.height),
        pixelsPerInch: 144)]
    cfg.graphicsDevices = [gfx]

    // 键盘和触控板都要。只有键盘的话鼠标在客人机里不动，
    // 而 macOS 的 Setup Assistant 第一屏就要点按钮。
    cfg.keyboards = [VZMacKeyboardConfiguration()]
    cfg.pointingDevices = [VZMacTrackpadConfiguration()]

    // NAT 网络。和 QEMU 那边一样只绑在本机：VZ 的 NAT 附件不对外监听，
    // 宿主机上看到的 192.168.64.x 是 NAT 内部地址。
    let net = VZVirtioNetworkDeviceConfiguration()
    net.attachment = VZNATNetworkDeviceAttachment()
    cfg.networkDevices = [net]

    // ---- virtiofs 共享目录 ----
    //
    // tag 只能用 VZVirtioFileSystemDeviceConfiguration.macOSGuestAutomountTag，
    // 不能自己起一个。lima 的源码注释明确警告：通用 sharing device 在 macOS
    // 客人机上「能挂但访问会 Operation not permitted」，必须用这个自动挂载 tag，
    // 客人才会把它挂到 /Volumes/<共享目录名> 并且真能读写。
    //
    // 那个常量的**运行时值不要硬编码**：实测 macOS 27.0.1 上它是
    // "com.apple.virtio-fs.automount"，而网上流传的是
    // "com.apple.vz.virtio-fs.automount"——照着抄会得到一个客人机不认的 tag，
    // 症状是 /Volumes 里什么都没有，而宿主机这边一切正常。直接用常量。
    if !spec.shares.isEmpty {
        // 配置可能被手改过，启动时再校验一次。目录不存在时 VZ 那边的错误
        // 只有一句「validate 失败」，看不出是哪个共享目录有问题。
        try MacVMPaths.validateShares(spec.shares)
        var dirs: [String: VZSharedDirectory] = [:]
        for sh in spec.shares {
            let url = try MacVMPaths.validateShareDir(sh.hostPath)
            dirs[sh.name] = VZSharedDirectory(url: url, readOnly: sh.readOnly)
        }
        let fs = VZVirtioFileSystemDeviceConfiguration(
            tag: VZVirtioFileSystemDeviceConfiguration.macOSGuestAutomountTag)
        // 多个目录用一个设备：VZMultipleDirectoryShare 的字典键就是客人机
        // /Volumes/ 下的名字。名字非法时这个 init 抛的是 ObjC 异常（进程直接
        // 崩，Swift catch 不住），所以上面必须先过白名单。
        fs.share = VZMultipleDirectoryShare(directories: dirs)
        cfg.directorySharingDevices = [fs]
    }

    // ---- vsock ----
    //
    // 一个 VM 只允许一个 vsock 设备（Apple 头文件原话 "Only one Virtio socket
    // device can be used per virtual machine"），所以所有转发共用这一个设备，
    // 端口区分在宿主机侧的代理里做（见 MacVsockProxy）。
    //
    // 没有转发时不起这个设备：macOS 客人机对设备个数是挑的，配错了 validate()
    // 直接拒，而报错只有一句看不懂的话。少配一个用不上的设备少一个风险。
    //
    // 端口在这儿先校验一遍：宿主机端口随后要转成 UInt16 去 bind，而
    // UInt16(70000) 是**直接陷阱**（Swift 的整数转换溢出会崩进程），不是抛错。
    // 配置可能被手改过，不能假设它一定合法。
    if !spec.forwards.isEmpty {
        try MacVMPaths.validateForwards(spec.forwards)
        cfg.socketDevices = [VZVirtioSocketDeviceConfiguration()]
    }

    return cfg
}

// MARK: - 运行态 --

/// 在 VZ 的专用队列上跑一个动作，同步等它的 completion。
///
/// 为什么必须有这一步：`VZVirtualMachine(configuration:)`（不带 queue 的那个）
/// 用的是**主队列**——Apple 的头文件写明 "uses the main queue to operate the
/// virtual machine ... The completion handlers from the asynchronous functions
/// are also invoked on that queue"。而在主线程上同步等一个要投递到主队列的
/// completion 是经典自死锁：app 点「启动」→ 主线程卡在 sem.wait() →
/// completion 要主队列才执行 → 主队列没人服务 → 永久卡死。
///
/// 所以两件事都要做：给 VZ 一条自己的串行队列，并且等待的那一方不能占着
/// 那条队列。这个函数把「派发到 vmQueue」和「在调用线程等」分开，
/// 调用线程是后台的（CLI）或主线程（app）都无所谓。
private func vzWait(_ queue: DispatchQueue,
                    _ start: @escaping (@escaping (Error?) -> Void) -> Void) -> Error? {
    let sem = DispatchSemaphore(value: 0)
    var err: Error?
    queue.async {
        start { e in err = e; sem.signal() }
    }
    sem.wait()
    return err
}

/// 一个正在运行的 macOS 客人机。
///
/// app 和 CLI 共用。区别只在于谁持有视图：app 把 `vm` 交给
/// VZVirtualMachineView 显示，CLI 什么视图都不建（headless）。
final class MacVMRuntime: NSObject, VZVirtualMachineDelegate, ObservableObject {
    @Published var state: VZVirtualMachine.State = .stopped
    @Published var lastError: String = ""
    /// 保护 vm / specName 的读写。
    ///
    /// 为什么需要：`vmz start` 装了一个 SIGTERM 信号源，它的 handler 跑在
    /// 信号队列上，而主线程可能还在 rt.start() 里写 vm——两边并发读写同一个
    /// 属性是数据竞争。app 里两条路都在主线程，碰不到；CLI 里碰得到。
    /// VZVirtualMachine 本身要求在 vmQueue 上调用，所以这里只用一把轻锁
    /// 护住这两个 Swift 属性，VZ 调用仍然全部走 vmQueue。
    private let propLock = NSLock()
    private var _vm: VZVirtualMachine?
    private var _specName: String = ""

    var vm: VZVirtualMachine? {
        get { propLock.lock(); defer { propLock.unlock() }; return _vm }
        set { propLock.lock(); _vm = newValue; propLock.unlock() }
    }
    var specName: String {
        get { propLock.lock(); defer { propLock.unlock() }; return _specName }
        set { propLock.lock(); _specName = newValue; propLock.unlock() }
    }
    /// 客人机自己停了（用户在里头选了关机）。app 据此关掉空窗口。
    var onGuestStop: (() -> Void)?
    /// 启动失败。消息可直接显示给用户——里面已经带了排查方向。
    var onFailed: ((String) -> Void)?

    /// VZ 的专用队列。所有 VZ 调用都派到这儿，等待的那一方不占着它——
    /// 见 vzWait 的注释（不带 queue 的 VZVirtualMachine 用主队列，
    /// 在主线程同步等它的 completion 会自死锁）。
    let vmQueue = DispatchQueue(label: "macvm.vz", qos: .userInitiated)
    /// 这一次启动有没有真的带上 provisioning。只有真带上了才该把
    /// spec.provisioned 置 true——见 start() 末尾的注释。
    private(set) var didProvision = false
    /// vsock 端口转发代理。没有转发时是 nil。只在启动成功后建、VM 真停了才拆。
    private var _vsockProxy: MacVsockProxy?

    private var vsockProxy: MacVsockProxy? {
        get { propLock.lock(); defer { propLock.unlock() }; return _vsockProxy }
        set { propLock.lock(); _vsockProxy = newValue; propLock.unlock() }
    }

    /// 启动。`host` 写进 pid 文件，标记这个 VM 归谁（见 macWritePID）。
    ///
    /// 默认 "app" 是故意选的安全侧：万一调用方忘了给，`vmz stop` 会拒绝发
    /// 信号并让用户去 app 里关机；而默认 "vmz" 的话，忘给的那一方（app）会被
    /// `vmz stop` 一发 SIGTERM 连整个 app 杀掉。前者只是烦，后者是数据损坏。
    func start(spec: MacVMSpec, provision: Bool, host: String = "app") throws {
        if vm != nil { return }          // 已经在跑，别起第二个
        specName = spec.name
        let cfg: VZVirtualMachineConfiguration
        do {
            cfg = try macMakeConfiguration(spec: spec)
            try cfg.validate()
        } catch let e as MacVMError {
            onFailed?(e.message)
            throw e
        } catch {
            onFailed?(error.localizedDescription)
            throw error
        }
        // 端口预检放在启动之前：等 VM 起来了才发现宿主机端口被占用，用户白等
        // 一次启动，而错误只有日志里一行 warn。在这儿失败，启动压根不开始。
        try macAssertPortsFree(spec.forwards)
        let machine = VZVirtualMachine(configuration: cfg, queue: vmQueue)
        machine.delegate = self
        self.vm = machine
        macWritePID(spec.name, Int32(ProcessInfo.processInfo.processIdentifier),
                    host: host)

        var opts: VZMacOSVirtualMachineStartOptions?
        if provision, #available(macOS 27.0, *) {
            // 只在第一次启动时给。每次启动都带 provisioning，macOS 会
            // 反复进 Setup Assistant 让你再建一遍账号——那不是 bug，
            // 是框架在照我们说的做。
            let o = VZMacOSVirtualMachineStartOptions()
            let p = VZMacGuestProvisioningOptions()
            p.fullName = spec.user
            p.username = spec.user
            p.password = spec.password
            p.logsInAutomatically = true
            // 开远程登录。不开的话 macOS 客人机没有 SSH，装完只能
            // 在窗口里操作——而「给 agent 当沙箱」的用法全靠 SSH。
            p.enablesRemoteLogin = true
            do {
                // Swift 3 的重命名规则把 setX:error: 变成 setX(_:) throws。
                try o.setGuestProvisioning(p)
                opts = o
            } catch {
                MacEmit.log("设置账号失败（不影响启动，装完要手动建账号）：\(error.localizedDescription)",
                            level: "warn")
            }
        } else if provision {
            // macOS 26 及以下没有 VZMacGuestProvisioningOptions：
            // 装完会停在 Setup Assistant，账号要用户自己建。
            MacEmit.log("这个 macOS 版本不支持自动建账号，装完请在窗口里自己设置。",
                        level: "warn")
        }

        // 用 completion handler 而不是 async start()：vzrun.swift 实测过
        // async 版本在这个环境一直 pending 不返回，completion 形式正常同步返回。
        //
        // 注意 start(options:) 的闭包收的是 (Error?)，而
        // start(completionHandler:) 收的是 Result<Void, Error>——
        // 两个 API 的形状不一样，混用会在编译期报「no member 'failure'」。
        //
        // 等的地方不能在 vmQueue 上，否则自己堵自己（见 vzWait）。
        if let opts {
            let err = vzWait(vmQueue) { done in
                machine.start(options: opts) { e in done(e) }
            }
            if let err { throw startFailed(spec, err) }
            didProvision = true
        } else {
            let err: Error?
            if provision {
                // macOS 26 及以下没有 VZMacGuestProvisioningOptions：
                // 装完会停在 Setup Assistant，账号要用户自己建。
                // 这时候 didProvision 保持 false，调用方据此不把
                // spec.provisioned 置 true——否则用户每次启动都看到
                // Setup Assistant，而提示只在第一次启动的日志里出现过一次。
                MacEmit.log("这个 macOS 版本不支持自动建账号，装完请在窗口里自己设置。",
                            level: "warn")
                didProvision = false
                err = vzWait(vmQueue) { done in machine.start { r in
                    if case .failure(let e) = r { done(e) } else { done(nil) }
                } }
            } else {
                didProvision = false
                err = vzWait(vmQueue) { done in machine.start { r in
                    if case .failure(let e) = r { done(e) } else { done(nil) }
                } }
            }
            if let err { throw startFailed(spec, err) }
        }
        // vsock 端口转发。放在启动成功之后：设备是 machine.socketDevices 里的
        // 那一个，VM 没起来之前拿不到。
        installVsockProxy(machine, forwards: spec.forwards)
        DispatchQueue.main.async { self.state = machine.state }
    }

    /// 把端口转发代理装起来。绑不上端口时**不停虚拟机**——VM 本身是好的，
    /// 少一个转发比用户等半天启动然后什么都没有强，但错误必须说出来。
    private func installVsockProxy(_ machine: VZVirtualMachine, forwards: [MacVMForward]) {
        guard !forwards.isEmpty else { return }
        // socketDevices 是 VZ 的属性，照这个项目的老规矩在 vmQueue 上读。
        var device: VZVirtioSocketDevice?
        let box = Box()
        vmQueue.async {
            device = machine.socketDevices.first(where: { $0 is VZVirtioSocketDevice })
                as? VZVirtioSocketDevice
            box.sem.signal()
        }
        box.sem.wait()
        guard let device else {
            MacEmit.log("配置了端口转发，但 VM 上没有 vsock 设备（socketDevices 是空的）。"
                + "这一次启动不带端口转发。", level: "warn")
            return
        }
        let proxy = MacVsockProxy(vmQueue: vmQueue)
        do {
            try proxy.start(forwards: forwards, device: device)
            vsockProxy = proxy
        } catch {
            MacEmit.log("端口转发没起来：\(error.localizedDescription)"
                + "（虚拟机照常运行，只是这条转发不通）", level: "warn")
        }
    }

    /// 拆掉端口转发。VM 真的停了才调（guestDidStop / didStopWithError /
    /// app 退出）——优雅停机超时的那个分支里客人机还在跑，代理要留着。
    private func teardownVsockProxy() {
        let p = vsockProxy
        vsockProxy = nil
        p?.stop()
    }

    private func startFailed(_ spec: MacVMSpec, _ err: Error) -> MacVMError {
        vm = nil
        try? FileManager.default.removeItem(at: MacVMPaths.pidFile(spec.name))
        let msg = "启动失败：\(err.localizedDescription)"
        onFailed?(msg)
        return MacVMError(msg)
    }

    /// 优雅停机。**不阻塞调用线程。**
    ///
    /// 原来在主线程同步等 machine.stop 的 completion，最长 90 秒——客人机停在
    /// 登录界面或有模态框不理关机请求时，整个 app 冻成彩球，点哪都没反应。
    /// 而 679-680 行的注释自己承认这个场景存在。现在派发就不等，
    /// 状态由 delegate 回调推进。
    ///
    /// 超时不扔引用：原来超时后照样 `vm = nil`，runtime 被 AppState 从字典
    /// 摘掉、MacVMRuntime 释放、VZVirtualMachine 跟着释放，而客人机还在跑——
    /// 没有任何对象能再 stop() 它，一直占 CPU/内存到 app 退出，UI 上还显示
    /// 「已停止」。现在超时只是打个警告，vm 留着，还能再叫一次 stop。
    ///
    /// `completion` 在停机真的结束（或等超时）之后调一次，跑在后台队列上，
    /// `timedOut` 为 true 表示「没停成，客人机还在跑」。
    ///
    /// 为什么需要它：vmz 的信号 handler 原来写 `rt.stop(); exit(0)`——
    /// stop 只是把 machine.stop 投递到 vmQueue 就返回，下一行 exit(0) 立刻
    /// 把进程带走，客人机从没收到 ACPI 关机事件，那一下跟 kill -9 等效。
    func stop(completion: ((_ timedOut: Bool) -> Void)? = nil) {
        guard let machine = vm else { completion?(false); return }
        DispatchQueue.main.async { self.state = .stopping }
        // 优雅停机。macOS 会收到关机事件、正常 flush 磁盘。
        // 强杀（kill -9）会让 APFS 留下未干净的日志，下次启动跑 fsck
        // 要好几分钟——跟 QEMU 那边 Windows 的坑同源。
        //
        // canStop 也必须在 vmQueue 上：VZVirtualMachine 的属性内部有
        // dispatch_assert_queue，在主线程读它和调它的方法一样直接 SIGTRAP。
        // 原来那句 `guard machine.canStop` 就在主线程，app 里点一次关机
        // 就是一个没有日志的崩溃。
        let box = Box()
        vmQueue.async {
            if machine.canStop {
                machine.stop { err in box.err = err; box.sem.signal() }
            } else {
                // 已经停了（或者正在停）。
                box.sem.signal()
            }
        }
        // 等，但在 vmQueue 之外的后台队列上等：调用方（app 主线程、CLI 信号
        // 队列）都不被堵住。
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            if box.sem.wait(timeout: .now() + 90) == .timedOut {
                MacEmit.log("优雅关机超时（客人机可能停在登录界面）。"
                    + "虚拟机仍在运行，可以再停一次，或者在客人机里选「关机」。",
                    level: "warn")
                completion?(true)
                return
            }
            guard let self else { completion?(false); return }
            self.vm = nil
            try? FileManager.default.removeItem(at: MacVMPaths.pidFile(self.specName))
            DispatchQueue.main.async { self.state = .stopped }
            completion?(false)
        }
    }

    /// 同步优雅停机，停完了才返回。**只有 app 退出时用。**
    ///
    /// 为什么单独要一个同步版：进程马上就要没了，没有「等回调」的机会——
    /// SwiftUI 不保证跑 .onDisappear，guestDidStop 也没人服务。原来 app
    /// 退出等于直接拔电源，APFS 留下未干净的日志，下次启动跑好几分钟 fsck。
    /// 超时不抛错：等不到也只能让进程走，但日志里要说清楚这次是非优雅停机。
    func stopAndWait(timeout: TimeInterval = 45) {
        guard let machine = vm else { return }
        DispatchQueue.main.async { self.state = .stopping }
        // 代理先拆：进程马上就要没了，而桥里的 DispatchIO 还在跑的话，
        // 退出时那些队列会被硬停，客人机侧的连接变成没人关的半开状态。
        teardownVsockProxy()
        let box = Box()
        // canStop 和 stop 都在 vmQueue 上，理由同 stop()。
        vmQueue.async {
            if machine.canStop {
                machine.stop { _ in box.sem.signal() }
            } else {
                box.sem.signal()
            }
        }
        if box.sem.wait(timeout: .now() + timeout) == .timedOut {
            MacEmit.log("退出时优雅关机等不到（等了 \(Int(timeout)) 秒）。"
                + "这一次是非优雅停机，APFS 可能留下未干净的日志，下次启动会跑 fsck。",
                level: "warn")
            return
        }
        vm = nil
        try? FileManager.default.removeItem(at: MacVMPaths.pidFile(specName))
        DispatchQueue.main.async { self.state = .stopped }
    }

    // MARK: VZVirtualMachineDelegate

    func guestDidStop(_ vm: VZVirtualMachine) {
        teardownVsockProxy()
        DispatchQueue.main.async {
            self.state = .stopped
            self.vm = nil
        }
        try? FileManager.default.removeItem(at: MacVMPaths.pidFile(specName))
        onGuestStop?()
    }

    func vm(_ vm: VZVirtualMachine, didStopWithError error: Error) {
        teardownVsockProxy()
        let msg = error.localizedDescription
        DispatchQueue.main.async {
            self.state = .stopped
            self.vm = nil
            self.lastError = msg
        }
        try? FileManager.default.removeItem(at: MacVMPaths.pidFile(specName))
        // 必须通知调用方。原来只写 lastError，而那个字段全仓库没有人读——
        // 客人机 panic 之后 app 里那张 sheet 还在（runtime 还在字典里，
        // 只是 vm 变 nil），VZVirtualMachineView 因为 virtualMachine 为 nil
        // 什么都不画：一块黑画面加一个绿点，用户只能猜，真实原因躺在
        // 一个没人看的字段里。
        onFailed?("虚拟机异常停止：\(msg)")
    }
}

// MARK: - vsock 端口转发 --

/// 宿主机侧的 vsock 端口转发代理：宿主机开 TCP 监听，每个连进来的连接换一条
/// 到客人机 vsock 端点的连接，然后把两边的字节对着抽。
///
/// 为什么需要它：VZNATNetworkDeviceAttachment 至今没有公开的端口映射 API
/// （运行时实测那个类只有 5 个方法，方法名里带 port 的有 0 个），vsock 是
/// 唯一公开的进客路径。参考实现是 applicative-systems/vzvm 的 VsockProxy.swift
/// （生产环境在给 Nix 的 Linux 客人机做 SSH 转发），这里照它的形状写，
/// 但改成适配这个项目的几处：
///   1. 端口占用直接报错，不学它那样等 60 秒——那是给 launchd 崩溃重启用的，
///      我们是交互式启动，用户要知道端口被谁占了。
///   2. 加一个看门狗超时。见下面坑1。
///   3. 设备从 MacVMRuntime 传进来，不自己持有 VM。
///
/// 三个坑，每一个都查证过：
///   1. **VZ 不告诉宿主客人机什么时候开始监听 vsock 端口。** Apple 头文件对
///      connectToPort 的原话是 "Does nothing if the guest does not listen on
///      that port"——也就是客人机没监听时 completion 可能压根不被调用。所以
///      光靠 completion 的失败重试不够，额外加一个看门狗：到点没回调就当失败
///      重试（vzvm 用的是 1500 ms × 60 次，这里一样）。
///   2. **VZVirtioSocketDevice 必须在 VM 的 queue 上 touch**（项目里就是
///      MacVMRuntime.vmQueue），所以 connect 和 connection.close() 都派发到
///      那条队列上。
///   3. **客人机里得有程序监听对应的 vsock 端口。** 这一点必须说清楚：
///      vzvm 的客人机是 Linux，nix-daemon/sshd 直接听在 vsock 端口上；而
///      macOS 客人机的 sshd 只听 TCP，系统里没有现成的 vsock 监听服务。
///      所以转发链路本身是通的，但「ssh 127.0.0.1:xxxx 能进客人机」还需要
///      客人机里有个听 vsock 的东西。启动日志里会把这句话打出来，
///      不装作已经通了。
final class MacVsockProxy {
    /// 重试间隔（毫秒）和次数。vzvm 用 1500 ms × 60（合计 90 秒）——客人机从
    /// 开机到服务就绪实测要几十秒，间隔太短只是空转。
    private static let retryIntervalMs = 1500
    private static let maxRetries = 60

    /// 一共等多少秒，给日志用。
    ///
    /// 必须由间隔 × 次数算出来，不能写死。原来两处都写成
    /// `maxRetries * 1500 / 60`——那是把毫秒除以 60（ms→分的除数）而不是 1000
    /// （ms→秒），算出来 1500，日志告诉用户「等了 1500 秒」，把 90 秒说成
    /// 25 分钟。改上面两个常量时只有这一处会跟着变。2026-11-11 审计补上。
    private static var totalWaitSeconds: Int {
        retryIntervalMs * maxRetries / 1000
    }

    private let vmQueue: DispatchQueue
    private let lock = NSLock()
    private var stopped = false
    private var device: VZVirtioSocketDevice?
    private var sources: [DispatchSourceRead] = []
    /// 存活的桥。Bridge 自己把自己留在这个集合里，两个方向都结束了才摘。
    private var bridges: Set<Bridge> = []

    init(vmQueue: DispatchQueue) {
        self.vmQueue = vmQueue
    }

    /// 开始转发。端口绑不上时抛错（调用方决定是报错还是只记一笔）。
    func start(forwards: [MacVMForward], device: VZVirtioSocketDevice) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { return }
        self.device = device
        var created: [DispatchSourceRead] = []
        do {
            for f in forwards {
                try MacVMPaths.validateForwards([f])
                let fd = try Self.bindLoopback(port: UInt16(f.hostPort))
                // 每条转发一条自己的队列：accept 循环和桥都跑在这儿，
                // 共用一条队列时一条慢连接会把别的转发一起堵住。
                let q = DispatchQueue(label: "macvm.vsock.\(f.hostPort)", qos: .userInitiated)
                let src = DispatchSource.makeReadSource(fileDescriptor: fd, queue: q)
                let guestPort = UInt32(f.guestPort)
                src.setEventHandler { [weak self] in
                    self?.acceptOne(listenFD: fd, guestPort: guestPort)
                }
                // cancel handler 里关 fd。放在这儿而不是 stop() 里手动 close：
                // source 的 cancel 是异步的，两边都关会 double close。
                src.setCancelHandler { close(fd) }
                created.append(src)
                src.resume()
                MacEmit.log("端口转发已就绪：127.0.0.1:\(f.hostPort) → 客人机 vsock 端口 \(f.guestPort)")
            }
        } catch {
            // 起了一半失败：已经 resume 的 source 必须 cancel 之后再让它们释放。
            // 直接释放一个还激活着的 DispatchSource，libdispatch 会断言失败
            // （崩在整个进程上，而 Swift 接不住）。
            for s in created { s.cancel() }
            throw error
        }
        sources = created
        if !forwards.isEmpty {
            MacEmit.log("注意：客人机里需要有程序监听对应的 vsock 端口，"
                + "这条转发才通。macOS 客人机默认没有这样的服务（它的 sshd 只听 TCP）。",
                level: "warn")
        }
    }

    func stop() {
        lock.lock()
        guard !stopped else { lock.unlock(); return }
        stopped = true
        let srcs = sources
        sources.removeAll()
        let bs = bridges
        bridges.removeAll()
        device = nil
        lock.unlock()
        for s in srcs { s.cancel() }
        for b in bs { b.close() }
    }

    // ------------------------------------------------------------ 监听 --

    /// 在 127.0.0.1 上开一个监听 socket，返回 fd。
    ///
    /// 端口被占用时抛错，而且错误里带上怎么办：这个项目不要求 root，
    /// 1024 以下的端口本来就绑不上，这两件事要分开说。
    static func bindLoopback(port: UInt16) throws -> Int32 {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw MacVMError("建不了 socket：\(String(cString: strerror(errno)))")
        }
        // SO_REUSEADDR：上一次的连接处于 TIME_WAIT 时也要能马上重绑，
        // 否则「停掉再起」会随机失败，而失败原因是一句看不懂的 "Address
        // already in use"。
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        // inet_addr 返回的已经是网络字节序，别再 htonl 一次。
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafeMutablePointer(to: &addr) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if bound != 0 {
            let why = String(cString: strerror(errno))
            close(fd)
            var hint = ""
            if errno == EADDRINUSE {
                hint = "\n这个端口已经被别的程序占了。换一个端口，"
                    + "或者先停掉那个程序（lsof -nP -iTCP:\(port) 能看出是谁）。"
            } else if errno == EACCES {
                hint = "\n1024 以下的端口要 root 权限，而这个工具不要求以 root 跑——"
                    + "换一个 1024 以上的端口。"
            }
            throw MacVMError("宿主机端口 \(port) 绑不上：\(why)。\(hint)")
        }
        guard listen(fd, 128) == 0 else {
            let why = String(cString: strerror(errno))
            close(fd)
            throw MacVMError("宿主机端口 \(port) listen 失败：\(why)")
        }
        // 非阻塞：accept 循环要把队列里排队的连接一次收干净。阻塞的 listen fd
        // 会让第二次 accept 挂住，而 DispatchSourceRead 是电平触发的——
        // 挂着不放，这个队列上别的事件（包括 stop）就全堵死了。
        let flags = fcntl(fd, F_GETFL, 0)
        _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)
        return fd
    }

    /// 把监听 fd 上排队的连接一次收干净。
    private func acceptOne(listenFD fd: Int32, guestPort: UInt32) {
        var accepted = 0
        while true {
            let client = accept(fd, nil, nil)
            if client < 0 {
                // 这几个 errno 是「没有更多连接了」而不是出错，别报警：
                //   EAGAIN/EWOULDBLOCK —— 非阻塞 listen fd 收完之后的常态；
                //   ECONNABORTED —— 客户端连上又立刻断开（扫端口、超时重试）；
                //   EINTR —— 被信号打断，重试即可。
                // 原来不管三七二十一都打一句 warn，表现是日志里周期性刷
                // 「accept 失败：Resource temporarily unavailable」，
                // 看着像坏了，其实一切正常。
                let benign = errno == EAGAIN || errno == EWOULDBLOCK
                    || errno == ECONNABORTED || errno == EINTR
                if !benign, accepted == 0 {
                    // accept 失败必须说。静默失败的症状是「端口听着但连上去
                    // 就断」，而日志里一个字都没有。
                    MacEmit.log("accept 失败：\(String(cString: strerror(errno)))",
                                level: "warn")
                }
                break
            }
            accepted += 1
            // SSH 是交互式的：小包也要马上发出去，不然敲一个字符顿一下。
            var yes: Int32 = 1
            setsockopt(client, IPPROTO_TCP, TCP_NODELAY, &yes,
                       socklen_t(MemoryLayout<Int32>.size))
            connect(client: client, guestPort: guestPort, attempt: 0)
        }
    }

    // ------------------------------------------------------------ 连接 --

    /// 把一条客户端连接送到客人机的 vsock 端口上，失败重试。
    private func connect(client: Int32, guestPort: UInt32, attempt: Int) {
        lock.lock()
        let dev = device
        let isStopped = stopped
        lock.unlock()
        if isStopped || dev == nil {
            close(client)
            return
        }
        guard let dev else { close(client); return }

        // 只认第一个回调。看门狗超时之后旧的那次 connect 万一又成功了，
        // 会多出一条没人管的连接——客人机那端要等到超时才断，而用户看到的是
        // 「连上了但没反应」。所以迟到的成功要立刻关掉。
        let finishLock = NSLock()
        var finished = false

        // 看门狗：Apple 头文件说客人机没监听时这个调用「does nothing」，
        // completion 永远不来。干等会把客户端挂死，所以到点也算一次失败。
        let watchdog = DispatchWorkItem { [weak self] in
            finishLock.lock()
            let first = !finished
            finished = true
            finishLock.unlock()
            guard first, let self else { return }
            self.retry(client: client, guestPort: guestPort, attempt: attempt,
                       reason: "客人机还没监听 vsock 端口 \(guestPort)")
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(Self.retryIntervalMs),
                                          execute: watchdog)

        // VZVirtioSocketDevice 必须在 VM 的队列上 touch，见类注释坑2。
        // 这个 completion 由 VZ 在调用时所用的队列上回调，也就是下面这条
        // vmQueue——所以里面直接 close() 连接对象仍然满足「在 vmQueue 上」。
        vmQueue.async { [weak self] in
            dev.__connect(toPort: guestPort) { conn, err in
                finishLock.lock()
                let first = !finished
                finished = true
                finishLock.unlock()
                watchdog.cancel()
                guard first else {
                    // 迟到的成功：没人要这条连接，关掉别泄漏。
                    conn?.close()
                    return
                }
                guard let self else {
                    // 代理已经没了（VM 停了）。两边都要关，各漏一个 fd 的
                    // 症状是「用着用着 descriptor 不够了」。
                    conn?.close()
                    close(client)
                    return
                }
                if self.isStopped {
                    // stop() 之后迟到的成功：VM 都没了，这条连接没有对端可接。
                    conn?.close()
                    close(client)
                    return
                }
                if let conn {
                    self.bridge(client: client, connection: conn)
                } else {
                    self.retry(client: client, guestPort: guestPort, attempt: attempt,
                               reason: err?.localizedDescription ?? "没给原因")
                }
            }
        }
    }

    /// 代理是不是已经停了。stop() 之后迟到的 connect 回调要靠它把连接收掉。
    private var isStopped: Bool {
        lock.lock(); defer { lock.unlock() }; return stopped
    }

    private func retry(client: Int32, guestPort: UInt32, attempt: Int, reason: String) {
        // 客户端已经走了就别再等：等客人机就绪是把一条没人要的连接挂在那儿，
        // 而挂着的连接会让客人机那侧的服务一直开着。
        if Self.clientGone(client) {
            MacEmit.log("客户端在客人机就绪之前就断开了（vsock \(guestPort)）")
            close(client)
            return
        }
        guard attempt < Self.maxRetries else {
            MacEmit.log("等客人机监听 vsock 端口 \(guestPort) 等不到"
                + "（等了 \(Self.totalWaitSeconds) 秒，最后一句：\(reason)），"
                + "这条连接放弃。", level: "warn")
            close(client)
            return
        }
        if attempt == 0 {
            // 说清楚在等什么。用户看到的是「ssh 卡住不动」，不知道卡在哪。
            MacEmit.log("客人机还没监听 vsock 端口 \(guestPort)（\(reason)）。"
                + "先挂着这条连接等它，最多 \(Self.totalWaitSeconds) 秒"
                + "——客户端看到的是握手慢，不是连接被拒。")
        }
        DispatchQueue.global().asyncAfter(
            deadline: .now() + .milliseconds(Self.retryIntervalMs)) {
            [weak self] in
            self?.connect(client: client, guestPort: guestPort, attempt: attempt + 1)
        }
    }

    /// 客户端是不是已经走了。用 TCP 连接状态而不是 peek：SSH 客户端会先发
    /// banner，未读的数据会把 FIN 挡住，peek 看不出来（vzvm 同一个坑）。
    private static func clientGone(_ fd: Int32) -> Bool {
        var info = tcp_connection_info()
        var size = socklen_t(MemoryLayout<tcp_connection_info>.size)
        guard getsockopt(fd, IPPROTO_TCP, TCP_CONNECTION_INFO, &info, &size) == 0 else {
            return true     // 问不出来就当走了：宁可早断，别挂着一个死连接
        }
        return info.tcpi_state != TCPS_ESTABLISHED
    }

    // ------------------------------------------------------------ 桥 --

    private func bridge(client: Int32, connection: VZVirtioSocketConnection) {
        let b = Bridge(tcpFD: client, connection: connection, vmQueue: vmQueue) {
            [weak self] finished in
            guard let self else { return }
            self.lock.lock()
            self.bridges.remove(finished)
            self.lock.unlock()
        }
        lock.lock()
        bridges.insert(b)
        lock.unlock()
        b.start()
    }
}

/// 把一条 TCP 连接和一条 vsock 连接对着抽，两个方向各一条 DispatchIO。
///
/// 单独一个类而不是闭包里硬写：它要自己把自己留在 MacVsockProxy 的集合里
/// （两个方向都结束了才摘），还要在 VM 队列上关 VZ 的连接对象。
private final class Bridge: Hashable {
    private let tcpFD: Int32
    /// 强引用：这个对象被释放时会自动关掉它的 fd，所以必须活到桥结束。
    private let connection: VZVirtioSocketConnection
    /// dup 出来的副本。DispatchIO 的清理处理器会关一次 fd，而那个 fd 归
    /// VZVirtioSocketConnection 所有——两边都关就是 double close，之后新
    /// accept 到的连接会拿到这个已经关掉的 fd 号，读写全错。vzvm 的 Bridge
    /// 就是这么处理的。
    private let vsockFD: Int32
    private let vmQueue: DispatchQueue
    private let queue = DispatchQueue(label: "macvm.vsock.bridge")
    private let onFinish: (Bridge) -> Void
    private var tcpIO: DispatchIO?
    private var vsockIO: DispatchIO?
    private var closed = false

    init(tcpFD: Int32, connection: VZVirtioSocketConnection,
         vmQueue: DispatchQueue, onFinish: @escaping (Bridge) -> Void) {
        self.tcpFD = tcpFD
        self.connection = connection
        self.vmQueue = vmQueue
        self.vsockFD = dup(connection.fileDescriptor)
        self.onFinish = onFinish
    }

    func start() {
        guard vsockFD >= 0 else {
            close()
            return
        }
        let tcp = DispatchIO(type: .stream, fileDescriptor: tcpFD, queue: queue) { _ in
            Darwin.close(self.tcpFD)
        }
        let vsock = DispatchIO(type: .stream, fileDescriptor: vsockFD, queue: queue) { _ in
            Darwin.close(self.vsockFD)
        }
        // 有字节就转发，不等缓冲区填满——SSH 敲一个字符也要马上过去。
        tcp.setLimit(lowWater: 1)
        vsock.setLimit(lowWater: 1)
        tcpIO = tcp
        vsockIO = vsock
        pump(from: tcp, to: vsock)
        pump(from: vsock, to: tcp)
    }

    private func pump(from source: DispatchIO, to sink: DispatchIO) {
        // 这两个变量只在本队列上碰（read/write 的 handler 都跑在 queue 上）。
        var writesInFlight = 0
        var sourceDone = false
        // 任一侧读完就整个拆掉。**不能做半关闭**：半关闭不会翻译成 vsock 的
        // 关闭语义，客人机那侧的服务要等到超时才断——症状是 ssh 退出后
        // 客人机里那个 sshd 会话还挂着。vzvm 在同一个地方留了同样的注释。
        let teardown: () -> Void = { [weak self] in
            if sourceDone && writesInFlight == 0 { self?.close() }
        }
        source.read(offset: 0, length: Int.max, queue: queue) { [weak self] done, data, error in
            guard let self else { return }
            if let data, !data.isEmpty {
                writesInFlight += 1
                sink.write(offset: 0, data: data, queue: self.queue) { written, _, writeError in
                    // 写不动了就别等了：这条链路已经坏了，整个拆掉。
                    if writeError != 0 { self.close(); return }
                    if written {
                        writesInFlight -= 1
                        teardown()
                    }
                }
            }
            if done || error != 0 {
                sourceDone = true
                teardown()
            }
        }
    }

    func close() {
        queue.async {
            guard !self.closed else { return }
            self.closed = true
            self.tcpIO?.close(flags: .stop)
            self.vsockIO?.close(flags: .stop)
            // VZ 的对象也要在 VM 的队列上 touch，见 MacVsockProxy 类注释坑2。
            self.vmQueue.async { self.connection.close() }
            self.onFinish(self)
        }
    }

    static func == (lhs: Bridge, rhs: Bridge) -> Bool { lhs === rhs }
    func hash(into hasher: inout Hasher) { hasher.combine(ObjectIdentifier(self)) }
}

/// 启动前确认要转发的宿主机端口都空着。
///
/// 为什么单独做一次预检：代理自己 bind 失败时 VM 已经起来了，那时候只能记
/// 一笔 warn——用户看到的是「启动成功了但转发不通」，而真实原因是端口撞了。
/// 在这儿失败，启动压根不开始，错误直接到用户脸上。
///
/// 做法就是真的 bind 一次再关掉：SO_REUSEADDR 下这个检查和代理随后那次 bind
/// 等价（都允许 TIME_WAIT，都不允许已被 listen 的端口），没有「检查通过但
/// 随后绑不上」的口子。
func macAssertPortsFree(_ forwards: [MacVMForward]) throws {
    for f in forwards {
        let fd = try MacVsockProxy.bindLoopback(port: UInt16(f.hostPort))
        close(fd)
    }
}

// MARK: - 磁盘空间 --

/// 装之前确认这个卷装得下。不够就明确说差多少，别让用户白等。
///
/// 为什么要有：这条路上原来唯一和空间有关的动作是建盘时的
/// `fh.truncate(atOffset:)`——稀疏盘 truncate 不分配块，所以它永远成功，
/// 真正的失败发生在下载写到 90%（ENOSPC）或者安装过程的中途。那时用户
/// 已经等了几十分钟，而「空间不够」这件事在开始之前花一次 statfs 就知道。
///
/// 估法：镜像（本地文件按精确大小；远程按 UniversalMac IPSW 的实测值
/// 25.6 GB 估）+ 系统盘标称大小。快照和克隆走 clonefile，底册共用，
/// 不额外算。
func macAssertFreeSpace(ipsw: UInt64, disk: UInt64) throws {
    let root = MacVMPaths.root
    let free = (try? root.resourceValues(forKeys: [.volumeAvailableCapacityKey]))
        .flatMap { $0.volumeAvailableCapacity } ?? 0
    // 问不出来（只读挂载、网络卷）就不拦：误拦比漏拦烦人，
    // 后面的下载和建盘会给出真正的错误。
    guard free > 0 else { return }
    let need = ipsw + disk
    guard UInt64(free) >= need else {
        throw MacVMError("磁盘空间不够：还需要约 \(macHuman(need))"
            + "（镜像 \(macHuman(ipsw)) + 系统盘 \(macHuman(disk))），"
            + "而 \(root.path) 所在的卷只剩 \(macHuman(UInt64(free)))。\n"
            + "清出空间再装，或者把 --disk 改小。")
    }
}

// MARK: - 安装 --

/// 装一个 macOS 虚拟机：下 IPSW（或复用本地/已下的）→ 建盘 → 跑安装器。
///
/// 和 Windows/Linux 那条路最大的不同：**安装必须在能看到画面的进程里跑**。
/// QEMU 那边可以把安装甩后台、app 关掉照样装；VZMacOSInstaller 需要
/// 一个活的 VZVirtualMachine，而这个对象不能跨进程传递。所以 macOS 的
/// 安装要么在 app 里跑（用户别关窗口），要么在 `vmz install` 那个终端里跑。
/// 这一点必须在界面和文档里说清楚，不能让用户以为关了 app 还在装。
func macInstall(spec: MacVMSpec, ipswLocal: String?,
                onProgress: @escaping (String, Double) -> Void) throws -> MacVMSpec {
    var spec = spec
    let name = spec.name
    try MacVMPaths.validateName(name)
    let dir = MacVMPaths.dir(name)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

    // ---- 1. 确定 IPSW 在哪儿，必要时先下 ----
    var ipswPath: URL

    if let local = ipswLocal, !local.isEmpty {
        ipswPath = URL(fileURLWithPath: (local as NSString).expandingTildeInPath)
        guard FileManager.default.fileExists(atPath: ipswPath.path) else {
            throw MacVMError("找不到 IPSW：\(ipswPath.path)")
        }
        // 本地镜像的大小是精确的，照它算。
        let ipswSize = (try? FileManager.default
            .attributesOfItem(atPath: ipswPath.path)[.size] as? UInt64) ?? 0
        try macAssertFreeSpace(ipsw: ipswSize, disk: macParseSize(spec.disk))
        MacEmit.stage("catalog", "读取本地恢复镜像")
        // 用户自己那份也登记进全局缓存：硬链一份过去，0 字节，
        // 而下一台 macOS 虚拟机就不用再下 25.6 GB。原文件不动。
        macRegisterIPSWInCache(from: ipswPath)
    } else {
        // 先问 Apple 要一份最新受支持的镜像地址。这个调用要 entitlement，
        // 没签名（或签了但没有 com.apple.security.virtualization）时返回的是
        // 「restore image catalog failed to load」——一句看不出原因的话，
        // 所以失败时把排查方向一起说出来。
        MacEmit.stage("catalog", "查询可装的 macOS 版本")
        let fetched = try macFetchLatestRestoreImage()
        // Swift 6 把 ObjC 的 URL 属性重命名成 url。
        let remote = fetched.url
        // 先看缓存，再决定要不要下。缓存命中时这里已经把镜像链到 VM 目录了，
        // macDownloadedBytes 随之非零——第二台 macOS 虚拟机因此一整段下载
        // 连开始都不会开始，省下 25.6 GB 和二十来分钟。
        ipswPath = macLocateIPSW(name: name, file: remote.lastPathComponent)
        // 下载之前先确认装得下。已经下过的那份镜像不占新空间（ipsw: 0）；
        // 没下过的按 UniversalMac 的实测大小 25.6 GB 往上取整成 26 GB 估
        // ——远程拿不到精确大小（要 HEAD 一次，而 macFetchIPSW 自己会问），
        // 宁可多估，也别让用户下到 90% 才失败。
        try macAssertFreeSpace(
            ipsw: macDownloadedBytes(ipswPath) > 0 ? 0 : 26 * 1024 * 1024 * 1024,
            disk: macParseSize(spec.disk))
        if macDownloadedBytes(ipswPath) == 0 {
            MacEmit.stage("download", "下载 \(remote.lastPathComponent)")
            // 单连接，不是并发：实测 Apple CDN 上加连接只会把每条的份额摊薄，
            // 聚合不涨（依据见 macFetchWithCurl 的注释）。
            MacEmit.log("单连接下载，断点续传。中断后重跑会接着下。", level: "info")
            let t0 = Date()
            try macFetchIPSW(remote, dest: ipswPath) { frac, got, total, speed in
                let left = speed > 0 ? Double(total - got) / speed : 0
                MacEmit.progress("download", frac: frac, bytes: got, total: total, speed: speed)
                if Int(frac * 100) % 10 == 0 {
                    MacEmit.log("已下 \(macHuman(UInt64(got))) / \(macHuman(UInt64(total)))"
                        + "（\(String(format: "%.1f", speed / 1e6)) MB/s"
                        + "，预计还要 \(Int(left / 60)) 分 \(Int(left) % 60) 秒）", level: "info")
                }
            }
            MacEmit.log("下载完成，用了 \(Int(Date().timeIntervalSince(t0) / 60)) 分钟", level: "info")
            // 下完了登记进全局缓存。失败只是少省一点空间，不影响这次安装。
            macRegisterIPSWInCache(from: ipswPath)
        }
    }
    onProgress("读取恢复镜像", 0.35)

    // ---- 2. 从 IPSW 里取出硬件模型 ----
    // 必须从本地文件重新加载：fetchLatestSupported 返回的那个对象 URL 是
    // 网络地址，而 VZMacOSInstaller 和 hardwareModel 都要本地文件。
    MacEmit.stage("hardware", "读取硬件模型")
    let image = try macLoadRestoreImageSync(ipswPath)
    guard let requirement = image.mostFeaturefulSupportedConfiguration else {
        throw MacVMError("这台 Mac 不支持这个恢复镜像里的任何硬件模型。")
    }
    spec.build = image.buildVersion
    // 低于最低配置的，VZ 会给出很难懂的行为（起不来、或起来就 panic）。
    // 在这里就说清楚，别让用户等 20 分钟下载完才失败。
    if spec.cpus < requirement.minimumSupportedCPUCount {
        MacEmit.log("CPU 从 \(spec.cpus) 提到 \(requirement.minimumSupportedCPUCount)"
            + "（这个 macOS 版本的最低要求）", level: "warn")
        spec.cpus = requirement.minimumSupportedCPUCount
    }
    let minMem = requirement.minimumSupportedMemorySize
    if macParseSize(spec.ram) < minMem {
        // 提到「整数 GiB」再拼成 "4G" 这样的串。
        //
        // 原来是 macHuman() + 把 " GB" 替换成 "G"：ByteCountFormatter 的
        // .file 风格是**十进制**单位，4 GiB 会显示成 "4.29 GB"，替换之后
        // macParseSize 又按 1024³ 解析——同一个数字在两种单位制里各走一遍，
        // 客人机实际拿到 4.29 GiB，而日志和界面上那句 "4.29G" 说的是另一回事。
        // 向上取整到整 GiB，显示值和 VZ 真正拿到的字节数就是同一个数。
        let gib = (minMem + 1024 * 1024 * 1024 - 1) / (1024 * 1024 * 1024)
        let want = "\(gib)G"
        MacEmit.log("内存从 \(spec.ram) 提到 \(want)"
            + "（这个 macOS 版本的最低要求）", level: "warn")
        spec.ram = want
    }
    try Data(requirement.hardwareModel.dataRepresentation)
        .write(to: MacVMPaths.hardwareModel(name), options: .atomic)

    // ---- 3. 机器标识与 NVRAM ----
    // 每个 VM 必须唯一。克隆时如果照抄这两个文件，两台机器在 macOS 眼里
    // 是同一台，iCloud、激活、Focus 模式会串——而且 Apple 文档明说
    // 同一个 identifier 并发运行是未定义行为。
    if !FileManager.default.fileExists(atPath: MacVMPaths.machineID(name).path) {
        try Data(VZMacMachineIdentifier().dataRepresentation)
            .write(to: MacVMPaths.machineID(name), options: .atomic)
    }
    if !FileManager.default.fileExists(atPath: MacVMPaths.auxStorage(name).path) {
        _ = try VZMacAuxiliaryStorage(creatingStorageAt: MacVMPaths.auxStorage(name),
                                      hardwareModel: requirement.hardwareModel,
                                      options: [])
    }

    // ---- 4. 系统盘 ----
    let diskURL = MacVMPaths.disk(name)
    let want = macParseSize(spec.disk)
    let have = (try? FileManager.default.attributesOfItem(atPath: diskURL.path)[.size] as? UInt64) ?? 0
    if have != want {
        if have > 0 {
            // 已有一块不同大小的盘。直接 truncate 会把文件系统搞坏
            // （APFS 容器大小记在盘里），只能删掉重来——所以先说清数据会没。
            throw MacVMError("系统盘已是 \(macHuman(have))，与要求的 \(macHuman(want)) 不符。\n"
                + "改已装好虚拟机的大小会破坏里面的文件系统，请删掉重装。")
        }
        FileManager.default.createFile(atPath: diskURL.path, contents: nil)
        let fh = try FileHandle(forWritingTo: diskURL)
        try fh.truncate(atOffset: want)
        try fh.close()
        MacEmit.log("建系统盘 \(macHuman(want))（稀疏，实际占用随写入增长）", level: "info")
    }

    try macSaveSpec(spec)
    onProgress("安装 macOS（这一步要十几分钟，别关窗口）", 0.4)

    // ---- 5. 跑安装器 ----
    let cfg = try macMakeConfiguration(spec: spec)
    try cfg.validate()
    // 专用队列：不带 queue 的那个用主队列，而下面同步等 install 的 completion
    // 会让主队列没人服务（CLI 里压根没有 dispatchMain 在服务它）。
    let vmQueue = DispatchQueue(label: "macvm.install", qos: .userInitiated)
    let machine = VZVirtualMachine(configuration: cfg, queue: vmQueue)
    let box = Box()
    // installer 的 init 也必须在 vmQueue 上跑，不能留在主线程。
    //
    // VZMacOSInstaller.init(virtualMachine:restoringFromImageAt:) 内部有一道
    // dispatch_assert_queue，断言「当前队列 == virtualMachine 的队列」。在别的
    // 队列上调它（包括主线程）不会返回 error，而是直接 brk → SIGTRAP 崩掉：
    // 2026-11-10 实测，vmz 在打完「建系统盘」之后消失，install.log 里一个错误
    // 都没有，只有 DiagnosticReports/vmz-2026-11-10-140247.ips，
    // 栈顶是 dispatch_assert_queue，下一帧就是 initWithVirtualMachine:。
    // 崩溃点在 VZ 内部，所以 Swift 侧没有任何可 catch 的东西。
    //
    // 此时 vmQueue 是空闲的（machine 刚建好、还没派活），sync 不会死锁。
    var installer: VZMacOSInstaller!
    vmQueue.sync {
        installer = VZMacOSInstaller(virtualMachine: machine,
                                     restoringFromImageAt: ipswPath)
    }
    MacEmit.stage("install", "正在安装 macOS \(spec.build)")
    // 进度只有开始和结束两个点：VZMacOSInstaller 只暴露 fractionCompleted，
    // 而它在整个恢复过程里几乎不动。所以这里报的是阶段而不是百分比——
    // 报一个假的百分比比不报更糟。
    let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    timer.schedule(deadline: .now() + 30, repeating: 30)
    var elapsed = 0
    timer.setEventHandler {
        elapsed += 30
        MacEmit.log("安装中…已过 \(elapsed / 60) 分 \(elapsed % 60) 秒"
            + "（进度 \(Int(installer.progress.fractionCompleted * 100))%）", level: "info")
    }
    timer.resume()
    // install 本身也要在 vmQueue 上发起。init 有队列断言，Apple 对
    // installWithCompletionHandler: 的要求是同一套： completion handler 跑在
    // VM 的队列上，从别的队列调它是未定义行为。用 async 而不是 sync——
    // completion 就在这个队列上，sync 会自己等自己。
    vmQueue.async {
        installer.install { result in
            timer.cancel()
            if case .failure(let e) = result { box.err = e }
            box.sem.signal()
        }
    }
    box.sem.wait()
    // 两条路都要先优雅停机。原来只在成功路径上 stop：装到一半失败时直接
    // throw → 进程退出 → 客人机是「拔电源」式死亡，APFS 留下未干净日志，
    // 下次启动跑好几分钟 fsck。
    //
    // canStop 和 stop **都**必须在 vmQueue 上。VZVirtualMachine 的属性和方法
    // 内部都有 dispatch_assert_queue，断言当前队列是创建它时给的那个队列，
    // 在主线程调它们和调 VZMacOSInstaller.init 一样直接 SIGTRAP。实测
    // （2026-11-10）：安装跑到 90% 崩在 -[VZVirtualMachine canStop]，
    // 栈顶是 dispatch_assert_queue——离装完只差一步，而日志里最后一个字
    // 是「进度 90%」，没有任何错误。
    let _ = vzWait(vmQueue) { done in
        if machine.canStop {
            machine.stop { _ in done(nil) }
        } else {
            done(nil)
        }
    }
    if let e = box.err {
        // 装到一半失败时把 IPSW 留着：25 GB 再下一遍太痛，重跑能直接复用。
        throw MacVMError("安装失败：\(e.localizedDescription)\n"
            + "已下载的镜像保留在 \(ipswPath.path)，重跑会复用。")
    }

    spec.installed = true
    try macSaveSpec(spec)
    MacEmit.log("macOS 装好了。第一次启动会自动建账号并打开远程登录（SSH）。", level: "info")
    return spec
}

/// 同步加载一个本地 IPSW。
private func macLoadRestoreImageSync(_ path: URL) throws -> VZMacOSRestoreImage {
    let box = RestoreBox()
    VZMacOSRestoreImage.load(from: path) { result in
        switch result {
        case .success(let img): box.image = img
        case .failure(let e): box.err = e
        }
        box.sem.signal()
    }
    box.sem.wait()
    if let e = box.err { throw MacVMError("读不了这个 IPSW：\(e.localizedDescription)") }
    guard let img = box.image else { throw MacVMError("读不了这个 IPSW（没有返回信息）") }
    return img
}

private final class RestoreBox {
    let sem = DispatchSemaphore(value: 0)
    var image: VZMacOSRestoreImage?
    var err: Error?
}

/// 问 Apple 要一份这台 Mac 能装的最新 macOS 恢复镜像信息。
func macFetchLatestRestoreImage() throws -> VZMacOSRestoreImage {
    let box = RestoreBox()
    VZMacOSRestoreImage.fetchLatestSupported { result in
        switch result {
        case .success(let img): box.image = img
        case .failure(let e): box.err = e
        }
        box.sem.signal()
    }
    box.sem.wait()
    if let e = box.err {
        throw MacVMError("拿不到 macOS 版本目录：\(e.localizedDescription)\n"
            + "这一条要 com.apple.security.virtualization entitlement，"
            + "签名里没有它就是这个错。手动编的话检查 codesign 的 --entitlements。")
    }
    guard let img = box.image else { throw MacVMError("拿不到 macOS 版本目录（没有返回镜像信息）") }
    return img
}

// MARK: - 快照与克隆 --

/// 拍一张快照：整盘 clonefile 到 snapshots/<tag>.img。
///
/// VZ 没有内部快照 API（QEMU 的 qcow2 snapshot 那一套在 macOS 客人机上
/// 不存在），所以用 APFS 的 clonefile：复制的是元数据，实际块照旧共用，
/// 秒级完成且几乎不占额外空间。qcow2 overlay 克隆在 QEMU 那边是同一个思路，
/// 这里换成 APFS 原生能力。
///
/// 前提是 VM 必须关机。运行中 clonefile 一份盘，得到的是「磁盘某个中间状态」，
/// 而客人机缓存里还有没落盘的东西——回滚到那一张会丢数据，不如不拍。
func macSnapshotCreate(_ name: String, tag: String) throws {
    try MacVMPaths.validateName(name)
    try MacVMPaths.validateTag(tag)
    guard macReadPID(name) == nil else {
        throw MacVMError("\(name) 正在运行。快照要在关机状态拍，不然回滚会丢数据。")
    }
    let disk = MacVMPaths.disk(name)
    guard FileManager.default.fileExists(atPath: disk.path) else {
        throw MacVMError("找不到系统盘，这个虚拟机可能还没装完。")
    }
    let dir = MacVMPaths.snapshots(name)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let dst = dir.appendingPathComponent("\(tag).img")
    try MacVMPaths.assertInside(dst, dir, what: "快照 \(tag)")
    if FileManager.default.fileExists(atPath: dst.path) {
        throw MacVMError("快照 \(tag) 已经存在。换个名字，或先删掉它。")
    }
    // clonefile 要求目标不存在。
    if clonefile(disk.path, dst.path, 0) != 0 {
        throw MacVMError("拍快照失败：\(String(cString: strerror(errno)))")
    }
    MacEmit.log("快照 \(tag) 已拍（\(macHuman(macDiskUsed(dst)))）", level: "info")
}

func macSnapshotDelete(_ name: String, tag: String) throws {
    try MacVMPaths.validateName(name)
    // tag 也走一次白名单：它会拼成文件名。快照名是用户输的，
    // `../../etc/passwd` 这种必须挡在入口。
    try MacVMPaths.validateTag(tag)
    let dst = MacVMPaths.snapshots(name).appendingPathComponent("\(tag).img")
    guard FileManager.default.fileExists(atPath: dst.path) else {
        throw MacVMError("没有叫 \(tag) 的快照")
    }
    try FileManager.default.removeItem(at: dst)
    MacEmit.log("快照 \(tag) 已删", level: "info")
}

func macSnapshotApply(_ name: String, tag: String) throws {
    try MacVMPaths.validateName(name)
    guard macReadPID(name) == nil else {
        throw MacVMError("\(name) 正在运行。先关机再回滚。")
    }
    try MacVMPaths.validateTag(tag)
    let src = MacVMPaths.snapshots(name).appendingPathComponent("\(tag).img")
    guard FileManager.default.fileExists(atPath: src.path) else {
        throw MacVMError("没有叫 \(tag) 的快照")
    }
    // 分两步，顺序是「先把活盘改名成备份，再让副本改回来」。
    //
    // 原来是「先删活盘再 clonefile」：clonefile 一失败（空间不够、权限、
    // 快照文件损坏）系统盘就直接没了，而它是唯一一份——回滚失败比不回滚
    // 糟得多。后来改成「删活盘 → move 副本」，仍然有窗口期：两步之间
    // disk.img 这个文件名不存在，此刻断电 = 盘没了。
    // 改名不删数据：任何一步失败，最坏的情况是盘躺在 .prev 里，
    // 用户改个名就能回去。
    let dst = MacVMPaths.disk(name)
    guard FileManager.default.fileExists(atPath: dst.path) else {
        throw MacVMError("找不到系统盘（\(dst.lastPathComponent)），这个虚拟机可能还没装完。")
    }
    let tmp = MacVMPaths.dir(name).appendingPathComponent("disk.img.rollback")
    // 上一回回滚到一半留下的临时文件。留着会碍事（clonefile 要求目标不存在）。
    try? FileManager.default.removeItem(at: tmp)
    if clonefile(src.path, tmp.path, 0) != 0 {
        let e = String(cString: strerror(errno))
        try? FileManager.default.removeItem(at: tmp)
        throw MacVMError("回滚失败：\(e)。系统盘没有被改动。")
    }
    let backup = MacVMPaths.dir(name).appendingPathComponent("disk.img.prev")
    try? FileManager.default.removeItem(at: backup)
    do {
        try FileManager.default.moveItem(at: dst, to: backup)
        try FileManager.default.moveItem(at: tmp, to: dst)
    } catch {
        // 把活盘改回原名。第一步就失败的话 backup 还不存在，这次 move 会
        // 白白失败，无所谓；第二步失败的话它真能把盘救回来。
        try? FileManager.default.moveItem(at: backup, to: dst)
        throw MacVMError("回滚到 \(dst.path) 失败：\(error.localizedDescription)。"
            + "系统盘没有被改动，副本还在 \(tmp.path)。")
    }
    // 新盘已经上位，旧盘的备份可以删了。删不掉也只多一个没人看的 .prev，
    // 比回滚失败好。
    try? FileManager.default.removeItem(at: backup)
    MacEmit.log("已回滚到快照 \(tag)", level: "info")
}

func macSnapshotList(_ name: String) -> [[String: Any]] {
    // list 也要校验名字。原来只有 create/apply/delete 校验：
    // `snapshot list --name ../../etc` 会把目录外的 .img 条目当快照列出来。
    // 只读、危害有限，但白名单漏一个入口就等于没有。
    //
    // 必须是 do/catch 而不是 `try?`：静默丢掉错误的话，非法名字和「这台机器
    // 真的没有快照」在调用方眼里一模一样（都是空列表），用户永远不知道是
    // 名字写错了。
    do {
        try MacVMPaths.validateName(name)
    } catch let e as MacVMError {
        MacEmit.log(e.message, level: "warn")
        return []
    } catch {
        MacEmit.log(error.localizedDescription, level: "warn")
        return []
    }
    let dir = MacVMPaths.snapshots(name)
    guard let files = try? FileManager.default.contentsOfDirectory(
        at: dir, includingPropertiesForKeys: [.fileSizeKey, .creationDateKey]) else { return [] }
    return files
        .filter { $0.pathExtension == "img" }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
        .map { f in
            let size = (try? f.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            let created = (try? f.resourceValues(forKeys: [.creationDateKey]).creationDate)
                ?? Date.distantPast
            return ["tag": f.deletingPathExtension().lastPathComponent,
                    "size": ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file),
                    "date": ISO8601DateFormatter().string(from: created)]
        }
}

/// 磁盘真实占用。稀疏盘的 st_size 是逻辑大小，st_blocks * 512 才是分配的块。
/// vmkit 的 disk_used_bytes 用同一个算法，两边显示才一致。
func macDiskUsed(_ url: URL) -> UInt64 {
    // 用 stat() 而不是 FileManager 的属性字典：Foundation 没有暴露
    // st_blocks 的键（.systemBlocks 在 macOS SDK 上不存在），而
    // app 那侧 VMDisk.usedText 用的就是 stat——两边算法必须一致，
    // 不然同一个盘在界面两个地方显示两个数。
    var st = stat()
    guard stat(url.path, &st) == 0 else { return 0 }
    return UInt64(st.st_blocks) * 512
}

/// 克隆一个 macOS 虚拟机。
///
/// 系统盘走 clonefile（秒级、共用底册），但**机器标识和 NVRAM 必须重新生成**：
/// Apple 文档明说两台 VM 用同一个 machineIdentifier 是未定义行为，实际表现是
/// iCloud 账号互相顶、Handoff 串台、Focus 模式同步。克隆的意义就是「折腾坏了一份
/// 还有一份」，要是两份在系统眼里是同一台，这个意义就不存在。
func macClone(_ name: String, newName: String) throws {
    try MacVMPaths.validateName(name)
    try MacVMPaths.validateName(newName)
    guard macReadPID(name) == nil else {
        throw MacVMError("\(name) 正在运行。先关机再克隆。")
    }
    let spec = try macLoadSpec(name)
    guard spec.installed else {
        throw MacVMError("\(name) 还没装完，克隆一个空盘没有意义。")
    }
    let srcDisk = MacVMPaths.disk(name)
    guard FileManager.default.fileExists(atPath: srcDisk.path) else {
        throw MacVMError("找不到 \(name) 的系统盘")
    }
    let dstDir = MacVMPaths.dir(newName)
    if FileManager.default.fileExists(atPath: dstDir.path) {
        throw MacVMError("\(newName) 已存在。换个名字，或先删掉它。")
    }
    try FileManager.default.createDirectory(at: dstDir, withIntermediateDirectories: true)

    // 全程 try，中途失败就把半成品目录删掉。原来硬件模型那次写入用 try?：
    // 磁盘满时它静默失败，而函数照返成功，用户得到一台「已克隆」但启动时报
    // 「找不到硬件模型」的机器。
    do {
        if clonefile(srcDisk.path, MacVMPaths.disk(newName).path, 0) != 0 {
            throw MacVMError("克隆磁盘失败：\(String(cString: strerror(errno)))")
        }
        // 硬件模型照抄：同一份 macOS 恢复镜像出来的，本来就该一样。
        try Data(contentsOf: MacVMPaths.hardwareModel(name))
            .write(to: MacVMPaths.hardwareModel(newName))
        // 标识和 NVRAM 换新的。这是上面注释里说的那条。
        try Data(VZMacMachineIdentifier().dataRepresentation)
            .write(to: MacVMPaths.machineID(newName), options: .atomic)
        let hmData = try Data(contentsOf: MacVMPaths.hardwareModel(name))
        guard let hm = VZMacHardwareModel(dataRepresentation: hmData) else {
            throw MacVMError("源的硬件模型读不出来，没法给它配 NVRAM")
        }
        _ = try VZMacAuxiliaryStorage(creatingStorageAt: MacVMPaths.auxStorage(newName),
                                      hardwareModel: hm, options: [])
        var newSpec = spec
        newSpec.name = newName
        // 克隆出来的是同一套账号密码，但那是新机器上第一次启动，
        // provisioning 要再跑一遍才能建出账号来。
        newSpec.provisioned = false
        try macSaveSpec(newSpec)
    } catch {
        // 半成品留在目录里比没有更糟：它有一块能挂载的盘和一份配置，
        // 用户会反复尝试启动它，而每次都是同一个错误。
        try? FileManager.default.removeItem(at: dstDir)
        if let e = error as? MacVMError { throw e }
        throw MacVMError("克隆失败：\(error.localizedDescription)。已清理半成品。")
    }
    MacEmit.log("已克隆成 \(newName)（共用底册，几乎不占额外空间）", level: "info")
}
