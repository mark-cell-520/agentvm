import Foundation

// vmz — macOS 虚拟机的命令行入口。
//
// 和 vmkit.py 的关系：vmkit 管元数据（vm.json、列表、端口分配）和另外两种
// 客人机（Windows 走 QEMU、Linux 走 QEMU），macOS 这一种的运行时全在这儿。
// 之所以不塞进 vmkit.py：VZ 只有 Swift/ObjC API，Python 调不了，
// 而 Virtualization.framework 没有命令行工具。
//
// 用法：
//   vmz check
//   vmz list
//   vmz install --name m1 [--cpu 4] [--mem 8G] [--disk 80G]
//                 [--user agent] [--password agentvm] [--ipsw ~/x.ipsw]
//                 [--share 宿主路径:挂载名]... [--forward 宿主机端口:客人机端口]...
//   vmz start --name m1        # 前台跑，Ctrl-C 或客人机自己关机就结束
//                 [--share ...]... [--forward ...]...   # 只对本次启动生效，不写进配置
//   vmz stop --name m1
//   vmz status [--name m1]
//   vmz share list --name m1
//   vmz share add --name m1 --path ~/code --mount code [--read-only]
//   vmz share remove --name m1 --mount code
//   vmz forward list --name m1
//   vmz forward add --name m1 --host 2222 --guest 22
//   vmz forward remove --name m1 --host 2222
//   vmz snapshot create --name m1 --tag clean
//   vmz snapshot list --name m1
//   vmz snapshot apply --name m1 --tag clean
//   vmz clone --name m1 --new-name m2
//   vmz delete --name m1
//
// 共享目录走 virtiofs，客人机自动挂到 /Volumes/<挂载名>，不需要在客人机里
// 敲 mount。端口转发走 vsock（VZ 的 NAT 没有公开的端口映射 API），宿主机上
// 只绑 127.0.0.1；注意客人机里得有程序监听对应的 vsock 端口，macOS 客人机
// 默认没有这样的服务——详见 MacVMEngine 里 MacVsockProxy 的注释。
//
// 密钥：--password 会出现在命令行参数里（本机任何用户 ps 一下就能看到，
// 而安装要跑几十分钟），推荐 --password-file <路径>，vmz 读完即删。

let argv = Array(CommandLine.arguments.dropFirst())
guard let cmd = argv.first else {
    FileHandle.standardError.write("vmz: 缺命令。check / list / install / start / stop / status / snapshot / clone / delete\n".data(using: .utf8)!)
    exit(2)
}

/// 读密码文件的内容。返回 nil 表示没有这个文件（调用方自己决定是不是错误）。
///
/// 为什么要有密码文件而不是 `--password xxx`：`ps` 对所有本地用户可见完整
/// 命令行，而 macOS 安装要跑几十分钟。项目规约是「密钥不经过命令行参数」，
/// macOS 这条路径一开始没照着做。
func readPasswordFile(_ path: String) -> String? {
    let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
    guard let data = try? Data(contentsOf: url),
          let s = String(data: data, encoding: .utf8) else { return nil }
    return s.trimmingCharacters(in: .whitespacesAndNewlines)
}

/// 自检 entitlement：让 codesign 看一眼自己签没签
/// com.apple.security.virtualization。
///
/// 为什么不调一次 VZMacOSRestoreImage.fetchLatestSupported 来真验：那一下要
/// 联网，而 `vmz check` 是 install.sh 装完立刻跑的（见 install.sh 第 61 行），
/// 离线机器上会假失败——脚本会把刚装好的 vmz 删掉。codesign 只读本地签名。
///
/// 为什么必须验：原来这一项硬编码 ok: true，什么都没查。签名漏了 entitlement
/// 的 vmz 照样报「通过」，而真缺 entitlement 时 VZ 抛的是未捕获的
/// NSInvalidArgumentException，进程直接 abort，报错和真实原因毫无关系。
func macEntitlementCheck() -> (ok: Bool, detail: String) {
    // 自己的可执行文件。argv[0] 可能是相对路径，解析成绝对路径再问。
    let selfPath = URL(fileURLWithPath: CommandLine.arguments[0])
        .resolvingSymlinksInPath().standardizedFileURL.path
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    p.arguments = ["-d", "--entitlements", "-", selfPath]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else {
        return (false, "跑不了 codesign，没法确认签名里有没有 virtualization entitlement")
    }
    p.waitUntilExit()
    let out = (try? pipe.fileHandleForReading.readToEnd())
        .flatMap { String(data: $0, encoding: .utf8) } ?? ""
    // 没签名的二进制 codesign 会报错退出、什么都不打，这里自然就是 false。
    if out.contains("com.apple.security.virtualization") {
        return (true, "com.apple.security.virtualization（签名里有）")
    }
    return (false, "签名里没有 com.apple.security.virtualization。"
        + "手动编的话：codesign -s - --entitlements app/Resources/entitlements.plist vmz")
}

