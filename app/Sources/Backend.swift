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

    /// 每被写一次就调一次。AppState 用它 bump 自己的 revision。
    ///
    /// 为什么需要这个回调，而不是让 AppState 在自己写的地方各自补 revision：
    /// errorMessage 有 8 个写入点，其中 5 个在 Backend 自己身上（run 和
    /// installDetached 的两个 guard、terminationHandler 里「没拿到日志路径」、
    /// cancel 的兜底），AppState 根本看不见那些路径。而漏掉的后果完全一样——
    /// 见 AppState.revision 的注释：ContentView 的 .alert(item:) 不呈现，
    /// 用户看不到任何原因。已实测踩过三次（2026-11-11 审计：check 失败时
    /// 界面只说「宿主机检查没全过」而下面一条明细都没有）。
    var onErrorMessageChanged: (() -> Void)?
    @Published var errorMessage: String? {
        didSet {
            // 只在真的变了时通知：handleEvent 里 error 事件每次都会重写同一个值
            // （同一个错误会连着来好几条），每次都 bump 会让 revision 白涨。
            if oldValue != errorMessage { onErrorMessageChanged?() }
        }
    }

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
    ///
    /// `completion` **一定会被调用**，包括起不来的那两条路。调用方全都把它当成
    /// 必然执行的回调：SettingsView.save 靠它把 `saving` 复位（不复位的话
    /// 「保存」「保存并重启」两个按钮从此永久禁用），StorageView 的各处
    /// flash()/fail() 也靠它收尾。原来起不来时只 `return false`，completion
    /// 一次都不执行，调用方就悬在中间状态。2026-11-11 审计补上。
    @discardableResult
    func run(_ args: [String],
             onEvent: @escaping ([String: Any]) -> Void,
             completion: (() -> Void)? = nil) -> Bool {
        guard let script = locateScript() else {
            errorMessage = "找不到 vmkit.py。app 资源目录里没有 backend/。"
            completion?()
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
            // 同上：起不来也要收尾，不然调用方永远等不到 completion。
            completion?()
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
            return
        }
        // 兜底**不能**去 terminate 那个共享的 process 字段。
        //
        // 安装期间每个 stage 都会 refreshStatus()，把 process 覆盖成一条 status
        // 子进程。terminate() 打在它身上什么用都没有——真正下载/安装的进程照样
        // 跑，而界面这边 installEnded = true、横幅消失、向导跳回配置页，
        // 用户以为停了，4 GB 还在后台继续下。2026-11-10 审计补上。
        //
        // installAlive 拿不到 pid 只有两种原因：进程真的已经退了（没什么可杀），
        // 或者那个 pid 已经被系统复用给别的进程（这时更不能按数字乱杀）。
        // 两种都该说清楚，让用户自己看一眼。
        errorMessage = "没能定位到正在安装的进程（install.pid 不在，或者那个 pid 已经"
            + "不是安装进程了）。在终端里确认一下：\n"
            + "  ps aux | grep vmkit.py | grep install\n"
            + "确认还在跑就按上面的 pid 手动 kill，然后重新安装。"
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
    /// 正在尾的那个文件的 inode。轮询时拿它判断日志有没有被换成另一个文件
    /// （--force 重装就会），见 readMore 里的说明。
    private var tailFileId: Int = 0
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
            // 见 run() 的注释：起不来也要收尾。
            completion()
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
                    // 不能无条件覆写 errorMessage。父进程秒死时（名字后端不认、
                    // 同名已存在）后端已经把具体原因写进 errorMessage 了，而这里
                    // 原来一句「安装没起来，没拿到日志路径」把它盖掉——用户看到
                    // 的和真实原因毫无关系，真实原因只留在日志区里要自己滚到底。
                    // 只在自己还没有更具体的原因时才补这句兜底。
                    if self?.errorMessage?.isEmpty != false {
                        self?.errorMessage = "安装没起来，没拿到日志路径。"
                            + "常见原因：虚拟机名字后端不接受，或者同名虚拟机已存在。"
                    }
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
            // 同上：起不来也要收尾。
            completion()
            return false
        }
    }

    /// 尾一个安装日志，把新增内容按事件交出去。
    ///
    /// 用轮询而不是 DispatchSource 监听文件：日志是另一个进程追加的，0.5 秒
    /// 轮一次完全跟得上，而且不用处理文件被 truncate 或替换的边界情况。
    /// 结束的判据是安装进程消失，不是「读到某个标记」——进程没了就一定结束了，
    /// 而标记可能因为崩溃没写出来。
    ///
    /// `fromStart` 为假时从 tailOffset 当前位置接着读，不清零。resumeTailing
    /// 已经把整份日志放出去过一遍了，再清零重读会让同样的事件重复触发——
    /// 同一个错误弹窗会连跳两次。实测踩过（2026-11-10）。
    func tailLog(_ url: URL, pid: Int32, fromStart: Bool = true,
                 onEvent: @escaping ([String: Any]) -> Void,
                 completion: @escaping () -> Void) {
        if fromStart {
            tailOffset = 0
            // 从当前这个文件的身份开始比。置 0 会让第一次轮询判定「换文件了」
            // 并把 offset 归零——从头读正是 fromStart 想要的效果。为假时
            // （resumeTailing）不动它：那边已经按当前文件对齐过了，置零会让
            // 整份日志被重读一遍、同一个错误弹窗连跳两次（2026-11-10 踩过）。
            tailFileId = 0
        }
        tailPid = pid

        let parser = StreamParser()
        parser.onPlain = { [weak self] text in
            DispatchQueue.main.async { self?.appendLog(.init(level: "info", text: text)) }
        }

        func readMore() {
            // 先确认这个路径上还是不是刚才那个文件。
            //
            // --force 重装时 vmkit 会把整个 VM 目录 rmtree 掉再重建 install.log
            // （子进程的 _rmtree + _reopen_install_log），路径没变、内容却换了
            // 一整个文件。原来这里只按 tailOffset 接着读：上一轮日志有 40 MB 时
            // tailOffset 比新文件还大，seek 过去 readDataToEndOfFile 永远返回空，
            // 而 `if !data.isEmpty` 让ingest 一次都不发生——新安装的日志一条都收
            // 不到，界面停在「准备中」直到新文件字节数长过旧文件为止。
            // 2026-11-11 审计复验：vmkit.py 的 spawn_detached_install 用 append
            // 写表头，子进程随后 _rmtree 换新 inode。
            // 判据用 inode（systemFileNumber），不用 mtime：同一秒内写完再删重建
            // 时 mtime 可能一模一样。
            let fileId = self.fileId(of: url)
            let attrs = (try? FileManager.default.attributesOfItem(atPath: url.path))
                ?? [:]
            let size = (attrs[.size] as? Int) ?? 0
            if fileId != tailFileId {
                // 换文件了。offset 归零从头读——这正是我们想看的那个新日志。
                // 旧内容已经读过一遍，不会重复弹错误（见 tailLog 的 fromStart）。
                tailFileId = fileId
                tailOffset = 0
            } else if UInt64(size) < tailOffset {
                // 同一个 inode 但变小了：被 truncate 过。同样从头读。
                tailOffset = 0
            }
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
            //
            // 判活走 installPidAlive，**不能写 kill(pid, 0)**。原来这里就是裸的
            // kill(pid, 0)，而它正是 installPidAlive 注释里描述的那个最坏情况：
            // 安装进程已经退出、那个 pid 又被系统复用给一个无关进程时，裸判活
            // 永远为真——定时器永不 cancel、running 永不归零、busy 恒真、
            // installEnded 永不来、向导钉在安装页且「停止」点下去没有任何效果，
            // 用户什么都做不了只能强杀 app。2026-11-11 审计补上。
            if !self.installPidAlive(pid) {
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

    /// 这个路径上当前文件的 inode。读不到给 0——那时 readMore 会判定「文件被
    /// 换过」并把 offset 归零，对刚创建的日志来说正是要的行为。
    private func fileId(of url: URL) -> Int {
        let attrs = (try? FileManager.default.attributesOfItem(atPath: url.path))
            ?? [:]
        return (attrs[.systemFileNumber] as? Int) ?? 0
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
            // 身份也要对齐，否则 tailLog(fromStart: false) 之后的第一次轮询会
            // 判定「文件被换过」而把整份日志重读一遍。
            tailFileId = fileId(of: url)
            parser.ingest(data, onEvent: onEvent)
        }
        // 同一个理由：不能写 kill(pid, 0)。见 tailLog 里那段注释。
        if !installPidAlive(pid) {
            // 已经结束了，不需要再尾
            running -= 1
            completion()
            return
        }
        tailLog(url, pid: pid, fromStart: false, onEvent: onEvent,
                completion: completion)
    }

    /// 某个虚拟机的安装进程是否还活着。
    ///
    /// 读 vmkit 写下的 install.pid，不用 pgrep 匹配命令行：命令行里参数顺序
    /// 不固定，写出来的模式很容易对不上，还可能误匹配到别的进程。
    ///
    /// 判活本身在 installPidAlive 里，那儿的注释说明了为什么不能用 kill(pid, 0)。
    func installAlive(name: String) -> Int32? {
        let pidFile = Backend.vmsDirectory
            .appendingPathComponent(name)
            .appendingPathComponent("install.pid")
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0 else { return nil }
        return installPidAlive(pid) ? pid : nil
    }

    /// 这个 pid 是不是一个活着的安装进程。
    ///
    /// 判活不能用 `kill(pid, 0)`：**僵尸进程照样返回成功**，而安装是被强杀之后
    /// 最常见的状态就是僵尸。用 os.kill 会永远认为上一次还在跑——app 从此拒绝
    /// 再接任何安装，而用户看到的是一个永远不动的「正在安装」。用 ps 排除 Z 状态。
    ///
    /// **还必须核对进程名。** install.pid 在安装结束时本该被 vmkit 删掉
    /// （main() 里 install 那段 finally），但崩溃、强杀、旧版本留下的文件都会
    /// 让它留着一个死 pid；而 macOS 的 pid 会回绕复用，这个号随时可能被派给
    /// 一个毫无关系的进程。只判「活着」会把一个早就结束的安装当成永远在跑：
    /// 顶部横幅钉在「正在安装」、running 不归零于是 busy 恒为真、所有按钮全灰，
    /// 用户什么都做不了只能强杀 app。实测踩过（2026-11-10）。
    ///
    /// 三个调用方（cancel、resumeWatching、tailLog 的轮询）原来各写各的判据，
    /// 其中尾随那两处用的是裸 kill(pid, 0)——也就是上面说的那个最坏情况：
    /// 0.5 秒的轮询定时器永不 cancel、running 永不归零、向导钉在安装页且
    /// 「停止」点下去没有任何效果。2026-11-11 审计补上。
    func installPidAlive(_ pid: Int32) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        // state 和 command 都要：前者排僵尸，后者核对是不是我们的安装进程。
        // 只取 comm 不够——python3 的 comm 是 "Python"，任何 Python 程序都算。
        p.arguments = ["-p", String(pid), "-o", "state=,command="]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            // ps 是几十毫秒就完的小命令，同步等不会卡界面；而调用方（cancel、
            // resumeWatching）都在用户操作的路径上，本来就该等一下结果。
            // 尾随的轮询跑在后台队列上，同样不该为了一条 ps 去异步化。
            p.waitUntilExit()
        } catch {
            // ps 起不来就别猜：宁可当作没在跑，让用户重新触发一次
            return false
        }
        let out = (try? pipe.fileHandleForReading.readToEnd())
            .flatMap { String(data: $0, encoding: .utf8) } ?? ""
        let line = out.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty || line.hasPrefix("Z") { return false }
        // 归属判据：安装进程只有两种——vmkit.py 自己（Linux/Windows/local），
        // 以及 vmz（macOS，VZ 只有 Swift API）。对不上就当作没在跑，那个方向
        // 是安全的：最坏是让用户重点一次安装，不会把人锁死在灰屏界面上。
        let cmd = line.dropFirst()
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return cmd.contains("vmkit.py") || cmd.contains("/vmz")
            || cmd.hasSuffix(" vmz")
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
