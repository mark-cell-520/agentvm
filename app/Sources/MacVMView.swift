import SwiftUI
import Virtualization

/// macOS 客人机的画面。
///
/// 和另外两种客人机最大的区别：**没有 VNC**。VZ 不提供 VNC server，所以
/// app 里那套自研 RFB 客户端在这儿完全用不上——画面是 VZVirtualMachineView，
/// 一个原生 NSView。好处是比 VNC 清楚也快（没有编码/解码往返），坏处是
/// 它必须和 VZVirtualMachine 在同一个进程里，所以 macOS 客人机的寿命跟
/// app 绑定：app 一退客人机就停。这是框架限制，不是选择。
///
/// 因此这个视图不接受一个「端口」，只接受一个活着的 VZVirtualMachine。
/// 那个对象由 AppState.macRuntime 持有。
struct MacVMWindow: View {
    @EnvironmentObject var state: AppState
    let vm: VMInfo
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Group {
            // 判据是 runtime.vm != nil，不是「runtime 在不在字典里」。
            // 客人机 panic 之后 runtime 还在（AppState 的字典没清），
            // 只是 vm 变 nil——照旧判据会得到一块黑画面加一个绿点，
            // 而真实原因在 macVMLastError 里没人看。
            if let runtime = state.macRuntime(for: vm), runtime.vm != nil {
                MacVMCanvas(runtime: runtime)
                    .overlay(alignment: .topLeading) { badge }
            } else {
                // 没起来。三种情况：还没装完、启动失败、已经停了。
                VStack(spacing: 14) {
                    Image(systemName: "desktopcomputer")
                        .font(.system(size: 44))
                        .foregroundStyle(.secondary)
                    Text(state.macVMLastError.isEmpty
                         ? "macOS 虚拟机没在运行"
                         : state.macVMLastError)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 420)
                    if !(vm.macInstalled ?? false) {
                        Text("这个虚拟机还没装完。先在列表里选中它，点「开始安装」。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Button("启动") { state.startMacVM(vm) }
                            .buttonStyle(.borderedProminent)
                            .disabled(state.backend.busy)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 900, minHeight: 620)
        .navigationTitle("\(vm.name) · macOS 虚拟机")
        // app 退出时必须停掉 VZ 虚拟机。VZ 没有「后台常驻」这回事，
        // 进程一走机器就没了——如果不在退出前优雅停机，APFS 会留下
        // 未干净的日志，下次启动跑好几分钟 fsck。
        .onDisappear { state.stopMacVM(vm) }
    }

    /// 顶上一条状态：分辨率、build、以及「关掉这个窗口 = 关机」。
    ///
    /// 最后那条必须写：用户在 Windows/Linux 那边的经验是「关掉窗口 app 还在跑」，
    /// 照那个预期操作会莫名其妙丢数据。
    private var badge: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(state.macRuntime(for: vm)?.vm != nil ? Color.green : Color.gray)
                .frame(width: 8, height: 8)
            Text(state.macRuntime(for: vm)?.vm != nil ? "运行中" : "已停止")
                .font(.caption.weight(.medium))
            if let res = vm.macResolution, !res.isEmpty {
                Text("· \(res)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text("关掉这个窗口 = 关机")
                .font(.caption)
                .foregroundStyle(.orange)
            // 明确的关闭出口。这个面板原来一个关闭控件都没有：没有按钮、
            // 没有 .cancelAction，而 VZVirtualMachineView 会把 Esc 转给客人机。
            // 「关掉 = 关机」这件事又让用户不敢去点窗口那张 ×。2026-11-10 审计补上。
            Button("关闭窗口") { dismiss() }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("会先优雅停机再关——不关机就等于丢数据")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.bar)
    }
}

/// 把 VZVirtualMachineView 包进 SwiftUI。
///
/// NSViewRepresentable 而不是自己写 NSView：VZVirtualMachineView 已经处理了
/// 鼠标/键盘事件的转发、分辨率适配、Retina 缩放。自己包一层只会漏掉其中
/// 几件——实测的典型症状是鼠标能动但点不中，键盘要额外点一下才激活。
private struct MacVMCanvas: NSViewRepresentable {
    let runtime: MacVMRuntime

    func makeNSView(context: Context) -> VZVirtualMachineView {
        let view = VZVirtualMachineView()
        // 没有 machine 时赋值会被忽略而不是报错，所以这里判一下。
        if let machine = runtime.vm {
            view.virtualMachine = machine
        }
        return view
    }

    func updateNSView(_ nsView: VZVirtualMachineView, context: Context) {
        if nsView.virtualMachine == nil, let machine = runtime.vm {
            nsView.virtualMachine = machine
        }
    }
}
