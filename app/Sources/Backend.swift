import Foundation

/// 跑 scripts/vmkit.py 并把它的 JSON 事件流转成可绑定状态。
///
/// 为什么要一个常驻子进程而不是一次性拿结果：装一个 Windows 要十几分钟，
/// 期间要把下载进度、校验、磁盘创建、Setup 阶段实时显示出来。vmkit 的
/// 输出协议是「stdout 每行一个 JSON 对象」，这里按行解析；不是 JSON 的
/// 行按普通日志显示——被委托的 winarm-vm.py 里有几处直接 print，客户端
/// 宽容一点比去改那条已经跑通的路径安全。
final class Backend: ObservableObject {

    struct LogLine: Identifiable, Equatable {
        let id = UUID()
        let level: String
        let text: String
    }

    @Published var busy = false
    @Published var stageId = ""
    @Published var stageLabel = ""
    @Published var progress: Double = 0
    @Published var progressDetail = ""
    @Published var logLines: [LogLine] = []
    @Published var errorMessage: String?

    /// 最近一次命令的完整输出，出问题时给用户看。
    @Published var rawOutput = ""

    private var process: Process?
    /// 同时在跑的命令数。启动时读目录、读列表、跑检查是三条并发命令，
    /// 用一个布尔量互斥会把后两条直接拒掉，界面就永远是空的。
    private var running = 0 {
        didSet { DispatchQueue.main.async { self.busy = self.running > 0 } }
    }

    // ------------------------------------------------------------ 定位 --

