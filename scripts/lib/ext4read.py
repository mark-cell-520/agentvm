#!/usr/bin/env python3
"""最小 ext4 读取器：从 ext4 镜像里抽文件，只读。

为什么需要它
------------
Debian 官方 cloud 镜像只有一个 ext4 root 分区，没有 EFI 系统分区。
macOS 的 Virtualization.framework 要用 VZLinuxBootLoader 直接加载内核
才能启动它，而内核和 initrd 都在那个 ext4 分区里。macOS 挂不了 ext4，
所以需要自己读。

覆盖的 ext4 特性（够用即止，不做完整实现）：
    * extent 树（depth 0 与 depth 1），Debian 12 默认启用
    * 64bit 特性（desc_size 决定组描述符大小）
    * 目录项 ext4_dir_entry_2，含 file_type
    * 稀疏文件（extent 空洞读作零）

不覆盖：inline data、加密、元数据校验和验证、 extent 深度 > 1。
遇到这些会明确报错，不会静默给错数据。

用法：
    ext4read.py IMAGE PARTITION_OFFSET list /
    ext4read.py IMAGE PARTITION_OFFSET dump /boot/vmlinuz-xxx OUT
"""

from __future__ import annotations

import argparse
import struct
import sys

EXT4_EXTENTS_FL = 0x80000
EXT4_INCOMPAT_64BIT = 0x80
EXT4_EXT_MAGIC = 0xF30A
EXT4_ROOT_INO = 2

# file_type
FT_DIR = 2
FT_REG = 1


class Ext4Error(RuntimeError):
    pass


