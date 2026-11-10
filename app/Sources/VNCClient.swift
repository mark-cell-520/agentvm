import AppKit
import CoreGraphics
import Foundation
import Network

/// 一个刚刚够用的 RFB（VNC）3.8 客户端。
///
/// 为什么自己写一个而不用现成库：这个 app 是用 swiftc 单命令编的，没有包管理，
/// 引第三方库就得把整个依赖树塞进仓库；而 VNC 协议比想象中薄——握手 12 字节
/// 版本串、选安全类型（QEMU 没设密码时只有 None 一种）、ServerInit 给出画面
/// 尺寸，之后就是「客户端请求一帧、服务器回若干矩形」。真正要写的只有 raw 编码
/// 的矩形拷贝和键盘鼠标事件。
///
/// 三个实现上的决定：
///
/// 1. **主动发 SetPixelFormat 固定成 32 位 BGRA。** 不猜服务器的格式。QEMU 的
///    默认值就是 32/24/真彩/位移 16-8-0，小端存下来正好是 BGRA 字节序，直接
///    对应 CGContext 的 byteOrder32Little + noneSkipFirst，一帧 3 MB 也能一次
///    memcpy 进去。如果照收服务器的 16 位 565，就要为每种格式写一套转换。
/// 2. **DesktopSize 伪编码要请求。** 客人机改分辨率（Windows Setup 就会）时
///    靠它通知我们重建 framebuffer，否则画面会被裁掉一半。
/// 3. **解析必须是增量的。** TCP 不保证一次给全一帧：一个 3 MB 的矩形会切成
///    几十段到。所以状态机每次只吃「确定已经到齐」的那部分，剩下的留在缓冲里
///    等下一次。
final class VNCClient: ObservableObject {

    // ------------------------------------------------------------ 对外状态 --

    @Published private(set) var frame: CGImage?
    @Published private(set) var guestWidth = 0
    @Published private(set) var guestHeight = 0
    @Published private(set) var connected = false
    /// 连接标志的队列本地副本。`connected` 是 @Published，只在主线程写；而
    /// key / pointer / wheel 跑在 VNC 队列上读它，跨线程读一个没有同步的
    /// Bool 是未定义行为（当前只可能导致往已取消的连接多发一个事件，但修起来
    /// 就是一个字段的事）。发送前读这个副本。
    private var queueConnected = false
    @Published private(set) var statusText = "未连接"

    private let host: String
    private let port: UInt16

    init(host: String, port: UInt16) {
        self.host = host
        self.port = port
    }

    // ------------------------------------------------------------ 内部状态 --

    private var conn: NWConnection?
    /// 所有状态都在这个队列上改。收数据的 completion 也在它上面跑，所以解析
    /// 过程中不需要加锁——加锁只会把「偶发花屏」变成「偶发死锁」。
    private let queue = DispatchQueue(label: "agentvm.vnc")
    /// 还没解析的字节。TCP 会把一帧切成很多段，也可能把好几帧粘在一段里。
    private var buf = Data()
    private var stopped = true
    /// 连接代次。每次 connect() 建一条新连接就 +1，旧连接上的迟到事件按代次丢掉。
    ///
    /// 为什么需要它：Network framework 把「本地 cancel」也报成
    /// isComplete = true、error = nil。而 connect() 开头为了清掉旧连接会调一次
    /// conn?.cancel()——那条旧连接上已经排队的 receive completion 之后才回来，
    /// 于是 fail("虚拟机那边把连接关了") 把一次主动重连判成了服务端断开，
    /// 还顺手把刚建好的新连接一起 cancel 掉。症状正是这次用户遇到的：
    /// QEMU 好端端在监听（monitor 的 info vnc 能看到服务器、python 客户端连上
    /// 30 秒一个字节不断），app 却坚持说「虚拟机那边把连接关了」并且再也不动。
    private var generation = 0
    /// framebuffer。桌面大小变化时重建。
    private var ctx: CGContext?
    private var fbW = 0
    private var fbH = 0
    /// 当前这个 FramebufferUpdate 还剩几个矩形没读。
    private var pendingRects: Int?
    private var wantFull = true
    private var lastRequest = Date.distantPast
    /// 最后一次收到整帧的时间。保活定时器看它判断要不要补请求。
    private var lastFrameAt = Date.distantPast
    private var keepAlive: DispatchSourceTimer?
    /// 最后发给客人机的指针位置。滚轮事件要复用这个位置，见 wheel()。
    private var lastX = 0
    private var lastY = 0