    /// 虚拟机数据目录，和 vmkit.py 的 ROOT 一致：~/.agentvm/vms。
    ///
    /// 不要图省事用 Application Support。vmkit 把 VM 放在 ~/.agentvm 下，
    /// app 跑去找 Application Support 会一个都看不到，而失败是静默的——
    /// 界面只是「没有虚拟机」。
    static var vmsDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".agentvm/vms", isDirectory: true)
    }

    /// vmkit.py 在哪。依次找：环境变量、app 自带资源、仓库。
    func locateScript() -> URL? {
        if let env = ProcessInfo.processInfo.environment["AGENTVM_BACKEND"],
           FileManager.default.fileExists(atPath: env + "/vmkit.py") {
            return URL(fileURLWithPath: env + "/vmkit.py")
        }
        if let res = Bundle.main.resourceURL?.appendingPathComponent("backend/vmkit.py"),
           FileManager.default.fileExists(atPath: res.path) {
            return res
        }
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("AgentVM/backend/vmkit.py")
        if FileManager.default.fileExists(atPath: support.path) {
            return support
        }
        return nil
    }

    /// 用哪个 python3。vmkit 只用标准库，系统自带那个就够，不要求用户装过
    /// 任何 Python——这是「一键」的一部分。
    func locatePython() -> String {
        if let env = ProcessInfo.processInfo.environment["AGENTVM_PYTHON"],
           FileManager.default.isExecutableFile(atPath: env) {
            return env
        }
        for cand in ["/usr/bin/python3", "/usr/local/bin/python3",
                     "/opt/homebrew/bin/python3"] {
            if FileManager.default.isExecutableFile(atPath: cand) {
                return cand
            }
        }
        return "/usr/bin/python3"
    }

    // ------------------------------------------------------------ 执行 --

    /// 跑一条命令。`onEvent` 在每个事件到达时调用，可能在任意线程；
    /// 需要更新 UI 的调用方自己 dispatch 回主线程。
    @discardableResult
    func run(_ args: [String],
             onEvent: @escaping ([String: Any]) -> Void,
             completion: (() -> Void)? = nil) -> Bool {
        guard let script = locateScript() else {
            errorMessage = "找不到 vmkit.py。app 资源目录里没有 backend/。"
            return false
        }
        // 不做 busy 互斥。读目录、读列表、跑检查在启动时是三条并发命令，
        // 各自一个 Process，本来就该同时跑。

        running += 1

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: locatePython())
        proc.arguments = [script.path] + args
        // 环境要干净但 PATH 不能空：vmkit 靠 find_tool 在 ~/hb、/opt/homebrew
        // 里找 QEMU，那个函数自己有兜底，但 PATH 里有交互 shell 的目录更容易
        // 找到。launchd 拉起的 app 不带 shell PATH，所以这里显式补。
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        env["PATH"] = "\(home)/hb/bin:/opt/homebrew/bin:/usr/local/bin:"
            + "/usr/bin:/bin:/usr/sbin:/sbin"
        env["HOME"] = home
        proc.environment = env

        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err

        let outHandle = out.fileHandleForReading
        let errHandle = err.fileHandleForReading

        // 每条命令一个独立的行缓冲器和一段独立的 rawOutput。启动时目录、
        // 列表、检查是三条并发命令，共用缓冲会把三条输出按字节穿插起来，
        // 切出来的行谁都不认识——症状是列表永远是空的，而单独跑命令是好的。
        let parser = StreamParser()
        let raw = RawSink()
        // 非 JSON 的行（winarm-vm.py 有几处直接 print）转成日志显示
        parser.onPlain = { [weak self] text in
            DispatchQueue.main.async { self?.appendLog(.init(level: "info", text: text)) }
        }

        // 读 stdout。用 readabilityHandler 而不是 readDataToEndOfFile，后者
        // 要等进程结束才返回，十几分钟的安装就完全没有进度可言。
        outHandle.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { return }
            parser.ingest(data, onEvent: onEvent)
        }
        errHandle.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
            raw.append(s)
        }

        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                outHandle.readabilityHandler = nil
                errHandle.readabilityHandler = nil
                self?.running -= 1
                self?.rawOutput += raw.text
                if p.terminationStatus != 0 { self?.progress = 0 }
                completion?()
            }
        }

        do {
            try proc.run()
            process = proc
            return true
        } catch {
            running -= 1
            errorMessage = "启动失败：\(error.localizedDescription)"
            return false
        }
    }

    func cancel() {
        // 必须杀真正的安装进程，不是那条引导父进程。
        //
        // 原来只 `process?.terminate()`。installDetached 里的 process 指的是
        // `install --detach` 那条引导进程，它写完 install.pid 就退出了——
        // terminate() 打在一条死进程上，真正干活的 setsid 子进程照样跑。
        // 而 installFinished 永远不会来，安装那一步的页脚只有「停止」一个按钮，
        // 用户被关在一个冻结的页面里，只能强杀 app。
        // （顺带：pid 若被系统复用，terminate() 有误杀不相关进程的小概率风险。）
        //
        // 现在先按 watchingInstall 找到 VM，读 install.pid 对那个进程发信号；
        // 找不到才退回老路。
        if let name = watchingName, let pid = installAlive(name: name) {
            kill(pid, SIGTERM)
            // 给 graceful shutdown 一点时间，不退就再补一下
            DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                if self.installAlive(name: name) != nil { kill(pid, SIGKILL) }
            }
        }
        process?.terminate()
    }

    /// cancel() 要杀的安装进程属于哪个虚拟机。由 AppState 在开始安装时设上。
    private var watchingName: String?

    func setWatching(_ name: String?) {
        watchingName = name
    }

    // -------------------------------------------------- 后台安装与日志尾随 --

    private var tailTimer: DispatchSourceTimer?
    /// 日志已读到的字节数。轮询时用它 seek，避免把同一段解析两遍。
    private var tailOffset: UInt64 = 0
    /// 正在尾的安装进程 pid。用来判断安装是否结束。
    private var tailPid: Int32?

    /// 装一个虚拟机。安装跑在脱离 app 的后台进程里，这里只负责起它和尾它的日志。
    ///
    /// 为什么不直接当子进程跑：装一个 Windows 要十几分钟，而 app 是个普通
    /// macOS app，用户关掉窗口 app 就退出，子进程跟着没，十几分钟的下载和
    /// 安装全部作废。实测踩过：4.3 GB 镜像下完、校验通过、正在改造介质的
    /// 时候窗口被关，整个安装凭空消失，日志里一个字都没留。
    /// 返回 false 表示没起来（找不到后端），但调用方多半不关心，标成
    /// @discardableResult 省得每次都写 `_ =`。
    @discardableResult
    func installDetached(_ args: [String],
                         onEvent: @escaping ([String: Any]) -> Void,
                         completion: @escaping () -> Void) -> Bool {
        guard let script = locateScript() else {
            errorMessage = "找不到 vmkit.py。app 资源目录里没有 backend/。"
            return false
        }
        running += 1

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: locatePython())
        proc.arguments = [script.path, "install"] + args + ["--detach"]
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        env["PATH"] = "\(home)/hb/bin:/opt/homebrew/bin:/usr/local/bin:"
            + "/usr/bin:/bin:/usr/sbin:/sbin"
        env["HOME"] = home
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        let handle = pipe.fileHandleForReading

        let parser = StreamParser()
        parser.onPlain = { [weak self] text in
            DispatchQueue.main.async { self?.appendLog(.init(level: "info", text: text)) }
        }

        // 这个短命父进程只做一件事：交出日志路径和安装进程的 pid
        var logURL: URL?
        var installPid: Int32 = 0
        let bootstrap: ([String: Any]) -> Void = { ev in
            if ev["t"] as? String == "done",
               // 注意 Emit.done 是把内容包在 vm 键里的，log 和 pid 都在里面，
               // 不在顶层。从顶层取会什么都拿不到，而失败是静默的。
               let vm = ev["vm"] as? [String: Any] {
                if let p = vm["log"] as? String { logURL = URL(fileURLWithPath: p) }
                if let pid = vm["pid"] as? Int { installPid = Int32(pid) }
            }
            onEvent(ev)
        }
        handle.readabilityHandler = { h in
            let data = h.availableData
            if data.isEmpty { return }
            parser.ingest(data, onEvent: bootstrap)
        }

        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                handle.readabilityHandler = nil
                guard let url = logURL, installPid > 0 else {
                    // 连日志路径都没拿到，说明起就没起来
                    self?.running -= 1
                    self?.errorMessage = "安装没起来，没拿到日志路径"
                    completion()
                    return
                }
                self?.tailLog(url, pid: installPid, onEvent: onEvent,
                              completion: completion)
            }
        }

        do {
            try proc.run()
            process = proc
            return true
        } catch {
            running -= 1
            errorMessage = "启动失败：\(error.localizedDescription)"
            return false
        }
    }

    /// 尾一个安装日志，把新增内容按事件交出去。
    ///
    /// 用轮询而不是 DispatchSource 监听文件：日志是另一个进程追加的，0.5 秒
    /// 轮一次完全跟得上，而且不用处理文件被 truncate 或替换的边界情况。
    /// 结束的判据是安装进程消失，不是「读到某个标记」——进程没了就一定结束了，
    /// 而标记可能因为崩溃没写出来。
    func tailLog(_ url: URL, pid: Int32,
                 onEvent: @escaping ([String: Any]) -> Void,
                 completion: @escaping () -> Void) {
        tailOffset = 0
        tailPid = pid

        let parser = StreamParser()
        parser.onPlain = { [weak self] text in
            DispatchQueue.main.async { self?.appendLog(.init(level: "info", text: text)) }
        }

        func readMore() {
            guard let fh = try? FileHandle(forReadingFrom: url) else { return }
            defer { try? fh.close() }
            fh.seek(toFileOffset: tailOffset)
            let data = fh.readDataToEndOfFile()
            if !data.isEmpty {
                tailOffset += UInt64(data.count)
                parser.ingest(data, onEvent: onEvent)
            }
        }

        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now(), repeating: 0.5)
        timer.setEventHandler { [weak self] in
            readMore()
            guard let self else { return }
            // 进程没了就是安装结束了。最后再读一次，把缓冲区里剩的收干净。
            if kill(pid, 0) != 0 {
                readMore()
                timer.cancel()
                DispatchQueue.main.async {
                    self.running -= 1
                    self.tailTimer = nil
                    self.tailPid = nil
                    completion()
                }
            }
        }
        tailTimer = timer
        timer.resume()
    }

    /// app 重新打开时接着看一个还在跑的安装。
    func resumeTailing(_ url: URL, pid: Int32,
                       onEvent: @escaping ([String: Any]) -> Void,
                       completion: @escaping () -> Void) {
        running += 1
        // 先把已有的内容整段放出来，界面立刻有东西可看，不用等下一次轮询
        let parser = StreamParser()
        parser.onPlain = { [weak self] text in
            DispatchQueue.main.async { self?.appendLog(.init(level: "info", text: text)) }
        }
        if let data = try? Data(contentsOf: url), !data.isEmpty {
            tailOffset = UInt64(data.count)
            parser.ingest(data, onEvent: onEvent)
        }
        if kill(pid, 0) != 0 {
            // 已经结束了，不需要再尾
            running -= 1
            completion()
            return
        }
        tailLog(url, pid: pid, onEvent: onEvent, completion: completion)
    }

    /// 某个虚拟机的安装进程是否还活着。
    ///
    /// 读 vmkit 写下的 install.pid，不用 pgrep 匹配命令行：命令行里参数顺序
    /// 不固定，写出来的模式很容易对不上，还可能误匹配到别的进程。
    ///
    /// 判活不能用 `kill(pid, 0)`：**僵尸进程照样返回成功**，而安装是被强杀之后
    /// 最常见的状态就是僵尸。用 os.kill 会永远认为上一次还在跑——app 从此拒绝
    /// 再接任何安装，而用户看到的是一个永远不动的「正在安装」。用 ps 排除 Z 状态。
    func installAlive(name: String) -> Int32? {
        let pidFile = Backend.vmsDirectory
            .appendingPathComponent(name)
            .appendingPathComponent("install.pid")
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0 else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-p", String(pid), "-o", "state="]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            // ps 是几十毫秒就完的小命令，同步等不会卡界面；而调用方（cancel、
            // resumeWatching）都在用户操作的路径上，本来就该等一下结果。
            p.waitUntilExit()
        } catch {
            // ps 起不来就别猜：宁可当作没在跑，让用户重新触发一次
            return nil
        }
        let out = (try? pipe.fileHandleForReading.readToEnd())
            .flatMap { String(data: $0, encoding: .utf8) } ?? ""
        let state = out.trimmingCharacters(in: .whitespacesAndNewlines)
        if state.isEmpty || state.hasPrefix("Z") { return nil }
        return pid
    }

    // 抓屏（vmkit.py screenshot）在 app 里已经不需要了：控制台是真 VNC 客户端，
    // 既能看也能操作，比隔几秒抓一张图强。命令行为排障留着。


    func appendLog(_ line: LogLine) {
        logLines.append(line)
        if logLines.count > 500 { logLines.removeFirst(logLines.count - 500) }
    }

    func clearLog() {
        logLines.removeAll()
        rawOutput = ""
        stageLabel = ""
        stageId = ""
        progress = 0
        progressDetail = ""
    }
}

