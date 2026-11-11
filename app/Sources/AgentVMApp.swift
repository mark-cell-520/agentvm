import SwiftUI
import AppKit

@main
struct AgentVMApp: App {
    @StateObject private var state = AppState()
    // app 退出时的唯一可靠钩子。没有它，app 一退 macOS 客人机就是「拔电源」
    // 式死亡：APFS 留下未干净的日志，下次启动跑好几分钟 fsck。
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    init() {
        // delegate 是个 static 弱引用指向 AppState：SwiftUI 持有 AppState，
        // 而 delegate 由 @NSApplicationDelegateAdaptor 创建，两边没有现成的
        // 传递路径。退出时才用到它，那时候 AppState 当然还活着。
        AppDelegate.state = state
    }

    var body: some Scene {
        WindowGroup("AgentVM") {
            ContentView()
                .environmentObject(state)
                .frame(minWidth: 820, minHeight: 540)
        }
        .defaultSize(width: 1000, height: 680)
        // 不要加 .windowStyle(.titleBar)。那个样式是「只要标题栏、不要工具栏」，
        // 加上之后 .toolbar 里的按钮全都不进工具栏——AppKit 里只能枚举到系统
        // 自带的「隐藏边栏」和「更多工具栏项」，自己的按钮像不存在一样。
        // 窗口必须是普通样式，QEMU 的原生窗口才有地方由用户自己拖。
    }
}

/// app 级钩子。只有一件事：退出前把 macOS 客人机停干净。
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var state: AppState?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let state = Self.state, state.hasRunningMacVM else { return .terminateNow }
        // 同步等，最多 45 秒。这会挡住退出进度，但比让 APFS 留未干净的日志
        // 值得——那几分钟 fsck 是每次启动都要付的。
        state.stopAllMacVMs()
        return .terminateNow
    }
}
