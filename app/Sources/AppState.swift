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
    func resetWizard() {
        vmName = ""
        vmPassword = "Agentvm2026"
        showPassword = false
        pickedDistro = nil
        installFinished = false
        installedVM = nil
        wizardStep = nil
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
        DispatchQueue.main.async { [weak self] in self?.vms = vms }
    }

    func runCheck() {
        backend.run(["check"], onEvent: handleCheck)
    }

    private func handleCheck(_ ev: [String: Any]) {
        guard ev["t"] as? String == "check" else { return }
        let items = (ev["items"] as? [[String: Any]] ?? []).compactMap { d -> CheckItem? in
            guard let data = try? JSONSerialization.data(withJSONObject: d)
                else { return nil }
            return try? JSONDecoder().decode(CheckItem.self, from: data)
        }
        DispatchQueue.main.async {
            self.checkItems = items
            self.checkOk = (ev["ok"] as? Bool) ?? false
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
        backend.clearLog()

        var args: [String]
        if let u = d.sourceURL {
            // 用户自己贴的网址：后端先下到 <vm>/iso/ 再当本地包装。断点续传
            // 和进度条都是现成的（和 catalog 那条共用 download）。
            args = ["--source-url", u]
        } else if let src = d.sourcePath {
            // 本地安装包。没有 catalog 条目、没有校验和、没有推荐配置，
            // 后端直接走 cmd_install_local——建盘、开控制台，剩下的用户自己点。
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
        }
        watchingInstall = vmName
        backend.setWatching(vmName)
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
        backend.resumeTailing(newest.dir.appendingPathComponent("install.log"),
                              pid: pid,
                              onEvent: handleEvent) { [weak self] in
            self?.refreshStatus()
            self?.watchingInstall = nil
            self?.backend.setWatching(nil)
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

    func action(_ cmd: String, name: String) {
        backend.run([cmd, "--name", name], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
    }

    func deleteVM(_ vm: VMInfo) {
        backend.run(["delete", "--name", vm.name], onEvent: handleCommandEvent) { [weak self] in
            self?.refreshStatus()
        }
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
    func nativeWindow(_ vm: VMInfo) {
        backend.run(["window", "--name", vm.name], onEvent: handleCommandEvent) { [weak self] in
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
