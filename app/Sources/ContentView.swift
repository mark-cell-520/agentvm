import SwiftUI

struct ContentView: View {
    @EnvironmentObject var state: AppState
    @State private var selection: String?
    // showNewVM 在 AppState 里：详情页的「重装」按钮要能直接把向导开到安装页，
    // 放在这儿它够不着。

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
                    state.openNewVMWizard(at: .installing)
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
                        state.openNewVMWizard()
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
                        state.openNewVMWizard()
                    } label: {
                        Label("新建虚拟机", systemImage: "plus")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .sheet(isPresented: $state.showNewVM) {
            NewVMWizard(startStep: state.wizardStep ?? .pick)
                .environmentObject(state)
        }
        // 注意：这里原来还有一个 .onChange(of: showNewVM) { resetWizard() }。
        // 它会把「重装一台没装完的虚拟机」设好的 wizardStep = .installing 一起
        // 清掉——用户点「重装」，向导却回到第一步，而他刚从那儿过来。
        // 现在清表单这件事由 AppState.openNewVMWizard() / resetWizard(startAt:)
        // 在打开之前显式做，不再靠 onChange 补刀。
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
        // macOS 虚拟机单独一个 sheet：它的画面是 VZVirtualMachineView 而不是
        // VNC，所以不能用 ConsoleView。见 MacVMWindow 的注释。
        .sheet(item: $state.macVM) { vm in
            MacVMWindow(vm: vm)
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

                if vm.isBroken {
                    brokenBanner
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

    /// 半成品虚拟机的警告条：文件不齐，起不来，只能重装。
    ///
    /// 原来没有这个状态：半成品在列表里和正常虚拟机一模一样，用户点「启动」
    /// 才撞上一句「VM 不完整，缺 disk.qcow2。重新 install。」，而 install 又
    /// 回「已存在，换个名字或加 --force」——两句都是死胡同。在这儿说清楚，
    /// 并把唯一能走的那条路（重装）直接给成一个按钮。
    @ViewBuilder
    private var brokenBanner: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text("这台上次没装完").bold()
            }
            // 不写死「缺的文件：」前缀——broken 现在有两种成因：真缺文件（原来
            // 那种），以及文件在但盘是空的（本地包建了稀疏盘、安装没写进去，
            // 2026-11-11 新增）。后一种点「启动」会落进 UEFI Interactive
            // Shell，一屏英文，比报错难懂得多。两种都直接印后端给的理由。
            //
            // 注意：Swift 的注释里不能出现全角标点（括号、逗号都不行），
            // swiftc 会报「invalid character in source file」而指不出是哪个字。
            // 中文说明照写，标点一律用半角。
            Text("\(vm.broken ?? ""). 现在这样既启动不了，也没法原地修好。"
                 + "重装会清掉它的目录从头装——它没有可用数据，不用担心丢东西。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("重装这一台") { state.reinstall(vm) }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(state.backend.busy)
                Button("删掉它") { confirmDelete(vm) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(state.backend.busy)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
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
            // macOS 客人机一个端口都没有：VZ 的 NAT 不对外监听，VNC 不存在。
            // 后端给的是 0，直接显示「127.0.0.1:0」是自相矛盾——
            // 同一个文件的 loginInfo 里已经说明了 macOS 没有端口。
            if !vm.isMacOS {
                GridRow { Text("SSH 端口").foregroundStyle(.secondary); Text("127.0.0.1:\(vm.sshPort)") }
                GridRow { Text("VNC").foregroundStyle(.secondary); Text("127.0.0.1:\(vm.vncPort)") }
            }
            if let rdp = vm.rdpPort {
                GridRow { Text("远程桌面").foregroundStyle(.secondary); Text("127.0.0.1:\(rdp)") }
            }
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
        // 半成品：文件不齐，启动/控制台/快照这一整套点过去全是同一句死胡同
        // 错误。只给能走的那两条路。用 if/else 而不是提前 return——
        // ViewBuilder 里的 return 在这个 Swift 版本上会触发编译器的
        // 「failed to produce diagnostic」，报错行号离真正的原因很远。
        Group {
        if vm.isBroken {
            HStack(spacing: 10) {
                Button("重装这一台") { state.reinstall(vm) }
                    .buttonStyle(.borderedProminent)
                    .disabled(state.backend.busy)
                Button("删除", role: .destructive) { confirmDelete(vm) }
                    .disabled(state.backend.busy)
                Spacer()
            }
        } else {
            HStack(spacing: 10) {
            if vm.running {
                Button("关机") { state.action("stop", name: vm.name) }
                    .disabled(state.backend.busy)
                if vm.isMacOS {
                    // macOS 客人机没有 VNC，「控制台」这个概念不适用：
                    // 画面是 app 里的 VZVirtualMachineView。所以主入口就是
                    // 这个窗口按钮，点它等于启动 + 显示。
                    Button("打开窗口") { state.startMacVM(vm) }
                        .buttonStyle(.borderedProminent)
                        .disabled(state.backend.busy)
                } else {
                    // 控制台是日常操作虚拟机的主入口：装系统、看启动、进桌面，
                    // 都在这个内嵌的 VNC 画面里。不依赖原生窗口是否存在。
                    Button("控制台") { state.consoleVM = vm }
                        .buttonStyle(.borderedProminent)
                        .disabled(state.backend.busy)
                }
                Button("显示窗口") { state.action("open", name: vm.name) }
                    .disabled(state.backend.busy)
            } else {
                Button("启动") { state.action("start", name: vm.name) }
                    .disabled(state.backend.busy)
            }
            // macOS 客人机没有「原生窗口模式」这个开关：它的窗口就是 app
            // 里的那个 VZVirtualMachineView，不存在第二个显示方式。
            // 摆一个按钮上去点了只会重启，说清楚比不给好。
            if !vm.isMacOS {
                // 无窗口跑的 VM 没有窗口可切，只能重启才能有一个——按钮上要说清楚，
                // 不然用户点了发现虚拟机重启了，会以为出了故障。
                //
                // 关机状态下这个按钮的意思是「启动并用系统窗口打开」。原来关机时
                // 按钮文字和热运行时候一模一样，点下去后端 die「VM 没在运行，
                // 先 start」——一个死胡同对话框，而用户想要的是看画面。
                // 2026-11-10 用户截图报的就是这个。
                Button(vm.running
                       ? (vm.headless ? "用系统窗口打开（会重启）" : "用系统窗口打开")
                       : "启动并用系统窗口打开") {
                    if vm.running {
                        state.nativeWindow(vm)
                    } else {
                        state.startWithWindow(vm)
                    }
                }
                .disabled(state.backend.busy)
            }

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
            } else if vm.isMacOS {
                // macOS 客人机的 SSH 在 NAT 内部（192.168.64.x），宿主机上
                // 没有转发端口——VZNATNetworkDeviceAttachment 不对外监听。
                // 所以「怎么连上去」的答案就是：用那个窗口。不写一句
                // 「SSH 端口」之类的假信息。
                row("远程登录", "第一次启动时已自动打开远程登录，账号 \(vm.user)。"
                    + "SSH 只能从同一 NAT 里的机器连，宿主机上不转发端口。") {}
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
