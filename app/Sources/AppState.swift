import Foundation
import Combine

/// 应用状态：镜像目录、虚拟机列表、以及一次安装的全过程。
final class AppState: ObservableObject {

    @Published var distros: [Distro] = []
    @Published var vms: [VMInfo] = []
    @Published var checkItems: [CheckItem] = []
    @Published var checkOk = false
    @Published var loading = false
    @Published var installFinished = false
    @Published var installedVM: VMInfo?
    /// 安装进程已经结束了——不管成功还是失败。
    ///
    /// 为什么不能只靠 installFinished：那个只在收到一个**能解成 VMInfo 的**
    /// done 事件时置位，而安装失败、被用户停止、后端发来的 done 形状对不上时
    /// 它永远是 false。后果是向导钉在「正在安装」那一页——那一页原来只有一个
    /// 「停止」按钮，而安装早就死了，点了没反应，用户没有任何出口，只能强杀
    /// app。实测踩过（2026-11-10，用户原话「一直跳出无法关闭」）。
    @Published var installEnded = false
    /// 「新建虚拟机」向导开不开。原来这是 ContentView 里的 @State，
    /// AppState 想从详情页直接打开安装页时够不着，于是搬到这里。
    @Published var showNewVM = false
    /// 正在被监听的安装对应的虚拟机名。app 重启后接上没装完的安装时设上，
    /// 界面据此显示「安装中」横幅。不设的话用户重新打开 app 只看到一个停在
    /// 半路的条目，完全不知道后台还在下 4 GB。
    @Published var watchingInstall: String?
    /// 向导打开时直接停在哪一步。接上没装完的安装时用 .installing。
    @Published var wizardStep: WizardStep?
    /// 正在看控制台的虚拟机。控制台是一个独立的 sheet，装系统期间和日常
    /// 操作都用它——本地安装包整条安装流程都在里面点完。
    @Published var consoleVM: VMInfo?
    /// 正在配置的虚拟机。装完之后改 CPU、内存、磁盘都走这个面板。
    @Published var settingsVM: VMInfo?
    /// 正在管理存储的虚拟机（附加盘、快照、共享目录）。
    @Published var storageVM: VMInfo?
    /// 正在看的 macOS 虚拟机。它的画面是 VZVirtualMachineView 而不是 VNC，
    /// 所以走一个独立的 sheet——见 MacVMWindow。
    @Published var macVM: VMInfo?
    /// macOS 虚拟机最近一次启动失败的原因。空字符串表示没失败。
    @Published var macVMLastError = ""
    /// 快照列表。storageVM 打开时由后端拉。
    @Published var snapshots: [VMSnapshot] = []

    let backend = Backend()

    /// backend 每动一次就 +1。
    ///
    /// 为什么需要它：`backend` 是 AppState 里的一个普通 `let`，不是
    /// `@Published`。视图写 `state.backend.progress` 时确实能读到值，但 SwiftUI
    /// 对「嵌套 ObservableObject 的属性访问」的依赖追踪不可靠——实测症状是
    /// 整个安装期间的反馈界面是死的：阶段标签停在第一句、进度条钉在 0、日志区
    /// 空白、红色错误不出现，只有装完那一刻 installFinished 变化，界面才瞬间
    /// 跳变。十几分钟的安装，用户看到的是一个不动的进度条。
    ///
    /// 加上它之后，handleEvent 每写一次 backend 就同步碰一下 AppState 自己的
    /// @Published，objectWillChange 一定发得出去，所有观测 AppState 的视图都会
    /// 重算并读到 backend 的新值。代价是一次无意义的重算，可以忽略。
    @Published private(set) var revision = 0

    /// 打开新建向导时把表单清干净。
    ///
    /// 原来不清：装完 win11 再点「+」，名称框里还是「win11」（pick() 只在为空时
    /// 填），点「开始安装」→ vmkit die「win11 已存在。换个名字，或加 --force
    /// 重装。」→ 错误事件进了 handleEvent 但界面不刷新（见 revision 的注释），
    /// installFinished 永远不来，向导停在「正在安装」，而「停止」又停不掉真正
    /// 的安装进程。用户在第一步就卡死。
    ///
    /// `startAt` 给一个步骤时不清表单——那是「重装一台没装完的虚拟机」，
    /// 名字得留着，直接进安装页。
    func resetWizard(startAt: WizardStep? = nil) {
        if startAt == nil {
            vmName = ""
            vmPassword = "Agentvm2026"
            showPassword = false
            pickedDistro = nil
        }
        installFinished = false
        installedVM = nil
        installEnded = false
        wizardStep = startAt
    }

    /// 新建虚拟机的表单。默认值在选了系统之后按该系统的推荐值填。
    @Published var pickedDistro: Distro?
    @Published var vmName = ""
    @Published var vmCpus = 2
    @Published var vmRam = "2G"
    @Published var vmDisk = "32G"
    @Published var vmUser = "agent"
    // 默认密码故意写成满足复杂度要求的形式（大小写 + 数字 + 长度）。
    // autounattend.xml 建的是 Administrators 组的本地账户，弱密码会让
    // Windows 把它标成「下次登录必须改密码」，而自动登录那个会话一被挡，
    // sshd 就起不来——表象是「装完了但 SSH 连不上」。
    @Published var vmPassword = "Agentvm2026"
    @Published var showPassword = false

