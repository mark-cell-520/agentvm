import AppKit
import SwiftUI

/// 虚拟机的可视化控制台：一个真 RFB 客户端嵌在 app 里。
///
/// 这是「系统都能可视化安装」的落点。三类安装全走它：
///   · Windows：安装期间 QEMU 是无窗口跑的（窗口能被一下关掉、整个安装跟着没，
///     见 vmkit.py 里 HEADLESS 那段），画面只有这里能看到。
///   · 本地 ISO：安装器是图形交互的，要选盘、确认格式化、看进度，全在这儿点。
///   · Linux 云镜像：启动和登录同样看得见。
///
/// 和原来「隔几秒抓一张图」的区别不只是流畅：VNC 是双向的，键盘鼠标真的送到
/// 客人机里，所以拖动滚动条、在输入框里打字都行——抓图做不到这两件，而装系统
/// 必然要用到。
struct ConsoleView: View {
    let name: String
    let port: Int
    /// 端口不合法时的说明。空串表示端口正常，可以连。
    @State private var portError = ""

    @StateObject private var client: VNCClient
    @State private var locked = false
    /// 这个面板只连一次。SwiftUI 不保证 onAppear 只走一遍（sheet 嵌套、父视图
    /// 重绘都会再触发），而 connect() 每次都会 cancel 掉上一条连接再建新的——
    /// 连两次就白丢一次已建好的连接，用户看到的是「画面卡一下又重来」。
    @State private var didConnect = false

