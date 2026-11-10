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
    /// 是不是被嵌在别的面板里（新建向导的安装页就嵌了一块 300px 高的）。
    ///
    /// 嵌着的时候不能给「关闭」按钮、也不能让 Esc 去关窗口：`dismiss()` 和
    /// `performClose` 作用的是最外层那个 sheet，一关就把整个向导关了，而用户
    /// 只是想收起那块画面。2026-11-10 实测踩过——刚给独立控制台加的「关闭」
    /// 按钮，在向导里变成了「关闭整个安装」。
    var embedded = false
    /// 关掉这个窗口。sheet 自己那张 × 在某些窗口配置下点不到，而 Esc 又会
    /// 被画面视图吃掉（见 VNCScreenView.keyDown），所以必须给一个看得见的出口。
    @Environment(\.dismiss) private var dismiss

    @StateObject private var client: VNCClient
    @State private var locked = false
    /// 这个面板只连一次。SwiftUI 不保证 onAppear 只走一遍（sheet 嵌套、父视图
    /// 重绘都会再触发），而 connect() 每次都会 cancel 掉上一条连接再建新的——
    /// 连两次就白丢一次已建好的连接，用户看到的是「画面卡一下又重来」。
    @State private var didConnect = false

    init(name: String, port: Int, embedded: Bool = false) {
        self.name = name
        self.port = port
        self.embedded = embedded
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
            // 关闭。放在这儿而不是只靠窗口的 × / Esc：控制台是个全屏画面，
            // 用户操作久了会想关掉它。关窗口等于断开 VNC，客人机继续跑。
            // 嵌在向导里时不给：那会关掉整个向导（见 embedded 的注释）。
            if !embedded {
                Button("关闭") { dismiss() }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help("只是关掉这个画面，虚拟机还在后台跑")
                // 给客人机发 Esc。键盘上那个 Esc 归「关窗口 / 释放鼠标」了，
                // 而装系统时取消对话框、退回上一步全靠它。
                Button("Esc 键") { sendEsc() }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help("把 Esc 送给虚拟机（键盘上的 Esc 用来关窗口和释放鼠标）")
                    .disabled(!client.connected)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var screen: some View {
        GeometryReader { geo in
            VNCScreen(client: client, locked: $locked, embedded: embedded)
                .frame(width: geo.size.width, height: geo.size.height)
                .background(Color.black)
                .clipped()
        }
    }

    private var footer: some View {
        HStack(spacing: 16) {
            // 原来写「Esc 释放鼠标」，而 Esc 现在归关窗口用了（没锁鼠标时）。
            // 照旧写，用户按下去发现窗口关了，会以为出了故障。
            Label("点进去就能操作；锁定鼠标后能把指针拖出画面", systemImage: "cursorarrow")
            Spacer()
            // 端口非法时画面里已经写了原因，footer 再说一句「虚拟机可能没在跑」
            // 是把用户往错的方向引。
            if !client.connected, portError.isEmpty, !hint.isEmpty {
                Text(hint)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    /// 给客人机发一个 Esc。键盘上那个 Esc 被「关窗口 / 释放鼠标」占了，
    /// 而装系统时取消对话框、退回上一步全靠它——不给这条路，用户只能对着
    /// 画面干瞪眼。
    private func sendEsc() {
        // ConsoleView 这边的 client 是 @StateObject，不是可空的。
        client.key(0xFF1B, down: true)
        client.key(0xFF1B, down: false)
    }

    /// 连不上的时候给的那句补充。分两种，原来是一句通用的「虚拟机可能没在跑」：
    /// 还在试的时候说 VM 没跑是冤枉它（TCP 通了、正在等画面尺寸也是这个状态），
    /// 而真断了才该提示去检查 VM。
    private var hint: String {
        if client.statusText.contains("正在") {
            return "等虚拟机给出画面…"
        }
        return "虚拟机可能没在跑，或者刚起来 VNC 还没监听"
    }
}

// ------------------------------------------------------------ 画面视图 --

private struct VNCScreen: NSViewRepresentable {
    @ObservedObject var client: VNCClient
    @Binding var locked: Bool
    /// 嵌在向导里时为真：Esc 不关窗口（那会关掉整个向导）。
    var embedded = false

    func makeNSView(context: Context) -> VNCScreenView {
        let v = VNCScreenView()
        v.client = client
        // 每帧自己要重画，绕开 SwiftUI 的发布通道。见 VNCClient.frame 的注释：
        // 那是「反应很慢」的一部分，2026-11-10 实测每帧 12 毫秒 CPU 里的大头。
        v.onFrame = { [weak v] in v?.needsDisplay = true }
        v.onLock = { locked = $0 }
        v.embedded = embedded
        return v
    }

    func updateNSView(_ v: VNCScreenView, context: Context) {
        v.client = client
        v.locked = locked
        v.embedded = embedded
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
    /// 新帧到了就要重画。由 VNCScreen 接上，见 VNCClient.frame 的注释。
    var onFrame: (() -> Void)? {
        didSet { needsDisplay = true }
    }

    var locked = false {
        didSet {
            if locked { acquireLock() } else { releaseLock() }
        }
    }
    /// 嵌在向导里时为真，见 ConsoleView.embedded：Esc 不关窗口。
    var embedded = false

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
            // 原来这里一律写「连不上虚拟机」，而「正在取画面」（TCP 通了、在等
            // ServerInit）同样是 connected == false——那一刻并不是连不上。
            // 头部状态栏、footer、画面中央三处各说各话，用户不知道该信哪个。
            s = client.statusText.contains("正在")
                ? "正在连，等一下…" : "连不上虚拟机"
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
        // **刚刚才把指针移送到这儿，就不再憋那 120ms。**
        //
        // 那 120ms 本来是为了迁就图形安装器「要先看见指针到位才认点击」。而现在
        // 鼠标移动本来就实时送给客人机（M29 之后就是了），只要 200ms 内移过，
        // 客人机早就看见指针在这个位置上了，前提已经满足，再憋只是让每一次点击
        // 都慢半拍。2026-11-10 用户报「反应很慢，基本没法用」——每点一下多等
        // 120 毫秒正是其中一笔，而且是用户完全能感觉到的一笔。
        //
        // 反过来，指针已经在按钮上停了很久（用户盯着屏幕找人），或者压根没移过
        // （刚连上、或者用键盘 Tab 走过去再回车），还是照老样子憋 120ms。
        if Date().timeIntervalSince(lastMoveSentAt) <= 0.2 {
            client?.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()), mask: buttons)
            return
        }
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

    /// 最近一次把指针位置送给客人机的时间。见 mouseDown 里那 120ms 的判断。
    private var lastMoveSentAt = Date.distantPast

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
            // 按下补在按下点（那是用户想点的东西），松开放开手指的位置。
            // 原来两下都发在 pending 上：拖动结束时客人机看到的是「在 A 按下、
            // 在 A 松开」，中间那些带着 button1 的移动全都白费——拖选文字、
            // 拖滚动条整个不工作。
            client?.pointer(x: Int(pending.x.rounded()), y: Int(pending.y.rounded()),
                            mask: buttons | 1)
            buttons &= ~1
            client?.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()), mask: buttons)
            return
        }
        buttons &= ~1
        client?.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()), mask: buttons)
    }

    /// 已经送过移动、但 120ms 延迟按压还没发出去的位置。
    private var pendingPress: NSPoint?

    /// 把憋着的那次按下立刻发掉（如果还没发）。拖动和松开都要先调它。
    private func flushPendingPress() {
        guard let q = pendingPress else { return }
        pendingPress = nil
        client?.pointer(x: Int(q.x.rounded()), y: Int(q.y.rounded()), mask: buttons)
    }

    /// 鼠标移动的通道：自己装的那个跟踪区域。
    ///
    /// **这是「鼠标移不动」的真正原因，2026-11-10 在本机实测定位。**
    ///
    /// 上一轮的修法是 `window.acceptsMouseMovedEvents = true`。端到端实测
    /// （真 ConsoleView + 假 RFB 服务端 + 合成鼠标事件）证明那条路有个前提：
    /// 它只把 mouseMoved 送给 **first responder**。而在还没点过任何一下的时候，
    /// 这个视图不是 first responder（SwiftUI 里通常是 hosting view 或窗口自己），
    /// 于是 5 次鼠标移动送到服务端是 **0 个 PointerEvent**——点击、拖动、滚轮、
    /// 键盘全都正常，唯独移动整个不工作，正是用户报的现象。点过一下之后这条通路
    /// 才通，而「先移动、再点」是人的操作顺序。
    ///
    /// NSTrackingArea 不依赖 first responder，也不依赖 hit-test（owner 直接收），
    /// 所以它才是主通道。biztalk72/VNCClient 的 VNCView.swift 和 loomhand/swift-rfb
    /// 的 RFBView.swift 两个独立实现都只用它，全程不碰 acceptsMouseMovedEvents。
    ///
    /// **两条通路不能同时开。** 同一份实测里，跟踪区域加上窗口标志之后，点击后的
    /// 每次移动都往客人机送了两次一模一样的 PointerEvent（视图成了 first responder，
    /// 两条路都通）。所以窗口那个标志只在锁定模式下才置，见 acquireLock()。
    ///
    /// **选项必须是 .activeAlways。** 本机 SDK 里 `.activeInKeyWindow` 的位值是 32，
    /// 实测它在这个视图上不投递（窗口明明是 key 的）；换成 .activeAlways（128）
    /// 点击前的移动立刻就到了。kanka-jp/konnyaku 的 PR #25 也是被同类问题逼着从
    /// 「依赖激活态」改成 .activeAlways 的。代价是控制台不在前台时，鼠标划过画面
    /// 也会带动客人机光标——对一个远程桌面窗口来说这是合理行为。
    private var moveTracking: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        installMoveTracking()
    }

    /// 装（或重装）移动跟踪区域。锁定模式下不装：那时候走 acquireLock() 的
    /// 窗口级监视器，两条同时开会把一次移动送两遍。
    private func installMoveTracking() {
        guard !locked, window != nil else { return }
        removeMoveTracking()
        // 只拆自己装的那个。`trackingAreas.forEach(removeTrackingArea)` 会把
        // SwiftUI 自己的跟踪区域一起拆掉（konnyaku 那么干是因为它是 overlay），
        // 而这里是内容视图，拆了会影响 SwiftUI 的 hover 行为。
        //
        // rect 用 bounds：updateTrackingAreas 被调用时 bounds 已经是最新的，
        // 视图尺寸变化时 AppKit 会自动再调一次，不用手动重装。
        let a = NSTrackingArea(rect: bounds,
                               options: [.mouseMoved, .mouseEnteredAndExited,
                                         .inVisibleRect, .activeAlways],
                               owner: self, userInfo: nil)
        addTrackingArea(a)
        moveTracking = a
    }

    private func removeMoveTracking() {
        if let moveTracking {
            removeTrackingArea(moveTracking)
            self.moveTracking = nil
        }
    }

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

    override func mouseDragged(with event: NSEvent) {
        // 120ms 的延迟按压还没发出去就拖动：先把按下补在按下点上，再送这次拖动。
        //
        // 原来直接 send(event)，而 mouseDown 早就把 buttons 的第 0 位置上了——
        // 于是客人机看到一个「带着 button1 的移动」跑在按下前面，接着按下才到
        // （还在按下点），最后松开又回到按下点。中间那些移动全白费：拖选文字、
        // 拖安装器的滚动条，整个不工作。2026-11-10 端到端 harness 实测到的顺序
        // 是：移动@A(无键) → 拖动@B(带键) → 按下@A(带键) → 松开@A(无键)。
        flushPendingPress()
        send(event)
    }

    override func mouseMoved(with event: NSEvent) {
        // 不锁鼠标时也要把移动发下去。原来是 `guard locked else { return }`：
        // 非锁定模式下把鼠标在画面上滑来滑去，客人机里的光标一动不动（只有点下去
        // 那一下才跳过去）——安装器里的悬停高亮、Tooltip 全部无效，而用户完全
        // 看不出是客户端的功劳还是客人机的毛病。
        //
        // 而这条代码能不能跑到，取决于 mouseMoved 到底会不会被送到这个视图，
        // 那又是 moveTracking 那条跟踪区域的事——见那里的长注释。
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
        lastMoveSentAt = Date()
        warpToCenter()
    }

    private func send(_ event: NSEvent) {
        guard let client else { return }
        // 只要这一下带着按键，就得保证「按下」已经先送出去了。
        //
        // 放在这个唯一出口上，而不是只放在 mouseDragged 里：实测合成事件在按住
        // 左键移动时是以 mouseMoved（不是 mouseDragged）送到视图的，只堵
        // mouseDragged 堵不住。不堵的后果是客人机看到一个「带着 button1 的移动」
        // 跑在按下前面，接着按下才到，整段拖动白做（见 mouseDragged 的注释）。
        if buttons != 0 { flushPendingPress() }
        let p = guestPoint(convert(event.locationInWindow, from: nil))
        if !locked { guestCursor = p }
        lastMoveSentAt = Date()
        client.pointer(x: Int(p.x.rounded()), y: Int(p.y.rounded()),
                       mask: buttons)
    }

    override func scrollWheel(with event: NSEvent) {
        guard let client else { return }
        // 触控板的惯性滚动会再补一大批事件（momentumPhase 非空），照收就是
        // 手指已经停了、画面还在继续滚好几秒。只认手指真正在划的那段。
        // 普通鼠标滚轮两个 phase 都是空，不受影响。
        if event.hasPreciseScrollingDeltas,
           !event.momentumPhase.isEmpty || event.phase == .ended { return }
        // 精确触控板给的是小数，累加到阈值再发，否则一帧发十几下滚轮事件。
        accumulatedY += event.scrollingDeltaY
        accumulatedX += event.scrollingDeltaX
        let stepY = event.hasPreciseScrollingDeltas ? 4 : 1
        let stepX = event.hasPreciseScrollingDeltas ? 4 : 1
        // **垂直方向原来是反的。** RFB 的 button 4 是往上滚（掩码 8）、button 5
        // 是往下滚（掩码 16），而 macOS 的 scrollingDeltaY > 0 就是往上滚。
        // 原来把正的发给 16，于是手指往上推、内容往下跑，每次都得反向操作。
        // 水平方向本来是对的（+64 右、+32 左），不要一起动。
        while accumulatedY >= CGFloat(stepY) {
            client.wheel(8); accumulatedY -= CGFloat(stepY)
        }
        while accumulatedY <= -CGFloat(stepY) {
            client.wheel(16); accumulatedY += CGFloat(stepY)
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

    /// 当前正按着、并且已经送给客人机的键：**虚拟键码 → keysym**。
    ///
    /// 为什么需要一份名单：AppKit 不保证每个按下去的键都有对应的 keyUp 送到这个
    /// 视图——Command 按住期间松开的普通键没有 keyUp（loomhand/swift-rfb 的
    /// RFBView.swift 记过这条），切窗口、点标题栏按钮也不会有。漏一个，客人机就
    /// 一直认为那个键还按着：装系统时敲一个大写字母能卡住后面全部输入。
    ///
    /// **键码当键，不是 keysym 当键。** 原来用 `Set<UInt32>` 存 keysym，而 keysym
    /// 是按下那一刻按修饰键算出来的：Shift+a 送下去的是 'A'(0x41)，可用户常常先
    /// 松 Shift 再松 a，那一刻 keyUp 的 modifierFlags 里已经没有 Shift，照
    /// characters 算出来是 'a'(0x61)——对不上，释放永远发不出去，客人机认为 A
    /// 一直按着。2026-11-10 端到端 harness 实测：Shift 按下、A 按下、Shift 松开、
    /// a 松开，服务端只收到 A 的按下，没有松开。同一个物理键的按下和松开必须能
    /// 对上，而键码才是那个稳定不变的东西。
    private var held = [UInt16: UInt32]()

    override func keyDown(with event: NSEvent) {
        // Esc 归谁，看锁不锁鼠标：
        //   锁着 -> 释放鼠标。锁定模式没有真实光标，不给一个逃出口等于把用户
        //           关在里面。
        //   没锁 -> 关窗口。这个画面视图是 first responder，事件到不了 SwiftUI
        //           的 .cancelAction，而 sheet 的 × 在全屏画面上也不一定点得到——
        //           2026-11-10 实测「窗口只能打开，无法关闭」。装向导和详情页的
        //           控制台都嵌着这一个视图，两边同时中。
        //           要给客人机发 Esc，用标题栏那个「Esc 键」按钮。
        if event.keyCode == 0x35 {
            if locked {
                locked = false
                onLock?(false)
            } else if !embedded {
                // 独立控制台才关窗口。嵌在向导里时最外层 sheet 是整个新建流程，
                // 关它就白装一半——那时 Esc 什么都不做，只让客人机收不到它。
                window?.performClose(nil)
            } else {
                // 嵌着：客人机该收到这个 Esc（安装器里取消对话框常用），
                // 但窗口不能关。
                client?.key(0xFF1B, down: true)
                client?.key(0xFF1B, down: false)
            }
            return
        }
        // 带 Command 的组合键是本地快捷键，不进客人机。
        //
        // 实测踩过（2026-11-10 端到端 harness）：按 Cmd+a，客人机收到了一个裸的
        // 0x61 按下——Windows/Linux 里那是全选，而用户只是想按个本地快捷键。
        // XQuartz 的 X11Application.m 和 Chicken 的 EventFilter.m 都是这个做法：
        // 本地吃掉，down 和 up 都不转发。
        //
        // 但不能反过来把这个键记进 held：那会让 keyUp 补发一个客人机从没收到过
        // 的按下对应的释放（第一版就这么写，harness 里抓到了一个孤立的
        // 0x61 key-up）。真正要兜的是另一种情况——这个键先是以普通键送下去的
        // （先按 a，再按着 Cmd 补一下 a），AppKit 又不会在 Command 按住期间补发
        // 它的 keyUp，所以在这里主动补释放。
        if event.modifierFlags.contains(.command) {
            // 只补释放、不记按下：这个键在此之前是以普通键送下去的（先按 a，
            // 再按着 Cmd 补一下 a），AppKit 又不会在 Command 按住期间补发它的
            // keyUp，所以在这儿主动收口。见 held 的注释。
            if let ks = held.removeValue(forKey: event.keyCode) {
                client?.key(ks, down: false)
            }
            return
        }
        guard let ks = keysym(for: event) else { return }
        client?.key(ks, down: true)
        held[event.keyCode] = ks
    }

    override func keyUp(with event: NSEvent) {
        // Esc 的按下被 keyDown 提前消费掉了（关窗口 / 释放鼠标 / 嵌入时补发），
        // 这里不能再发一次释放——客人机会收到一个没有按下的 Esc key-up。
        if event.keyCode == 0x35 { return }
        // 只有确实送过按下才补释放。没送过就发一个 key-up，客人机会看到一个
        // 凭空松开的键。
        //
        // 按键码取，不按 keysym 取：松手那一刻的修饰键可能已经变了（先松 Shift
        // 再松字母就是），照 keysym 找会找不到，客人机就一直卡着那个键。
        if let ks = held.removeValue(forKey: event.keyCode) {
            client?.key(ks, down: false)
        }
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

    /// 把还按着的修饰键全部补发释放。
    ///
    /// 只在视图内部维护 prevFlags 是不够的：按住 Shift 去点标题栏的「重连」，
    /// 或者 Cmd+Tab 切到别的 app 再松开——这两种情况视图都收不到 flagsChanged，
    /// 而客人机那边的 Shift 一直按着。切回来打字全是 Shift+a。
    private func releaseModifiers() {
        let f = prevFlags
        prevFlags = []
        for (mask, ks) in Self.modifierKeys where f.contains(mask) {
            client?.key(ks, down: false)
        }
    }

    /// 把送下去过的键全部补发释放，见 held。
    ///
    /// 修饰键走 prevFlags 那条路（flagsChanged 里维护），普通键走 held 这条路
    /// （keyDown/keyUp 里维护）。两条路都必须在这里收口，否则客人机留着卡键。
    private func releaseAllKeys() {
        releaseModifiers()
        for ks in held.values {
            client?.key(ks, down: false)
        }
        held.removeAll()
    }

    override func resignFirstResponder() -> Bool {
        releaseAllKeys()
        return super.resignFirstResponder()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        // updateTrackingAreas() 可能在视图还没有窗口时就被调过一次（那时
        // installMoveTracking 直接返回了），补装一次。AppKit 只在 bounds 变化时
        // 自动重调，不会因为「刚拿到窗口」再调一遍。
        installMoveTracking()
    }

    override func viewWillMove(toWindow newWindow: NSWindow?) {
        super.viewWillMove(toWindow: newWindow)
        // 原来这里还会 makeFirstResponder(self)。那会让键盘焦点从向导的按钮上
        // 被抢走：新建向导的安装页嵌着这块画面，Tab / Enter / Space 全部失灵，
        // 用户没法用键盘走到「下一步」。改成点了画面才抢焦点（mouseDown 里
        // 已经这么做了）。
        //
        // 切 app 时补发按键释放，见 releaseAllKeys()。
        if let w = newWindow {
            // 换窗口先把上一个观察者摘掉。原来直接覆盖 keyObserver：老窗口的
            // token 一直到 deinit 才移除，而它的回调还会打过来（窗口已经换了，
            // releaseAllKeys 打的是一条没人看的连接）。
            if let keyObserver {
                NotificationCenter.default.removeObserver(keyObserver)
                self.keyObserver = nil
            }
            keyObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didResignKeyNotification, object: w, queue: nil)
            { [weak self] _ in self?.releaseAllKeys() }
        }
    }

    /// 观察窗口失焦的 token，换窗口时要先摘掉，否则老窗口的事件还会打过来。
    private var keyObserver: Any?

    /// NSEvent 的虚拟键码 → X11 keysym。
    private func keysym(for e: NSEvent) -> UInt32? {
        if let special = Self.specialKeys[e.keyCode] { return special }
        var s = e.charactersIgnoringModifiers ?? ""
        // Shift 时取带修饰符的那一份：keysym 要区分 a 和 A，而
        // charactersIgnoringModifiers 永远给小写。
        //
        // Command 按住时不这么做：那个组合键整体是被本地吃掉的（见 keyDown），
        // 而 Command 会改变 characters 的结果（Cmd+Shift+a 是 'A'），照它算出来
        // 的 keysym 和按下时记进 held 的那个对不上，补释放时就漏了一个卡键。
        if e.modifierFlags.contains(.shift), !e.modifierFlags.contains(.command),
           let up = e.characters, up.count == 1 {
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
        // 锁定期间键盘焦点必须在这个视图上。
        //
        // 「锁定鼠标」是个按钮：点它之后 first responder 是那个按钮，而不是画面
        // 视图。这时候按 Esc，事件送到按钮上，哪儿也不去——而按钮的提示文字明明
        // 写着「按 Esc 释放」。用户按了一通没反应，只能去找「释放鼠标」按钮点。
        // 所以一锁定就把焦点抢回来（Esc 的处理在 keyDown 里）。
        win.makeFirstResponder(self)
        NSCursor.hide()
        // 锁定模式切到窗口级监视器：相对移动要把指针无限拖出画面，而跟踪区域只
        // 覆盖自己的 bounds，指针一出画面就再也收不到事件。
        //
        // 监视器要窗口这个标志才看得见 mouseMoved（否则 AppKit 压根不生成），
        // 所以这里必须置。存原值、releaseLock 时还原。
        // **非锁定模式下不置**：那时它会把 mouseMoved 送给 first responder，和
        // 跟踪区域那条路重复，一次移动送两遍 PointerEvent（2026-11-10 实测）。
        if prevAcceptsMouseMoved == nil {
            prevAcceptsMouseMoved = win.acceptsMouseMovedEvents
            win.acceptsMouseMovedEvents = true
        }
        // 跟踪区域让位，否则锁定模式下两条路同时送。
        removeMoveTracking()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .mouseMoved) {
            [weak self] e in
            guard let self, e.window == win else { return e }
            self.mouseMoved(with: e)
            // **吃掉，不再往下传。**
            //
            // 锁定期间这个视图是 first responder（acquireLock 开头抢的，为了收
            // Esc），而窗口的 acceptsMouseMovedEvents 又置着——事件顺着响应链
            // 会再进一次 mouseMoved。2026-11-10 端到端实测：不return nil 时，
            // 同一点连发三次，客人机收到 7 个 PointerEvent，每步位移翻倍，
            // 指针像脱缰一样冲到屏幕边缘被夹住。
            return nil
        }
        guestCursor = NSPoint(x: (client?.guestWidth ?? 0) / 2,
                              y: (client?.guestHeight ?? 0) / 2)
        warpToCenter()
    }

    /// `window.acceptsMouseMovedEvents` 的原值，releaseLock 时还原。
    private var prevAcceptsMouseMoved: Bool?

    private func releaseLock() {
        if let monitor {
            NSEvent.removeMonitor(monitor)
            self.monitor = nil
        }
        if let prev = prevAcceptsMouseMoved {
            window?.acceptsMouseMovedEvents = prev
            prevAcceptsMouseMoved = nil
        }
        NSCursor.unhide()
        // 解锁之后回到跟踪区域那条路。
        installMoveTracking()
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
        if let keyObserver {
            NotificationCenter.default.removeObserver(keyObserver)
        }
        if let monitor { NSEvent.removeMonitor(monitor) }
        // 隐藏过光标就必须配对取消，否则光标会一直留着不见
        if locked { NSCursor.unhide() }
        // 送出去过的键要补释放：这个视图一消失，客人机那边再也不会收到 keyUp，
        // 不在这儿收口就会留下一堆卡键（Shift、Ctrl、以及任何正在按的字母）。
        // 此时连接通常已经被 ConsoleView.onDisappear 断掉了，所以这一步多半
        // 是空跑——但万一没断，就不该留着卡键。
        releaseAllKeys()
    }
}