    init() {
        // 必须第一个设：Backend 自己身上那 5 个写 errorMessage 的地方（起不来、
        // 被取消、尾随时断线）AppState 看不见，只能让它写的时候通知过来。
        // 见 Backend.onErrorMessageChanged 的注释。
        backend.onErrorMessageChanged = { [weak self] in
            // errorMessage 可能从 readabilityHandler 那个后台线程写进来，
            // 而 revision 是 @Published，只能在主线程碰。
            DispatchQueue.main.async { self?.revision += 1 }
        }
        refreshCatalog()
        refreshStatus()
        runCheck()
        // 放在最后：要先拿到 vms 才知道有没有没装完的。顺序反了会漏。
        resumeWatching()
    }

    // -------------------------------------------------------------- 读取 --

    func refreshCatalog() {
        backend.run(["catalog"], onEvent: handleCatalog)
    }

    private func handleCatalog(_ ev: [String: Any]) {
        // catalog 失败（catalog.json 不在、python3 报错）时原来这个 guard 会把
        // error 事件一起挡掉，界面表现是向导第一步永远转「正在读镜像目录…」，
        // 不给任何原因。命令行跑同样的命令却能看出问题。2026-11-10 审计补上。
        if ev["t"] as? String == "error" {
            backend.errorMessage = (ev["msg"] as? String) ?? "镜像目录读不到"
            return
        }
        guard ev["t"] as? String == "catalog",
              let arr = ev["distros"] as? [[String: Any]] else { return }
        // compactMap 会把解码失败的条目静默丢掉：vmkit 侧加字段、改类型或漏发
        // 一个非可选字段时，界面只表现为「正在读镜像目录…」转不停，而命令行跑
        // 同样的命令是好的。所以失败时至少留一条错误，别让界面空转。
        var dec: [Distro] = []
        for d in arr {
            guard let data = try? JSONSerialization.data(withJSONObject: d),
                  let one = try? JSONDecoder().decode(Distro.self, from: data) else {
                backend.errorMessage = "镜像目录里有一个条目读不懂："
                    + "\(d["id"] as? String ?? "?")"
                revision += 1
                continue
            }
            dec.append(one)
        }
        DispatchQueue.main.async { [weak self] in self?.distros = dec }
    }

    func refreshStatus() {
        backend.run(["status"], onEvent: handleStatus)
    }

