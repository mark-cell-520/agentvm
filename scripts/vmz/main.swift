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
//   vmz start --name m1        # 前台跑，Ctrl-C 或客人机自己关机就结束
//   vmz stop --name m1
//   vmz status [--name m1]
//   vmz snapshot create --name m1 --tag clean
//   vmz snapshot list --name m1
//   vmz snapshot apply --name m1 --tag clean
//   vmz clone --name m1 --new-name m2
//   vmz delete --name m1

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
    ]
}

private extension URL {
    func createDirectoryIfNeeded() throws {
        try FileManager.default.createDirectory(at: self, withIntermediateDirectories: true)
    }
}

func requireName() -> String {
    guard let n = opt("name"), !n.isEmpty else {
        MacEmit.error("\(cmd) 需要 --name")
    }
    return n
}

switch cmd {

// ---------------------------------------------------------------- check --

case "check":
    #if arch(arm64)
    let v = ProcessInfo.processInfo.operatingSystemVersion
    MacEmit.w(["t": "check", "items": [
        ["name": "Apple Silicon", "ok": true,
         "detail": ProcessInfo.processInfo.processorCount.description + " 核"],
        ["name": "macOS 版本", "ok": v.majorVersion >= 12,
         "detail": "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
            + (v.majorVersion >= 27 ? "（支持无人值守建账号）" : "（装完要手动建账号）")],
        ["name": "entitlement", "ok": true,
         "detail": "com.apple.security.virtualization（VZ 必需）"],
    ]])
    if v.majorVersion < 12 {
        MacEmit.log("macOS 12 以下没有 VZMacOSRestoreImage，装不了 macOS 客人机。", level: "warn")
    }
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
            try? FileManager.default.removeItem(at: MacVMPaths.dir(name))
            MacEmit.log("--force：清掉 \(name) 重新装", level: "warn")
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
        } else {
            spec.password = opt("password") ?? "agentvm"
        }
        spec.width = Int(opt("width") ?? "") ?? 1440
        spec.height = Int(opt("height") ?? "") ?? 900
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
        let spec = try macLoadSpec(name)
        if !spec.installed {
            MacEmit.error("\(name) 还没装完。没有系统的盘启动只会卡在黑屏，先跑 install。")
        }
        if macReadPID(name) != nil {
            MacEmit.error("\(name) 已经在跑（pid \(macReadPID(name)!)）。")
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
        // SIGTERM/SIGINT 走优雅停机。直接 kill -9 会让 APFS 留下未干净的
        // 日志，下次启动跑好几分钟 fsck；而 `vmz stop` 发的就是这个信号。
        let sig = DispatchSource.makeSignalSource(signal: SIGTERM)
        let intr = DispatchSource.makeSignalSource(signal: SIGINT)
        sig.setEventHandler {
            stopping = true
            MacEmit.log("收到停止信号，正在优雅关机", level: "info")
            rt.stop()
            exit(0)
        }
        intr.setEventHandler {
            stopping = true
            rt.stop()
            exit(0)
        }
        sig.activate()
        intr.activate()
        // 忽略默认的 SIGTERM 行为，否则信号一到进程当场就没了，
        // 上面的 handler 根本没机会跑。
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)

        MacEmit.stage("start", "启动 \(name)")
        try rt.start(spec: spec, provision: !spec.provisioned)
        if !spec.provisioned {
            var s = spec
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
    // 等它优雅停完。最多 100 秒（vmz 内部等 90 秒 + 余量）。
    var waited = 0
    while waited < 100 {
        if macReadPID(name) == nil {
            MacEmit.log("\(name) 已停", level: "info")
            MacEmit.w(["t": "stopped", "name": name])
            break
        }
        usleep(500_000)
        waited += 1
    }
    if macReadPID(name) != nil {
        MacEmit.error("等不停。guest 里选「关机」更可靠，"
            + "或者 kill \(entry.pid)（会丢未落盘数据）。")
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
            MacEmit.w(["t": "snapshots", "name": name, "snapshots": macSnapshotList(name)])
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
        MacEmit.log("\(name) 已删（\(dir.path)整个目录）", level: "info")
        MacEmit.w(["t": "deleted", "name": name])
    } catch let e as MacVMError {
        MacEmit.error(e.message)
    } catch {
        MacEmit.error(error.localizedDescription)
    }

default:
    MacEmit.error("不认识的命令：\(cmd)")
}
