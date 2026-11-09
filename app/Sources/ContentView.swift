import SwiftUI

struct ContentView: View {
    @EnvironmentObject var state: AppState
    @State private var selection: String?
    @State private var showNewVM = false

    /// 有安装在进行时显示的进度横幅。安装是脱离 app 的后台任务，用户可能刚
    /// 重新打开 app，也可能中途把向导关了——不管哪种都得让他看见还在装。
    @ViewBuilder
    private var installBanner: some View {
        if state.watchingInstall != nil {
            HStack(spacing: 12) {
                ProgressView(value: state.backend.progress)
                    .frame(width: 120)
                VStack(alignment: .leading, spacing: 2) {
                    Text(state.backend.stageLabel.isEmpty
                         ? "正在安装 \(state.watchingInstall ?? "")"
                         : state.backend.stageLabel)
                        .font(.callout.weight(.medium))
                    if !state.backend.progressDetail.isEmpty {
                        Text(state.backend.progressDetail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("查看详情") {
                    state.wizardStep = .installing
                    showNewVM = true
                }
                .buttonStyle(.borderless)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(.blue.opacity(0.10))
        }
    }

    private var selected: VMInfo? {
        guard let selection else { return nil }
        return state.vms.first { $0.name == selection }
    }

    /// 错误弹窗的内容。用一个可识别的包装，这样 .alert(item:) 才能在用户点「好」
    /// 之后真的把自己关掉（.constant 绑定做不到，见下面那处的注释）。
    private var alertError: AlertError? {
        guard let m = state.backend.errorMessage, !m.isEmpty else { return nil }
        return AlertError(message: m)
    }

    var body: some View {
        VStack(spacing: 0) {
            installBanner
            NavigationSplitView {
            VStack(spacing: 0) {
                // 侧栏自己带头部，按钮放在这里而不是 .toolbar。
                // .toolbar 在这个窗口配置下实测不生效：AppKit 里只能枚举到
                // 系统自带的「隐藏边栏」和「更多工具栏项」，自己的按钮根本不
                // 进工具栏。改成界面里的真按钮最稳，不跟 AppKit 赌。
                HStack(spacing: 8) {
                    Text("虚拟机").font(.headline)
                    Spacer()
                    Button {
                        state.refreshStatus()
                        state.runCheck()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .help("刷新")

                    Button {
                        showNewVM = true
                    } label: {
                        Image(systemName: "plus")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .help("新建虚拟机")
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)

                Divider()

                List(selection: $selection) {
                    if state.vms.isEmpty {
                        Text("还没有虚拟机")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(state.vms) { vm in
                            Label {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(vm.name).font(.headline)
                                    Text(vm.stateText)
                                        .font(.caption)
                                        .foregroundStyle(vm.running ? .green : .secondary)
                                }
                            } icon: {
                                Image(systemName: vm.isWindows
                                      ? "desktopcomputer" : "terminal")
                            }
                            .tag(vm.name)
                        }
                    }
                }
            }
            .navigationTitle("虚拟机")
        } detail: {
            if let vm = selected {
                VMDetailView(vm: vm)
            } else {
                // 空状态。工具栏在某些 macOS 配置下不一定渲染，所以这里放一个
                // 显眼的主按钮兜底——「一键装虚拟机」的入口不能只靠工具栏。
                VStack(spacing: 16) {
                    Image(systemName: "cube.box")
                        .font(.system(size: 48))
                        .foregroundStyle(.secondary)
                    Text("在左边选一个虚拟机，或新建一个")
                        .foregroundStyle(.secondary)
                    Button {
                        showNewVM = true
                    } label: {
                        Label("新建虚拟机", systemImage: "plus")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .sheet(isPresented: $showNewVM) {
            NewVMWizard(startStep: state.wizardStep ?? .pick)
                .environmentObject(state)
        }
        // 每次打开新建向导都先把表单清干净，别把上一次填的名字带给下一次。
        // 见 AppState.resetWizard() 的注释：不清会让第二个虚拟机必然重名失败，
        // 而失败还停在一个关不掉的「正在安装」页面上。
        .onChange(of: showNewVM) { _, open in
            if open { state.resetWizard() }
        }
        // 控制台单独一个窗口/sheet。用 item 而不是布尔量：关掉的时候要把
        // consoleVM 清空，否则下一次打开同一个 VM 时 SwiftUI 认为内容没变，
        // 不会重建——而那个客户端连着上一次的连接。
        .sheet(item: $state.consoleVM) { vm in
            ConsoleView(name: vm.name, port: vm.vncPort)
                .frame(minWidth: 860, minHeight: 620)
        }
        .sheet(item: $state.settingsVM) { vm in
            SettingsView(vm: vm)
                .environmentObject(state)
        }
        .sheet(item: $state.storageVM) { vm in
            StorageView(vm: vm)
                .environmentObject(state)
        }
        .onChange(of: state.vms.count) {
            // 装完之后 selection 还指向旧名字就什么都看不到，自动选中新的
            if let new = state.installedVM, selection != new.name {
                selection = new.name
            }
        }
        // 原来是 `.constant(state.backend.errorMessage != nil)`：错误弹窗出现后
        // 点「好」，errorMessage 被置 nil，但绑定是常量，SwiftUI 认为仍处于
        // presented 状态，弹窗关不掉。用 item: 绑在一个可识别的包装上。
        .alert(item: Binding(get: { alertError }, set: { _ in
            state.backend.errorMessage = nil
        })) { e in
            Alert(title: Text("出错"), message: Text(e.message),
                  dismissButton: .default(Text("好")))
        }
        }
    }
}

/// 一个虚拟机的详情页：状态、登录方式、开关机等操作。
struct VMDetailView: View {
    @EnvironmentObject var state: AppState
    let vm: VMInfo

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {

                header

                if !state.checkOk {
                    warningBanner
                }

                GroupBox("状态") { infoGrid }
                GroupBox("操作") { actions }

                if vm.ready {
                    GroupBox("怎么连上去") { loginInfo }
                }

                Spacer()
            }
            .padding(24)
        }
        .navigationTitle(vm.name)
    }

    // ------------------------------------------------------------ 各部分 --

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: vm.isWindows ? "desktopcomputer" : "terminal")
                .font(.system(size: 40))
                .foregroundStyle(vm.isWindows ? .blue : .orange)
            VStack(alignment: .leading, spacing: 4) {
                Text(vm.name).font(.title).bold()
                Text("\(distroName) · \(vm.cpus) 核 · \(vm.ram) 内存 · \(vm.disk) 磁盘")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            statusPill
        }
    }

    private var statusPill: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(vm.running ? Color.green : Color.gray)
                .frame(width: 9, height: 9)
            Text(vm.stateText)
        }
        .font(.subheadline.weight(.medium))
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(.quaternary, in: Capsule())
    }

    private var warningBanner: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.yellow)
            VStack(alignment: .leading, spacing: 4) {
                Text("宿主机检查没全过").bold()
                ForEach(state.checkItems.filter { !$0.ok }) { item in
                    Text("· \(item.name)：\(item.detail)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(.yellow.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    private var infoGrid: some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
            GridRow { Text("进程").foregroundStyle(.secondary); Text(pidText) }
            GridRow { Text("磁盘已用").foregroundStyle(.secondary); Text(vm.diskUsedText) }
            GridRow { Text("SSH 端口").foregroundStyle(.secondary); Text("127.0.0.1:\(vm.sshPort)") }
            if let rdp = vm.rdpPort {
                GridRow { Text("远程桌面").foregroundStyle(.secondary); Text("127.0.0.1:\(rdp)") }
            }
            GridRow { Text("VNC").foregroundStyle(.secondary); Text("127.0.0.1:\(vm.vncPort)") }
                GridRow { Text("登录账号").foregroundStyle(.secondary); Text("\(vm.user)") }
                if let web = vm.webPort {
                    GridRow { Text("Web 界面").foregroundStyle(.secondary)
                        Text("http://127.0.0.1:\(web)") }
                }
                if let src = vm.source {
                    GridRow { Text("安装包").foregroundStyle(.secondary)
                        Text(URL(fileURLWithPath: src).lastPathComponent)
                            .lineLimit(1)
                            .truncationMode(.middle) }
                }
            }
            .font(.callout)
    }

    private var actions: some View {
        HStack(spacing: 10) {
            if vm.running {
                Button("关机") { state.action("stop", name: vm.name) }
                    .disabled(state.backend.busy)
                // 控制台是日常操作虚拟机的主入口：装系统、看启动、进桌面，
                // 都在这个内嵌的 VNC 画面里。不依赖原生窗口是否存在。
                Button("控制台") { state.consoleVM = vm }
                    .buttonStyle(.borderedProminent)
                    .disabled(state.backend.busy)
                Button("显示窗口") { state.action("open", name: vm.name) }
                    .disabled(state.backend.busy)
            } else {
                Button("启动") { state.action("start", name: vm.name) }
                    .disabled(state.backend.busy)
            }
            // 无窗口跑的 VM 没有窗口可切，只能重启才能有一个——按钮上要说清楚，
            // 不然用户点了发现虚拟机重启了，会以为出了故障。
            Button(vm.headless ? "用系统窗口打开（会重启）" : "用系统窗口打开") {
                state.nativeWindow(vm)
            }
            .disabled(state.backend.busy)

            // 配置。装完之后改 CPU、内存、磁盘都在这儿，不用删了重装。
            Button("配置") { state.settingsVM = vm }
                .disabled(state.backend.busy)
            // 存储：附加盘、快照、共享目录、克隆。
            Button("存储") { state.storageVM = vm }
                .disabled(state.backend.busy)

            Button("开机自启") { state.toggleAutostart(vm) }
                .disabled(state.backend.busy)

            Spacer()

            // 删除是不可逆的，而且删的是整个目录（系统盘、几年数据）。原来点了
            // 立刻就没了，没有任何确认——同一块界面上删快照、删附加盘都做了
            // NSAlert，就这个最重的操作没有。
            Button("删除", role: .destructive) { confirmDelete(vm) }
                .disabled(state.backend.busy)
        }
    }

    private var loginInfo: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let web = vm.webPort {
                row("Web 界面", "http://127.0.0.1:\(web)") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString("http://127.0.0.1:\(web)",
                                                   forType: .string)
                }
            }
            if !vm.sshCommand.isEmpty {
                row("SSH", vm.sshCommand) {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(vm.sshCommand, forType: .string)
                }
            }
            if vm.isWindows {
                row("远程桌面", "Microsoft Remote Desktop 连 127.0.0.1:\(vm.rdpPort ?? 0)") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString("127.0.0.1:\(vm.rdpPort ?? 0)",
                                                   forType: .string)
                }
            } else if vm.webPort == nil {
                // 原文写「打开终端窗口」，实际按钮叫「控制台」。
                row("控制台", "点「控制台」，用 \(vm.user) 和安装时设的密码登录") {}
            }
            if vm.isLocal {
                // 本地装的系统，重启后必须不带光盘启动，否则会退回安装器。
                // 这条坑不写在这儿，用户重启一次就栽一次。
                Text("重启用「启动」即可，会直接从硬盘进系统。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func row(_ label: String, _ value: String, onCopy: @escaping () -> Void)
        -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary).frame(width: 72, alignment: .leading)
            Text(value).font(.system(.callout, design: .monospaced))
                .textSelection(.enabled)
            Spacer()
            Button("复制") { onCopy() }
                .buttonStyle(.borderless)
        }
    }