/// 把一路 stdout 按行切开。每条命令一个实例，缓冲不共享。
///
/// 为什么必须是每条命令一个：启动时目录、列表、环境检查是三条并发命令，
/// 共用一个缓冲会把三条输出按字节穿插起来，切出来的行谁都不认识——症状是
/// 列表永远是空的，而同样的命令单独在终端里跑完全正常。
private final class StreamParser {
    private var buffer = Data()

    /// 收到非 JSON 行时调用。由 Backend 接上，转成日志。
    var onPlain: ((String) -> Void)?

    func ingest(_ data: Data, onEvent: @escaping ([String: Any]) -> Void) {
        buffer.append(data)
        // 按换行切，最后一段可能不完整，留在 buffer 里等下一次
        while let range = buffer.firstRange(of: Data([0x0A])) {
            let line = buffer.subdata(in: buffer.startIndex..<range.lowerBound)
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            guard let text = String(data: line, encoding: .utf8)?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                  !text.isEmpty else { continue }
            handleLine(text, onEvent: onEvent)
        }
    }

    private func handleLine(_ text: String, onEvent: @escaping ([String: Any]) -> Void) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any],
              dict["t"] as? String != nil else {
            // 不是 JSON。被委托的 winarm-vm.py 里有几处直接 print，客户端
            // 宽容一点比去改那条已经跑通的路径安全。
            DispatchQueue.main.async { self.onPlain?(text) }
            return
        }
        // 必须回主线程再交事件。readabilityHandler 跑在 Foundation 自己的
        // 串行队列上，在那里直接改 @Published 是未定义行为：实测症状是进度
        // 条偶尔动一下、stage 标签和日志一直不更新，而下载其实在正常跑。
        DispatchQueue.main.async { onEvent(dict) }
    }
}

/// stderr 的收集器。同样每条命令一个，避免并发追加同一个 String。
private final class RawSink {
    private var lock = NSLock()
    private var acc = ""

    func append(_ s: String) {
        lock.lock(); defer { lock.unlock() }
        acc += s
    }

    var text: String {
        lock.lock(); defer { lock.unlock() }
        return acc
    }
}