    private func handleStatus(_ ev: [String: Any]) {
        // 同上。status 失败时原来界面显示「还没有虚拟机」，而 ~/.agentvm/vms 下
        // 明明有东西——用户会以为自己记错，或者去点新建，装出第二份来。
        if ev["t"] as? String == "error" {
            backend.errorMessage = (ev["msg"] as? String) ?? "虚拟机状态读不到"
            return
        }
        guard ev["t"] as? String == "vms",
              let arr = ev["vms"] as? [[String: Any]] else { return }
        // 同上：静默丢条目会让虚拟机从列表里消失，而终端里 status 完全正常。
        var vms: [VMInfo] = []
        for dict in arr {
            guard let data = try? JSONSerialization.data(withJSONObject: dict),
                  let one = try? JSONDecoder().decode(VMInfo.self, from: data) else {
                backend.errorMessage = "虚拟机 \(dict["name"] as? String ?? "?") "
                    + "的状态读不懂，可能是 vm.json 缺字段。命令行跑 "
                    + "vmkit.py status 看看。"
                revision += 1
                continue
            }
            vms.append(one)
        }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.vms = vms
            // 打开着的面板要跟着换成新的一份。
            //
            // settingsVM / storageVM / consoleVM 都是点按钮时赋一次值的快照，
            // 不同步的话：加完一块盘，列表还写着「还没有附加盘」；改完 CPU 内存，
            // 面板里显示的仍是旧值——用户以为没生效，会反复点同一个按钮。
            // 「保存」按钮拿旧 vm 当 dirty 基线，还会出现「什么都没改却可点」
            // 的反向情况。macVM 那边本来就是同步的（见 startMacVM 收尾），
            // 这三个漏了。2026-11-10 审计补上。
            sync(&self.settingsVM)
            sync(&self.storageVM)
            sync(&self.consoleVM)
        }
    }

    /// 按名字把一份可能过期的 VMInfo 换成 status 刚给的新那份。找不到就原样留着
    /// （VM 被删了的情况由各面板自己的逻辑处理）。
    private func sync(_ vm: inout VMInfo?) {
        guard let old = vm, let fresh = vms.first(where: { $0.name == old.name })
        else { return }
        vm = fresh
    }

    func runCheck() {
        backend.run(["check"], onEvent: handleCheck)
    }

    private func handleCheck(_ ev: [String: Any]) {
        if ev["t"] as? String == "error" {
            backend.errorMessage = (ev["msg"] as? String) ?? "宿主机检查没跑起来"
            return
        }
        // check 失败时原来 checkItems 空 + checkOk == false，每个详情页顶一条
        // 「宿主机检查没全过」而下面一条明细都没有——看着像一堆故障，实际只是
        // 这条命令没跑成。
        guard ev["t"] as? String == "check" else { return }
        var items: [CheckItem] = []
        var dropped = 0
        for d in (ev["items"] as? [[String: Any]] ?? []) {
            guard let data = try? JSONSerialization.data(withJSONObject: d),
                  let item = try? JSONDecoder().decode(CheckItem.self, from: data)
            else {
                // 解不出来的条目要计数，不能静默丢掉。
                // 原来是 `.compactMap { try? ... }`：后端改/漏 CheckItem 的
                // name/ok/detail 类型时，那一项就从「宿主机检查没全过」的明细里
                // 消失，用户看不到全部未过项。同文件的 handleCatalog、handleStatus
                // 对这类「解码失败静默丢条目」都在上一轮补了 errorMessage +
                // revision，唯独 check 这条漏了。2026-11-11 审计补上。
                dropped += 1
                continue
            }
            items.append(item)
        }
        DispatchQueue.main.async {
            self.checkItems = items
            self.checkOk = (ev["ok"] as? Bool) ?? false
            if dropped > 0 {
                self.errorMessage = "宿主机检查有 \(dropped) 项显示不出来"
                    + "（后端给的字段和这个版本的 app 对不上）"
                self.revision += 1
            }
        }
    }

    // -------------------------------------------------------------- 安装 --

    func pick(_ d: Distro) {
        pickedDistro = d
        // 按该系统的推荐值填，用户想改再改
        vmCpus = d.defaultCpus
        vmRam = d.defaultRam
        vmDisk = d.defaultDisk
        vmUser = d.defaultUser
        if vmName.isEmpty {
            vmName = d.isWindows ? "win11" : "linux\(vms.count + 1)"
        }
    }

    func startInstall() {
        guard let d = pickedDistro else { return }
        installFinished = false
        installedVM = nil
        installEnded = false
        backend.clearLog()

        var args: [String]
        if let u = d.sourceURL {
            // 用户自己贴的网址：后端先下到 <vm>/iso/ 再当本地包装。断点续传
            // 和进度条都是现成的（和 catalog 那条共用 download）。
            args = ["--source-url", u]
        } else if let src = d.sourcePath {
            // 本地安装包。没有 catalog 条目、没有校验和、没有推荐配置，
            // 后端走 cmd_install_local——建盘、把图形安装器跑起来，然后由
            // autoinstall 那个驱动器自己点完（2026-11-11 起默认如此）。
            // 不传 --no-auto：这条路的卖点就是「一键」，多问一句反而添乱。
            args = ["--source", src]
        } else {
            args = ["--distro", d.id]
        }
        args += ["--name", vmName,
                 "--cpu", String(vmCpus), "--mem", vmRam, "--disk", vmDisk,
                 "--user", vmUser, "--password", vmPassword]

        // 安装是脱离 app 的后台任务。关窗口、甚至 app 崩了都不影响它跑完，
        // app 重新打开时 resumeWatching() 会把日志接上继续显示。
        backend.installDetached(args, onEvent: handleEvent) { [weak self] in
            self?.refreshStatus()
            self?.watchingInstall = nil
            self?.backend.setWatching(nil)
            // 进程没了就是结束了。必须在这儿置位：安装失败时 installFinished
            // 永远不会来，而向导的出口只有它一个，用户会被关在安装页里。
            self?.installEnded = true
        }
        watchingInstall = vmName
        backend.setWatching(vmName)
    }

    /// 打开新建向导。`at` 给一个步骤时直接停在那儿（不清表单）——
    /// 横幅上的「查看详情」要的是「跳到正在安装那一页」，不是回到第一步。
    func openNewVMWizard(at step: WizardStep? = nil) {
        resetWizard(startAt: step)
        showNewVM = true
    }

    /// app 启动时接上还在跑的安装。
    ///
    /// 不接的话，装到一半关掉 app 再打开，界面显示「还没有虚拟机」或者一个
    /// 停在半路的条目，用户完全不知道后台还在下 4 GB。
    func resumeWatching() {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: Backend.vmsDirectory, includingPropertiesForKeys: nil) else { return }
        // 只接最近的一个未完成安装。
        //
        // 原来循环里每个都接，而 Backend 的 tailTimer / tailOffset / tailPid 都是
        // 单例：第二个把第一个的定时器覆盖掉，第一个没人 cancel、永远每 0.5 秒
        // 轮询；tailOffset 被两个安装共享导致读错位置、事件重复或错位；running
        // 只被最后一个递减，busy 卡在 true，所有按钮从此禁用。watchingInstall
        // 也只剩最后一个名字。
        //
        // 两个同时没装完本身是异常状态（vmkit 的 spawn_detached_install 现在会
        // 拒绝第二个），所以接最新的那个、其余只在日志里提一句就够。
        var pending: [(name: String, dir: URL, mtime: Date)] = []
        for dir in entries {
            let log = dir.appendingPathComponent("install.log")
            let vmJSON = dir.appendingPathComponent("vm.json")
            guard FileManager.default.fileExists(atPath: log.path),
                  let data = try? Data(contentsOf: vmJSON),
                  // 不能拿 VMInfo 解 vm.json：VMInfo 对应的是 status 命令的输出，
                  // 里面有 ssh_command、disk_used_bytes 这些 vm.json 里没有的
                  // 非可选字段，解不出来——而失败是静默的，接不上就看不出原因。
                  // 这里只关心 ready 一个字段，直接读字典。
                  let obj = try? JSONSerialization.jsonObject(with: data),
                  let dict = obj as? [String: Any],
                  let ready = dict["ready"] as? Bool,
                  !ready else { continue }
            let mtime = (try? FileManager.default
                .attributesOfItem(atPath: log.path)[.modificationDate]) as? Date
                ?? .distantPast
            pending.append((dir.lastPathComponent, dir, mtime))
        }
        guard let newest = pending.max(by: { $0.mtime < $1.mtime }) else { return }
        guard let pid = backend.installAlive(name: newest.name) else {
            // 有日志但没有活进程：上次的安装已经死了（崩了或被杀了）。别装作
            // 还在跑——原来会一直显示「正在安装」而后台什么都没有。
            backend.appendLog(.init(level: "warn",
                                    text: "发现 \(newest.name) 有安装日志但进程已结束"))
            return
        }
        for p in pending where p.name != newest.name {
            backend.appendLog(.init(level: "warn",
                                    text: "\(p.name) 也有未完成的安装，但一次只跟一个"))
        }
        watchingInstall = newest.name
        backend.setWatching(newest.name)
        wizardStep = .installing
        installEnded = false
        backend.resumeTailing(newest.dir.appendingPathComponent("install.log"),
                              pid: pid,
                              onEvent: handleEvent) { [weak self] in
            self?.refreshStatus()
            self?.watchingInstall = nil
            self?.backend.setWatching(nil)
            self?.installEnded = true
        }
    }

    private func handleEvent(_ ev: [String: Any]) {
        switch ev["t"] as? String ?? "" {
        case "stage":
            backend.stageId = ev["id"] as? String ?? ""
            backend.stageLabel = ev["label"] as? String ?? ""
            backend.progress = 0
            backend.progressDetail = ""
            backend.appendLog(.init(level: "stage",
                                    text: ev["label"] as? String ?? ""))
        case "progress":
            let f = ev["frac"] as? Double ?? 0
            backend.progress = min(max(f, 0), 1)
            let bytes = ev["bytes"] as? Int ?? 0
            let total = ev["total"] as? Int ?? 0
            let speed = ev["speed"] as? Double ?? 0
            if total > 0 {
                backend.progressDetail =
                    "\(Self.bytes(bytes)) / \(Self.bytes(total))"
                    + (speed > 1024 ? "  \(Self.bytes(Int(speed)))/s" : "")
            } else {
                backend.progressDetail = Self.bytes(bytes)
            }
        case "log":
            let level = ev["level"] as? String ?? "info"
            let msg = ev["msg"] as? String ?? ""
            backend.appendLog(.init(level: level, text: msg))
        case "error":
            backend.errorMessage = ev["msg"] as? String ?? "未知错误"
            backend.appendLog(.init(level: "error",
                                    text: ev["msg"] as? String ?? ""))
        case "done":
            // 后台安装那条路有两个形状完全不同的 done：
            //   · 引导进程的：{"name","log","pid","detached":true}，只说明
            //     「安装已经甩到后台跑了」，不是装好了；
            //   · 安装进程自己的：{"vm":{…完整 VM 记录…}}，这才是装好了。
            // 原来两个都按后者解，引导那个的 VMInfo 解码必然失败——而失败是
            // 静默的（try?），installFinished 永远不置位，用户看到的是一台
            // 装好了的虚拟机配一个钉在「正在安装」的向导。实测踩过（2026-11-10）。
            if let inner = ev["vm"] as? [String: Any],
               inner["detached"] as? Bool == true { break }
            if let dict = ev["vm"] as? [String: Any],
               let data = try? JSONSerialization.data(withJSONObject: dict),
               let vm = try? JSONDecoder().decode(VMInfo.self, from: data) {
                installedVM = vm
                installFinished = true
                backend.progress = 1
            }
        default: break
        }
        // 见 revision 的注释：不碰一下这个，界面收不到 backend 的任何变化。
        revision += 1

        // 安装期间也刷一次列表。原来只在 completion 里刷，而安装要跑十几分钟，
        // 这期间 state.vms 一直是空的——InstallProgress.vncPort(for:) 永远返回
        // nil，向导里那个内嵌控制台从头到尾显示「虚拟机还没起来」，而客人机其实
        // 早就起来在跑 Setup 等人点。这个内嵌控制台等于死代码。
        // 按 stage 节流：每个阶段刷一次，不是每个事件刷一次。
        if ev["t"] as? String == "stage" {
            DispatchQueue.main.async { [weak self] in self?.refreshStatus() }
        }
    }

    /// 所有操作类命令共用的事件处理：把 error 事件填进 backend.errorMessage。
    ///
    /// 原来这些地方全写 `{ _ in }`，把后端发来的 error 事件整个丢掉。后果是
    /// 界面显示「已保存」而实际什么都没改：
    ///   · 配置面板里虚拟机正在运行时把磁盘从 32G 改到 64G，后端 die「虚拟机
    ///     正在运行，镜像被占着写锁」，app 收不到 → 绿色「已保存」亮起，
    ///     重启后磁盘还是 32G。
    ///   · 详情页点「关机」「启动」「显示窗口」失败（VM 已经死了、无窗口 VM
    ///     没有窗口可切）也全部静默，按钮像没反应。
    private func handleCommandEvent(_ ev: [String: Any]) {
        if ev["t"] as? String == "error" {
            backend.errorMessage = ev["msg"] as? String ?? "后端报错了，没给原因"
            revision += 1
        }
    }

    // ------------------------------------------------------------ 操作 --

    // ------------------------------------------- macOS 客人机（Virtualization.framework） --

    /// 每个 macOS 虚拟机一个运行态。
    ///
    /// 为什么不用单个：用户可以同时开多个虚拟机，而 VZVirtualMachine 是
    /// 一一对应一台机器的，混用一个会把画面接到错误的机器上。
    private var macRuntimes: [String: MacVMRuntime] = [:]

    func macRuntime(for vm: VMInfo) -> MacVMRuntime? {
        macRuntimes[vm.name]
    }

    /// 有没有正在跑的 macOS 客人机。app 退出前的判据（见 stopAllMacVMs）。
    var hasRunningMacVM: Bool { !macRuntimes.isEmpty }

    /// 启动一个 macOS 虚拟机，并打开它的窗口。
    ///
    /// 和另外两种的区别：另外两种是 `vmkit start` 起完就返回（QEMU 自己跑），
    /// 这一种必须由 app 进程持有 VZVirtualMachine——画面要显示在 app 里，
    /// 而 VZVirtualMachineView 不能跨进程。所以 start 之后 app 就是这个
    /// 虚拟机的宿主，app 退出它就停。
    func startMacVM(_ vm: VMInfo) {
        macVMLastError = ""
        guard vm.macInstalled ?? false else {
            macVMLastError = "这个虚拟机还没装完。先在列表里选中它，点「开始安装」。"
            return
        }
        if macRuntimes[vm.name]?.vm != nil {
            macVM = vm
            return
        }
        guard let spec = MacVMSpec.load(name: vm.name) else {
            macVMLastError = "读不到 macOS 虚拟机的配置（~/.agentvm/vms/"
                + vm.name + "/macvm.json）。"
            return
        }
        let rt = MacVMRuntime()
        macRuntimes[vm.name] = rt
        // 客人机自己关机时把窗口也关掉：画面已经没了，留一个空窗口只会让人
        // 以为它还在跑。
        rt.onGuestStop = { [weak self] in
            DispatchQueue.main.async {
                self?.macRuntimes.removeValue(forKey: vm.name)
                if self?.macVM?.name == vm.name { self?.macVM = nil }
                self?.revision += 1
            }
        }
        rt.onFailed = { [weak self] msg in
            DispatchQueue.main.async {
                self?.macVMLastError = msg
                self?.macRuntimes.removeValue(forKey: vm.name)
                self?.revision += 1
            }
        }
        // start() 里 vzWait 会同步等 machine.start 的 completion（几秒），
        // 主线程就这么卡着：app 彩球，点哪都没反应，而用户以为点了没生效
        // 会再点一次。派到后台队列跑，UI 更新再回主线程。
        // rt 要强引用：它同时也在 macRuntimes 字典里，强捕获不会造成循环，
        // 却能保证「客人机自己停了把 runtime 摘掉」这种事不会在 start 中途
        // 把 VZVirtualMachine 释放掉。
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                try rt.start(spec: spec, provision: !spec.provisioned, host: "app")
                let didProvision = rt.didProvision
                DispatchQueue.main.async {
                    guard let self else { return }
                    // 同 vmz 侧：只有 didProvision 才写 provisioned。否则 macOS 26
                    // 及以下的机器会被标成「已建过账号」，以后再也不会建。
                    //
                    // 写盘失败必须说出来。原来是 `try? MacVMSpec.save(s)`：
                    // 磁盘满时它静默失败，而界面照显「已启动」，下次启动又跑
                    // 一遍 Setup Assistant——用户看到的是「每次开机都要建账号」。
                    if didProvision, !spec.provisioned {
                        var s = spec
                        s.provisioned = true
                        do {
                            try s.save()
                        } catch {
                            self.macVMLastError = "账号建好了，但「已建过账号」这个标记写不回去（\(error.localizedDescription)）。下次启动会再问一遍账号，选取消即可。"
                        }
                    }
                    self.macVM = vm
                    self.revision += 1
                }
            } catch {
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.macRuntimes.removeValue(forKey: vm.name)
                    self.macVMLastError = error.localizedDescription
                    self.revision += 1
                }
            }
        }
    }

    /// 停掉一个 macOS 虚拟机。走 VZ 的优雅停机，不是杀进程。
    ///
    /// runtime 不能在这儿就从字典里摘掉：rt.stop() 是异步的（它把
    /// machine.stop 投到 vmQueue 就返回），一释放 runtime，VZVirtualMachine
    /// 跟着释放，收尾块里那句 `guard let self` 也不再有人调——macvm.pid
    /// 永不删除、状态卡在 .stopping、onGuestStop/onFailed 全丢（delegate 是
    /// weak）。所以等 stop 真的结束（或等超时）再摘。
    func stopMacVM(_ vm: VMInfo) {
        guard let rt = macRuntimes[vm.name] else { return }
        macVM = nil
        revision += 1
        rt.stop { [weak self] timedOut in
            DispatchQueue.main.async {
                guard let self else { return }
                if timedOut {
                    // 等超时：客人机还在跑，runtime 必须留着——vm 还在里面，
                    // 还能再叫一次 stop。摘掉就等于没有任何对象能停它了，
                    // 它会一直占 CPU/内存到 app 退出，而界面上显示「已停止」。
                    self.macVMLastError = "优雅关机等超时（客人机可能停在登录界面）。虚拟机还在运行，可以再停一次，或者在客人机里选「关机」。"
                    self.revision += 1
                    return
                }
                self.macRuntimes.removeValue(forKey: vm.name)
                self.revision += 1
                self.refreshStatus()
            }
        }
    }

    /// app 退出时停掉所有 macOS 客人机。同步等，因为进程马上就要没了。
    ///
    /// 为什么必须专门做：VZ 没有「后台常驻」这回事，进程一走机器就没了。
    /// 原来 app 退出等于直接拔电源——APFS 留下未干净的日志，下次启动跑
    /// 好几分钟 fsck，而用户完全不知道发生了什么。
    ///
    /// 调用方是 app delegate 的 applicationShouldTerminate（全仓库唯一的
    /// 退出钩子；sheet 的 .onDisappear 只覆盖关窗口，app 终止时 SwiftUI
    /// 不保证跑它）。
    ///
    /// 总时长有上限。原来是逐台 `stopAndWait(timeout: 45)`、同步等——超时按台数
    /// 相乘：同时跑着 3 台 macOS 客人机且都迟迟不响应关机时，
    /// applicationShouldTerminate 会堵 45x3 = 135 秒（主线程），用户看到退出
    /// 卡住，而注释只按「一台」考虑了 45 秒。2026-11-11 审计补上总预算。
    func stopAllMacVMs(totalTimeout: TimeInterval = 45) {
        let deadline = Date().addingTimeInterval(totalTimeout)
        for rt in macRuntimes.values {
            let left = deadline.timeIntervalSinceNow
            if left <= 0 {
                // 剩下的不等了。退出卡住比留一台没优雅关机更难看——后者下次
                // 启动自愈，前者让用户以为 app 死了。
                NSLog("AgentVM: 退出时还有 macOS VM 没关完，总超时 %.f 秒已到，不再等",
                      totalTimeout)
                break
            }
            rt.stopAndWait(timeout: left)
        }
        macRuntimes.removeAll()
        macVM = nil
        revision += 1
    }

    func action(_ cmd: String, name: String) {
        // macOS 客人机不走 vmkit 的运行时命令：它的进程在 app 里（VZ 的 VM
        // 跟进程绑定），vmkit start 那种「起了就返回」的语义在这儿不成立。
        if let vm = vms.first(where: { $0.name == name }), vm.isMacOS {
            switch cmd {
            case "start":
                startMacVM(vm)
                return
            case "stop":
                stopMacVM(vm)
                return
            default:
                break
            }
        }
        backend.run([cmd, "--name", name], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
    }

    func deleteVM(_ vm: VMInfo) {
        backend.run(["delete", "--name", vm.name], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
    }

    /// 重装一台虚拟机：清掉它的目录从头装。给「没装完」的那几台用。
    ///
    /// 为什么需要这条：半成品 VM（install 中途失败留下的）既起不动也没法原地
    /// 修好——点「启动」拿到「VM 不完整，缺 disk.qcow2。重新 install。」，
    /// 而从新建向导装同样的名字又回「已存在，换个名字或加 --force」。两句都是
    /// 死胡同，用户只能自己去终端删目录。实测踩过（2026-11-10）。
    ///
    /// 半成品没有可用数据，所以调用方可以直接调，不用再确认一遍。
    func reinstall(_ vm: VMInfo) {
        var args: [String]
        if vm.isLocal, let src = vm.source, !src.isEmpty {
            args = ["--source", src]
        } else if !vm.distro.isEmpty {
            args = ["--distro", vm.distro]
        } else {
            backend.errorMessage = "这台没有记录装的是哪个系统，没法重装。删掉它重新建一台。"
            revision += 1
            return
        }
        args += ["--name", vm.name, "--force",
                 "--cpu", String(vm.cpus), "--mem", vm.ram,
                 "--disk", vm.disk, "--user", vm.user]
        // 密码跟新建向导同一个来源。不传的话 vmkit 用它自己的默认值，
        // 而用户从向导里看到的一直是表单里那个——两处不一致，
        // 装完登不进去时没有任何线索指向这里。
        if !vmPassword.isEmpty { args += ["--password", vmPassword] }
        installFinished = false
        installedVM = nil
        installEnded = false
        backend.clearLog()
        // 直接进安装页：用户点的是「重装」，给他看进度，而不是回第一步重选系统。
        resetWizard(startAt: .installing)
        showNewVM = true
        backend.installDetached(args, onEvent: handleEvent) { [weak self] in
            self?.refreshStatus()
            self?.watchingInstall = nil
            self?.backend.setWatching(nil)
            self?.installEnded = true
        }
        watchingInstall = vm.name
        backend.setWatching(vm.name)
    }

    func toggleAutostart(_ vm: VMInfo) {
        // 当前是否已装自启：看 plist 在不在。简单可靠，不用解析 launchctl 输出。
        //
        // 路径原来写成 `.applicationSupportDirectory` 再
        // `.deletingLastPathComponent()` 再 `appendingPathComponent("Library/
        // LaunchAgents/…")`，解析出来是 ~/Library/Library/LaunchAgents/…——多套
        // 了一层 Library。而 vmkit.py 写的是 ~/Library/LaunchAgents/…。于是
        // `installed` 恒为 false，每次点「开机自启」都只传 --name 不传
        // --disable：自启只会被反复装上，永远卸不掉，按钮文案也永远不反映状态。
        let plist = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.agentvm.\(vm.name).plist")
        let installed = FileManager.default.fileExists(atPath: plist.path)
        var args = ["autostart", "--name", vm.name]
        if installed { args.append("--disable") }
        backend.run(args, onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
    }

    /// 用原生 macOS 窗口打开虚拟机。
    ///
    /// 只能通过重启实现：显示模式（-display）是 QEMU 的启动参数，跑起来之后
    /// 改不了。所以后端会把这次重启说清楚（warn 一句），不是静默重启。
    /// 无窗口跑的 VM 才需要走这条；本来就有窗口的只是带到前台。
    ///
    /// **没在运行的时候不走这条。** 原来按钮在关机状态也摆着，点下去后端 die
    /// 一句「VM 没在运行，先 start」——一个纯报错的对话框，用户除了点「好」
    /// 什么都不能做，而他的意图（看画面）根本没被回应。2026-11-10 用户截图报
    /// 的就是这个。这时候调用方应该走 startWithWindow，见那儿。
    func nativeWindow(_ vm: VMInfo) {
        backend.run(["window", "--name", vm.name], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
    }

    /// 没在运行的时候「用系统窗口打开」：一步启动 + 原生窗口。
    ///
    /// 走 `start --window` 而不是先 start 再 window：后者要等一次完整的启动
    /// （Linux 要好几十秒）再发第二条命令，中间任何一步失败用户就看到一半；
    /// 而 `--window` 就是把 headless 置成 false 再启动，一次 QEMU 就带着
    /// 原生窗口起来，和用户点的是同一个意思。
    ///
    /// 已经在跑也不会坏事：后端 start_linux 看到 pid 活着就回一句
    /// 「VM 已在运行」然后返回，不会起第二个 QEMU。刷新之后按钮自己会变回
    /// 「用系统窗口打开」。
    func startWithWindow(_ vm: VMInfo) {
        backend.run(["start", "--name", vm.name, "--window"],
                    onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
    }

    /// 改一个已经建好的虚拟机的配置。
    ///
    /// CPU、内存、显示模式、端口、挂不挂光盘，改完都要重启才生效——它们全是 QEMU
    /// 的启动参数。后端不偷偷重启，要么用户自己点重启，要么走 `restartAndApply`
    /// 一键重启。
    func configure(_ vm: VMInfo, settings: VMSettings,
                   completion: @escaping () -> Void) {
        var args = ["configure", "--name", vm.name,
                    "--cpu", String(settings.cpus), "--mem", settings.ram]
        if settings.disk != vm.disk { args += ["--disk", settings.disk] }
        if settings.user != vm.user { args += ["--user", settings.user] }
        if settings.password != VMSettings.unchangedPassword {
            args += ["--password", settings.password]
        }
        if settings.headless != vm.headless {
            args += [settings.headless ? "--headless" : "--window"]
        }
        // 基线必须是 vm.attachISO，不是 vm.isLocalWithISO。后者还看「安装包文件
        // 还在不在」：ISO 被用户移走之后它变 false 而 s.attachISO 还是 true，
        // 于是什么都没改也判成要改，点「保存」传 --iso，后端 die「安装包不见了」。
        if settings.attachISO != vm.attachISO {
            args += [settings.attachISO ? "--iso" : "--no-iso"]
        }
        if settings.sshPort != vm.sshPort {
            args += ["--ssh-port", String(settings.sshPort)]
        }
        if settings.vncPort != vm.vncPort {
            args += ["--vnc-port", String(settings.vncPort)]
        }
        // rdp / web 端口只有真的改过才传。**不能拿 0 当「没改」的哨兵。**
        // VMSettings 把 nil 存成 0，而 catalog Linux 虚拟机和本地包装的虚拟机
        // 根本没有这两项（vm.rdpPort 是 nil）——`0 != nil` 成立，于是每次打开
        // 配置面板保存都会传一个 `--rdp-port 0`，vmkit 的 take_port(0) 直接
        // die「远程桌面 端口要给 1024 到 65535 之间，你给了 0」，整条 configure
        // 什么都还没改就退出。
        if let r = settings.rdpPort, r != vm.rdpPort {
            args += ["--rdp-port", String(r)]
        }
        if let w = settings.webPort, w != vm.webPort {
            args += ["--web-port", String(w)]
        }
        backend.run(args, onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
            completion()
        }
    }

    /// 一键重启，让改过的配置生效。
    ///
    /// 后端把「关机 + 开机」合成一步。没在运行就直接启动，不报错——用户点
    /// 「保存并重启」时期望的就是起来。
    func restart(name: String, completion: @escaping () -> Void = {}) {
        backend.run(["restart", "--name", name], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
            completion()
        }
    }

    // ------------------------------------------------------ 附加盘 --

    func diskAdd(name: String, size: String, format: String, bus: String,
                 completion: @escaping () -> Void = {}) {
        backend.run(["disk", "add", "--name", name, "--size", size,
                     "--format", format, "--bus", bus], onEvent: handleCommandEvent)
            { [weak self] in
                self?.refreshStatus(); completion()
            }
    }

    /// 摘一块附加盘。会连文件一起删，界面上必须先让用户确认过。
    func diskRemove(name: String, disk: String, keepFile: Bool,
                    completion: @escaping () -> Void = {}) {
        var args = ["disk", "remove", "--name", name, "--disk", disk]
        if keepFile { args.append("--keep-file") }
        backend.run(args, onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus(); completion()
        }
    }

    // ------------------------------------------------------ 快照 --

    func snapshotList(name: String) {
        backend.run(["snapshot", "list", "--name", name], onEvent: handleSnapshots)
    }

    private func handleSnapshots(_ ev: [String: Any]) {
        guard ev["t"] as? String == "done",
              let vm = ev["vm"] as? [String: Any],
              let arr = vm["snapshots"] as? [[String: Any]] else { return }
        let list = arr.compactMap { d -> VMSnapshot? in
            guard let tag = d["tag"] as? String else { return nil }
            return VMSnapshot(id: d["id"] as? String ?? tag, tag: tag,
                              size: d["size"] as? String ?? "",
                              date: d["date"] as? String ?? "")
        }
        DispatchQueue.main.async { [weak self] in self?.snapshots = list }
    }

    func snapshotCreate(name: String, tag: String,
                        completion: @escaping () -> Void = {}) {
        backend.run(["snapshot", "create", "--name", name, "--tag", tag],
                    onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
            self?.snapshotList(name: name)
            completion()
        }
    }

    /// 回滚。这个点之后的所有改动都会没，界面上必须先确认过。
    func snapshotApply(name: String, tag: String,
                       completion: @escaping () -> Void = {}) {
        backend.run(["snapshot", "apply", "--name", name, "--tag", tag],
                    onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
            self?.snapshotList(name: name)
            completion()
        }
    }

    func snapshotDelete(name: String, tag: String,
                        completion: @escaping () -> Void = {}) {
        backend.run(["snapshot", "delete", "--name", name, "--tag", tag],
                    onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
            self?.snapshotList(name: name)
            completion()
        }
    }

    // ------------------------------------------------------ 共享目录 --

    func shareAdd(name: String, tag: String, path: String,
                  completion: @escaping () -> Void = {}) {
        backend.run(["share", "add", "--name", name, "--tag", tag,
                     "--path", path], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus(); completion()
        }
    }

    func shareRemove(name: String, tag: String,
                     completion: @escaping () -> Void = {}) {
        backend.run(["share", "remove", "--name", name, "--tag", tag],
                    onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus(); completion()
        }
    }

    // ------------------------------------- macOS 客人机的共享目录与端口转发 --

    /// 存 macOS 客人机的共享目录和端口转发（virtiofs / vsock）。
    ///
    /// 为什么不像 CPU、内存那样走 vmkit configure：这两项是 macvm.json 里
    /// MacVMSpec 的字段，而 app 本来就直接读这份配置（startMacVM 里就是
    /// MacVMSpec.load）。再开一条 vmkit → vmz 的路，只是多传一遍参数、
    /// 多一处校验。写盘是原子的（macSaveSpec 用 .atomic 并设 0600，
    /// 和 vmkit 的 configure_macos 同一套规矩）。
    ///
    /// 调用方要先校验过（MacVMPaths.validateShares / validateForwards），
    /// 这里只管存——错误信息由界面就地显示，比弹一个后端错误好用。
    func macSaveDevices(name: String, shares: [MacVMShare],
                        forwards: [MacVMForward]) throws {
        guard var spec = MacVMSpec.load(name: name) else {
            throw MacVMError("读不到 macOS 虚拟机的配置（~/.agentvm/vms/\(name)/macvm.json）")
        }
        try MacVMPaths.validateShares(shares)
        try MacVMPaths.validateForwards(forwards)
        spec.shares = shares
        spec.forwards = forwards
        try spec.save()
    }

    // ------------------------------------------------------ 克隆 --

    func clone(from name: String, to newName: String,
               completion: @escaping () -> Void = {}) {
        backend.run(["clone", "--name", name, "--new-name", newName],
                    onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus(); completion()
        }
    }

    // ------------------------------------------------------------ 工具 --

    static func bytes(_ n: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file)
    }
}
