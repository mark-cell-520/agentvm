import SwiftUI
import AppKit

/// 宿主机资源条：一眼看出「这个虚拟机要占掉机器多少」。
///
/// 为什么要有它：QEMU 的客人机线程是真实线程，内存也是真金白银。把 12 核
/// 全部分给一个虚拟机，macOS 自己的界面线程会被挤下去，表象是键盘鼠标失灵，
/// 而用户完全看不出这两件事有关系。所以分配的时候就把占比画出来。
struct ResourceBar: View {
    /// 已占用值，例如 4
    let value: Double
    /// 总量，例如 12
    let total: Double
    let label: String
    let valueText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).foregroundStyle(.secondary)
                Spacer()
                Text(valueText)
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(color)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3)
                        .fill(Color.primary.opacity(0.10))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(color)
                        .frame(width: geo.size.width * fraction)
                }
            }
            .frame(height: 6)
        }
    }

    private var fraction: Double {
        guard total > 0 else { return 0 }
        return min(max(value / total, 0), 1)
    }

    /// 过半才变色，且只变一次。频繁跳色比不变更难看，也更容易看错。
    private var color: Color {
        let f = fraction
        if f > 0.7 { return .red }
        if f > 0.45 { return .orange }
        return .accentColor
    }
}

/// 三档预设。给「不知道给多少合适」的人一个起点，不是限制——选完照样能改。
struct ResourcePreset: Identifiable, Hashable {
    let id = UUID()
    let name: String
    let detail: String
    let cpus: Int
    let ram: String
    let disk: String

    static let presets: [ResourcePreset] = [
        ResourcePreset(name: "轻量", detail: "命令行、编译、跑服务",
                       cpus: 2, ram: "2G", disk: "32G"),
        ResourcePreset(name: "均衡", detail: "桌面环境、NAS、数据库",
                       cpus: 4, ram: "8G", disk: "64G"),
        ResourcePreset(name: "高性能", detail: "Windows、跑测试、多服务",
                       cpus: 6, ram: "12G", disk: "128G"),
    ]
}

/// 宿主机的真实规格。装之前算占比要用，装完之后改配置也要用。
enum Host {
    static let cores = max(ProcessInfo.processInfo.processorCount, 1)
    static let bytes = ProcessInfo.processInfo.physicalMemory

    static func memText(_ s: String) -> String { s }

    /// 给宿主机留 2 GB 之后，客人机最多能拿多少内存。
    static var guestMemCeiling: Int64 {
        max(Int64(bytes) - Int64(2 << 30), Int64(512 << 20))
    }

    static var coresText: String { "\(cores) 核" }
    static var memText: String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .memory)
    }
}

/// 安装时的配置表单。预设 + 资源条 + 可改的每一项。
struct ConfigForm: View {
    @EnvironmentObject var state: AppState
    /// 是不是本地安装包。本地包的推荐配置按实测基线给（见 Models.local）。
    let isLocalPackage: Bool