    private enum Phase {
        /// 等 12 字节的 "RFB 003.008\n"
        case version
        case securityCount
        case securityTypes(Int)
        case securityResult
        case serverInit
        case messages
    }

    private var phase: Phase = .version

    // ------------------------------------------------------------ 连接 --

    func connect() {
        // 重连之前必须把旧连接的保活定时器和连接本身都收掉。
        // 原来直接新建 NWConnection：旧的 keepAlive DispatchSourceTimer 从未
        // cancel()（只有 disconnect/fail 会停），一直每 250ms 触发并把
        // FramebufferUpdateRequest 发到当前连接上；旧的 TCP 连接也敞着不收数据。
        // 连上之后反复点「重连」十几次，就是十几个定时器叠加着打请求。
        stopKeepAlive()
        conn?.cancel()
        conn = nil
        reconnectAttempts = 0
        stopped = false
        generation &+= 1
        let mine = generation
        set("正在连接 \(host):\(port)")
        let c = NWConnection(host: NWEndpoint.Host(host),
                             port: NWEndpoint.Port(rawValue: port)!,
                             using: .tcp)
        c.stateUpdateHandler = { [weak self] st in
            guard let self else { return }
            // 只认当前这一条连接的状态。被 cancel 掉的旧连接也会走到这里
            // （.cancelled / .failed），按代次丢掉，别让它动新连接的状态。
            guard self.generation == mine else { return }
            switch st {
            case .ready:
                // 每一条新连接都要把状态机复位。复用旧连接时 buf 里可能还剩着
                // 上一条连接的残包，phase 也可能停在半路。
                self.buf.removeAll(keepingCapacity: true)
                self.phase = .version
                self.pendingRects = nil
                self.wantFull = true
                self.lastFrameAt = Date()
                self.reconnectAttempts = 0
                // **不在这一刻宣布「已连接」。** .ready 只说明 TCP 三次握手成了，
                // 不代表 RFB 握得下来：端口被别的东西占着、backlog 满了、对方是个
                // 不收数据的服务，都会走到这里然后永远等不到 ServerInit。那时界面
                // 显示「已连接」却没有画面，而状态明明是绿的——最难查的一种。
                // 真正拿到画面尺寸（serverInit 那一步）才叫连上。
                self.publish(connected: false, status: "正在取画面")
                self.startKeepAlive()
                self.receive(mine)
            case .failed(let e):
                self.fail("连不上 \(self.host):\(self.port)：\(e.localizedDescription)")
            case .cancelled:
                break
            default:
                break
            }
        }
        conn = c
        c.start(queue: queue)
    }

    func disconnect() {
        stopped = true
        reconnectAttempts = 0
        reconnectWork?.cancel()
        stopKeepAlive()
        conn?.cancel()
        conn = nil
        publish(connected: false, status: "已断开")
    }

    /// 断开时自动重连，带退避。
    ///
    /// 原来断线（VM 重启、QEMU 侧关了 VNC）之后状态灯变红、文字变成「虚拟机那边
    /// 把连接关了」，然后再也不动——必须用户自己想起去点「重连」。而断线在这个
    /// 场景里太常见了：用户改完配置重启虚拟机，控制台就瞎了。
    private var reconnectAttempts = 0
    private var reconnectWork: DispatchWorkItem?