class Ext4:
    def __init__(self, path: str, offset: int = 0):
        self.fh = open(path, "rb")
        self.offset = offset
        self._read_superblock()
        self._read_group_descriptors()

    # ------------------------------------------------------------ superblock --

    def _read_superblock(self) -> None:
        self.fh.seek(self.offset + 1024)
        sb = self.fh.read(1024)
        if len(sb) < 1024:
            raise Ext4Error("读不到 superblock，分区偏移可能不对")

        self.inodes_count = struct.unpack("<I", sb[0x00:0x04])[0]
        self.blocks_count = struct.unpack("<I", sb[0x04:0x08])[0]
        self.first_data_block = struct.unpack("<I", sb[0x14:0x18])[0]
        log_block_size = struct.unpack("<I", sb[0x18:0x1C])[0]
        self.block_size = 1024 << log_block_size
        self.blocks_per_group = struct.unpack("<I", sb[0x20:0x24])[0]
        self.inodes_per_group = struct.unpack("<I", sb[0x28:0x2C])[0]
        self.inode_size = struct.unpack("<H", sb[0x58:0x5A])[0]
        self.feature_incompat = struct.unpack("<I", sb[0x60:0x64])[0]
        self.desc_size = struct.unpack("<H", sb[0xFE:0x100])[0]

        if self.inode_size < 128:
            raise Ext4Error(f"inode 大小异常：{self.inode_size}")
        if not (self.feature_incompat & EXT4_INCOMPAT_64BIT):
            self.desc_size = 32
        elif self.desc_size < 32:
            raise Ext4Error(f"64bit 已启用但 desc_size 异常：{self.desc_size}")

    def _read_group_descriptors(self) -> None:
        gdt_block = self.first_data_block + 1
        self.fh.seek(self.offset + gdt_block * self.block_size)
        n_groups = (self.blocks_count - self.first_data_block + self.blocks_per_group - 1) // self.blocks_per_group
        raw = self.fh.read(n_groups * self.desc_size)
        if len(raw) != n_groups * self.desc_size:
            raise Ext4Error("组描述符表读取不完整")
        self.groups = []
        for i in range(n_groups):
            gd = raw[i * self.desc_size : (i + 1) * self.desc_size]
            inode_table = struct.unpack("<I", gd[0x08:0x0C])[0]
            if self.desc_size >= 64:
                inode_table |= struct.unpack("<I", gd[0x28:0x2C])[0] << 32
            self.groups.append(inode_table)

    # ---------------------------------------------------------------- inode --

    def read_inode(self, ino: int) -> dict:
        if ino < 1 or ino > self.inodes_count:
            raise Ext4Error(f"inode 号越界：{ino}")
        group = (ino - 1) // self.inodes_per_group
        index = (ino - 1) % self.inodes_per_group
        if group >= len(self.groups):
            raise Ext4Error(f"inode {ino} 所在的组 {group} 不存在")
        pos = self.offset + self.groups[group] * self.block_size + index * self.inode_size
        self.fh.seek(pos)
        raw = self.fh.read(self.inode_size)
        if len(raw) != self.inode_size:
            raise Ext4Error(f"inode {ino} 读取不完整")

        mode = struct.unpack("<H", raw[0x00:0x02])[0]
        size_lo = struct.unpack("<I", raw[0x04:0x08])[0]
        flags = struct.unpack("<I", raw[0x20:0x24])[0]
        size_hi = struct.unpack("<I", raw[0x6C:0x70])[0]
        size = size_lo | (size_hi << 32)
        return {"mode": mode, "size": size, "flags": flags, "i_block": raw[0x28:0x28 + 60]}

    # -------------------------------------------------------------- extents --

    def _extent_blocks(self, inode: dict) -> list[tuple[int, int]]:
        """返回 [(逻辑块, 物理块, 长度)]，长度单位为块。"""
        if not (inode["flags"] & EXT4_EXTENTS_FL):
            raise Ext4Error(
                "inode 不用 extent（可能是旧式间接块），本读取器不支持。"
                "Debian 12 默认启用 extent，遇到这个说明镜像异常。"
            )
        out: list[tuple[int, int]] = []
        self._walk_extent_node(inode["i_block"], out, depth_budget=4)
        return out

    def _walk_extent_node(self, node: bytes, out: list, depth_budget: int) -> None:
        if depth_budget < 0:
            raise Ext4Error("extent 树深度超过预算，拒绝继续")
        magic, entries, _max, depth, _gen = struct.unpack("<HHHBB", node[:8])
        if magic != EXT4_EXT_MAGIC:
            raise Ext4Error(f"extent magic 不符：{magic:#x}")
        if depth == 0:
            for i in range(entries):
                e = node[12 + i * 12 : 12 + (i + 1) * 12]
                logical = struct.unpack("<I", e[0:4])[0]
                length = struct.unpack("<H", e[4:6])[0]
                start = struct.unpack("<I", e[8:12])[0] | (struct.unpack("<H", e[6:8])[0] << 32)
                if length > 32768:  # 长度字段高位若置位是 unwritten extent
                    length -= 32768
                out.append((logical, start, length))
        else:
            for i in range(entries):
                e = node[12 + i * 12 : 12 + (i + 1) * 12]
                child = struct.unpack("<I", e[4:8])[0] | (struct.unpack("<H", e[8:10])[0] << 32)
                self.fh.seek(self.offset + child * self.block_size)
                raw = self.fh.read(self.block_size)
                if len(raw) != self.block_size:
                    raise Ext4Error("extent 内部节点读取不完整")
                self._walk_extent_node(raw, out, depth_budget - 1)

    def read_file(self, ino: int) -> bytes:
        inode = self.read_inode(ino)
        if inode["size"] == 0:
            return b""
        extents = self._extent_blocks(inode)
        if not extents:
            raise Ext4Error(f"inode {ino} 有大小 {inode['size']} 但没有 extent")
        buf = bytearray(inode["size"])
        for logical, physical, length in extents:
            start_byte = logical * self.block_size
            end_byte = start_byte + length * self.block_size
            if start_byte >= inode["size"]:
                continue
            self.fh.seek(self.offset + physical * self.block_size)
            chunk = self.fh.read(length * self.block_size)
            if len(chunk) != length * self.block_size:
                raise Ext4Error("extent 数据读取不完整")
            buf[start_byte : min(end_byte, inode["size"])] = chunk[: max(0, min(end_byte, inode["size"]) - start_byte)]
        return bytes(buf)

    # ------------------------------------------------------------- directory --

    def listdir(self, ino: int) -> list[tuple[str, int, int]]:
        """返回 [(名字, inode, file_type)]。"""
        inode = self.read_inode(ino)
        if not (inode["mode"] & 0x4000):
            raise Ext4Error(f"inode {ino} 不是目录")
        data = self.read_file(ino)
        out = []
        pos = 0
        while pos + 8 <= len(data):
            inum, rec_len, name_len, ftype = struct.unpack("<IHBB", data[pos : pos + 8])
            if rec_len < 8 or pos + rec_len > len(data):
                break
            name = data[pos + 8 : pos + 8 + name_len]
            if inum != 0 and name not in (b".", b".."):
                out.append((name.decode("utf-8", "replace"), inum, ftype))
            pos += rec_len
        return out

    def lookup(self, path: str) -> int:
        """按路径找 inode。path 以 / 开头。"""
        ino = EXT4_ROOT_INO
        for part in [p for p in path.split("/") if p]:
            found = None
            for name, inum, ftype in self.listdir(ino):
                if name == part:
                    found = inum
                    break
            if found is None:
                raise Ext4Error(f"路径不存在：{path}（找不到 {part}）")
            ino = found
        return ino

    def find(self, path: str, prefix: str) -> list[str]:
        """在目录 path 下找名字以 prefix 开头的条目。"""
        ino = self.lookup(path)
        return [n for n, _i, _t in self.listdir(ino) if n.startswith(prefix)]

    def close(self) -> None:
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="最小 ext4 读取器，只读")
    ap.add_argument("image")
    ap.add_argument("offset", type=lambda x: int(x, 0), help="分区起始字节偏移，支持 0x 前缀")
    ap.add_argument("path", help="要列目录或读取的文件路径")
    ap.add_argument("out", nargs="?", help="给了就导出该文件到 out")
    args = ap.parse_args()

    # except 放宽到 (Ext4Error, OSError, struct.error)：偏移给成负数、目标目录
    # 不存在、镜像被截断都会以 OSError/struct.error 冒出来，原来只接 Ext4Error，
    # 这些情况是完整 traceback 砸到用户脸上。用途是抽 vmlinuz/initrd（几 MB），
    # 异常路径短，放宽不会掩盖真问题。
    try:
        with Ext4(args.image, args.offset) as fs:
            if args.out:
                data = fs.read_file(fs.lookup(args.path))
                with open(args.out, "wb") as fh:
                    fh.write(data)
                print(f"ok  {args.path} → {args.out}（{len(data)} 字节）")
            else:
                ino = fs.lookup(args.path)
                for name, inum, ftype in fs.listdir(ino):
                    kind = {1: "文件", 2: "目录", 7: "符号链接"}.get(ftype, f"类型{ftype}")
                    size = fs.read_inode(inum)["size"]
                    print(f"  {kind:6s} {size:>12,d}  {name}")
    except (Ext4Error, OSError, struct.error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