    var body: some View {
        Form {
            Section {
                LabeledContent("名称") {
                    TextField("例如 win11", text: $state.vmName)
                        .frame(width: 200)
                }
            } header: {
                Text("起个名字")
            } footer: {
                // 规则要和 vmkit 的 check_vm_name 一致：首字符字母数字，其余可以
                // 加 . - _，最长 63。原来这句漏了「点号可以用」也没说首字符和长度，
                // 而两端的真实规则都不是它写的这样。2026-11-11 审计补上。
                Text("名字会当目录名用：以字母或数字开头，其余可以是字母、数字、. - _，最长 63 个字符。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                HStack(spacing: 10) {
                    ForEach(ResourcePreset.presets) { p in
                        PresetCard(p: p, picked: picked(p)) {
                            state.vmCpus = min(p.cpus, Host.cores)
                            state.vmRam = p.ram
                            state.vmDisk = p.disk
                            // 用户动过资源配置。记下来，pick() 再来就不覆盖
                            // （见 AppState.pick 的注释）。
                            state.markTuned()
                        }
                    }
                }
            } header: {
                Text("先选一档")
            } footer: {
                Text("只是个起点，下面每一样都能改。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                Stepper("CPU 核心：\(state.vmCpus)",
                        value: Binding(get: { state.vmCpus },
                                       set: { state.vmCpus = $0; state.markTuned() }),
                        in: 1...Host.cores)
                ResourceBar(value: Double(state.vmCpus), total: Double(Host.cores),
                            label: "CPU", valueText: "\(state.vmCpus) / \(Host.coresText)")
                Picker("内存", selection: Binding(get: { state.vmRam },
                                                  set: { state.vmRam = $0; state.markTuned() })) {
                    ForEach(memOptions, id: \.self) { Text($0) }
                }
                ResourceBar(value: memBytes(state.vmRam),
                            total: Double(Host.bytes),
                            label: "内存",
                            valueText: "\(state.vmRam) / \(Host.memText)")
                Picker("磁盘", selection: Binding(get: { state.vmDisk },
                                                  set: { state.vmDisk = $0; state.markTuned() })) {
                    ForEach(diskOptions, id: \.self) { Text($0) }
                }
                Text("磁盘是稀疏文件，写到了才真占空间，所以给大一点不浪费。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("硬件")
            } footer: {
                Text("内存和 CPU 别贪大。QEMU 的客人机线程是真实线程，占太满会把 "
                     + "macOS 自己的界面线程挤下去，表象是键盘鼠标失灵。"
                     + "上面留了 2 GB 给 macOS。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                LabeledContent("登录名") {
                    TextField("用户名",
                              text: Binding(get: { state.vmUser },
                                            set: { state.vmUser = $0; state.markTuned() }))
                        .frame(width: 200)
                }
                LabeledContent("密码") {
                    Group {
                        // 两个绑定都带上 markPasswordTouched：重装时要靠它区分
                        // 「用户设的密码」和「表单里的默认值」。见 AppState.reinstall。
                        if state.showPassword {
                            TextField("密码",
                                      text: Binding(get: { state.vmPassword },
                                                    set: { state.vmPassword = $0
                                                           state.markPasswordTouched() }))
                                .frame(width: 200)
                        } else {
                            SecureField("密码",
                                        text: Binding(get: { state.vmPassword },
                                                      set: { state.vmPassword = $0
                                                             state.markPasswordTouched() }))
                                .frame(width: 200)
                        }
                    }
                }
                Toggle("显示密码", isOn: $state.showPassword)
            } header: {
                Text("账号")
            } footer: {
                Text("SSH 公钥会自动生成并装进虚拟机，所以密码只是备用的登录方式。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    // ------------------------------------------------------------ 选项 --

    /// 内存档位按宿主机实际大小给，不列一堆选不动的值。
    private var memOptions: [String] {
        var out: [String] = []
        for g in [1, 2, 4, 8, 12, 16] as [Int] {
            if Double(Int64(g) << 30) <= Double(Host.guestMemCeiling) {
                out.append("\(g)G")
            }
        }
        // 宿主机很小（比如 8 GB）时至少给两档，否则 Picker 只有一个选项很难看
        if out.count < 2 { out = ["1G", "2G"] }
        return out
    }

    private var diskOptions: [String] {
        isLocalPackage ? ["16G", "32G", "64G", "128G", "256G"]
                       : ["16G", "32G", "64G", "128G"]
    }

    /// "4G" → 字节数。给资源条算占比用。
    private func memBytes(_ s: String) -> Double {
        let t = s.uppercased()
        let n = Double(t.filter { $0.isNumber || $0 == "." }) ?? 0
        if t.hasSuffix("G") { return n * 1073741824 }
        if t.hasSuffix("M") { return n * 1048576 }
        return n
    }

    private func picked(_ p: ResourcePreset) -> Bool {
        state.vmCpus == min(p.cpus, Host.cores) && state.vmRam == p.ram
            && state.vmDisk == p.disk
    }
}

private struct PresetCard: View {
    let p: ResourcePreset
    let picked: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(p.name).font(.headline)
                    Spacer()
                    if picked {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(Color.accentColor)
                    }
                }
                Text("\(p.cpus) 核 · \(p.ram) · \(p.disk)")
                    .font(.caption.monospacedDigit())
                Text(p.detail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(picked ? Color.accentColor.opacity(0.12)
                               : Color.primary.opacity(0.04),
                        in: RoundedRectangle(cornerRadius: 8))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(picked ? Color.accentColor : .clear, lineWidth: 1.5)
            )
        }
        .buttonStyle(.plain)
    }
}


/// 一个虚拟机的可改属性。装完之后在「配置」面板里改的就是这些。
///
/// 为什么单独一个结构体而不是一堆参数：要传五六个值给 configure，参数列表长得
/// 没法看，而且以后加属性又要改调用处。集中在一处，界面和后端各取所需。
struct VMSettings {
    var cpus: Int
    var ram: String
    var disk: String
    var user: String
    var password: String
    /// 显示模式：false = 开原生 macOS 窗口，true = 无窗口（画面只走 VNC，
    /// 在 app 内嵌控制台里操作）。
    var headless: Bool
    /// 启动时挂上安装光盘。只有本地包装的虚拟机有这项，且默认关——挂着光盘重启
    /// 有可能退回安装器而不是进装好的系统（见 vmkit local_qemu_args 的注释）。
    var attachISO: Bool
    var sshPort: Int
    var vncPort: Int
    /// 远程桌面端口。**必须保持可选，不能存成 0。** catalog Linux 虚拟机和
    /// 本地包装的虚拟机根本没有这项（vm.rdpPort 是 nil）。原来存成 0，而
    /// configure 拿 0 和 nil 比，`0 != nil` 成立，于是每次保存都传一个
    /// `--rdp-port 0`，vmkit 的 take_port(0) 直接 die「端口要给 1024 到 65535
    /// 之间，你给了 0」——整条配置保存还没开始改任何东西就失败。
    var rdpPort: Int?
    /// 宿主机上转 Web UI 的端口。同上，只有本地包装的 NAS 类系统有这项。
    var webPort: Int?

    /// 密码的「没改」哨兵。界面上显示一串点，用户不动它就不传 --password，
    /// 免得把密码覆盖成一句提示文字。
    static let unchangedPassword = "········"

    init(vm: VMInfo) {
        cpus = vm.cpus
        ram = vm.ram
        disk = vm.disk
        user = vm.user
        password = VMSettings.unchangedPassword
        headless = vm.headless
        attachISO = vm.attachISO
        sshPort = vm.sshPort
        vncPort = vm.vncPort
        rdpPort = vm.rdpPort
        webPort = vm.webPort
    }
}

/// 已经建好一个系统的配置面板。
///
/// 和安装时的 ConfigForm 是两回事：那边决定「建成什么样」，这边改「已经装好的
/// 这个东西」。原来根本没有这个面板——想加内存只能删了重装，而删了重装意味着
/// 系统里的东西全没。
///
/// 改完有两档：「保存」（下次重启生效）和「保存并重启」（立刻生效）。后者就是
/// 「不需要重新安装系统，重启一下就切换」的那个按钮。
struct SettingsView: View {
    let vm: VMInfo
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) private var dismiss

    @State private var s: VMSettings
    @State private var saving = false
    @State private var errorMsg: String?
    @State private var done = false
    /// macOS 客人机的共享目录和端口转发。从 macvm.json 读，保存时写回去
    /// （见 AppState.macSaveDevices）。非 macOS 虚拟机这两项恒为空、界面也不显示。
    @State private var macShares: [MacVMShare]
    @State private var macForwards: [MacVMForward]
    @State private var newSharePath = ""
    @State private var newShareName = "host"
    @State private var newShareReadOnly = false
    @State private var newHostPort = ""
    @State private var newGuestPort = ""

    init(vm: VMInfo) {
        self.vm = vm
        _s = State(initialValue: VMSettings(vm: vm))
        let spec = MacVMSpec.load(name: vm.name)
        _macShares = State(initialValue: spec?.shares ?? [])
        _macForwards = State(initialValue: spec?.forwards ?? [])
    }

    /// 有哪些改动需要重启才生效。
    ///
    /// 基线一律用 vm 上存的值，不用 vm.isLocalWithISO——后者还看「安装包文件
    /// 还在不在」。ISO 被用户移走之后它变 false 而 s.attachISO 还是 true，于是
    /// 什么都没改也提示「要重启才生效」，「保存」按钮无需任何修改就是可点的；
    /// 点下去后端 die「安装包不见了」，而界面显示「已保存」。
    private var needsRestart: Bool {
        s.cpus != vm.cpus || s.ram != vm.ram || s.headless != vm.headless
            || s.attachISO != vm.attachISO
            || s.sshPort != vm.sshPort || s.vncPort != vm.vncPort
            || s.rdpPort != vm.rdpPort || s.webPort != vm.webPort
            // macOS 的共享目录和端口转发是创建 VZVirtualMachine 时的设备参数，
            // 和 CPU 内存一样改完要重启。
            || (vm.isMacOS && devicesChanged)
    }

    /// 共享目录 / 端口转发相对磁盘上的配置有没有动。
    private var devicesChanged: Bool {
        // 读不到配置时当作没改：保存那一下会报出真正的错误（见 save() 里
        // macSaveDevices 的分支），在这儿弹一个看不懂的解码错误没用。
        guard let spec = MacVMSpec.load(name: vm.name) else { return false }
        return macShares != spec.shares || macForwards != spec.forwards
    }

    private var dirty: Bool {
        needsRestart || s.disk != vm.disk || s.user != vm.user
            || s.password != VMSettings.unchangedPassword
    }

    /// 端口是不是都在合法范围里。
    ///
    /// 原来只在输入框旁边画个红色感叹号，`dirty` 不看这个，「保存」照样可点；
    /// vmkit 的 take_port 会 die，而错误又被 handleCommandEvent 之外的地方吞掉，
    /// 结果是「已保存」但端口没改（撞了别的端口的话更糟，要等下次启动 QEMU 绑不上
    /// 才暴露，只留在 qemu.log 里）。越域时禁用保存并就地说明。
    private var portsValid: Bool {
        (1024...65535).contains(s.sshPort)
            && (5900...65535).contains(s.vncPort)
            && (s.rdpPort.map { (1024...65535).contains($0) } ?? true)
            && (s.webPort.map { (1024...65535).contains($0) } ?? true)
            // macOS 的共享目录 / 端口转发表单填到一半（端口不是数字、目录不存在）
            // 时也不许保存——和上面那几个端口框同一套规矩：越域就地说明，
            // 而不是让用户点下去撞一个后端错误。
            && (!vm.isMacOS || (shareFormError == nil && forwardFormError == nil))
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    presetRow
                    Divider()
                    hardware
                    Divider()
                    displayAndMedia
                    Divider()
                    ports
                    Divider()
                    account
                    // 共享目录和端口转发只有 macOS 客人机有（virtiofs + vsock）。
                    // 另外两种走 9p 和 QEMU 的端口参数，在「存储」面板里。
                    if vm.isMacOS {
                        Divider()
                        macSharesSection
                        Divider()
                        macForwardsSection
                    }
                    if let e = errorMsg {
                        Text(e).font(.caption).foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                    if done {
                        Label("已保存", systemImage: "checkmark.circle.fill")
                            .font(.callout).foregroundStyle(.green)
                    }
                }
                .padding(20)
            }
            Divider()
            footer
        }
        .frame(width: 640, height: 680)
    }

    // ------------------------------------------------------------ 头部 --

    private var header: some View {
        HStack {
            Text("配置 \(vm.name)").font(.title2).bold()
            Spacer()
            Text(vm.running ? "运行中" : "已关机")
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.quaternary, in: Capsule())
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var presetRow: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("先选一档").font(.headline)
            HStack(spacing: 10) {
                ForEach(ResourcePreset.presets) { p in
                    PresetCard(p: p, picked: s.cpus == min(p.cpus, Host.cores)
                                && s.ram == p.ram) {
                        s.cpus = min(p.cpus, Host.cores)
                        s.ram = p.ram
                    }
                }
            }
            Text("只是个起点，下面每一样都能改。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    // ------------------------------------------------------------ 硬件 --

    private var hardware: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("硬件").font(.headline)
            Stepper("CPU 核心：\(s.cpus)", value: $s.cpus, in: 1...Host.cores)
            ResourceBar(value: Double(s.cpus), total: Double(Host.cores),
                        label: "CPU", valueText: "\(s.cpus) / \(Host.coresText)")
            Picker("内存", selection: $s.ram) {
                ForEach(memOptions, id: \.self) { Text($0) }
            }
            ResourceBar(value: memBytes(s.ram), total: Double(Host.bytes),
                        label: "内存", valueText: "\(s.ram) / \(Host.memText)")

            Picker("磁盘", selection: $s.disk) {
                ForEach(diskOptions, id: \.self) { Text($0) }
            }
            if s.disk != vm.disk {
                Text("磁盘要从 \(vm.disk) 扩大到 \(s.disk)。文件会马上扩大，但系统里"
                     + "还要再扩一次分区和文件系统：Linux 用 growpart 加 resize2fs，"
                     + "Windows 用 diskpart extend。而且改这个大小必须先把虚拟机关机。")
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("磁盘只能往大改。当前 \(vm.disk)。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // -------------------------------------------------- 显示与光盘 --

    private var displayAndMedia: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("显示与启动").font(.headline)

            if vm.isMacOS {
                // macOS 客人机没有「显示模式」这个开关：它的画面就是 app 里那个
                // VZVirtualMachineView，不存在第二种方式。原来这组单选钮照样显示，
                // 用户切到「原生窗口」点保存，后端拿到 --window 而 configure_macos
                // 不认这个参数——界面说「已保存」，实际什么都没发生。
                Text("macOS 虚拟机只有一种显示方式：app 里那个窗口。"
                     + "它没有 VNC，也没有原生窗口模式可切。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Picker("显示模式", selection: $s.headless) {
                    Text("原生 macOS 窗口").tag(false)
                    Text("app 内嵌控制台（无窗口）").tag(true)
                }
                .pickerStyle(.radioGroup)
                Text(s.headless
                     ? "画面只从 VNC 出，在 app 的「控制台」里操作。安装期间必须用这种——"
                       + "这个版本的 QEMU 没有 -no-quit，点窗口红按钮会把整个虚拟机干掉。"
                     : "会多一个原生窗口。鼠标点进去会被虚拟机捕获，按 Control+Option+G 释放。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if vm.isLocalWithISO {
                Toggle("启动时挂上安装光盘", isOn: $s.attachISO)
                Text(s.attachISO
                     ? "重启后会进安装器，不是进装好的系统。想重装或者重新配置系统时才开；"
                       + "装完记得关掉，否则以后每次重启都可能退回安装器。"
                     : "重启直接从硬盘进系统。这是默认，也是装完之后该有的状态。")
                    .font(.caption)
                    .foregroundStyle(s.attachISO ? .orange : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // ------------------------------------------------------------ 端口 --

    private var ports: some View {
        VStack(alignment: .leading, spacing: 10) {
            if vm.isMacOS {
                // macOS 客人机没有可改的 SSH/VNC 端口：VZ 的 NAT 附件不对外监听，
                // VNC 压根不存在。摆三个端口框在这儿只会让用户填一堆永远不生效的
                // 数字。要让宿主机连进客人机，用下面的「端口转发」（vsock）。
                Text("端口").font(.headline)
                Text("macOS 虚拟机没有可改的端口。它的网络是 VZ 的 NAT"
                     + "（内部 192.168.64.x），宿主机上不转发任何端口，"
                     + "也没有 VNC——画面直接显示在 app 的窗口里。\n"
                     + "要让宿主机连进客人机，用下面的「端口转发」。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
            Text("端口").font(.headline)
            Text("全都只绑在 127.0.0.1 上，外面连不进来。改完要重启才生效，"
                 + "而且不能和别的虚拟机或宿主机上的程序撞。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            portRow("SSH", $s.sshPort, range: 1024...65535)
            portRow("控制台（VNC）", $s.vncPort, range: 5900...65535)
            if vm.isWindows {
                portRow("远程桌面", $s.rdpPort, range: 1024...65535)
            }
            if vm.webPort != nil {
                portRow("Web 界面", $s.webPort, range: 1024...65535)
            }
            }
        }
    }

    // ------------------------------------------------ 共享目录（virtiofs） --

    /// macOS 客人机的共享目录。virtiofs，客人机自动挂到 /Volumes/<挂载名>。
    ///
    /// 和 Linux 那边（「存储」面板里的 9p）最大的区别：不用进客人机敲 mount。
    /// 这是 VZ 的 macOSGuestAutomountTag 给的，见 MacVMEngine 里
    /// macMakeConfiguration 的注释——那个 tag 不能自己起，否则客人机里
    /// 能看见卷但一访问就是 Operation not permitted。
    private var macSharesSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("共享目录").font(.headline)
            Text("把宿主机上的目录挂进客人机，传文件不用再走 scp。"
                 + "macOS 客人机会自动挂到 /Volumes/<挂载名>，不需要在客人机里敲 mount。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if macShares.isEmpty {
                Text("还没有共享目录")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(macShares) { sh in
                        HStack(spacing: 10) {
                            Image(systemName: "folder")
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("/Volumes/\(sh.name)").font(.callout)
                                Text(sh.hostPath + (sh.readOnly ? "（只读）" : ""))
                                    .font(.caption2).foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            Spacer()
                            Button("摘掉", role: .destructive) {
                                macShares.removeAll { $0.id == sh.id }
                            }
                            .buttonStyle(.borderless)
                        }
                        .padding(8)
                        .background(.quaternary.opacity(0.5),
                                    in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }

            HStack(spacing: 8) {
                Text(newSharePath.isEmpty ? "还没选目录" : newSharePath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button("选择目录…") { pickShareDir() }
                    .controlSize(.small)
            }
            HStack(spacing: 8) {
                Text("挂载名").frame(width: 56, alignment: .leading)
                TextField("例如 host", text: $newShareName)
                    .frame(width: 140)
                    .textFieldStyle(.roundedBorder)
                Toggle("只读", isOn: $newShareReadOnly)
                    .toggleStyle(.checkbox)
                Button("加") { addShare() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            }

            if let e = shareFormError {
                Text(e).font(.caption).foregroundStyle(.red)
            } else {
                Text("改完要重启虚拟机才生效。只读的那份在客人机里改不了。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    /// 加共享目录前的就地校验。不在点「加」的时候弹窗：路径不存在、
    /// 名字不合法这两种错误，用户在输入框旁边就看到比弹窗清楚。
    private var shareFormError: String? {
        guard !newSharePath.isEmpty else { return nil }
        do {
            _ = try MacVMPaths.validateShareName(newShareName)
            _ = try MacVMPaths.validateShareDir(newSharePath)
            var all = macShares
            all.append(MacVMShare(hostPath: newSharePath, name: newShareName,
                                  readOnly: newShareReadOnly))
            try MacVMPaths.validateShares(all)
            return nil
        } catch let e as MacVMError {
            return e.message
        } catch {
            return error.localizedDescription
        }
    }

    private func addShare() {
        guard !newSharePath.isEmpty else { return }
        do {
            let one = try macParseShare(newShareReadOnly
                                        ? "\(newSharePath):\(newShareName):ro"
                                        : "\(newSharePath):\(newShareName)")
            var all = macShares
            all.append(one)
            try MacVMPaths.validateShares(all)
            macShares = all
            newSharePath = ""
            newShareName = "host"
            newShareReadOnly = false
        } catch {
            // 校验失败时不动界面上的列表——错误已经在 shareFormError 里显示了，
            // 这里再写一次 errorMsg 会让同一个原因出现两遍。
            errorMsg = nil
        }
    }

    private func pickShareDir() {
        let panel = NSOpenPanel()
        panel.title = "选一个要共享进去的目录"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            newSharePath = url.path
            // 用目录名当默认挂载名：/Volumes 下显示的就是它，用户一眼能认出
            // 是哪个目录。非法字符（空格等）会被校验拦下，那时再手改。
            let base = url.lastPathComponent
            if !base.isEmpty { newShareName = base }
        }
    }

    // -------------------------------------------------- 端口转发（vsock） --

    /// macOS 客人机的端口转发。宿主机 TCP → 客人机 vsock。
    ///
    /// 这是 VZ 的 NAT 之外唯一的公开进客路径（NAT 附件没有端口映射 API）。
    /// 宿主机这边只绑 127.0.0.1，外面连不进来。
    private var macForwardsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("端口转发").font(.headline)
            Text("把宿主机上的一个端口接到客人机的 vsock 端口上。"
                 + "宿主机这边只绑 127.0.0.1，外面连不进来。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if macForwards.isEmpty {
                Text("还没有端口转发")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(macForwards) { f in
                        HStack(spacing: 10) {
                            Image(systemName: "arrow.left.arrow.right")
                                .foregroundStyle(.secondary)
                            Text("127.0.0.1:\(f.hostPort) → 客人机 vsock \(f.guestPort)")
                                .font(.callout)
                            Spacer()
                            Button("摘掉", role: .destructive) {
                                macForwards.removeAll { $0.id == f.id }
                            }
                            .buttonStyle(.borderless)
                        }
                        .padding(8)
                        .background(.quaternary.opacity(0.5),
                                    in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }

            HStack(spacing: 8) {
                Text("宿主机端口").frame(width: 76, alignment: .leading)
                TextField("例如 2222", text: $newHostPort)
                    .frame(width: 90)
                    .textFieldStyle(.roundedBorder)
                Text("客人机 vsock 端口").frame(width: 110, alignment: .leading)
                TextField("例如 22", text: $newGuestPort)
                    .frame(width: 70)
                    .textFieldStyle(.roundedBorder)
                Button("加") { addForward() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            }

            if let e = forwardFormError {
                Text(e).font(.caption).foregroundStyle(.red)
            } else {
                // 这句话必须写：转发链路是通的，但「ssh 127.0.0.1:2222 能进
                // 客人机」还需要客人机里有个听 vsock 的程序，而 macOS 客人机
                // 默认没有。不写的话用户会以为加了转发就能 SSH 了。
                Text("注意：客人机里得有程序监听对应的 vsock 端口，这条转发才通。"
                     + "macOS 客人机默认没有这样的服务（它的 SSH 只听 TCP）。"
                     + "改完要重启虚拟机才生效。")
                    .font(.caption2).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var forwardFormError: String? {
        guard !newHostPort.isEmpty || !newGuestPort.isEmpty else { return nil }
        do {
            guard let host = Int(newHostPort), let guest = Int(newGuestPort) else {
                return "端口要填数字"
            }
            let one = try macParseForward("\(host):\(guest)")
            var all = macForwards
            all.append(one)
            try MacVMPaths.validateForwards(all)
            return nil
        } catch let e as MacVMError {
            return e.message
        } catch {
            return error.localizedDescription
        }
    }

    private func addForward() {
        do {
            let one = try macParseForward("\(newHostPort):\(newGuestPort)")
            var all = macForwards
            all.append(one)
            try MacVMPaths.validateForwards(all)
            macForwards = all
            newHostPort = ""
            newGuestPort = ""
        } catch {
            errorMsg = nil
        }
    }

    /// 端口用文本框而不是步进器：1024 到 65535 之间一格一格走，点到明天也到不了。
    /// 输入不合法就地显示红框，不让用户把明显不对的值存进去。
    ///
    /// 可选端口（rdp / web）走下面那个重载：这个虚拟机没有这项时传 nil，
    /// 界面显示「无」而**不是 0**——存成 0 会让 configure 传一个 `--rdp-port 0`
    /// 给 vmkit，它直接 die「端口要给 1024 到 65535 之间，你给了 0」。
    private func portRow(_ label: String, _ value: Binding<Int>,
                         range: ClosedRange<Int>) -> some View {
        HStack {
            Text(label).frame(width: 110, alignment: .leading)
            TextField("", value: value, format: .number)
                .frame(width: 80)
                .multilineTextAlignment(.trailing)
                .textFieldStyle(.roundedBorder)
            if !range.contains(value.wrappedValue) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .help("要给 \(range.lowerBound) 到 \(range.upperBound) 之间")
            }
            Spacer()
        }
    }

    private func portRow(_ label: String, _ value: Binding<Int?>,
                         range: ClosedRange<Int>) -> some View {
        HStack {
            Text(label).frame(width: 110, alignment: .leading)
            let text = value.wrappedValue.map { String($0) } ?? ""
            TextField("", text: Binding(
                get: { text },
                // 空串表示「这个虚拟机没有这项」。存回 nil 而不是 0。
                set: { value.wrappedValue = Int($0) }
            ))
                .frame(width: 80)
                .multilineTextAlignment(.trailing)
                .textFieldStyle(.roundedBorder)
            if let v = value.wrappedValue, !range.contains(v) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .help("要给 \(range.lowerBound) 到 \(range.upperBound) 之间")
            }
            Spacer()
        }
    }

    // ------------------------------------------------------------ 账号 --

    private var account: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("账号").font(.headline)
            LabeledContent("登录名") {
                TextField("用户名", text: $s.user).frame(width: 200)
            }
            LabeledContent("密码") {
                SecureField("不改就留空", text: $s.password).frame(width: 200)
            }
            Text("改登录名和密码只改 vmkit 记的账，系统里那个账号还是原来的——"
                 + "要真换成另一个用户，进系统里自己建。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // ------------------------------------------------------------ 底部 --

    private var footer: some View {
        HStack(spacing: 10) {
            if needsRestart && vm.running {
                Label("要重启才生效", systemImage: "arrow.clockwise")
                    .font(.caption).foregroundStyle(.orange)
            }
            if !portsValid {
                Label("端口不在 1024 到 65535 之间", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red)
            }
            Spacer()
            Button("取消") { dismiss() }
            Button("保存") { save(restartAfter: false) }
                .disabled(!dirty || saving || !portsValid)
            // 「保存并重启」：改完立刻生效，不用重新安装系统。关机状态点它等于保存+启动。
            Button(restartLabel) { save(restartAfter: true) }
                .buttonStyle(.borderedProminent)
                .disabled(!dirty || saving || !portsValid)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    private var restartLabel: String {
        vm.running ? "保存并重启" : "保存并启动"
    }

    // ------------------------------------------------------------ 动作 --

    private func save(restartAfter: Bool) {
        saving = true
        errorMsg = nil
        done = false
        // macOS 的共享目录和端口转发先写 macvm.json，再走 vmkit 改 CPU 内存那些。
        // 顺序反过来也没关系——两边写的是不同文件的不同字段；先写这边是因为
        // 它在本进程内完成，失败能立刻显示，而 vmkit 那条要等子进程。
        if vm.isMacOS, devicesChanged {
            do {
                try state.macSaveDevices(name: vm.name, shares: macShares,
                                         forwards: macForwards)
            } catch {
                saving = false
                errorMsg = error.localizedDescription
                return
            }
        }
        state.configure(vm, settings: s) {
            if let err = state.backend.errorMessage, !err.isEmpty {
                saving = false
                errorMsg = err
                state.backend.errorMessage = nil
                return
            }
            if restartAfter {
                state.restart(name: vm.name) {
                    saving = false
                    dismiss()
                }
            } else {
                saving = false
                done = true
            }
        }
    }

    // ------------------------------------------------------------ 选项 --

    private var memOptions: [String] {
        var out: [String] = []
        for g in [1, 2, 4, 8, 12, 16] as [Int] {
            if Double(Int64(g) << 30) <= Double(Host.guestMemCeiling) {
                out.append("\(g)G")
            }
        }
        if out.count < 2 { out = ["1G", "2G"] }
        // 现在的值得在列表里，否则 Picker 会显示一个不在选项里的值，看着像坏了
        if !out.contains(s.ram) { out.insert(s.ram, at: 0) }
        return out
    }

    private var diskOptions: [String] {
        var out = ["16G", "32G", "64G", "128G", "256G"]
        if !out.contains(vm.disk.uppercased()) { out.insert(vm.disk.uppercased(), at: 0) }
        return out.filter { parseGB($0) >= parseGB(vm.disk) }
    }

    /// "32G" → 32，"1T" → 1024。界面里只用到比大小。
    private func parseGB(_ s: String) -> Int {
        let t = s.uppercased()
        let n = Int(t.filter { $0.isNumber }) ?? 0
        return t.hasSuffix("T") ? n * 1024 : n
    }

    private func memBytes(_ str: String) -> Double {
        let t = str.uppercased()
        let n = Double(t.filter { $0.isNumber || $0 == "." }) ?? 0
        if t.hasSuffix("G") { return n * 1073741824 }
        if t.hasSuffix("M") { return n * 1048576 }
        return n
    }
}