/// 取 --flag 的值。`--flag value` 和 `--flag=value` 都认。
func opt(_ name: String, _ def: String? = nil) -> String? {
    var i = 0
    while i < argv.count {
        if argv[i] == "--\(name)" || argv[i] == "-\(name)" {
            if i + 1 < argv.count { return argv[i + 1] }
            return def
        }
        if argv[i].hasPrefix("--\(name)=") {
            return String(argv[i].dropFirst("--\(name)=".count))
        }
        i += 1
    }
    return def
}

/// 取一个**可重复**的 --flag 的所有值。
///
/// `--share a:b --share c:d` 两个都要收到。opt() 只返回第一个匹配，
/// 用它就会静默丢掉后面的共享目录——用户看到的是「加了两个只挂上一个」。
func opts(_ name: String) -> [String] {
    var out: [String] = []
    var i = 0
    while i < argv.count {
        if argv[i] == "--\(name)" || argv[i] == "-\(name)" {
            if i + 1 < argv.count {
                out.append(argv[i + 1])
                i += 1
            }
        } else if argv[i].hasPrefix("--\(name)=") {
            out.append(String(argv[i].dropFirst("--\(name)=".count)))
        }
        i += 1
    }
    return out
}

func hasFlag(_ name: String) -> Bool {
    argv.contains("--\(name)") || argv.contains("-\(name)")
}

if hasFlag("no-json") || hasFlag("human") { MacEmit.on = false }

/// 造一条和 vmkit 的 vm_record 同形状的记录。
/// app 那侧的 VMInfo 就是照这个解的，字段少一个整条条目会被静默丢掉。
func macRecord(_ spec: MacVMSpec, pid: Int32?) -> [String: Any] {
    [
        "name": spec.name,
        // distro 显示成「macOS 27.2.1 (26B131)」。界面按 distro id 找名字的
        // 那条路走不通（没有 catalog 条目），所以这里直接给可读文本。
        "distro": spec.build.isEmpty ? "macOS" : "macOS \(spec.build)",
        "kind": "macos",
        "running": pid != nil,
        "pid": pid.map { Int($0) } ?? NSNull(),
        "ready": spec.installed,
        "cpus": spec.cpus,
        "ram": spec.ram,
        "disk": spec.disk,
        "user": spec.user,
        // macOS 客人机没有这三个端口：VZ 的 NAT 不对外监听，
        // SSH 走 NAT 内部地址，VNC 不存在。给 0 表示「没有」。
        "ssh_port": 0,
        // NSNull 而不是 nil：字典的字面量槽位是 Any，塞 nil 编译不过；
        // 而 JSON 里要的确实是 null（app 侧 rdpPort 是 Int?，null 才解成 nil）。
        "rdp_port": NSNull(),
        "vnc_port": 0,
        "web_port": NSNull(),
        "headless": true,
        "ssh_command": "",
        "disk_used_bytes": Int(macDiskUsed(MacVMPaths.disk(spec.name))),
        "attach_iso": false,
        "disks": [] as [[String: Any]],
        "shares": [] as [[String: Any]],
        "source": NSNull(),
        "mac_build": spec.build,
        "mac_resolution": "\(spec.width)x\(spec.height)",
        "mac_installed": spec.installed,
        // macOS 自己的共享目录和端口转发。**不能塞进 "shares" 那个键**：
        // app 那侧 VMInfo.shares 解的是 {tag, path}（Linux 的 9p 共享目录），
        // 塞这份 {name, host_path, read_only} 进去会让整条 VMInfo 解码失败，
        // 而 AppState 那边是 compactMap——失败的表现是「虚拟机从列表里
        // 消失」，命令行跑同样的命令却是好的，极难查。
        // VMInfo 不认这两个键，JSONDecoder 会直接跳过，安全。
        "mac_shares": macShareRecords(spec.shares),
        "mac_forwards": macForwardRecords(spec.forwards),
    ]
}

