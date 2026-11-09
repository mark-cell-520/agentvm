import SwiftUI

@main
struct AgentVMApp: App {
    @StateObject private var state = AppState()

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
