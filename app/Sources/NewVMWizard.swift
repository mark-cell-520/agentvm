import SwiftUI
import AppKit

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
        .frame(width: 760, height: 560)
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
        HStack {
            if state.backend.busy {
                ProgressView().scaleEffect(0.6).frame(width: 18)
                Text(state.backend.stageLabel).foregroundStyle(.secondary)
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
                    .disabled(!canInstall)
                    .keyboardShortcut(.defaultAction)
            } else if step == .installing {
                Button("停止") { state.backend.cancel() }
                    .disabled(!state.backend.busy)
            } else {
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .onChange(of: state.installFinished) { _, finished in
            if finished { step = .done }
        }
    }

    private var canInstall: Bool {
        guard let d = state.pickedDistro else { return false }
        let name = state.vmName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return false }
        // 名字会当目录名和主机名用，只放字母数字和 - _
        let allowed = name.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
        // 密码下限。VZMacGuestProvisioningOptions 会校验，太短的话第一次启动
        // 静默不建账号——界面看不出问题，用户登不进去也不知道为什么。
        // 在这儿就挡住，比装完 40 分钟才发现强。
        let pwOK = state.vmPassword.count >= 4
        return allowed && pwOK && !state.backend.busy && !d.id.isEmpty
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
                ProgressView(value: state.backend.progress)
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

    /// 虚拟机画面。这是一个真 VNC 客户端，不是隔几秒抓一张图——Windows 和
    /// 本地包的安装过程都要人在画面上点，抓图点不了。
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
            ConsoleView(name: name, port: port)
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
                    state.consoleVM = vm
                    dismiss()
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
