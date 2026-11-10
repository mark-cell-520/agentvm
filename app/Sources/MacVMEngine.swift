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

    static func w(_ obj: [String: Any]) {
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

    enum CodingKeys: String, CodingKey {
        case name, cpus, ram, disk, user, password, provisioned, build
        case width, height, installed
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
}

// MARK: - 大小解析 --

/// "8G" / "512M" / "80G" → 字节。
/// 和 vmkit 的 parse_size 同一套语义：G 按 1024³，不是 10⁹。
func macParseSize(_ s: String) -> UInt64 {
    let t = s.trimmingCharacters(in: .whitespaces).uppercased()
    var mult: UInt64 = 1
    var num = t
    if t.hasSuffix("G") { mult = 1024 * 1024 * 1024; num = String(t.dropLast()) }
    else if t.hasSuffix("M") { mult = 1024 * 1024; num = String(t.dropLast()) }
    else if t.hasSuffix("T") { mult = 1024 * 1024 * 1024 * 1024; num = String(t.dropLast()) }
    else if t.hasSuffix("K") { mult = 1024; num = String(t.dropLast()) }
    let v = Double(num.trimmingCharacters(in: .whitespaces)) ?? 0
    return UInt64(v * Double(mult))
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
/// 才等于下完了。进行中的下载在 .part 里，由 checkpoint 文件记录进度。
func macDownloadedBytes(_ dest: URL) -> Int64 {
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: dest.path),
          let size = attrs[.size] as? UInt64 else { return 0 }
    return Int64(size)
}

/// 进行中的下载写到哪儿了。
///
/// checkpoint 文件里存的是「已确认完整落盘的字节数」，每 64 MB 更新一次
/// （不是每一段：25 GB / 8 MB = 3200 段，每段写一次元数据太碎）。
/// 中断时最多重下 64 MB——拿几秒钟换 3200 次小写入，值。
func macPartialBytes(_ part: URL) -> Int64 {
    let ck = part.appendingPathExtension("ckpt")
    guard let text = try? String(contentsOf: ck, encoding: .utf8),
          let v = Int64(text.trimmingCharacters(in: .whitespacesAndNewlines)),
          v > 0 else { return 0 }
    // 不能超过文件实际大小。checkpoint 比文件大说明上一次写坏了。
    let size = (try? FileManager.default.attributesOfItem(atPath: part.path)[.size] as? UInt64) ?? 0
    return min(v, Int64(size))
}

/// 装一次同步 HTTP 结果的盒子。Swift 的闭包捕获值语义在多线程下不好使，
/// 用一个类装可变状态最直白。
private final class Box {
    let sem = DispatchSemaphore(value: 0)
    var err: Error?
    var resp: URLResponse?
}

/// 多连接并发下载器。
///
/// 为什么要并发，单连接差多少：实测这台机器到 updates.cdn-apple.com
/// 单连接约 1 MB/s，6 个连接各 3.1 MB/s、聚合 18.8 MB/s。25.6 GB 的
/// UniversalMac IPSW 从 7 小时变成 23 分钟。curl 也是分段并发的，
/// 这里自己实现是为了能报总体进度、能断点续传。
///
/// 四个必须点，每一个都是实测踩出来的：
///   1. urlCache 必须设 nil。URLSession 默认把响应体写进磁盘缓存，
///      25 GB 的文件会占双倍空间——下载完看着 25 GB，实际吃了 51 GB。
///   2. 写 .part、完成才 rename。原来直接把 dest truncate 成完整大小，
///      而 `attributesOfItem[.size]` 是逻辑大小：稀疏文件 truncate 到
///      25.6 GB 之后 st_size 就是 25600000000 而 st_blocks 是 0。于是
///      中断后重跑会判定「已下完」，拿一个全是零洞的文件去装，报一句
///      「读不了这个 IPSW」而重跑多少次都一样。现在只有正式名算数。
///   3. 分段写盘要串行化，进度节流状态也要。FileHandle 的 seek+write 不是
///      线程安全的，8 个线程同时写会互相覆盖，症状是镜像校验失败而日志全绿。
///      节流用的 lastReportAt / lastReportBytes 同样在多线程下读写。
///   4. 不能把整个响应读进内存。服务端忽略 Range 时 dataTask 会把 25 GB
///      作为一个 Data 交给 completion，进程直接被 jetsam 杀，而日志里一个字
///      都没有。所以先用一个小 Range 探一次，确认服务端真的返回 206。
final class SegmentedDownloader: NSObject, URLSessionDataDelegate {
    private let dest: URL          // 正式文件（.part 完成后的 rename 目标）
    private let part: URL          // 进行中的文件
    private let fh: FileHandle
    private let lock = NSLock()
    private var doneBytes: Int64
    private let total: Int64
    private let startedAt = Date()
    private var lastReportAt = Date()
    private var lastReportBytes: Int64
    private let onProgress: (Double, Int64, Int64, Double) -> Void
    /// 每 64 MB 更新一次 checkpoint，见 macPartialBytes 的注释。
    private let ckptEvery: Int64 = 64 * 1024 * 1024
    private var nextCkpt: Int64
    /// 每一段已收到的字节数。URLSessionDataDelegate 是逐块回调的，
    /// 靠它拼出「这一段写完了没有」，写完了才能更新 doneBytes。
    private var segGot = 0
    private var segWant = 0
    private var segOffset = 0

    init(dest: URL, total: Int64, alreadyHave: Int64,
         onProgress: @escaping (Double, Int64, Int64, Double) -> Void) throws {
        self.dest = dest
        self.part = dest.appendingPathExtension("part")
        self.total = total
        self.doneBytes = alreadyHave
        self.lastReportBytes = alreadyHave
        self.nextCkpt = alreadyHave + 64 * 1024 * 1024
        self.onProgress = onProgress
        try FileManager.default.createDirectory(
            at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
        // .part 已存在就接着写，不存在就建一个稀疏文件。
        // 这一步仍然 truncate 到 total，但**正式名不存在**，所以
        // macDownloadedBytes 不会把进行中的下载误判成已下完。
        if !FileManager.default.fileExists(atPath: part.path) {
            FileManager.default.createFile(atPath: part.path, contents: nil)
            let h = try FileHandle(forWritingTo: part)
            try h.truncate(atOffset: UInt64(total))
            try h.close()
        }
        fh = try FileHandle(forUpdating: part)
    }

    deinit { try? fh.close() }

    func run(url: URL, connections: Int = 6, segmentSize: Int = 8 * 1024 * 1024) throws {
        // ---- 先探一次：服务端到底支不支持 Range ----
        // 不支持的话分段下载整个不成立。原来没这道检查，第一个分段就会把
        // 25 GB 整个读进内存（见类注释第 4 条）。探的代价是 1 KB。
        let probe = try Self.probeRange(url: url)
        guard probe else {
            throw MacVMError("下载服务端不支持断点续传（忽略 Range 请求），"
                             + "无法分段下载 \(macHuman(UInt64(total)))。")
        }

        var segments: [(offset: Int, end: Int)] = []
        var off = Int(doneBytes)
        while off < total {
            let end = min(off + segmentSize - 1, Int(total) - 1)
            segments.append((off, end))
            off = end + 1
        }
        if segments.isEmpty {
            finish()
            return
        }

        let group = DispatchGroup()
        // 并发上限。不是越多越好：实测 6 个连接就到 18.8 MB/s，
        // 再往上单连接速度反而掉（CDN 侧限流）。
        let limit = DispatchSemaphore(value: max(2, connections))
        let failLock = NSLock()
        var firstError: Error?

        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil          // 见类注释第 1 条
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 3600
        // delegate 必须给：要靠 urlSession(_:dataTask:didReceive:) 边收边写，
        // 不能让 completion 把整段 Data 交上来。
        let dq = OperationQueue()
        dq.maxConcurrentOperationCount = connections + 2
        let session = URLSession(configuration: config, delegate: self,
                                 delegateQueue: dq)
        defer { session.finishTasksAndInvalidate() }

        for seg in segments {
            failLock.lock()
            let failed = firstError != nil
            failLock.unlock()
            if failed { break }

            limit.wait()
            group.enter()
            var req = URLRequest(url: url)
            req.setValue("bytes=\(seg.offset)-\(seg.end)", forHTTPHeaderField: "Range")
            req.cachePolicy = .reloadIgnoringLocalCacheData
            session.dataTask(with: req) { [weak self] _, resp, err in
                defer { group.leave(); limit.signal() }
                guard let self else { return }
                func fail(_ e: Error) {
                    failLock.lock()
                    if firstError == nil { firstError = e }
                    failLock.unlock()
                }
                if let err { fail(err); return }
                guard let http = resp as? HTTPURLResponse else {
                    fail(MacVMError("下载服务端没有返回数据")); return
                }
                // 探活已经确认支持 Range，这里再见到 200 说明中间换了端点，
                // 照常写会把 25 GB 全塞进第一段的位置。
                if http.statusCode == 200 {
                    if seg.offset != 0 {
                        fail(MacVMError("下载服务端突然不支持 Range 了，无法继续"))
                        return
                    }
                } else if http.statusCode != 206 {
                    fail(MacVMError("下载服务端返回 HTTP \(http.statusCode)，无法继续"))
                    return
                }
                self.lock.lock()
                let got = self.segGot
                self.lock.unlock()
                if got != seg.end - seg.offset + 1 {
                    fail(MacVMError("第 \(seg.offset) 段少收了 \(seg.end - seg.offset + 1 - got) 字节"))
                    return
                }
            }.resume()
        }

        group.wait()

        failLock.lock()
        let err = firstError
        failLock.unlock()
        if let err { throw err }
        finish()
    }

    /// 收尾：确认大小、写 checkpoint、rename 成正式名。
    private func finish() {
        lock.lock()
        defer { lock.unlock() }
        let actual = (try? fh.offsetInFile) ?? 0
        try? fh.close()
        guard actual == UInt64(total) else {
            // 大小不对就留在 .part 里，下次重跑接着下。不 rename——
            // 一个不完整的正式文件会被 macDownloadedBytes 当成「已下完」，
            // 正是这个类要修的那个坑。
            return
        }
        try? FileManager.default.removeItem(at: dest)
        do {
            try FileManager.default.moveItem(at: part, to: dest)
        } catch {
            return
        }
        try? FileManager.default.removeItem(at: part.appendingPathExtension("ckpt"))
        onProgress(1.0, total, total, 0)
    }

    /// 探一下服务端支不支持 Range。要 1 KB，判 206。
    private static func probeRange(url: URL) throws -> Bool {
        var req = URLRequest(url: url)
        req.setValue("bytes=0-1023", forHTTPHeaderField: "Range")
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let cfg = URLSessionConfiguration.ephemeral
        cfg.urlCache = nil
        let box = Box()
        let session = URLSession(configuration: cfg)
        session.dataTask(with: req) { _, resp, err in
            box.err = err
            box.resp = resp
            box.sem.signal()
        }.resume()
        box.sem.wait()
        if box.err != nil { return false }
        return (box.resp as? HTTPURLResponse)?.statusCode == 206
    }

    // MARK: URLSessionDataDelegate —— 边收边写

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        lock.lock()
        let rng = (response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Range") ?? ""
        // "bytes 12345-23456/25595954706"
        let parts = rng.split(separator: " ")
        if parts.count == 2, let dash = parts[1].split(separator: "/").first {
            let b = dash.split(separator: "-")
            if b.count == 2, let s = Int(b[0]), let e = Int(b[1]) {
                segOffset = s
                segWant = e - s + 1
                segGot = 0
            }
        }
        lock.unlock()
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive data: Data) {
        lock.lock()
        // 内存峰值只有一段（8 MB）而不是整个文件。这是这个 delegate 存在的
        // 全部理由——dataTask 的 completion 形式会把整段 Data 交上来，
        // 服务端忽略 Range 时那就是 25 GB。
        do {
            try fh.seek(toFileOffset: UInt64(segOffset + segGot))
            try fh.write(contentsOf: data)
            segGot += data.count
            doneBytes += Int64(data.count)
            let done = doneBytes
            let needCkpt = done >= nextCkpt
            lock.unlock()
            if needCkpt { writeCkpt(done) }
            report(done: done)
        } catch {
            lock.unlock()
        }
    }

    /// 写 checkpoint。原子：先写临时文件再 rename。
    private func writeCkpt(_ done: Int64) {
        lock.lock()
        nextCkpt = done + ckptEvery
        lock.unlock()
        let ck = part.appendingPathExtension("ckpt")
        let tmp = ck.appendingPathExtension("tmp")
        try? Data("\(done)\n".utf8).write(to: tmp)
        try? FileManager.default.moveItem(at: tmp, to: ck)
    }

    /// 报进度。限流到每 0.4 秒一次，状态全在锁内：
    /// app 那侧每条事件都要过一次主线程，8 个连接每块都报会把界面刷爆；
    /// 而两个 completion 队列并发读写这几个字段会让进度条回跳，
    /// 甚至让两行 JSON 在 stdout 上交错（写 stdout 不是线程安全的）。
    private func report(done: Int64) {
        lock.lock()
        defer { lock.unlock() }
        let now = Date()
        guard now.timeIntervalSince(lastReportAt) >= 0.4 else { return }
        lastReportAt = now
        let elapsed = now.timeIntervalSince(startedAt)
        let speed = elapsed > 0 ? Double(done - lastReportBytes) / elapsed : 0
        lastReportBytes = done
        onProgress(total > 0 ? Double(done) / Double(total) : 0, done, total, speed)
    }
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
    }
    // 续传起点：正式名已经有的一部分（理论上不会，正式名只在下完时出现）
    // 加上 .part 里已确认落盘的部分。
    let resume = min(have, total) + macPartialBytes(part)
    let dl = try SegmentedDownloader(dest: dest, total: total, alreadyHave: resume,
                                     onProgress: onProgress)
    try dl.run(url: url)
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

    func start(spec: MacVMSpec, provision: Bool) throws {
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
        let machine = VZVirtualMachine(configuration: cfg, queue: vmQueue)
        machine.delegate = self
        self.vm = machine
        macWritePID(spec.name, Int32(ProcessInfo.processInfo.processIdentifier),
                    host: "app")

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
        DispatchQueue.main.async { self.state = machine.state }
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
    func stop() {
        guard let machine = vm else { return }
        guard machine.canStop else {
            // 已经停了（或者正在停）。收尾。
            vm = nil
            try? FileManager.default.removeItem(at: MacVMPaths.pidFile(specName))
            DispatchQueue.main.async { self.state = .stopped }
            return
        }
        DispatchQueue.main.async { self.state = .stopping }
        // 优雅停机。macOS 会收到关机事件、正常 flush 磁盘。
        // 强杀（kill -9）会让 APFS 留下未干净的日志，下次启动跑 fsck
        // 要好几分钟——跟 QEMU 那边 Windows 的坑同源。
        let box = Box()
        vmQueue.async {
            machine.stop { err in box.err = err; box.sem.signal() }
        }
        // 等，但在 vmQueue 之外的后台队列上等：调用方（app 主线程、CLI 信号
        // 队列）都不被堵住。
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            if box.sem.wait(timeout: .now() + 90) == .timedOut {
                MacEmit.log("优雅关机超时（客人机可能停在登录界面）。"
                    + "虚拟机仍在运行，可以再停一次，或者在客人机里选「关机」。",
                    level: "warn")
                return
            }
            guard let self else { return }
            self.vm = nil
            try? FileManager.default.removeItem(at: MacVMPaths.pidFile(self.specName))
            DispatchQueue.main.async { self.state = .stopped }
        }
    }

    // MARK: VZVirtualMachineDelegate

    func guestDidStop(_ vm: VZVirtualMachine) {
        DispatchQueue.main.async {
            self.state = .stopped
            self.vm = nil
        }
        try? FileManager.default.removeItem(at: MacVMPaths.pidFile(specName))
        onGuestStop?()
    }

    func vm(_ vm: VZVirtualMachine, didStopWithError error: Error) {
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
        MacEmit.stage("catalog", "读取本地恢复镜像")
    } else {
        // 先问 Apple 要一份最新受支持的镜像地址。这个调用要 entitlement，
        // 没签名（或签了但没有 com.apple.security.virtualization）时返回的是
        // 「restore image catalog failed to load」——一句看不出原因的话，
        // 所以失败时把排查方向一起说出来。
        MacEmit.stage("catalog", "查询可装的 macOS 版本")
        let fetched = try macFetchLatestRestoreImage()
        // Swift 6 把 ObjC 的 URL 属性重命名成 url。
        let remote = fetched.url
        ipswPath = MacVMPaths.ipsw(name, file: remote.lastPathComponent)
        if macDownloadedBytes(ipswPath) > 0 {
            MacEmit.log("复用已下载的 \(remote.lastPathComponent)", level: "info")
        } else {
            MacEmit.stage("download", "下载 \(remote.lastPathComponent)")
            MacEmit.log("多连接并发下载，断点续传。中断后重跑会接着下。", level: "info")
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
        MacEmit.log("内存从 \(spec.ram) 提到 \(macHuman(UInt64(minMem)))"
            + "（这个 macOS 版本的最低要求）", level: "warn")
        spec.ram = macHuman(UInt64(minMem)).replacingOccurrences(of: " GB", with: "G")
            .replacingOccurrences(of: " MB", with: "M")
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
    let installer = VZMacOSInstaller(virtualMachine: machine, restoringFromImageAt: ipswPath)
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
    installer.install { result in
        timer.cancel()
        if case .failure(let e) = result { box.err = e }
        box.sem.signal()
    }
    box.sem.wait()
    // 两条路都要先优雅停机。原来只在成功路径上 stop：装到一半失败时直接
    // throw → 进程退出 → 客人机是「拔电源」式死亡，APFS 留下未干净日志，
    // 下次启动跑好几分钟 fsck。
    if machine.canStop {
        let _ = vzWait(vmQueue) { done in machine.stop { _ in done(nil) } }
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
    // 先 clone 到一个临时名，成功了才替换正式的 disk.img。
    // 原来是「先删活盘再 clonefile」：clonefile 一失败（空间不够、权限、
    // 快照文件损坏）系统盘就直接没了，而它是唯一一份——回滚失败比不回滚
    // 糟得多。
    let dst = MacVMPaths.disk(name)
    let tmp = MacVMPaths.dir(name).appendingPathComponent("disk.img.rollback")
    try? FileManager.default.removeItem(at: tmp)
    if clonefile(src.path, tmp.path, 0) != 0 {
        let e = String(cString: strerror(errno))
        try? FileManager.default.removeItem(at: tmp)
        throw MacVMError("回滚失败：\(e)。系统盘没有被改动。")
    }
    try? FileManager.default.removeItem(at: dst)
    do {
        try FileManager.default.moveItem(at: tmp, to: dst)
    } catch {
        throw MacVMError("回滚到 \(dst.path) 失败：\(error.localizedDescription)。"
            + "副本还在 \(tmp.path)，可以手动改回去。")
    }
    MacEmit.log("已回滚到快照 \(tag)", level: "info")
}

func macSnapshotList(_ name: String) -> [[String: Any]] {
    // list 也要校验名字。原来只有 create/apply/delete 校验：
    // `snapshot list --name ../../etc` 会把目录外的 .img 条目当快照列出来。
    // 只读、危害有限，但白名单漏一个入口就等于没有。
    try? MacVMPaths.validateName(name)
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
