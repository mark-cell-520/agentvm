import AppKit
import SwiftUI

/// 存储面板：附加盘、快照、共享目录、克隆。
///
/// 这四样都是「装完之后还想动这个虚拟机」才会用到的。原来一件都没有——想加一块盘
/// 存数据只能删了重装，想退回干净状态也只能删了重装，而删了重装系统里的东西全没。
struct StorageView: View {
    let vm: VMInfo
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) private var dismiss

    @State private var err: String?
    @State private var note: String?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    disks
                    Divider()
                    snapshots
                    Divider()
                    shares
                    Divider()
                    cloneSection
                    if let e = err {
                        Text(e).font(.caption).foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                    if let n = note {
                        Label(n, systemImage: "checkmark.circle.fill")
                            .font(.caption).foregroundStyle(.green)
                    }
                }
                .padding(20)
            }
            Divider()
            footer
        }
        .frame(width: 660, height: 700)
        .onAppear { state.snapshotList(name: vm.name) }
    }

    private var header: some View {
        HStack {
            Text("存储 · \(vm.name)").font(.title2).bold()
            Spacer()
            Text(vm.running ? "运行中" : "已关机")
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.quaternary, in: Capsule())
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var footer: some View {
        HStack {
            Text("加盘、拍快照、加共享目录，这些都要重启虚拟机才生效")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
            Button("完成") { dismiss() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    private func flash(_ s: String) {
        err = nil
        note = s
    }

    private func fail() {
        note = nil
        if let e = state.backend.errorMessage, !e.isEmpty {
            err = e
            state.backend.errorMessage = nil
        } else {
            err = "没做成，后端没说什么原因。看 qemu.log。"
        }
    }

    // ------------------------------------------------------------ 附加盘 --

    @State private var newDiskSize = "64G"
    @State private var newDiskFormat = "raw"

    private var disks: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("附加盘").font(.headline)
            Text("装完之后想加一块盘存数据，就加在这儿。系统盘是安装时建的那一个，"
                 + "不在这列表里。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if vm.disks.isEmpty {
                Text("还没有附加盘")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(vm.disks) { d in
                        HStack(spacing: 10) {
                            Image(systemName: "internaldrive")
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(d.id)  \(d.size)  \(d.format)")
                                    .font(.callout)
                                Text("\(d.bus) · 实际占用 \(d.usedText)")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("摘掉", role: .destructive) {
                                confirmRemoveDisk(d)
                            }
                            .buttonStyle(.borderless)
                            .disabled(vm.running)
                        }
                        .padding(8)
                        .background(.quaternary.opacity(0.5),
                                    in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }

            if vm.running {
                Text("虚拟机正在运行，摘盘要先关机。加盘可以现在加，重启后生效。")
                    .font(.caption).foregroundStyle(.orange)
            }

            HStack(spacing: 8) {
                TextField("大小", text: $newDiskSize)
                    .frame(width: 70)
                Picker("格式", selection: $newDiskFormat) {
                    Text("raw").tag("raw")
                    Text("qcow2").tag("qcow2")
                }
                .frame(width: 110)
                Button("加一块盘") {
                    state.diskAdd(name: vm.name, size: newDiskSize,
                                  format: newDiskFormat, bus: "virtio-blk") {
                        if state.backend.errorMessage != nil { fail() }
                        else { flash("已加盘，重启后生效") }
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }

            Text("新盘是空的，系统里还要分区格式化才能用：Linux 用 mkfs.ext4 再 mount，"
                 + "Windows 用磁盘管理初始化。qcow2 格式能拍快照，raw 更省事。")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func confirmRemoveDisk(_ d: VMDisk) {
        let a = NSAlert()
        a.messageText = "摘掉 \(d.id)？"
        a.informativeText = "盘里的数据会一起删掉，删了找不回来。\n\n"
            + "\(d.file)\n\n只想摘下来、数据还要的话，选「只摘不删」。"
        a.addButton(withTitle: "取消")
        a.addButton(withTitle: "只摘不删")
        a.addButton(withTitle: "删文件")
        a.buttons[2].hasDestructiveAction = true
        a.alertStyle = .warning
        // 第三个按钮是破坏性的，默认聚焦放在「取消」上
        a.buttons[0].keyEquivalent = "\r"
        let r = a.runModal()
        if r == .alertSecondButtonReturn {
            state.diskRemove(name: vm.name, disk: d.id, keepFile: true) {
                state.backend.errorMessage == nil
                    ? flash("已摘掉 \(d.id)，文件还在") : fail()
            }
        } else if r == .alertThirdButtonReturn {
            state.diskRemove(name: vm.name, disk: d.id, keepFile: false) {
                state.backend.errorMessage == nil
                    ? flash("已摘掉 \(d.id) 并删了文件") : fail()
            }
        }
    }

    // ------------------------------------------------------------ 快照 --

    @State private var newTag = ""

    private var snapshots: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("快照").font(.headline)
            Text("拍一张，之后想退回这个状态就回滚，不用重装。秒级，不额外占空间。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if state.snapshots.isEmpty {
                Text("还没有快照")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(state.snapshots) { sn in
                        HStack(spacing: 10) {
                            Image(systemName: "clock.arrow.circlepath")
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(sn.tag).font(.callout)
                                Text("\(sn.date)  状态 \(sn.size)")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("回滚") { confirmRollback(sn) }
                                .buttonStyle(.borderless)
                                .disabled(vm.running)
                            // 删快照也是不可逆的（回滚点没了），同一个文件里删附加盘
                            // 和回滚都做了确认，就这两处是漏的。
                            Button("删", role: .destructive) {
                                confirmDeleteSnapshot(sn)
                            }
                            .buttonStyle(.borderless)
                            .disabled(vm.running)
                        }
                        .padding(8)
                        .background(.quaternary.opacity(0.5),
                                    in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }

            if vm.running {
                Text("虚拟机正在运行。拍出来的快照回滚等于强制断电加丢写入，"
                     + "要先关机。")
                    .font(.caption).foregroundStyle(.orange)
            }

            HStack(spacing: 8) {
                TextField("快照名字，如 clean", text: $newTag)
                    .frame(width: 180)
                Button("拍一张") {
                    let t = newTag.trimmingCharacters(in: .whitespaces)
                    guard !t.isEmpty else { err = "给快照起个名字"; return }
                    state.snapshotCreate(name: vm.name, tag: t) {
                        state.backend.errorMessage == nil
                            ? flash("已拍快照 \(t)") : fail()
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(vm.running)
            }

            // 只有 qcow2 能拍。自己挑安装包那条路用的是 raw 盘，实话实说。
            Text(vm.kind == "linux"
                 ? "这个虚拟机是 qcow2 盘，可以拍快照。"
                 : "注意：只有 qcow2 盘能拍内部快照。自己挑安装包装的那条路用的是 "
                   + "raw 盘（安装器直接写块设备，qcow2 它不认），拍不了——上面拍了"
                   + "也会报错。附加盘里加了 qcow2 格式的才能拍。")
                .font(.caption2)
                .foregroundStyle(vm.kind == "linux"
                                 ? Color.secondary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func confirmDeleteSnapshot(_ sn: VMSnapshot) {
        let a = NSAlert()
        a.messageText = "删掉快照 \(sn.tag)？"
        a.informativeText = "这个还原点就没了，之后想回到这个状态只能重新弄。\n\n"
            + "虚拟机当前的状态不受影响。"
        a.addButton(withTitle: "取消")
        a.addButton(withTitle: "删掉")
        a.buttons[1].hasDestructiveAction = true
        a.alertStyle = .warning
        a.buttons[0].keyEquivalent = "\r"
        if a.runModal() == .alertSecondButtonReturn {
            state.snapshotDelete(name: vm.name, tag: sn.tag) {
                state.backend.errorMessage == nil
                    ? flash("已删快照 \(sn.tag)") : fail()
            }
        }
    }

    private func confirmRollback(_ sn: VMSnapshot) {
        let a = NSAlert()
        a.messageText = "回滚到 \(sn.tag)？"
        a.informativeText = "这个点之后的所有改动都会没，而且找不回来。\n\n"
            + "虚拟机会先关机。"
        a.addButton(withTitle: "取消")
        a.addButton(withTitle: "回滚")
        a.alertStyle = .warning
        a.buttons[0].keyEquivalent = "\r"
        if a.runModal() == .alertSecondButtonReturn {
            state.snapshotApply(name: vm.name, tag: sn.tag) {
                state.backend.errorMessage == nil
                    ? flash("已回滚到 \(sn.tag)") : fail()
            }
        }
    }

    // ------------------------------------------------------------ 共享目录 --

    @State private var newTag9p = "host"
    @State private var newSharePath = ""

    private var shares: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("共享目录").font(.headline)
            Text("把宿主机上的一个文件夹挂进虚拟机，传文件不用再走 SSH scp。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if vm.shares.isEmpty {
                Text("还没有共享目录")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(vm.shares) { sh in
                        HStack(spacing: 10) {
                            Image(systemName: "folder")
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(sh.tag).font(.callout)
                                Text(sh.path)
                                    .font(.caption2).foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            Spacer()
                            Button("复制挂载命令") {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(
                                    "mount -t 9p -o trans=virtio,version=9p2000.L "
                                    + "\(sh.tag) /mnt/<空目录>", forType: .string)
                                flash("挂载命令已复制")
                            }
                            .buttonStyle(.borderless)
                            // 同上：这个文件里删附加盘和回滚都有确认框，共享目录没有。
                            // 摘共享目录不删宿主机上的文件，所以提示写轻一点，但还是要
                            // 确认——用户点是「清理界面」的心态，不是「断开一个连接」。
                            Button("摘掉", role: .destructive) {
                                let a = NSAlert()
                                a.messageText = "摘掉共享目录 \(sh.tag)？"
                                a.informativeText = "虚拟机里就看不到 \(sh.path) 了，"
                                    + "宿主机上的文件不动。\n\n重启后生效。"
                                a.addButton(withTitle: "取消")
                                a.addButton(withTitle: "摘掉")
                                a.alertStyle = .informational
                                a.buttons[0].keyEquivalent = "\r"
                                if a.runModal() == .alertSecondButtonReturn {
                                    state.shareRemove(name: vm.name, tag: sh.tag) {
                                        state.backend.errorMessage == nil
                                            ? flash("已摘掉 \(sh.tag)，文件没动") : fail()
                                    }
                                }
                            }
                            .buttonStyle(.borderless)
                        }
                        .padding(8)
                        .background(.quaternary.opacity(0.5),
                                    in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }

            HStack(spacing: 8) {
                TextField("标签", text: $newTag9p)
                    .frame(width: 70)
                Text(newSharePath.isEmpty ? "还没选目录" : newSharePath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button("选择目录…") { pickShareDir() }
                    .controlSize(.small)
                Button("加") {
                    guard !newSharePath.isEmpty else { err = "先选一个目录"; return }
                    let t = newTag9p.trimmingCharacters(in: .whitespaces)
                    guard !t.isEmpty else { err = "给个标签"; return }
                    state.shareAdd(name: vm.name, tag: t, path: newSharePath) {
                        state.backend.errorMessage == nil
                            ? flash("已共享 \(newSharePath)，重启后生效") : fail()
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }

            Text("客人机里要自己挂一次，这一步替不了（每个系统做法不一样）：\n"
                 + "Linux  mkdir -p /mnt/host && mount -t 9p -o "
                 + "trans=virtio,version=9p2000.L <标签> /mnt/host")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func pickShareDir() {
        let panel = NSOpenPanel()
        panel.title = "选一个要共享进去的目录"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            newSharePath = url.path
        }
    }

    // ------------------------------------------------------------ 克隆 --

    @State private var cloneName = ""

    private var cloneSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("克隆").font(.headline)
            Text("照这个虚拟机复制一份。想试什么东西就克隆一份去折腾，折腾坏了删掉，"
                 + "原来这份还在——「随删随建的试验场」说的就是这个。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                TextField("新名字", text: $cloneName)
                    .frame(width: 160)
                Button("克隆一份") {
                    let n = cloneName.trimmingCharacters(in: .whitespaces)
                    guard !n.isEmpty else { err = "给新虚拟机起个名字"; return }
                    state.clone(from: vm.name, to: n) {
                        state.backend.errorMessage == nil
                            ? flash("已克隆成 \(n)，端口是重新分的")
                            : fail()
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }

            Text("Linux 那条路是 qcow2 overlay，克隆是秒级的，和源共用底册、不额外占"
                 + "空间。自己挑安装包那条是 raw 盘，用 APFS clonefile 复制（也是瞬时"
                 + "且不占空间），失败了才退回普通拷贝。端口会重新分，不会和源撞。")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
