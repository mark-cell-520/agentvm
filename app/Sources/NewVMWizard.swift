import SwiftUI
import AppKit

/// 让承载本视图的 sheet 窗口可以缩放，并且一开始就给够高度。
///
/// 为什么必须走 AppKit：**SwiftUI 的 sheet 默认拖不动。** 2026-11-11 实测，
/// 用 AXSetSize 直接拉新建虚拟机那张 sheet，返回 kAXErrorCannotSetAttributeValue，
/// 尺寸一动不动；`.frame(minWidth:idealWidth:)` 也只让 sheet 拿到最小尺寸
/// （760x560），缩放按钮依然不出现。
///
/// 而这张 sheet 里的配置表单实测 744pt 高、可视区只有 464pt——磁盘大小、
/// 登录名、密码整段落在下面 280pt，用户既拖不大窗口、macOS 又把滚动条藏了，
/// 于是「下面的东西选不了」。用户原话：「无法进行选择，也无法放缩小」。
///
/// 能用的只有这一手：找到 sheet 那个 NSWindow，往 styleMask 里塞 .resizable，
/// 再把初始高度补到内容需要的大小。onAppear 之后窗口已经存在；延后几次是
/// 为了赶上 sheet 的出场动画（动画中 setContentSize 会被动画盖掉）。
struct ScalableSheet: ViewModifier {
    var minHeight: Double = 700

    func body(content: Content) -> some View {
        content.onAppear {
            for delay in [0.0, 0.2, 0.5, 1.0] {
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                    for w in NSApp.windows where w.sheetParent != nil {
                        if !w.styleMask.contains(.resizable) {
                            w.styleMask.insert(.resizable)
                        }
                        if w.frame.height < minHeight {
                            w.setContentSize(NSSize(
                                width: max(w.frame.width, 900), height: minHeight))
                        }
                    }
                }
            }
        }
    }
}

/// 向导的步骤。提到包作用域，好让 AppState 指定「打开时直接停在安装页」。
enum WizardStep: Int, CaseIterable {
    case pick = 0, config = 1, installing = 2, done = 3
    var title: String {
        switch self {
        case .pick: return "选一个系统"
        case .config: return "配置"
        case .installing: return "正在安装"
        case .done: return "完成"
        }
    }
}