private extension URL {
    func createDirectoryIfNeeded() throws {
        try FileManager.default.createDirectory(at: self, withIntermediateDirectories: true)
    }
}

/// 共享目录列表 → JSON 记录。键名和 macvm.json 里的一致（snake_case），
/// 这样 vmkit 那侧原样转出去也不会变形。
func macShareRecords(_ shares: [MacVMShare]) -> [[String: Any]] {
    shares.map { ["host_path": $0.hostPath, "name": $0.name, "read_only": $0.readOnly] }
}

func macForwardRecords(_ forwards: [MacVMForward]) -> [[String: Any]] {
    forwards.map { ["host_port": $0.hostPort, "guest_port": $0.guestPort] }
}

/// 取 --name 的值，顺带做白名单校验。
///
/// 校验放在这一处收口：这个名字会拼成文件路径（MacVMPaths.dir(name)），
/// `../` 或者绝对路径都能跳出 ~/.agentvm/vms。实测 `vmz delete --name
/// ../../avmaudit-victim-dir` 真的把目录外的真目录递归删掉了，退出码 0
/// 还打一句「已删」。每个子命令都走这个入口，漏一个就等于没校验
/// （快照 tag 那边踩过同样的坑）。
func requireName() -> String {
    guard let n = opt("name"), !n.isEmpty else {
        MacEmit.error("\(cmd) 需要 --name")
    }
    do {
        try MacVMPaths.validateName(n)
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }
    return n
}

