import Foundation

/// 一个可安装的系统。字段与 scripts/catalog.json 一一对应。
struct Distro: Identifiable, Codable, Hashable {
    let id: String
    // "windows" | "linux" | "macos" | "local"
    // macos 是 Apple Silicon 上用 Virtualization.framework 跑的 macOS 客人机，
    // 安装源是 Apple 的恢复镜像（IPSW），跟另外三种完全不同，见 MacVMEngine。
    let kind: String
    let name: String
    let subtitle: String
    let url: String
    let sha256: String?
    let sha512: String?
    let size: Int
    let format: String?
    let defaultCpus: Int
    let defaultRam: String
    let defaultDisk: String
    let defaultUser: String
    let installMinutes: [Int]
    let note: String
    /// 本地安装包的路径。catalog 里的条目这项是 nil。有值就走
    /// `install --source`，没有就走 `install --distro`。
    let sourcePath: String?
    /// 用户自己贴的安装包网址。要走 `install --source-url`，先下再装。
    let sourceURL: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, name, subtitle, url, sha256, sha512, size, format, note
        // catalog.json 是 snake_case，必须显式映射。写成 defaultCpus 会去找
        // 一个不存在的键，解码整个条目失败——而 compactMap 会静默丢掉失败的
        // 条目，界面只表现为「正在读镜像目录…」转不停。
        case defaultCpus = "default_cpus"
        case defaultRam = "default_ram"
        case defaultDisk = "default_disk"
        case defaultUser = "default_user"
        case installMinutes = "install_minutes"
        // 老 catalog.json 里没有这两个键，没有默认值的话旧文件会解码失败
        case sourcePath = "source_path"
        case sourceURL = "source_url"
    }

    var isWindows: Bool { kind == "windows" }
    /// 用户自己挑的本地安装包。装法完全另开一条路，见 vmkit 的 cmd_install_local。
    var isLocal: Bool { sourcePath != nil }
    /// 用户自己贴的网址，要先下载。同样走本地安装包那条路。
    var isURL: Bool { sourceURL != nil }

    var sizeText: String {
        // 从网址装的一开始不知道多大（还没下），别显示一个假的 0 字节
        if isURL { return "下载后才知道" }
        return ByteCountFormatter.string(fromByteCount: Int64(size),
                                         countStyle: .file)
    }

    /// 安装要多久。Linux 是云镜像，下载完就能登；Windows 要真跑 Setup；
    /// 本地包是用户自己点的，我们不知道它要多久，不猜。
    var timeText: String {
        if isLocal { return "取决于安装包" }
        if isURL { return "取决于网速和安装包" }
        if installMinutes.count == 2 {
            return "约 \(installMinutes[0])–\(installMinutes[1]) 分钟"
        }
        return "时间未知"
    }

    /// 有强校验和的才显示「已校验」，没有的明说没校验，不装作校验过。
    var checksumText: String {
        if isLocal { return "本地文件，未校验" }
        // 用户贴的网址，我们没有可信的哈希。vmkit 只校验大小，这里要说清楚。
        if isURL { return "网址下载，未校验" }
        if sha512 != nil { return "SHA-512 已校验" }
        if sha256 != nil { return "SHA-256 已校验" }
        return "无校验和"
    }

    static let placeholder = Distro(
        id: "", kind: "linux", name: "载入中", subtitle: "", url: "",
        sha256: nil, sha512: nil, size: 0, format: nil,
        defaultCpus: 2, defaultRam: "2G", defaultDisk: "32G",
        defaultUser: "agent", installMinutes: [0, 0], note: "",
        sourcePath: nil, sourceURL: nil)

    /// 用户选了一个本地文件时造一个对应的条目。
    ///
    /// 复用 Distro 而不是另开一个模型：配置表单、向导步骤、`pick(_:)` 全都围着
    /// 它转，另开一个模型要把这些地方都改成两种。本地包没有推荐配置，这里给的
    /// 2 核 / 2G / 32G 是实测能跑起来的基线（fnOS 安装器在这套配置下正常）。
    static func local(path: String) -> Distro {
        let url = URL(fileURLWithPath: path)
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        let stem = url.deletingPathExtension().lastPathComponent
        return Distro(
            id: "local:\(path)", kind: "local",
            // 文件名当名字显示。有些包名很长，界面里会截断，但详情页能看全
            name: stem,
            subtitle: "本地安装包 \(url.pathExtension.uppercased())",
            url: "", sha256: nil, sha512: nil, size: size, format: nil,
            defaultCpus: 2, defaultRam: "2G", defaultDisk: "32G",
            defaultUser: "agent", installMinutes: [5, 30],
            note: "从本地文件安装", sourcePath: path, sourceURL: nil)
    }

    /// 用户贴了一个安装包网址。下载由后端做（带断点续传和进度），所以这里
    /// 没有体积可显示；校验和也没有——我们没有可信的哈希，后端只校验大小。
    static func fromURL(_ u: String) -> Distro {
        Distro(
            id: "url:\(u)", kind: "local",
            name: u.lastPathComponentOrHost,
            subtitle: "从网址下载后安装",
            url: "", sha256: nil, sha512: nil, size: 0, format: nil,
            defaultCpus: 2, defaultRam: "2G", defaultDisk: "32G",
            defaultUser: "agent", installMinutes: [5, 30],
            note: "从网址下载", sourcePath: nil, sourceURL: u)
    }
}