/// 新建虚拟机向导：选系统 → 配置 → 装。
struct NewVMWizard: View {
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) private var dismiss

    /// 打开时停在哪一步。接上没装完的安装时用 .installing。
    let startStep: WizardStep
    @State private var step: WizardStep

    init(startStep: WizardStep = .pick) {
        self.startStep = startStep
        _step = State(initialValue: startStep)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
            Divider()
            footer
        }
        // minWidth/minHeight 是缩放的下限，也是 ScalableSheet 补尺寸之前
        // 那一瞬的初始尺寸。真正的「能拖大」靠 ScalableSheet（见文件顶部注释）：
        // SwiftUI 的 sheet 不会因为给了 idealWidth 就带缩放按钮。
        .frame(minWidth: 800, idealWidth: 900,
               minHeight: 640, idealHeight: 700)
        .modifier(ScalableSheet(minHeight: 720))
    }

    // ------------------------------------------------------------ 框架 --

    private var header: some View {
        HStack {
            Text("新建虚拟机").font(.title2).bold()
            Spacer()
            // 步骤条。四步都走完才显示成完成，中间步骤只点亮当前和已过的。
            HStack(spacing: 6) {
                ForEach(WizardStep.allCases, id: \.rawValue) { s in
                    Circle()
                        .fill(s.rawValue <= step.rawValue
                              ? Color.accentColor : Color.secondary.opacity(0.25))
                        .frame(width: 7, height: 7)
                }
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    @ViewBuilder
    private var content: some View {
        switch step {
        case .pick: DistroPicker()
        case .config: ConfigForm(isLocalPackage: state.pickedDistro?.isLocal == true)
        case .installing: InstallProgress()
        case .done: DoneView()
        }
    }

    @ViewBuilder
    private var footer: some View {
        VStack(spacing: 0) {
            // 「开始安装」点不了的时候，必须在这儿说清楚差什么。
            //
            // 原来是光把按钮置灰：用户看到的是一颗灰钮、左边一个转圈，
            // 既不知道该补哪一项，也分不清是自己在等还是 app 卡住了。
            // 2026-11-11 用户截图报「一直无法安装」，界面零解释。
            if step == .config, let why = installBlockedReason {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .font(.caption)
                    Text(why)
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .foregroundStyle(.orange)
                .padding(.horizontal, 20)
                .padding(.bottom, 6)
            }
            HStack {
                if state.backend.busy {
                    ProgressView().scaleEffect(0.6).frame(width: 18)
                    Text(state.backend.stageLabel.isEmpty
                         ? "后端正在执行上一条命令" : state.backend.stageLabel)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                if step == .pick {
                    Button("取消") { dismiss() }
                    Button("下一步") { step = .config }
                        .disabled(state.pickedDistro == nil)
                        .keyboardShortcut(.defaultAction)
                } else if step == .config {
                    Button("上一步") { step = .pick }
                    Button("开始安装") { state.startInstall(); step = .installing }
                        .disabled(installBlockedReason != nil)
                        .keyboardShortcut(.defaultAction)
                } else if step == .installing {
                // 安装页必须永远有出口。
                //
                // 原来这里只有「停止」一个按钮，而它 .disabled(!busy)：安装失败
                // 或被停止之后 busy 归零、按钮虽然能点但 cancel() 打在一个早就
                // 死掉的进程上什么也不做；installFinished 又永远不来（失败时收
                // 不到能解成 VMInfo 的 done），step 于是钉在 .installing。
                // 用户被关在这一页上，只能强杀 app。实测踩过（2026-11-10，
                // 用户原话「一直跳出无法关闭」）。
                if state.backend.busy {
                    Button("停止") { state.backend.cancel() }
                }
                // 关掉向导不等于取消安装：它是脱离 app 的后台任务，顶部横幅
                // 会继续显示进度。这句话必须说，不然用户以为一关就没了。
                Button("关闭（后台继续装）") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            } else {
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            }
            .padding(.horizontal, 20)
            .padding(.top, 10)
            .padding(.bottom, 12)
        }
        .onChange(of: state.installFinished) { _, finished in
            if finished { step = .done }
        }
        .onChange(of: state.installEnded) { _, ended in
            // 安装进程结束了，但没等到「装好了」——失败、被用户停止、
            // 或者后端发来的 done 形状解不出来。必须让用户离开这一页，
            // 否则就是上面那个没有出口的陷阱。回到配置页（能改名再装一次）；
            // 没有选过系统时（app 重启后接上来的安装）只能回第一步。
            if ended && !state.installFinished {
                step = state.pickedDistro == nil ? .pick : .config
            }
        }
    }

    /// 「开始安装」为什么点不了。nil 就是能装。
    ///
    /// 原来是把这些判据全塞进 `canInstall` 一个布尔量，置灰之后界面上
    /// 一个字都不解释。用户对着灰钮完全不知道该补什么——2026-11-11 的
    /// 原话是「一直无法安装」，而他唯一能看到的线索是页脚一个转圈。
    /// 现在同一套判据既决定按钮亮不亮，也直接生成给人看的那句话。
    private var installBlockedReason: String? {
        if state.backend.busy {
            return "后端正忙，等它跑完（左边那个转圈停下）再点"
        }
        guard state.pickedDistro != nil else { return "先回上一步选一个系统" }
        let name = state.vmName.trimmingCharacters(in: .whitespaces)
        if name.isEmpty { return "给虚拟机起个名字" }
        if !nameOK { return "名字只能以字母或数字开头，其余用字母、数字、. - _，最长 63 个字符" }
        if state.vmPassword.count < 4 { return "密码至少 4 位，客人机第一次登录要用" }
        return nil
    }

    /// 名字合不合法。判据必须和 vmkit 的 check_vm_name 逐字一致：
    /// 首字符是字母或数字，其余可以是字母、数字、点、- 或 _，总长不超过 63
    /// （见 scripts/vmkit.py 的 _VM_NAME_RE）。
    private var nameOK: Bool {
        let name = state.vmName.trimmingCharacters(in: .whitespaces)
        guard let first = name.unicodeScalars.first,
              CharacterSet.alphanumerics.contains(first) else { return false }
        let tailOK = name.unicodeScalars.dropFirst().allSatisfy {
            CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz"
                                     + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                     + "0123456789._-").contains($0)
        }
        return tailOK && name.unicodeScalars.count <= 63
    }
}

// ---------------------------------------------------------------- 选系统 --

private struct DistroPicker: View {
    @EnvironmentObject var state: AppState

    private let columns = [GridItem(.adaptive(minimum: 210), spacing: 14)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Windows 是完整安装，要跑一遍安装程序；Linux 用的是装好的云镜像，下载完就能登；"
                     + "macOS 从 Apple 官方恢复镜像装，全程自动但要下一大会儿。")
                    .font(.callout)
                    .foregroundStyle(.secondary)

                if state.distros.isEmpty {
                    HStack {
                        ProgressView().scaleEffect(0.7)
                        Text("正在读镜像目录…").foregroundStyle(.secondary)
                    }
                } else {
                    LazyVGrid(columns: columns, spacing: 14) {
                        ForEach(state.distros) { d in
                            DistroCard(d: d, picked: state.pickedDistro?.id == d.id)
                                .onTapGesture { state.pick(d) }
                        }
                    }
                }

                localFileRow
            }
            .padding(20)
        }
    }

    /// 本地安装包入口。
    ///
    /// 放在列表外面而不是塞进网格：catalog 是「我们替你选好、验过校验和的」，
    /// 本地文件不是——没有校验和、没有推荐配置、安装过程还得用户自己点。
    /// 混在一起会让人以为两者一样可靠。
    ///
    /// 两个入口：选一个已经下好的文件，或者贴一个网址让后端去下。后者照顾
    /// 「手动下载系统」——有些系统的官网只给下载页，用户把 ISO 直链贴过来就行，
    /// 不用先自己下到本地再选一遍。
    @ViewBuilder
    private var localFileRow: some View {
        Divider()
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: "internaldrive")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text("用自己的安装包装").font(.headline)
                    Text("选一个本地 ISO 或镜像文件，或者贴一个下载网址。"
                         + "安装过程要你自己在虚拟机画面里点完，"
                         + "我们不代劳——这类包没有应答文件可喂。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                if state.pickedDistro?.isLocal == true {
                    Label(state.pickedDistro?.name ?? "", systemImage: "checkmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(Color.accentColor)
                        .lineLimit(1)
                }
                Button("选择文件…") { pickLocalFile() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            }

            HStack(spacing: 8) {
                Image(systemName: "link")
                    .foregroundStyle(.secondary)
                TextField("https://example.com/system-arm64.iso",
                          text: $localURL)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.callout, design: .monospaced))
                Button("用这个网址") {
                    let u = localURL.trimmingCharacters(in: .whitespaces)
                    guard u.hasPrefix("http://") || u.hasPrefix("https://") else { return }
                    state.pick(.fromURL(u))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(!localURL.trimmingCharacters(in: .whitespaces)
                    .hasPrefix("http"))
            }
        }
    }

    @State private var localURL = ""

    private func pickLocalFile() {
        let panel = NSOpenPanel()
        panel.title = "选择系统安装包"
        panel.message = "选一个要装进虚拟机的 ISO 或磁盘镜像"
        // 默认停在下载文件夹：用户说包就在那儿
        panel.directoryURL = FileManager.default.urls(
            for: .downloadsDirectory, in: .userDomainMask).first
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        // 不设 allowedFileTypes：默认就是所有文件。有些安装镜像没有后缀
        // （或后缀不是 iso），限制了就选不了。新版 API 的
        // allowedContentTypes 同理，不去设它。
        if panel.runModal() == .OK, let url = panel.url {
            state.pick(.local(path: url.path))
        }
    }
}