switch cmd {

// ---------------------------------------------------------------- check --

case "check":
    #if arch(arm64)
    let v = ProcessInfo.processInfo.operatingSystemVersion
    let ent = macEntitlementCheck()
    MacEmit.w(["t": "check", "items": [
        ["name": "Apple Silicon", "ok": true,
         "detail": ProcessInfo.processInfo.processorCount.description + " 核"],
        ["name": "macOS 版本", "ok": v.majorVersion >= 12,
         "detail": "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
            + (v.majorVersion >= 27 ? "（支持无人值守建账号）" : "（装完要手动建账号）")],
        ["name": "entitlement", "ok": ent.ok, "detail": ent.detail],
    ]])
    if v.majorVersion < 12 {
        MacEmit.log("macOS 12 以下没有 VZMacOSRestoreImage，装不了 macOS 客人机。", level: "warn")
    }
    // **退出码必须反映检查结果。** 原来是打完 JSON 就往下走，退出码恒为 0——
    // 而 install.sh 装完跑的是 `vmz check >/dev/null 2>&1` 只看退出码，
    // app/build.sh 里也一样。于是一份没有 com.apple.security.virtualization
    // 权利的裸二进制，两个脚本都报「check 通过」，用户到点安装时才死在
    // 「restore image catalog failed to load」。2026-11-11 实测踩到：
    // 未签名那份的 check 输出里 entitlement 明写 ok:false，退出码却是 0。
    //
    // 只让 entitlement 这一项决定退出码：另外两项（核数、系统版本）在这一行
    // 之前就已经各自 warn 过，而且它们不是「装完了但跑不起来」那类静默故障。
    if !ent.ok { exit(1) }
    #else
    MacEmit.error("只在 Apple Silicon 的 Mac 上有效。")
    #endif

// ------------------------------------------------------------------ list --

case "list", "status":
    var names: [String] = []
    if let n = opt("name"), !n.isEmpty {
        names = [n]
    } else {
        names = (try? FileManager.default.contentsOfDirectory(
            at: MacVMPaths.root, includingPropertiesForKeys: nil)
            .filter { FileManager.default.fileExists(atPath: MacVMPaths.spec($0.lastPathComponent).path) }
            .map(\.lastPathComponent).sorted()) ?? []
    }
    var recs: [[String: Any]] = []
    for n in names {
        let spec: MacVMSpec
        do { spec = try macLoadSpec(n) }
        catch let e as MacVMError {
            // 指定了名字却找不到，必须报错。静默返回空列表的话，
            // 用户看到一个「没有虚拟机」，会以为装失败了——而实际只是打错字。
            if !names.isEmpty && names.count == 1 { MacEmit.error(e.message) }
            continue
        }
        let pid = macReadPID(n)
        recs.append(macRecord(spec, pid: pid))
    }
    MacEmit.w(["t": "vms", "vms": recs])

// --------------------------------------------------------------- catalog --

case "catalog":
    #if arch(arm64)
    do {
        let img = try macFetchLatestRestoreImage()
        let req = img.mostFeaturefulSupportedConfiguration
        let v = img.operatingSystemVersion
        var rec: [String: Any] = [
            "build": img.buildVersion,
            "version": "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)",
            "url": img.url.absoluteString,
            "supported": req != nil,
        ]
        if let req {
            rec["min_cpus"] = req.minimumSupportedCPUCount
            rec["min_mem_bytes"] = req.minimumSupportedMemorySize
        }
        MacEmit.w(["t": "catalog", "macos": rec])
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }
    #else
    MacEmit.error("只在 Apple Silicon 的 Mac 上有效。")
    #endif

// --------------------------------------------------------------- install --

case "install":
    let name = requireName()
    do {
        try MacVMPaths.validateName(name)
        if FileManager.default.fileExists(atPath: MacVMPaths.spec(name).path), !hasFlag("force") {
            MacEmit.error("\(name) 已存在。换个名字，或加 --force 重装（里面的数据一起没）。")
        }
        if hasFlag("force") {
            if macReadPID(name) != nil {
                MacEmit.error("\(name) 正在运行。先 stop 再 --force。")
            }
            // 只清 vmz 自己的产物，别把整个目录端掉。两个理由，都是实测踩的：
            //
            // 1. **日志会凭空消失。** vmkit 的 spawn_detached_install 在起这个
            //    进程之前就 open("a") 了 <vm>/install.log，stdout 也重定向到
            //    那个句柄。整目录 removeItem 之后句柄还开着、文件已经 unlink，
            //    于是安装照跑、输出写进一个谁都打不开的 inode：install.log
            //    不再出现，`vmkit status` 的日志路径指向空气，而 macOS 安装要
            //    几十分钟，全程零可观测。
            // 2. **25.6 GB 的 IPSW 白下。** ipsw/ 在这个目录里，一起删等于
            //    macInstall 再也走不到「复用已下载的」那条分支，重装必须重新
            //    下 25.6 GB（本次实测下载要 19 分钟）。
            //
            // 所以逐项删：系统盘、NVRAM、机器标识、硬件模型、配置。快照留着
            // 不合理（那是旧盘的 COW 克隆，底册变了就是脏数据），一起删。
            for u in [MacVMPaths.spec(name), MacVMPaths.disk(name),
                      MacVMPaths.auxStorage(name), MacVMPaths.machineID(name),
                      MacVMPaths.hardwareModel(name), MacVMPaths.pidFile(name),
                      MacVMPaths.snapshots(name)] {
                try? FileManager.default.removeItem(at: u)
            }
            MacEmit.log("--force：清掉 \(name) 的磁盘与配置重新装（已下的 IPSW 保留）",
                        level: "warn")
        }
        var spec = MacVMSpec()
        spec.name = name
        spec.cpus = Int(opt("cpu") ?? "") ?? 4
        spec.ram = opt("mem") ?? "8G"
        spec.disk = opt("disk") ?? "80G"
        spec.user = opt("user") ?? "agent"
        // 密码两个来源：--password（用户手输， CLI 场景）和 --password-file
        // （vmkit 传的，避免密码进命令行参数）。文件优先。
        if let pf = opt("password-file"), let p = readPasswordFile(pf) {
            spec.password = p
            // 用完即删。留在 VM 目录里一个明文密码文件，下一个翻目录的人
            // 就拿到了。
            try? FileManager.default.removeItem(atPath: pf)
        } else if let pw = opt("password") {
            spec.password = pw
            // 显式警告，别让用户以为自己和 vmkit 走了同一条安全路径：
            // ps 对所有本地用户可见完整命令行，而 macOS 安装要跑几十分钟，
            // 这期间任何本机用户都能看到这个密码。vmkit 传的是
            // --password-file（见 _vmz_password_args），只有 CLI 直用
            // 没有这层保护。
            MacEmit.log("警告：--password 会把密码留在命令行参数里，本机任何用户 "
                + "ps 一下就能看到。建议改用 --password-file <路径>（读完即删）。",
                level: "warn")
        } else {
            spec.password = "agentvm"
        }
        spec.width = Int(opt("width") ?? "") ?? 1440
        spec.height = Int(opt("height") ?? "") ?? 900
        // 共享目录和端口转发。写在配置里，之后每次启动都带着。
        // 校验在 macParseShares/macParseForwards 里做（目录必须真的存在），
        // 装到一半才发现路径打错了更烦。
        do {
            spec.shares = try macParseShares(opts("share"))
            spec.forwards = try macParseForwards(opts("forward"))
        } catch let e as MacVMError {
            MacEmit.error(e.message)
        } catch {
            MacEmit.error(error.localizedDescription)
        }
        // 密码要有最低强度：VZMacGuestProvisioningOptions 会校验，
        // 太简单的密码在第一次启动时静默不建账号，而日志里只有一句 warn。
        if spec.password.count < 4 {
            MacEmit.error("密码至少 4 个字符（客人机建账号时的最低要求）。")
        }
        try MacVMPaths.dir(name).createDirectoryIfNeeded()
        // 安装必须在能看到画面的进程里跑（VZMacOSInstaller 需要一个活的
        // VZVirtualMachine），所以这里就是前台跑完，不能甩后台。
        // 界面那边要说清楚：关掉这个终端 = 安装作废。
        spec = try macInstall(spec: spec, ipswLocal: opt("ipsw")) { _, _ in }
        spec.provisioned = false
        try macSaveSpec(spec)
        MacEmit.w(["t": "done", "vm": macRecord(spec, pid: nil)])
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

// ----------------------------------------------------------------- start --

case "start":
    let name = requireName()
    do {
        let saved = try macLoadSpec(name)
        if !saved.installed {
            MacEmit.error("\(name) 还没装完。没有系统的盘启动只会卡在黑屏，先跑 install。")
        }
        if macReadPID(name) != nil {
            MacEmit.error("\(name) 已经在跑（pid \(macReadPID(name)!)）。")
        }
        // spec 是这次启动用的，saved 是磁盘上的。命令行给的 --share / --forward
        // 只改 spec：**不写回 saved**——「临时试一个端口」和「改配置」是两件事，
        // 混在一起会让用户下次启动莫名其妙多出一条转发（原来只有一处会写盘：
        // 下面把 provisioned 置 true 那一下，而它整体重写整个文件）。
        // 要持久化用 `vmz share add` / `vmz forward add`。
        var spec = saved
        do {
            let shareArgs = opts("share")
            let forwardArgs = opts("forward")
            if !shareArgs.isEmpty { spec.shares = try macParseShares(shareArgs) }
            if !forwardArgs.isEmpty { spec.forwards = try macParseForwards(forwardArgs) }
            if !shareArgs.isEmpty || !forwardArgs.isEmpty {
                MacEmit.log("本次启动临时使用命令行给的共享目录/端口转发（没写进配置）",
                            level: "warn")
            }
        } catch let e as MacVMError {
            MacEmit.error(e.message)
        } catch {
            MacEmit.error(error.localizedDescription)
        }
        if !spec.shares.isEmpty {
            MacEmit.log("共享目录 \(spec.shares.count) 个："
                + spec.shares.map { $0.name + " → " + $0.hostPath }.joined(separator: "、")
                + "（客人机里挂在 /Volumes/<名字>）")
        }
        if !spec.forwards.isEmpty {
            MacEmit.log("端口转发 \(spec.forwards.count) 条："
                + spec.forwards.map { "127.0.0.1:\($0.hostPort) → vsock \($0.guestPort)" }
                    .joined(separator: "、"))
        }
        let rt = MacVMRuntime()
        // 前台跑：VZ 的 VM 跟进程同生共死，这个终端一退客人机就停。
        // 这是框架限制，不是偷懒——见 MacVMEngine 顶部注释。
        var stopping = false
        rt.onGuestStop = {
            if !stopping {
                MacEmit.log("客人机自己关机了", level: "info")
                exit(0)
            }
        }
        // 收到停止信号后的收尾。只做一次：用户连按 Ctrl-C、或者同一个信号
        // 被投递两次时，第二次直接忽略。
        var stopRequested = false
        // 两个信号源共用一条串行队列，handler 不会并发跑，
        // 所以上面那个标志不需要加锁。
        let sigQueue = DispatchQueue(label: "vmz.signal")
        let shutdown: () -> Void = {
            guard !stopRequested else { return }
            stopRequested = true
            stopping = true
            MacEmit.log("收到停止信号，正在优雅关机（最长 90 秒）", level: "info")
            // **不能在这儿 exit(0)。** rt.stop() 只是把 machine.stop 投递到
            // vmQueue 就返回，下一行退出进程的话客人机从没收到 ACPI 关机事件，
            // 那一下跟 kill -9 等效：APFS 留下未干净的日志，下次启动跑几分钟
            // fsck。所以等 stop 真的结束（或等超时）才退。
            rt.stop { timedOut in
                if timedOut {
                    MacEmit.log("没等上优雅停机，强制退出（这次是非优雅停机）",
                                level: "warn")
                } else {
                    MacEmit.log("客人机已停下，退出", level: "info")
                }
                exit(0)
            }
            // 兜底：completion 万一没被调用（runtime 被提前释放之类），
            // 也不能让进程永远赖着不走。90 秒停机 + 10 秒余量。
            sigQueue.asyncAfter(deadline: .now() + 100) {
                MacEmit.log("等停机等不到，强制退出", level: "warn")
                exit(0)
            }
        }
        // 顺序很重要：先 signal(SIG_IGN) 再 activate。反过来的话，装 handler
        // 和激活信号源之间有几十微秒，这期间信号到来仍按默认行为处理——
        // SIGTERM 的默认行为就是当场杀掉进程，上面的 handler 一次都没跑。
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        // SIGTERM/SIGINT 走优雅停机。直接 kill -9 会让 APFS 留下未干净的
        // 日志，下次启动跑好几分钟 fsck；而 `vmz stop` 发的就是这个信号。
        let sig = DispatchSource.makeSignalSource(signal: SIGTERM, queue: sigQueue)
        let intr = DispatchSource.makeSignalSource(signal: SIGINT, queue: sigQueue)
        sig.setEventHandler(handler: shutdown)
        intr.setEventHandler(handler: shutdown)
        sig.activate()
        intr.activate()

        MacEmit.stage("start", "启动 \(name)")
        // host: "vmz" ——这台 VM 归命令行宿主。原来这里硬编码 "app"，
        // 于是 `vmz stop` 读到 app:<pid> 就拒绝发信号，用户停不了
        // 自己刚用命令行起的 VM。
        try rt.start(spec: spec, provision: !spec.provisioned, host: "vmz")
        // 只有真的跑过 provisioning 才标记已建账号。macOS 26 及以下的
        // VZMacGuestProvisioningOptions 不存在，provision 传进去会被忽略，
        // 而客人机仍然要人手动过一遍 Setup Assistant——这时候把 provisioned
        // 置真，下次启动就再也不建账号了，用户对着登录界面干瞪眼。
        // AppState 侧同一处逻辑判的就是 rt.didProvision，两边要对齐。
        if rt.didProvision, !spec.provisioned {
            // 存的是 saved（磁盘上那份），不是 spec——spec 里可能带着命令行
            // 临时覆盖的共享目录/端口转发，见上面 spec/saved 的注释。
            var s = saved
            s.provisioned = true
            try? macSaveSpec(s)
        }
        MacEmit.log("macOS 客人机已启动。第一次启动会自动建账号 \(spec.user) 并打开远程登录。", level: "info")
        MacEmit.log("这个终端保持前台，关掉它客人机就停（Ctrl-C 优雅关机）。", level: "info")
        MacEmit.w(["t": "running", "name": name, "pid": Int(ProcessInfo.processInfo.processIdentifier)])
        // 常驻。DispatchMain 会一直跑 signal source 和 delegate 回调。
        dispatchMain()
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

// ------------------------------------------------------------------ stop --

case "stop":
    let name = requireName()
    guard let entry = macReadPIDFile(name) else {
        MacEmit.error("\(name) 没在运行。")
    }
    // 只对 vmz 宿主发信号。宿主是 app 时，那个 pid 是 AgentVM.app 自己的，
    // 发 SIGTERM 会把整个 app 当场杀掉——用户看到「已停」，实际 app 没了，
    // VM 属于非优雅停机，APFS 留下未干净日志。见 macWritePID 的注释。
    if entry.host == "app" {
        MacEmit.error("这个 macOS 虚拟机正在 AgentVM app 里运行（pid \(entry.pid)）。"
            + "命令行不能停它——发信号会连 app 一起杀掉。"
            + "请在 app 的虚拟机窗口里选「关机」，或者关掉那个窗口。")
    }
    MacEmit.log("给 \(name)（pid \(entry.pid)）发停止信号", level: "info")
    kill(entry.pid, SIGTERM)
    // 等它优雅停完，最多 100 秒：MacVMRuntime.stop 内部等 90 秒，这里再加
    // 10 秒余量（收尾、删 pid 文件）。
    //
    // 单位必须和注释对得上：原来写「最多 100 秒」而循环是 100 次 ×
    // usleep(500_000) = 50 秒。排障的人照着注释等，会在第 50 秒看到一句
    // 「等不停」，而客人机其实还在正常关机。
    let deadline = Date().addingTimeInterval(100)
    while Date() < deadline {
        if macReadPID(name) == nil {
            MacEmit.log("\(name) 已停", level: "info")
            MacEmit.w(["t": "stopped", "name": name])
            break
        }
        usleep(500_000)
    }
    if macReadPID(name) != nil {
        MacEmit.error("等不停。guest 里选「关机」更可靠，"
            + "或者 kill \(entry.pid)（会丢未落盘数据）。")
    }

// ------------------------------------------------------- 共享目录（virtiofs） --

case "share":
    let name = requireName()
    guard let action = argv.count > 1 ? argv[1] : nil,
          ["list", "add", "remove"].contains(action) else {
        MacEmit.error("share 需要动作：list / add / remove")
    }
    do {
        var spec = try macLoadSpec(name)
        switch action {
        case "add":
            let path = opt("path") ?? ""
            let mount = (opt("mount") ?? "").trimmingCharacters(in: .whitespaces)
            guard !path.isEmpty else { MacEmit.error("share add 需要 --path <宿主机目录>") }
            guard !mount.isEmpty else { MacEmit.error("share add 需要 --mount <挂载名>") }
            let one = try macParseShare(hasFlag("read-only")
                                        ? "\(path):\(mount):ro" : "\(path):\(mount)")
            // 整份重排：新的放后面。重复名字/重复目录由 validateShares 拦。
            spec.shares.append(one)
            try MacVMPaths.validateShares(spec.shares)
            try macSaveSpec(spec)
            MacEmit.log("已共享 \(one.hostPath) → 客人机 /Volumes/\(one.name)"
                + (one.readOnly ? "（只读）" : "")
                + "。重启虚拟机后生效。", level: "info")
            MacEmit.w(["t": "done", "vm": ["shares": macShareRecords(spec.shares)]])
        case "remove":
            let mount = (opt("mount") ?? "").trimmingCharacters(in: .whitespaces)
            guard !mount.isEmpty else { MacEmit.error("share remove 需要 --mount <挂载名>") }
            let before = spec.shares.count
            spec.shares.removeAll { $0.name == mount }
            guard spec.shares.count != before else {
                MacEmit.error("没有挂载名叫 \(mount) 的共享目录。"
                    + "现有的：\(spec.shares.map(\.name).joined(separator: "、"))")
            }
            try macSaveSpec(spec)
            MacEmit.log("已摘掉共享目录 \(mount)（宿主机上的文件没动）。重启后生效。",
                        level: "info")
            MacEmit.w(["t": "done", "vm": ["shares": macShareRecords(spec.shares)]])
        default:
            // 形状照 snapshot list 的先例：app 那侧只认 t == "done"。
            MacEmit.w(["t": "done", "vm": ["shares": macShareRecords(spec.shares)]])
            if !hasFlag("no-json") && !hasFlag("human") {
                for sh in spec.shares {
                    MacEmit.log("\(sh.name)\t\(sh.hostPath)"
                        + (sh.readOnly ? "\t（只读）" : ""), level: "info")
                }
            }
        }
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

// ------------------------------------------------------ 端口转发（vsock） --

case "forward":
    let name = requireName()
    guard let action = argv.count > 1 ? argv[1] : nil,
          ["list", "add", "remove"].contains(action) else {
        MacEmit.error("forward 需要动作：list / add / remove")
    }
    do {
        var spec = try macLoadSpec(name)
        switch action {
        case "add":
            guard let host = Int(opt("host") ?? ""), let guest = Int(opt("guest") ?? "") else {
                MacEmit.error("forward add 需要 --host <宿主机端口> --guest <客人机 vsock 端口>")
            }
            let one = try macParseForward("\(host):\(guest)")
            spec.forwards.append(one)
            try MacVMPaths.validateForwards(spec.forwards)
            try macSaveSpec(spec)
            MacEmit.log("已加转发 127.0.0.1:\(one.hostPort) → 客人机 vsock 端口 \(one.guestPort)。"
                + "重启虚拟机后生效。", level: "info")
            MacEmit.w(["t": "done", "vm": ["forwards": macForwardRecords(spec.forwards)]])
        case "remove":
            guard let host = Int(opt("host") ?? "") else {
                MacEmit.error("forward remove 需要 --host <宿主机端口>")
            }
            let before = spec.forwards.count
            spec.forwards.removeAll { $0.hostPort == host }
            guard spec.forwards.count != before else {
                // 现有的列表先拼成串再插值：字符串插值里再套带引号的字符串
                // 字面量，Swift 的词法分析会找不到收尾的引号。
                let have = spec.forwards
                    .map { "\($0.hostPort)→\($0.guestPort)" }
                    .joined(separator: "、")
                MacEmit.error("没有宿主机端口 \(host) 的转发。现有的：\(have)")
            }
            try macSaveSpec(spec)
            MacEmit.log("已摘掉转发 127.0.0.1:\(host)。重启后生效。", level: "info")
            MacEmit.w(["t": "done", "vm": ["forwards": macForwardRecords(spec.forwards)]])
        default:
            MacEmit.w(["t": "done", "vm": ["forwards": macForwardRecords(spec.forwards)]])
            if !hasFlag("no-json") && !hasFlag("human") {
                for f in spec.forwards {
                    MacEmit.log("127.0.0.1:\(f.hostPort) → 客人机 vsock 端口 \(f.guestPort)",
                                level: "info")
                }
            }
        }
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

// -------------------------------------------------------------- snapshot --

case "snapshot":
    let name = requireName()
    guard let action = argv.count > 1 ? argv[1] : nil,
          ["create", "list", "apply", "delete"].contains(action) else {
        MacEmit.error("snapshot 需要动作：create / list / apply / delete")
    }
    let tag = opt("tag") ?? ""
    do {
        switch action {
        case "create":
            guard !tag.isEmpty else { MacEmit.error("create 需要 --tag") }
            try macSnapshotCreate(name, tag: tag)
        case "delete":
            guard !tag.isEmpty else { MacEmit.error("delete 需要 --tag") }
            try macSnapshotDelete(name, tag: tag)
        case "apply":
            guard !tag.isEmpty else { MacEmit.error("apply 需要 --tag") }
            try macSnapshotApply(name, tag: tag)
        default:
            // 形状必须和 vmkit 自己的 snapshot list 一样：那边发的是
            // {"t":"done","vm":{"snapshots":[...]}}（Emit.done({"snapshots": out})），
            // 而 vmkit 的 _relay 是把 vmz 的事件原样转出去、不改形状的。
            // 原来这里发 {"t":"snapshots","name":...,"snapshots":[...]}，
            // 而 app 侧只认 t == "done"——macOS 虚拟机的快照列表在 app 里
            // 永远是空的，命令行却是好的。
            // 顺带：--json 关掉时 vmkit 的人话模式会把 t == "done" 的事件
            // pretty print 出来，t == "snapshots" 什么都不打。
            MacEmit.w(["t": "done", "vm": ["snapshots": macSnapshotList(name)]])
        }
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

// ----------------------------------------------------------------- clone --

case "clone":
    let name = requireName()
    guard let newName = opt("new-name") ?? opt("newName"), !newName.isEmpty else {
        MacEmit.error("clone 需要 --new-name")
    }
    do { try macClone(name, newName: newName) }
    catch let e as MacVMError { MacEmit.error(e.message) }
    catch { MacEmit.error(error.localizedDescription) }

// ---------------------------------------------------------------- delete --

case "delete":
    let name = requireName()
    do {
        if macReadPID(name) != nil {
            MacEmit.error("\(name) 正在运行。先 stop 再删。")
        }
        let dir = MacVMPaths.dir(name)
        guard FileManager.default.fileExists(atPath: dir.path) else {
            MacEmit.error("没有 \(name) 这个虚拟机")
        }
        try FileManager.default.removeItem(at: dir)
        // 说清「什么被释放了、什么没有」。恢复镜像在全局缓存里是硬链，
        // 删 VM 只是少一个链接，25.6 GB 那份数据还在（别的虚拟机可能正指着
        // 它）。原来只说「整个目录」，用户会以为那 25.6 GB 也回来了。
        let cache = MacVMPaths.ipswCacheDir
        let shared = (try? FileManager.default.contentsOfDirectory(atPath: cache.path))?
            .filter { !$0.hasPrefix(".") } ?? []
        var msg = "\(name) 已删（\(dir.path) 整个目录）"
        if !shared.isEmpty {
            msg += "。恢复镜像仍在全局缓存 \(cache.path)（\(shared.joined(separator: "、"))），"
                + "硬链删掉不影响它，别的 macOS 虚拟机还在用"
        }
        MacEmit.log(msg, level: "info")
        MacEmit.w(["t": "deleted", "name": name])
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

default:
    MacEmit.error("不认识的命令：\(cmd)")
}