    /// 删虚拟机前的二次确认。删的是整个目录，包括系统盘和里面几年的数据。
    private func confirmDelete(_ vm: VMInfo) {
        let a = NSAlert()
        a.messageText = "删除虚拟机 \(vm.name)？"
        a.informativeText = "整个目录会一起删掉，系统盘和里面的数据都找不回来：\n\n"
            + Backend.vmsDirectory.appendingPathComponent(vm.name).path
            + "\n\n只是想留个干净基线的话，用「存储」里的克隆，不用删。"
        a.addButton(withTitle: "取消")
        a.addButton(withTitle: "删除")
        a.buttons[1].hasDestructiveAction = true
        a.alertStyle = .warning
        // 默认聚焦放「取消」：误触一下不该删掉一台装好的虚拟机
        a.buttons[0].keyEquivalent = "\r"
        if a.runModal() == .alertSecondButtonReturn {
            state.deleteVM(vm)
        }
    }

    // ------------------------------------------------------------ 计算 --

    private var pidText: String {
        if let pid = vm.pid, vm.running { return "\(pid)" }
        return "未运行"
    }

    private var distroName: String {
        state.distros.first { $0.id == vm.distro }?.name ?? vm.distro
    }
}

import AppKit

/// 错误弹窗的载体。 Identifiable 才能配 .alert(item:)。
private struct AlertError: Identifiable {
    let id = UUID()
    let message: String
}