    init(name: String, port: Int) {
        self.name = name
        self.port = port
        // UInt16(port) 在越界时直接 trap：vm.json 是用户可手改的文件，
        // vnc_port 写成 99999 就走到了这条路上。那是配置错误，该给的是一句
        // 人能看懂的话，不是整个 app 崩掉。
        if (0...65535).contains(port) {
            _client = StateObject(
                wrappedValue: VNCClient(host: "127.0.0.1", port: UInt16(port)))
        } else {
            _portError = State(initialValue:
                "端口 \(port) 不是合法的 TCP 端口（要 0 到 65535）。"
                + "这个虚拟机的 vm.json 里 vnc_port 被改坏了，改回去再打开控制台。")
            // 端口 0 连不上，但客户端照样建得起来，界面逻辑不用为它分叉。
            _client = StateObject(
                wrappedValue: VNCClient(host: "127.0.0.1", port: 0))
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if portError.isEmpty {
                screen
            } else {
                // 端口非法：把原因写在屏幕上，不去连一个连不上的端口。
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 34))
                        .foregroundStyle(.orange)
                    Text(portError)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 440)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            footer
        }
        .onAppear {
            if portError.isEmpty, !didConnect {
                didConnect = true
                client.connect()
            }
        }
        .onDisappear {
            client.disconnect()
            locked = false
        }
    }

    // ------------------------------------------------------------ 头部 --

    private var header: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(client.connected ? Color.green : Color.red)
                .frame(width: 9, height: 9)
            Text(client.statusText)
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
            if client.guestWidth > 0 {
                Text("\(client.guestWidth)×\(client.guestHeight)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Button(locked ? "释放鼠标" : "锁定鼠标") { locked.toggle() }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("锁定之后鼠标相对移动，可以拖到画面外；按 Esc 释放")
            Button("重连") { client.connect() }
                .buttonStyle(.bordered)
                .controlSize(.small)
                // 端口非法时重连没有意义（连的是端口 0）。
                .disabled(!portError.isEmpty)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var screen: some View {
        GeometryReader { geo in
            VNCScreen(client: client, locked: $locked)
                .frame(width: geo.size.width, height: geo.size.height)
                .background(Color.black)
                .clipped()
        }
    }

    private var footer: some View {
        HStack(spacing: 16) {
            Label("点进去就能操作，Esc 释放鼠标", systemImage: "cursorarrow")
            Spacer()
            if !client.connected {
                Text("虚拟机可能没在跑，或者刚起来 VNC 还没监听")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

// ------------------------------------------------------------ 画面视图 --

private struct VNCScreen: NSViewRepresentable {
    @ObservedObject var client: VNCClient
    @Binding var locked: Bool

    func makeNSView(context: Context) -> VNCScreenView {
        let v = VNCScreenView()
        v.client = client
        v.onLock = { locked = $0 }
        return v
    }

    func updateNSView(_ v: VNCScreenView, context: Context) {
        v.client = client
        v.locked = locked
        v.needsDisplay = true
    }
}

/// 画一帧、把鼠标键盘转成 RFB 事件。
///
/// `isFlipped` 返回 true 是故意的：客人机 framebuffer 的第 0 行在顶部，NSView
/// 默认坐标系第 0 行在底部。不翻的话所有鼠标点的 y 都是上下镜像的——点「下一步」
/// 会命中它上面那个控件，而这种错位在安装界面上看起来像「按钮没反应」。
final class VNCScreenView: NSView {

    var client: VNCClient? {
        didSet { needsDisplay = true }
    }
    var onLock: ((Bool) -> Void)?

    var locked = false {
        didSet {
            if locked { acquireLock() } else { releaseLock() }
        }
    }

    /// 当前按下的鼠标键掩码。拖动时要用它。
    private var buttons: UInt8 = 0
    /// 锁定模式下的客人机指针位置。
    private var guestCursor = NSPoint.zero
    /// 锁定模式专用的局部事件监听。局部监听能收到整个窗口内的 mouseMoved，
    /// 哪怕指针已经移出了这个视图——不装的话指针一出画面就再也收不到事件。
    private var monitor: Any?
    private var prevFlags: NSEvent.ModifierFlags = []

    override var acceptsFirstResponder: Bool { true }
    override var isFlipped: Bool { true }

    // ------------------------------------------------------------ 画 --

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill()
        bounds.fill()
        guard let client, client.guestWidth > 0, let img = client.frame else {
            placeholder()
            return
        }
        let r = fitRect(guest: NSSize(width: client.guestWidth,
                                     height: client.guestHeight))
        // framebuffer 数据第 0 行就是画面顶部，和 CGImage 的约定一致，直接画。
        NSGraphicsContext.current?.cgContext.draw(img, in: r)
    }

    private func placeholder() {
        let s: String
        if let client, !client.connected {
            s = "连不上虚拟机"
        } else if let client, client.guestWidth == 0 {
            s = "等虚拟机给出画面尺寸…"
        } else {
            s = "还没有画面"
        }
        let attrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.secondaryLabelColor,
            .font: NSFont.systemFont(ofSize: 13)]
        let size = s.size(withAttributes: attrs)
        s.draw(at: NSPoint(x: (bounds.width - size.width) / 2,
                           y: (bounds.height - size.height) / 2),
               withAttributes: attrs)
    }

    private func fitRect(guest: NSSize) -> NSRect {
        guard bounds.width > 0, bounds.height > 0,
              guest.width > 0, guest.height > 0 else { return .zero }
        let s = min(bounds.width / guest.width, bounds.height / guest.height)
        let w = guest.width * s
        let h = guest.height * s
        return NSRect(x: (bounds.width - w) / 2, y: (bounds.height - h) / 2,
                      width: w, height: h)
    }

    /// 界面坐标 → 客人机坐标。
    private func guestPoint(_ p: NSPoint) -> NSPoint {
        guard let client, client.guestWidth > 0, client.guestHeight > 0 else {
            return .zero
        }
        let r = fitRect(guest: NSSize(width: client.guestWidth,
                                     height: client.guestHeight))
        guard r.width > 0, r.height > 0 else { return .zero }
        return NSPoint(x: (p.x - r.origin.x) / r.width * CGFloat(client.guestWidth),
                       y: (p.y - r.origin.y) / r.height * CGFloat(client.guestHeight))
    }

    // ------------------------------------------------------------ 鼠标 --

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        let p = guestPoint(convert(event.locationInWindow, from: nil))
        // 先送一次移动，隔一会儿再送按下。
        //
        // 实测踩过：同样三个事件（移动、按下、松开），中间只隔 0.3 秒点不动
        // 安装器的「下一步」，隔 0.8 秒就点了。图形安装器那类界面要看到指针
        // 先移到按钮上才认这一下点击，按下去太快会被当成在别处按了一下而丢掉。
        // 真实用户本来就是先移动再点，所以这里照做，不赌客人机的脾气。
        client?.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()), mask: buttons)
        buttons |= 1
        pendingPress = p
        // 120ms 后如果还没松开，就真发一次按下。
        //
        // 为什么不是「按下即发、松开即抬」：实测同样三个事件（移动、按下、松开），
        // 中间只隔 0.3 秒点不动安装器的「下一步」，隔 0.8 秒就点了。图形安装器
        // 那类界面要看到指针先移到按钮上才认这一下点击，按下去太快会被当成在别处
        // 按了一下而丢掉。所以慢点发。而快抬的情况由 mouseUp 补上，见那里的注释。
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
            guard let self, let q = self.pendingPress else { return }
            self.pendingPress = nil
            self.client?.pointer(x: Int(q.x.rounded()), y: Int(q.y.rounded()),
                                 mask: self.buttons)
        }
    }

    override func mouseUp(with event: NSEvent) {
        let p = guestPoint(convert(event.locationInWindow, from: nil))
        // 按压可能还没发出去（120ms 还没到）。**这时候必须立刻补发 down+up，
        // 不能让这次点击整个丢掉。**
        //
        // 原来是延迟块里 `guard self.buttons & 1 == 1`：而 mouseUp 已经把按钮位
        // 清成 0 了，按下到松开短于 120ms 时（手点、触控板轻点，几乎人人如此）
        // 那个 guard 直接 return——客人机从头到尾没收到过 button-1 按下，只看到
        // 两次原地移动。快速点安装器的「下一步」整个没反应，而慢点就好，
        // 极难归因。
        if let pending = pendingPress {
            pendingPress = nil
            client?.pointer(x: Int(pending.x.rounded()), y: Int(pending.y.rounded()),
                            mask: buttons | 1)
            buttons &= ~1
            client?.pointer(x: Int(pending.x.rounded()), y: Int(pending.y.rounded()),
                            mask: buttons)
            return
        }
        buttons &= ~1
        client?.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()), mask: buttons)
    }

    /// 已经送过移动、但 120ms 延迟按压还没发出去的位置。
    private var pendingPress: NSPoint?

    override func rightMouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        buttons |= 4
        send(event)
    }

    override func rightMouseUp(with event: NSEvent) {
        buttons &= ~4
        send(event)
    }

    override func otherMouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        buttons |= 2
        send(event)
    }

    override func otherMouseUp(with event: NSEvent) {
        buttons &= ~2
        send(event)
    }

    override func mouseDragged(with event: NSEvent) { send(event) }

    override func mouseMoved(with event: NSEvent) {
        // 不锁鼠标时也要把移动发下去。原来是 `guard locked else { return }`：
        // 非锁定模式下把鼠标在画面上滑来滑去，客人机里的光标一动不动（只有点下去
        // 那一下才跳过去）——安装器里的悬停高亮、Tooltip 全部无效，而用户完全
        // 看不出是客户端的功劳还是客人机的毛病。
        if !locked {
            send(event)
            return
        }
        // 相对模式：视图中心是「没动」的位置，位移按界面比例折算成客人机像素，
        // 然后把系统光标掰回中心，这样指针可以无限拖出画面。
        let now = convert(event.locationInWindow, from: nil)
        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        let r = fitRect(guest: NSSize(width: client?.guestWidth ?? 0,
                                     height: client?.guestHeight ?? 0))
        guard r.width > 0, r.height > 0 else { return }
        let scale = min(r.width / CGFloat(client?.guestWidth ?? 1),
                        r.height / CGFloat(client?.guestHeight ?? 1))
        guestCursor.x += (now.x - center.x) / scale
        guestCursor.y += (now.y - center.y) / scale
        client?.pointer(x: Int(guestCursor.x.rounded()),
                        y: Int(guestCursor.y.rounded()), mask: buttons)
        warpToCenter()
    }

    private func send(_ event: NSEvent) {
        guard let client else { return }
        let p = guestPoint(convert(event.locationInWindow, from: nil))
        if !locked { guestCursor = p }
        client.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()),
                       mask: buttons)
    }

    override func scrollWheel(with event: NSEvent) {
        guard let client else { return }
        // 精确触控板给的是小数，累加到阈值再发，否则一帧发十几下滚轮事件。
        accumulatedY += event.scrollingDeltaY
        accumulatedX += event.scrollingDeltaX
        let stepY = event.hasPreciseScrollingDeltas ? 4 : 1
        let stepX = event.hasPreciseScrollingDeltas ? 4 : 1
        while accumulatedY >= CGFloat(stepY) {
            client.wheel(16); accumulatedY -= CGFloat(stepY)
        }
        while accumulatedY <= -CGFloat(stepY) {
            client.wheel(8); accumulatedY += CGFloat(stepY)
        }
        while accumulatedX >= CGFloat(stepX) {
            client.wheel(64); accumulatedX -= CGFloat(stepX)
        }
        while accumulatedX <= -CGFloat(stepX) {
            client.wheel(32); accumulatedX += CGFloat(stepX)
        }
    }
    private var accumulatedY: CGFloat = 0
    private var accumulatedX: CGFloat = 0

    // ------------------------------------------------------------ 键盘 --

    override func keyDown(with event: NSEvent) {
        // Esc 释放鼠标。锁定模式没有真实光标，不给一个逃出口等于把用户关在里面。
        if event.keyCode == 0x35 {
            locked = false
            onLock?(false)
            return
        }
        guard let ks = keysym(for: event) else { return }
        client?.key(ks, down: true)
    }

    override func keyUp(with event: NSEvent) {
        guard let ks = keysym(for: event) else { return }
        client?.key(ks, down: false)
    }

    /// 修饰键不走 keyDown/keyUp，走 flagsChanged。漏了它 Shift 就永远是按下的
    /// 状态——装系统时要敲大写字母和符号，卡在这一步很致命。
    override func flagsChanged(with event: NSEvent) {
        let f = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        for (mask, ks) in Self.modifierKeys {
            let was = prevFlags.contains(mask)
            let now = f.contains(mask)
            if was != now { client?.key(ks, down: now) }
        }
        prevFlags = f
    }

    override func viewWillMove(toWindow newWindow: NSWindow?) {
        super.viewWillMove(toWindow: newWindow)
        newWindow?.makeFirstResponder(self)
    }

    /// NSEvent 的虚拟键码 → X11 keysym。
    private func keysym(for e: NSEvent) -> UInt32? {
        if let special = Self.specialKeys[e.keyCode] { return special }
        var s = e.charactersIgnoringModifiers ?? ""
        // Shift 时取带修饰符的那一份：keysym 要区分 a 和 A，而
        // charactersIgnoringModifiers 永远给小写。
        if e.modifierFlags.contains(.shift), let up = e.characters, up.count == 1 {
            s = up
        }
        guard let c = s.first else { return nil }
        if let a = c.asciiValue { return UInt32(a) }
        // 非 ASCII（中文等）：X 的通用 keysym 是 0x01000000 + 码位
        return 0x0100_0000 + UInt32(c.unicodeScalars.first?.value ?? 0)
    }

    private static let specialKeys: [UInt16: UInt32] = [
        0x24: 0xFF0D,  // Return
        0x30: 0xFF09,  // Tab
        0x33: 0xFF08,  // Delete（向后）
        0x35: 0xFF1B,  // Escape
        0x39: 0xFFE5,  // CapsLock
        0x72: 0xFF63,  // Insert
        0x73: 0xFF50,  // Home
        0x74: 0xFF55,  // PageUp
        0x75: 0xFFFF,  // ForwardDelete
        0x77: 0xFF57,  // End
        0x79: 0xFF56,  // PageDown
        0x7A: 0xFFBE, 0x78: 0xFFBF, 0x63: 0xFFC0, 0x76: 0xFFC1,
        0x60: 0xFFC2, 0x61: 0xFFC3, 0x62: 0xFFC4, 0x64: 0xFFC5,
        0x65: 0xFFC6, 0x6D: 0xFFC7, 0x67: 0xFFC8, 0x6F: 0xFFC9,
        0x7B: 0xFF51, 0x7C: 0xFF53, 0x7D: 0xFF54, 0x7E: 0xFF52,  // 左右下上
    ]

    private static let modifierKeys: [(NSEvent.ModifierFlags, UInt32)] = [
        (.shift, 0xFFE1), (.control, 0xFFE3),
        (.option, 0xFFE9), (.command, 0xFFEB),
    ]

    // ------------------------------------------------------------ 锁定 --

    private func acquireLock() {
        guard let win = window else { return }
        NSCursor.hide()
        win.acceptsMouseMovedEvents = true
        monitor = NSEvent.addLocalMonitorForEvents(matching: .mouseMoved) {
            [weak self] e in
            if e.window == win { self?.mouseMoved(with: e) }
            return e
        }
        guestCursor = NSPoint(x: (client?.guestWidth ?? 0) / 2,
                              y: (client?.guestHeight ?? 0) / 2)
        warpToCenter()
    }

    private func releaseLock() {
        if let monitor {
            NSEvent.removeMonitor(monitor)
            self.monitor = nil
        }
        NSCursor.unhide()
    }

    /// 把系统光标掰回视图中心。失败也不致命：指针会被界面边缘挡住，表现为
    /// 「拖一会儿拖不动了」，按 Esc 解锁即可。
    private func warpToCenter() {
        guard let win = window,
              let screen = win.screen ?? NSScreen.main else { return }
        let g = win.convertPoint(toScreen: NSPoint(x: bounds.midX, y: bounds.midY))
        CGWarpMouseCursorPosition(CGPoint(x: g.x,
                                          y: screen.frame.height - g.y))
    }

    deinit {
        if let monitor { NSEvent.removeMonitor(monitor) }
        // 隐藏过光标就必须配对取消，否则光标会一直留着不见
        if locked { NSCursor.unhide() }
    }
}