private extension String {
    /// 网址里取个像名字的东西：取路径最后一段，没有路径就取主机名。
    var lastPathComponentOrHost: String {
        let noQuery = split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)[0]
        let last = noQuery.split(separator: "/", omittingEmptySubsequences: false).last ?? ""
        if let s = last as Substring?, !s.isEmpty { return String(s) }
        if let r = range(of: "://") {
            return String(self[r.upperBound...].split(separator: "/")[0])
        }
        return String(self)
    }
}

/// 一个已安装（或正在安装）的虚拟机。
///
/// 注意 CodingKeys：vmkit 输出的是 snake_case（ssh_port、disk_used_bytes），
/// Swift 属性是 camelCase。不写 CodingKeys 的话合成的解码会去找 sshPort 这个
/// 键，一个都对不上，解码全失败——而失败是静默的，界面只表现为「没有虚拟机」，
/// 单独在终端里跑同样的命令却是好的，很不好查。
struct VMInfo: Identifiable, Codable, Hashable {
    let name: String
    let distro: String
    let kind: String
    let running: Bool
    let pid: Int?
    let ready: Bool
    let cpus: Int
    let ram: String
    let disk: String
    let user: String
    let sshPort: Int
    let rdpPort: Int?
    let vncPort: Int
    /// 是不是无窗口跑的。无窗口时没有原生 macOS 窗口，「用系统窗口打开」要重启
    /// 一次客人机才能生效——按钮上得先把这件事说清楚。
    let headless: Bool
    let sshCommand: String
    let diskUsedBytes: Int
    let sshOk: Bool?
    /// 本地 ISO 装的 NAS 类系统，宿主机上转 Web UI 的端口。只有 kind=local 有。
    let webPort: Int?
    /// 安装包的路径。只有 kind=local 有。
    let source: String?
    /// 启动时挂不挂安装光盘。挂了会进安装器而不是进装好的系统，只有本地包有这项。
    let attachISO: Bool
    /// 之后另外加的附加盘。系统盘是安装时建的那一个，不在这里面。
    let disks: [VMDisk]
    /// 共享目录（virtio-9p）。
    let shares: [VMShare]
    /// macOS 客人机的 build 号，如 26B131。只有 kind=macos 有。
    let macBuild: String?
    /// macOS 客人机的显示分辨率，如 "1440x900"。
    let macResolution: String?
    /// macOS 客人机装完了没有。
    let macInstalled: Bool?

    enum CodingKeys: String, CodingKey {
        case name, distro, kind, running, pid, ready, cpus, ram, disk, user
        case sshPort = "ssh_port"
        case rdpPort = "rdp_port"
        case vncPort = "vnc_port"
        case headless = "headless"
        case sshCommand = "ssh_command"
        case diskUsedBytes = "disk_used_bytes"
        case sshOk = "ssh_ok"
        case webPort = "web_port"
        case source = "source"
        case attachISO = "attach_iso"
        case disks = "disks"
        case shares = "shares"
        case macBuild = "mac_build"
        case macResolution = "mac_resolution"
        case macInstalled = "mac_installed"
    }

    var id: String { name }
    var isWindows: Bool { kind == "windows" }
    var isLocal: Bool { kind == "local" }
    /// macOS 客人机。它的画面不走 VNC，走 app 里的 VZVirtualMachineView，
    /// 所以「控制台」按钮对它做的事情完全不同，见 AppState.openConsole。
    var isMacOS: Bool { kind == "macos" }

    /// 本地包装的、并且安装包还在的虚拟机。只有这种能挂/不挂安装光盘——
    /// catalog 里那些系统没有本地 ISO 可挂。
    var isLocalWithISO: Bool {
        guard isLocal, let src = source, !src.isEmpty else { return false }
        return FileManager.default.fileExists(atPath: src)
    }

    var stateText: String {
        if isMacOS && !(macInstalled ?? false) { return "未装完" }
        if running && (sshOk ?? false) { return "运行中" }
        if running { return "已启动" }
        return "已关机"
    }

    var diskUsedText: String {
        ByteCountFormatter.string(fromByteCount: Int64(diskUsedBytes),
                                  countStyle: .file)
    }
}

/// 环境检查的一项。
struct CheckItem: Identifiable, Decodable {
    let name: String
    let ok: Bool
    let detail: String
    var id: String { name }
}


/// 一块之后另外加的附加盘。系统盘是安装时建的那个，不走这个模型。
struct VMDisk: Identifiable, Codable, Hashable {
    let id: String
    let file: String
    let format: String
    let bus: String
    let size: String

    /// 实际占用。稀疏盘的逻辑大小和真占的空间差得远，显示逻辑大小会误导
    /// （64G 的盘 st_size 永远是 68.7e9）。st_blocks * 512 才是真正分配出去的块，
    /// 和后端 vmkit 的 disk_used_bytes 用同一个算法。
    var usedText: String {
        var st = stat()
        guard stat(file, &st) == 0 else { return "文件不在" }
        let bytes = UInt64(st.st_blocks) * 512
        return ByteCountFormatter.string(fromByteCount: Int64(bytes),
                                         countStyle: .file)
    }
}

/// 一个共享目录。tag 是客人机里挂载时用的名字。
struct VMShare: Identifiable, Codable, Hashable {
    let tag: String
    let path: String
    var id: String { tag }
}

/// 一个 qcow2 内部快照。
struct VMSnapshot: Identifiable, Hashable {
    let id: String
    let tag: String
    let size: String
    let date: String
}