    private func fail(_ msg: String) {
        guard !stopped else { return }
        stopped = true
        stopKeepAlive()
        conn?.cancel()
        conn = nil
        publish(connected: false, status: msg)
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        // 最多 8 次：1/2/4/8/16/32/64/128 秒，合计约 4 分钟。虚拟机可能要好几十秒
        // 才起来（重启装完系统、QEMU 重启都要这个量级），5 次只有 31 秒不够；
        // 但无限重连会在用户已经关掉面板之后还一直戳一个没人看的端口——那种情况
        // 由 disconnect() cancel 这个 work item 来兜，不是靠次数。
        guard reconnectAttempts < 8 else {
            publish(connected: false, status: "重连 8 次都没成，点「重连」再试")
            return
        }
        let delay = 1 << reconnectAttempts
        reconnectAttempts += 1
        let work = DispatchWorkItem { [weak self] in
            // **不能在这里判 !stopped。** fail() 刚把 stopped 置成 true（它靠这个
            // 让旧连接的 receive 循环停下来），而重连恰恰要在那个状态下发生——
            // 加上这个判断，自动重连一次都不会执行，断线就永久停在红字上。
            // 「用户不想连了」由 disconnect() cancel 本 work item 表达。
            guard let self else { return }
            self.publish(connected: false, status: "正在重连（第 \(self.reconnectAttempts) 次）")
            self.connect()
        }
        reconnectWork = work
        queue.asyncAfter(deadline: .now() + .seconds(delay), execute: work)
    }