private struct DistroCard: View {
    @EnvironmentObject var state: AppState
    let d: Distro
    let picked: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: d.isWindows || d.kind == "macos" ? "desktopcomputer" : "terminal")
                    .font(.title2)
                    .foregroundStyle(d.isWindows ? .blue
                                   : (d.kind == "macos" ? .purple : .orange))
                Spacer()
                if picked {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(Color.accentColor)
                }
            }
            Text(d.name).font(.headline).lineLimit(1)
            Text(d.subtitle)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)

            Divider()
            HStack {
                Label(d.sizeText, systemImage: "arrow.down.circle")
                Spacer()
                Text(d.timeText)
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            Text(d.checksumText)
                .font(.caption2)
                .foregroundStyle(d.sha512 != nil || d.sha256 != nil ? .green : .orange)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(picked ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.04),
                    in: RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(picked ? Color.accentColor : Color.clear, lineWidth: 1.5)
        )
        .contentShape(Rectangle())
    }
}

// ------------------------------------------------------------ 安装进度 --

private struct InstallProgress: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(state.backend.stageLabel.isEmpty ? "准备中" : state.backend.stageLabel)
                        .font(.headline)
                    Spacer()
                    if state.backend.progress > 0 {
                        Text("\(Int(state.backend.progress * 100))%")
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
                // 自动安装那一阶段没有百分比可显示（后端不知道安装器走到第几步），
                // 进度一直是 0。这时候画一条空进度条会让用户以为卡死了，所以换成
                // 不确定进度的转圈样式；下载阶段有真百分比，照旧画确定的。
                // （Swift 注释里不能有全角标点，所以这里的括号逗号都是半角。）
                if state.backend.progress > 0 {
                    ProgressView(value: state.backend.progress)
                } else {
                    ProgressView()
                }
                if !state.backend.progressDetail.isEmpty {
                    Text(state.backend.progressDetail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            screenRow

            LogView(lines: state.backend.logLines)

            if let err = state.backend.errorMessage {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
        .padding(20)
    }

    /// 虚拟机画面。这是一个真 VNC 客户端，不是隔几秒抓一张图——Windows 的
    /// Setup 和本地包的图形安装器都要人在画面上点，抓图点不了。
    ///
    /// 本地包那条路现在默认由后端自动点完（见 scripts/autoinstall.py），这个
    /// 控制台的作用从「让人点」变成「让人看见它在点什么」——自动安装中途想接手，
    /// 直接在这儿点就行。
    ///
    /// 端口从 status 里拿：装到「启动安装器」那一步之前 VM 还没起来，端口
    /// 也拿不到，这时先显示一句在等什么，别画一个永远连不上的控制台。
    @ViewBuilder
    private var screenRow: some View {
        // macOS 客人机没有 VNC：VZMacOSInstaller 是恢复镜像安装，全程自动，
        // 没有要人在画面上点的步骤（这正是它和 Windows Setup 的区别）。
        // 摆一个 VNC 客户端在这儿只会永远转圈。
        if state.watchingInstall != nil,
           let vm = state.vms.first(where: { $0.name == state.watchingInstall }),
           vm.isMacOS {
            VStack(alignment: .leading, spacing: 6) {
                Label("macOS 安装是全自动的，不需要人在画面上点", systemImage: "checkmark.shield")
                    .font(.callout)
                Text("正在从 Apple 服务器下载恢复镜像并写入磁盘。这一步要 40 到 60 分钟，"
                     + "进度看上面的进度条和下面的日志。可以关掉这个窗口，安装不受影响。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.blue.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
        } else if let name = state.watchingInstall, let port = vncPort(for: name) {
            ConsoleView(name: name, port: port, embedded: true)
                // 端口从无到有只会变一次，但每次变都必须重建客户端：
                // 旧的那个连的是不存在的端口。
                .id(port)
                .frame(height: 300)
        } else {
            HStack(spacing: 10) {
                ProgressView().scaleEffect(0.7)
                Text("虚拟机还没起来，画面要等它启动")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(.black.opacity(0.85), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    private func vncPort(for name: String) -> Int? {
        guard let vm = state.vms.first(where: { $0.name == name }) else { return nil }
        // macOS 客人机的 vnc_port 是 0（没有 VNC），给 nil 让调用方走
        // 「等它起来」那条路，而不是拿 0 去连一个不存在的端口。
        return vm.vncPort > 0 ? vm.vncPort : nil
    }
}

private struct LogView: View {
    let lines: [Backend.LogLine]
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(lines) { line in
                        Text(line.text)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(color(for: line.level))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .id(line.id)
                    }
                }
                .padding(10)
            }
            .background(.black.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
            .onChange(of: lines.count) {
                if let last = lines.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }

    private func color(for level: String) -> Color {
        switch level {
        case "warn": return .orange
        case "error": return .red
        case "stage": return .accentColor
        default: return .primary
        }
    }
}

// ---------------------------------------------------------------- 完成 --

private struct DoneView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: state.installedVM?.isLocal == true
                  ? "arrow.right.circle.fill" : "checkmark.circle.fill")
                .font(.system(size: 52))
                .foregroundStyle(state.installedVM?.isLocal == true
                                 ? .blue : .green)
            Text(state.installedVM?.isLocal == true ? "安装器已启动" : "装好了")
                .font(.title2).bold()

            if let vm = state.installedVM {
                if vm.isLocal {
                    localDone(vm)
                } else {
                    VStack(alignment: .leading, spacing: 8) {
                        row("名字", vm.name)
                        row("状态", vm.stateText)
                        if !vm.sshCommand.isEmpty { row("SSH", vm.sshCommand) }
                    }
                    .padding(14)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                }
            }

            Text(state.installedVM?.isLocal == true
                 ? "剩下的在「控制台」里点完：鼠标点进去就能操作，按 Esc 释放。"
                 : "虚拟机已经跑起来了。要看画面点「控制台」，"
                   + "鼠标点进去会被虚拟机捕获，按 Esc 释放。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// 本地安装包的完成态。这里没有「装好了」可讲——安装器刚起来，系统还没装。
    /// 说清楚接下来要干什么，比显示一个绿色的勾要紧。
    @ViewBuilder
    private func localDone(_ vm: VMInfo) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("接下来在这个窗口下面的「控制台」里完成安装：")
                .font(.callout.weight(.medium))
            step("1", "选安装位置，我们建的盘会以 vda 出现")
            step("2", "确认分区大小，下一步，确认格式化")
            step("3", "等它复制完文件、设好网络，然后重启")
            Text("重启后它会再次进入安装器，这是正常的——直接关机，"
                 + "然后在左边选中它点「启动」，就从硬盘进系统了。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                Button("打开控制台") {
                    // **必须先撤下自己，再让控制台出来，中间隔一帧。**
                    // 原来 `state.consoleVM = vm; dismiss()` 写在一起：同一次
                    // 状态更新里既撤销本 sheet 又呈现另一个，SwiftUI 对这种事
                    // 的处理不可靠——结果是 consoleVM 留着非 nil、控制台 sheet 却
                    // 不显示，而此后在详情页点「控制台」也不会再弹（item 没变），
                    // 用户完全没有入口。2026-11-10 实测踩过。
                    dismiss()
                    DispatchQueue.main.async {
                        state.consoleVM = vm
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                Button("用系统窗口打开") {
                    state.nativeWindow(vm)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("会重启一次虚拟机——显示模式改不了，只能重启")
            }

            if let web = vm.webPort {
                Divider()
                HStack {
                    Text("Web 界面").foregroundStyle(.secondary)
                        .frame(width: 72, alignment: .leading)
                    Text("http://127.0.0.1:\(web)")
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                    Spacer()
                    Button("复制") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("http://127.0.0.1:\(web)",
                                                       forType: .string)
                    }
                    .buttonStyle(.borderless)
                }
                Text("装完启动后，用这个地址打开系统的 Web 界面。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
    }

    private func step(_ n: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(n).font(.caption.weight(.bold))
                .frame(width: 16, height: 16)
                .background(Color.accentColor.opacity(0.18), in: Circle())
            Text(text).font(.callout)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k).foregroundStyle(.secondary).frame(width: 56, alignment: .leading)
            Text(v).font(.system(.callout, design: .monospaced))
                .textSelection(.enabled)
        }
    }
}
