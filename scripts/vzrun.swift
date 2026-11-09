import Virtualization
import Foundation

// vzrun — 用 Virtualization.framework 启动一个 ARM64 Linux 客户机。
//
// 用法: vzrun <系统盘.raw> <种子盘.iso> <vmlinuz> <initrd> <串口日志>
//
// 为什么不用 Tart：Tart 要求 cirruslabs 专用镜像格式，且只能从 OCI
// registry 拉取。这里是标准 raw 盘加直接加载内核，用于验证 agentvm
// 的客人机侧逻辑（cloud-init 注入、virtio 盘、串口控制台）。



func consoleSize() -> Int {
    let p = ProcessInfo.processInfo.environment["VZ_CONSOLE"] ?? ""
    guard !p.isEmpty else { return 0 }
    return (try? FileManager.default.attributesOfItem(atPath: p)[.size] as? Int) ?? 0
}

// stdout 在重定向或接管道时是全缓冲的，print 的内容会在进程被杀时
// 整段丢失，排障时看不见任何东西。诊断信息一律走无缓冲的 stderr。
func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}


let gLogger = VMLogger()

class VMLogger: NSObject, VZVirtualMachineDelegate {
    func guestDidStop(_ vm: VZVirtualMachine) {
        log("  !! 客人机已停止")
    }
    func vm(_ vm: VZVirtualMachine, didStopWithError error: Error) {
        log("  !! 客人机异常停止: \(error.localizedDescription)")
    }
    func networkDevice(_ networkDevice: VZNetworkDevice, attachmentWasDisconnectedBy err: Error) {
        log("  !! 网络断开: \(err.localizedDescription)")
    }
}

let a = CommandLine.arguments
guard a.count == 6 else {
    FileHandle.standardError.write("usage: vzrun <disk.raw> <seed.iso> <vmlinuz> <initrd> <console.log>\n".data(using: .utf8)!)
    exit(2)
}

let diskURL = URL(fileURLWithPath: a[1])
let seedURL = URL(fileURLWithPath: a[2])
let kernelURL = URL(fileURLWithPath: a[3])
let initrdURL = URL(fileURLWithPath: a[4])
let consoleURL = URL(fileURLWithPath: a[5])

do {
    let config = VZVirtualMachineConfiguration()
    config.cpuCount = 2
    config.memorySize = 2 * 1024 * 1024 * 1024

    // 直接加载内核与 initrd。镜像只有一个 ext4 root 分区，没有 EFI 系统
    // 分区，所以 UEFI 启动路径走不通，只能走这一条。
    // 注意 initialRamdiskURL 是创建后才能赋的属性，不在初始化器里。
    let bootLoader = VZLinuxBootLoader(kernelURL: kernelURL)
    bootLoader.initialRamdiskURL = initrdURL
    // console=hvc0 对应 virtio-console；root=/dev/vda1 是第一个 virtio 盘的
    // 第一个分区，第二个盘（种子）是 /dev/vdb。
    bootLoader.commandLine = "console=hvc0 root=/dev/vda1 rw rootwait"
    config.bootLoader = bootLoader

    config.platform = VZGenericPlatformConfiguration()

    // 系统盘可写，种子盘只读
    let sysAtt = try VZDiskImageStorageDeviceAttachment(url: diskURL, readOnly: false)
    let seedAtt = try VZDiskImageStorageDeviceAttachment(url: seedURL, readOnly: true)
    config.storageDevices = [
        VZVirtioBlockDeviceConfiguration(attachment: sysAtt),
        VZVirtioBlockDeviceConfiguration(attachment: seedAtt),
    ]

    // NAT 网络：客人机拿到 192.168.64.x，宿主是 192.168.64.1
    let netDev = VZVirtioNetworkDeviceConfiguration()
    netDev.attachment = VZNATNetworkDeviceAttachment()
    config.networkDevices = [netDev]

    // Linux 的 RNG 依赖 virtio-rng，缺了会在启动早期卡住
    config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]

    // 串口落到文件，用于观察启动过程与取客人机 IP。
    // ports 是只读的可变数组对象，用 add 添加而不是赋值。
    let serial = VZVirtioConsoleDeviceConfiguration()
    let port = VZVirtioConsolePortConfiguration()
    FileManager.default.createFile(atPath: consoleURL.path, contents: nil)
    let outHandle = try FileHandle(forWritingTo: consoleURL)
    // 读端必须给一个真实 fd，FileHandle.nullDevice 会被 VZ 判为无效
    // 并抛 NSInvalidArgumentException，所以显式打开 /dev/null。
    let nullFD = open("/dev/null", O_RDONLY)
    guard nullFD >= 0 else {
        FileHandle.standardError.write("打不开 /dev/null\n".data(using: .utf8)!)
        exit(1)
    }
    let nullHandle = FileHandle(fileDescriptor: nullFD, closeOnDealloc: true)
    port.attachment = VZFileHandleSerialPortAttachment(
        fileHandleForReading: nullHandle,
        fileHandleForWriting: outHandle
    )
    // ports 是 VZVirtioConsolePortConfigurationArray，用下标设置而非 add。
    // maximumPortCount 不设时取最高索引端口，设 index 0 即可。
    serial.ports[0] = port
    // Linux 客人机的 virtio 控制台放 consoleDevices；serialPorts 是给
    // Mac 虚拟机的 VZSerialPortConfiguration，类型不匹配会触发编译器
    // 内部错误而不是可读的诊断，所以这里必须分清。
    config.consoleDevices = [serial]

    try config.validate()
    let vm = VZVirtualMachine(configuration: config)
    vm.delegate = gLogger
    log("  ok 配置校验通过，VM 状态=\(vm.state.rawValue)")

    log("==> 启动客人机")
    // start() 在 Swift 里是 async（NS_SWIFT_ASYNC_NAME），用 Task 包裹
    // 用 completion handler 而不是 async 的 start()：实测 async 版本在
    // 这个环境会一直 pending 不返回，completion handler 形式则正常同步返回。
    vm.start { result in
        switch result {
        case .success:
            log("  ok start 回调成功，串口写入 \(consoleURL.path)")
        case .failure(let e):
            log("  !! start 回调错误: \(e.localizedDescription)")
        }
    }
    log("  .. start() 已同步返回")
    // 轮询状态与串口增长
    for i in 1...12 {
        Thread.sleep(forTimeInterval: 10)
        log("  .. \(i*10)s 状态=\(vm.state.rawValue) (1=running,4=pausing) 串口=\(consoleSize()) 字节")
        if consoleSize() > 1000 { log("  ok 串口开始有输出"); break }
    }
} catch {
    FileHandle.standardError.write("配置错误: \(error.localizedDescription)\n".data(using: .utf8)!)
    exit(1)
}

// 保持进程存活，客人机随本进程结束而终止
dispatchMain()