    private func receive(_ g: Int) {
        conn?.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) {
            [weak self] data, _, isComplete, err in
            // 代次对不上说明这条连接已经被取代（用户点了重连、或者换了一台虚拟机），
            // 它带来的 EOF/错误都是旧账，不能拿去动新连接的状态。
            guard let self, self.generation == g, !self.stopped else { return }
            if let data, !data.isEmpty {
                self.buf.append(data)
                self.pump()
            }
            if let err {
                self.fail("连接出错：\(err.localizedDescription)")
                return
            }
            if isComplete {
                self.fail("虚拟机那边把连接关了")
                return
            }
            self.receive(g)
        }
    }

    // ------------------------------------------------------------ 解析 --

    /// 循环吃字节，直到剩下的不够解析一个完整的单元。
    private func pump() {
        while let n = step() {
            if n > 0 { buf.removeFirst(n) }
        }
    }

    /// 解析一步，返回消耗掉的字节数；0 表示状态推进了但没吃字节；nil 表示数据不够。
    @discardableResult
    private func step() -> Int? {
        switch phase {
        case .version:
            guard buf.count >= 12 else { return nil }
            send(Data("RFB 003.008\n".utf8))
            phase = .securityCount
            return 12

        case .securityCount:
            guard buf.count >= 1 else { return nil }
            phase = .securityTypes(Int(buf[buf.startIndex]))
            return 1

        case .securityTypes(let n):
            guard buf.count >= n else { return nil }
            let types = buf[buf.startIndex..<(buf.startIndex + n)]
            // 1 = None（不用密码）。QEMU 不加 -vnc password 时就只有这一种。
            if !types.contains(1) {
                fail("这个 VNC 要密码，app 里连不上")
                return nil
            }
            send(Data([1]))
            phase = .securityResult
            return n

        case .securityResult:
            guard buf.count >= 4 else { return nil }
            // 4 字节安全结果之后必须发 ClientInit（1 字节，是否允许别人同时连）。
            // 漏了它服务端就一直等着，ServerInit 永远不来——症状是「连上了但
            // 没有画面」，而连接状态明明是好的，很不好查。
            send(Data([1]))
            phase = .serverInit
            return 4

        case .serverInit:
            // 宽2 高2 像素格式16 名字长4 = 24，再加名字本身
            guard buf.count >= 24 else { return nil }
            let w = Int(u16at(0))
            let h = Int(u16at(2))
            let nameLen = Int(u32at(20))
            guard buf.count >= 24 + nameLen else { return nil }
            setFramebuffer(w, h)
            sendSetPixelFormat()
            sendSetEncodings()
            phase = .messages
            // 画面尺寸拿到了，这一刻才叫真的连上。见 connect() 里 .ready 那段。
            publish(connected: true, status: "已连接")
            return 24 + nameLen

        case .messages:
            return stepMessage()
        }
    }

    private func stepMessage() -> Int? {
        guard let t = buf.first else { return nil }
        switch t {
        case 0:  // FramebufferUpdate
            if pendingRects == nil {
                guard buf.count >= 4 else { return nil }
                pendingRects = Int(u16at(2))
                return 4
            }
            guard pendingRects! > 0 else {
                // 一个矩形都没有的空更新。也当一帧收完处理。
                pendingRects = nil
                scheduleRequest()
                return 0
            }
            guard buf.count >= 12 else { return nil }
            let x = Int(u16at(0)), y = Int(u16at(2))
            let w = Int(u16at(4)), h = Int(u16at(6))
            let enc = Int32(bitPattern: u32at(8))
            // **伪编码的矩形不带像素数据，只消耗 12 字节。**
            //
            // 原来把 DesktopSize 也当 raw 算：`bytes = w*h*4`，而客人机改分辨率
            // 时 QEMU 发的就是一个只有 12 字节头、宽高填新尺寸的 DesktopSize 矩形
            // （1920×1080 就是 8.3 MB 的期望长度）。`guard buf.count >= 12 + bytes`
            // 永远不满足 → pump() 退出，这 12 字节死死卡在缓冲区头部；之后每次
            // receive 都先撞上同一段字节，后续所有消息全部解析不动。
            // 表现：画面冻结在改分辨率前那一帧、尺寸也不更新，而 keepAlive 还在每
            // 250ms 补请求，QEMU 持续回数据 → buf 无上限增长，内存跟着涨。
            // 装 Windows 时 Setup 改分辨率、Linux 切模式都会触发。
            if enc == encDesktopSize {
                setFramebuffer(w, h)
                pendingRects! -= 1
                if pendingRects! == 0 {
                    pendingRects = nil
                    publishFrame()
                    scheduleRequest()
                }
                return 12
            }
            let bytes = w * h * 4
            guard buf.count >= 12 + bytes else { return nil }
            if enc == encRaw {
                blit(x: x, y: y, w: w, h: h, at: 12)
            } else {
                fail("服务器用了我们不认的编码 \(enc)")
                return nil
            }
            pendingRects! -= 1
            if pendingRects! == 0 {
                // 这一帧的所有矩形都到齐了。
                //
                // **必须在这里判「收完」，不能等解析循环转回来。** 最后一个
                // 矩形往往正好把缓冲区吃到空（一整帧 800×600 是 1.9 MB，会被
                // TCP 切成十几段，最后一段常常刚好补满），那时 pump() 的下一
                // 次 step() 连一个字节都不剩，直接返回 nil 退出——原来写在
                // 「pendingRects == 0」那一支里的逻辑永远走不到，下一个更新
                // 请求也就不发了。RFB 里服务端只在收到请求时才发画面，于是
                // 画面死死停在第一帧：连接是好的、尺寸也对、敲键盘也有反应
                // （客人机确实收到了），就是看不到变化。实测踩过。
                pendingRects = nil
                // 整帧只发一次。每个矩形发一次会白出几张半截画面。
                publishFrame()
                scheduleRequest()
            }
            return 12 + bytes

        case 1:  // SetColourMapEntries
            guard buf.count >= 6 else { return nil }
            let n = Int(u16at(4))
            guard buf.count >= 6 + n * 6 else { return nil }
            return 6 + n * 6

        case 2:  // Bell，只有一个字节，不管
            return 1

        case 3:  // ServerCutText
            guard buf.count >= 8 else { return nil }
            let n = Int(u32at(4))
            guard buf.count >= 8 + n else { return nil }
            return 8 + n

        default:
            fail("服务器发来不认识的消息类型 \(t)")
            return nil
        }
    }

    // ------------------------------------------------------------ 画面 --

    private let encRaw: Int32 = 0
    private let encDesktopSize: Int32 = -223

    private func setFramebuffer(_ w: Int, _ h: Int) {
        guard w > 0, h > 0 else { return }
        guard w != fbW || h != fbH || ctx == nil else { return }
        fbW = w
        fbH = h
        // BGRA：32 位、每通道 8 位、alpha 占位在最高字节。小端内存里正好是
        // B,G,R,0，和 VNC 像素值 0x00RRGGBB 的小端存放一致。每像素位数由
        // bitmapInfo 推出来（8 位 × 4 通道），不用也不能显式给。
        let cs = CGColorSpace(name: CGColorSpace.sRGB)!
        ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                        bytesPerRow: w * 4, space: cs,
                        bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
                            | CGBitmapInfo.byteOrder32Little.rawValue)
        wantFull = true
        publish(width: w, height: h)
    }

    private func blit(x: Int, y: Int, w: Int, h: Int, at off: Int) {
        guard let ctx, let dst0 = ctx.data,
              x >= 0, y >= 0, x + w <= fbW, y + h <= fbH else { return }
        let rowBytes = ctx.bytesPerRow
        let dst = dst0.assumingMemoryBound(to: UInt8.self)
        buf.withUnsafeBytes { raw in
            guard let src = raw.baseAddress?.advanced(by: off) else { return }
            if x == 0 && w == fbW {
                memcpy(dst + y * rowBytes, src, w * h * 4)
            } else {
                for r in 0..<h {
                    memcpy(dst + (y + r) * rowBytes + x * 4,
                           src + r * w * 4, w * 4)
                }
            }
        }
        // 不在这里发帧：一帧可能有多个矩形，每个都发会白出几张半截画面。
        // 整帧到齐了才发，见 stepMessage 里 pendingRects 归零那段。
    }

    private func scheduleRequest() {
        // 全屏一帧 3 MB，客人机不动的时候服务器什么都不回，所以这个循环本身
        // 是被动节奏；加个 20ms 下限只是防止动画类画面把解析循环打满。
        let elapsed = Date().timeIntervalSince(lastRequest)
        let delay = elapsed >= 0.02 ? 0 : 0.02 - elapsed
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.stopped, self.ctx != nil else { return }
            self.lastRequest = Date()
            self.request(incremental: !self.wantFull)
            self.wantFull = false
        }
    }

    /// 保活：一段时间没收到画面就补一个更新请求。
    ///
    /// RFB 里服务端**只在收到 FramebufferUpdateRequest 时才发画面**，而「发过
    /// 一次请求」并不等于「之后会持续推」。实测 QEMU 会在某些情况下不再回应
    /// 增量请求：发出去的字节一个不错（用 python 裸写过同样的字节比对过）、
    /// 监控套接字里 framebuffer 也确实在变（光标在动），但客户端一个字节都
    /// 收不到。此时手动补一个请求，数据立刻恢复流动。
    ///
    /// 所以不能只依赖「收到一帧就再要一帧」——那条链一旦断掉就再也接不回来，
    /// 而现象是「连接好的、尺寸对的、键盘鼠标客人机也收到了，就是画面不动」，
    /// 特别不好查。空闲时每 250ms 补一个 10 字节的小包，代价可以忽略。
    private func startKeepAlive() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + .milliseconds(250),
                   repeating: .milliseconds(250))
        t.setEventHandler { [weak self] in
            guard let self, !self.stopped, self.ctx != nil else { return }
            if Date().timeIntervalSince(self.lastFrameAt) > 0.25 {
                self.request(incremental: true)
            }
        }
        t.resume()
        keepAlive = t
    }

    private func stopKeepAlive() {
        keepAlive?.cancel()
        keepAlive = nil
    }

    private func request(incremental: Bool) {
        guard fbW > 0, fbH > 0 else { return }
        var d = Data([3, incremental ? 1 : 0])
        d.appendUInt16(0)
        d.appendUInt16(0)
        d.appendUInt16(UInt16(fbW))
        d.appendUInt16(UInt16(fbH))
        send(d)
    }

    // ------------------------------------------------------------ 输入 --

    /// 发一个键。keysym 是 X11 的键符号，ASCII 区就是 ASCII 码本身。
    func key(_ keysym: UInt32, down: Bool) {
        guard queueConnected else { return }
        var d = Data([4, down ? 1 : 0, 0, 0])
        d.appendUInt32(keysym)
        send(d)
    }

    /// 发指针位置和按键掩码。坐标是客人机坐标系，不是界面坐标系。
    func pointer(x: Int, y: Int, mask: UInt8) {
        guard queueConnected else { return }
        lastX = clamping(x, 0, max(fbW - 1, 0))
        lastY = clamping(y, 0, max(fbH - 1, 0))
        var d = Data([5, mask])
        d.appendUInt16(UInt16(lastX))
        d.appendUInt16(UInt16(lastY))
        send(d)
    }

    /// 滚轮：往上一格 8、往下一格 16，左右各 32/64。
    ///
    /// 必须在当前指针位置发：PointerEvent 里 x/y 是必填的，填 0 会让客人机
    /// 把指针瞬间挪到左上角——滚动一下光标跳走，很莫名。
    func wheel(_ mask: UInt8) {
        guard queueConnected else { return }
        pointer(x: lastX, y: lastY, mask: mask)
        queue.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            guard let self, self.queueConnected else { return }
            self.pointer(x: self.lastX, y: self.lastY, mask: 0)
        }
    }

    // ------------------------------------------------------------ 发送 --

    private func send(_ data: Data) {
        conn?.send(content: data, completion: .contentProcessed { _ in })
    }

    private func sendSetPixelFormat() {
        // 消息共 20 字节：类型 1 + 补齐 3 + 像素格式 16。
        // 补齐少发一个字节，服务端会认为协议错了直接断开连接——症状是
        // 「ServerInit 都收到了，紧接着连接被关」，很不好查。
        var d = Data([0, 0, 0, 0])
        d.append(contentsOf: [32, 24, 0, 1])       // bpp, depth, 小端, 真彩
        d.appendUInt16(255)                        // 红最大值
        d.appendUInt16(255)
        d.appendUInt16(255)
        d.append(contentsOf: [16, 8, 0])           // 红绿蓝位移
        d.append(contentsOf: [0, 0, 0])            // 像素格式自身的补齐
        send(d)
    }

    private func sendSetEncodings() {
        var d = Data([2, 0])
        d.appendUInt16(2)
        d.appendUInt32(UInt32(bitPattern: encRaw))
        d.appendUInt32(UInt32(bitPattern: encDesktopSize))
        send(d)
        scheduleRequest()
    }

    // ------------------------------------------------------------ 小工具 --

    private func u16at(_ i: Int) -> UInt16 {
        UInt16(buf[buf.startIndex + i]) << 8 | UInt16(buf[buf.startIndex + i + 1])
    }

    private func u32at(_ i: Int) -> UInt32 {
        UInt32(buf[buf.startIndex + i]) << 24
            | UInt32(buf[buf.startIndex + i + 1]) << 16
            | UInt32(buf[buf.startIndex + i + 2]) << 8
            | UInt32(buf[buf.startIndex + i + 3])
    }

    private func clamping(_ v: Int, _ lo: Int, _ hi: Int) -> Int {
        max(lo, min(hi, v))
    }

    private func set(_ s: String) { publish(status: s) }

    private func publish(connected: Bool? = nil, status: String? = nil,
                         width: Int? = nil, height: Int? = nil) {
        // queueConnected 在自己的队列上同步改：key / pointer / wheel 跑在这个
        // 队列上读它。放在 DispatchQueue.main.async 里改就还是跨线程。
        if let connected { queueConnected = connected }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if let connected { self.connected = connected }
            if let status { self.statusText = status }
            if let width { self.guestWidth = width }
            if let height { self.guestHeight = height }
        }
    }

    private func publishFrame() {
        lastFrameAt = Date()
        guard let img = ctx?.makeImage() else { return }
        DispatchQueue.main.async { [weak self] in
            self?.frame = img
        }
    }
}

private extension Data {
    mutating func appendUInt16(_ v: UInt16) {
        append(UInt8(v >> 8)); append(UInt8(v & 0xFF))
    }

    mutating func appendUInt32(_ v: UInt32) {
        // 中间两个字节必须掩码。`UInt8(v >> 16)` 在 v 大于 0xFFFF 时直接崩
        // （「Not enough bits to represent the passed value」）——而编码字段
        // 的值恰恰是 0xFFFFFF21 这种带符号的负数，第一次发 SetEncodings 就崩。
        append(UInt8((v >> 24) & 0xFF)); append(UInt8((v >> 16) & 0xFF))
        append(UInt8((v >> 8) & 0xFF)); append(UInt8(v & 0xFF))
    }
}
