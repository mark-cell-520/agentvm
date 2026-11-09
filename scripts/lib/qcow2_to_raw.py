#!/usr/bin/env python3
"""qcow2 → raw 转换器，纯 Python 零依赖。

为什么需要它
------------
Hyper-V 不认 qcow2，只认 VHDX / VHD。标准做法是 `qemu-img convert -O vhdx`，
但那条路要求目标机装有 QEMU。在 Windows on ARM 上 QEMU 没有官方原生构建，
为了一次性格式转换让用户装一个 250 MB 的 x86_64 模拟层工具并不理想。

本脚本提供不依赖 qemu-img 的备选路径：直接读 qcow2 结构，写出等长 raw。
跨平台，任何有 Python 3 的地方都能跑。

支持的 qcow2 特性
----------------
    * version 2 / 3
    * 任意 cluster_bits
    * 未分配簇（读作零）
    * 全零簇（L2 bit 0）
    * zlib 压缩簇（L2 bit 62，version 3 默认算法）
    * backing file 不处理：Debian 官方 cloud 镜像不带 backing file

用法
----
    qcow2_to_raw.py IN.qcow2 OUT.raw [--no-sparse]

--sparse 用 ftruncate 建立空洞，输出文件只占实际数据量。3.22 GB 的
Debian cloud 镜像实际数据约 1.1 GB，稀疏输出省 2 GB 磁盘。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

QCOW2_MAGIC = 0x514649FB  # "QFI\xfb"
L2_FLAG_ALL_ZEROS = 1 << 0
L2_FLAG_COMPRESSED = 1 << 62
CLUSTER_OFFSET_MASK = (1 << 62) - 1
COMPRESSED_SECTOR_SIZE = 512


class Qcow2Error(RuntimeError):
    pass


class Qcow2Reader:
    """只读 qcow2 镜像，按簇产出数据。"""

    def __init__(self, path: str):
        self.fh = open(path, "rb")
        self._read_header()

    def _read_header(self) -> None:
        h = self.fh.read(112)
        if len(h) < 72:
            raise Qcow2Error("文件太小，不是 qcow2 镜像")
        magic = struct.unpack(">I", h[0:4])[0]
        if magic != QCOW2_MAGIC:
            raise Qcow2Error(f"magic 不匹配：{magic:#x}，应为 {QCOW2_MAGIC:#x}")

        self.version = struct.unpack(">I", h[4:8])[0]
        if self.version not in (2, 3):
            raise Qcow2Error(f"不支持的 qcow2 版本：{self.version}")

        self.cluster_bits = struct.unpack(">I", h[20:24])[0]
        self.size = struct.unpack(">Q", h[24:32])[0]
        self.l1_size = struct.unpack(">I", h[36:40])[0]
        self.l1_offset = struct.unpack(">Q", h[40:48])[0]
        self.backing_offset = struct.unpack(">Q", h[8:16])[0]

        if self.cluster_bits < 9 or self.cluster_bits > 21:
            raise Qcow2Error(f"cluster_bits 超出合理范围：{self.cluster_bits}")

        self.cluster_size = 1 << self.cluster_bits
        self.l2_entries = self.cluster_size // 8

        if self.version >= 3:
            incompat = struct.unpack(">Q", h[72:80])[0]
            if incompat & (1 << 0):
                raise Qcow2Error("镜像带 dirty 位，可能未干净卸载，拒绝转换")
            if incompat & (1 << 1):
                raise Qcow2Error("镜像标记为 corrupt，拒绝转换")
            if incompat & (1 << 2):
                raise Qcow2Error("镜像使用外部数据文件，本转换器不支持")
            # bit 3 是压缩类型：0 = zlib，1 = zstd。zstd 需要额外模块。
            if incompat & (1 << 3):
                raise Qcow2Error(
                    "镜像声明使用 zstd 压缩，本转换器只支持 zlib。"
                    "请改用 qemu-img。"
                )

        if self.backing_offset:
            raise Qcow2Error(
                "镜像有 backing file，本转换器不处理 backing 链。"
                "请先用 qemu-img rebase 或改用 qemu-img。"
            )

        self._l1 = self._read_l1()

    def _read_l1(self) -> list[int]:
        self.fh.seek(self.l1_offset)
        raw = self.fh.read(8 * self.l1_size)
        if len(raw) != 8 * self.l1_size:
            raise Qcow2Error("L1 表读取不完整")
        return list(struct.unpack(f">{self.l1_size}Q", raw))

    def _read_l2(self, offset: int) -> list[int]:
        self.fh.seek(offset)
        raw = self.fh.read(8 * self.l2_entries)
        if len(raw) != 8 * self.l2_entries:
            raise Qcow2Error("L2 表读取不完整")
        return list(struct.unpack(f">{self.l2_entries}Q", raw))

    def _inflate(self, blob: bytes, coffset: int) -> bytes:
        """解压一个压缩簇的数据块。

        qcow2 的压缩流有两种形态，都真实存在：
            * 标准 zlib：带 2 字节头（0x78 0x9c 之类）
            * raw deflate：无头
        实测 Debian 官方 cloud 镜像是 raw deflate，用标准 zlib 解会报
        "incorrect header check"。两种都试，先标准后 raw，
        换一个镜像源不至于让转换器失效。
        """
        last_err: Exception | None = None
        for wbits in (15, -15):
            try:
                out = zlib.decompressobj(wbits).decompress(blob)
            except zlib.error as exc:
                last_err = exc
                continue
            if out:
                return out
        raise Qcow2Error(
            f"解压失败（偏移 {coffset}）：{last_err}。"
            "已尝试标准 zlib 与 raw deflate 两种形态。"
        )

    def _decompress(self, entry: int) -> bytes:
        """解压一个压缩簇。

        qcow2 v3 压缩簇描述符的位布局（QEMU include/block/qcow2.h）：
            bit 62      压缩标志
            bit 54-61   压缩数据占用的 512 字节扇区数减 1
            bit 0-53    压缩数据在镜像文件中的字节偏移
        csize 要减掉 coffset 未对齐到 512 的零头，因为扇区计数只数满扇区。
        """
        csize_shift = 62 - (self.cluster_bits - 8)
        csize_mask = (1 << (self.cluster_bits - 8)) - 1

        # 位布局（实测 Debian cloud 镜像反推确认，非照抄文档）：
        #   bit 62        压缩标志
        #   bit csize_shift .. 61   压缩数据占用的 512 字节扇区数减 1
        #   bit 0 .. csize_shift-1  压缩数据在镜像文件中的字节偏移
        # 注意 offset 掩码是 (1<<csize_shift)-1 而不是 (1<<62)-1：
        # 高位被 nb_csectors 占着，用 62 位掩码会把扇区数并进偏移，
        # 得到一个远超文件长度的假偏移。
        coffset = entry & ((1 << csize_shift) - 1)
        nb_csectors = ((entry >> csize_shift) & csize_mask) + 1
        csize = nb_csectors * COMPRESSED_SECTOR_SIZE - (coffset & (COMPRESSED_SECTOR_SIZE - 1))

        self.fh.seek(coffset)
        blob = self.fh.read(csize)
        if len(blob) != csize:
            raise Qcow2Error(f"压缩簇数据读取不完整：期望 {csize} 字节，实得 {len(blob)}")

        out = self._inflate(blob, coffset)

        if len(out) > self.cluster_size:
            raise Qcow2Error(
                f"解压后 {len(out)} 字节超过簇大小 {self.cluster_size}，镜像可能损坏"
            )
        # 解压结果不足一簇时补零：qcow2 允许压缩数据的逻辑长度短于簇
        return out.ljust(self.cluster_size, b"\x00")

    def iter_clusters(self):
        """按顺序产出 (cluster_index, data_bytes)。未分配与全零簇都产出零块。

        **产出个数必须是 ceil(size / cluster_size)，不能是 l1_size × l2_entries。**
        后者按 L1 表的行数走，而 l1_size = ceil(size / (l2_entries × cluster_size))
        向上取整——只要虚拟大小不是「一个 L2 表覆盖范围」的整数倍，多出来的都是
        不存在的簇。默认 64 KiB 簇下一个 L2 表覆盖 512 MiB：
            virtual=3,453,002,496、cluster_bits=16 → 需要 52,689 簇，
            实际产出 57,344 簇 → 转换到 99% 才报「写入超出虚拟盘大小」。
        2 GiB 的镜像恰好是 4×512 MiB 所以能过，Debian/Ubuntu 云镜像换个不是
        整数倍的版本就中招，而这是 Hyper-V 路径上唯一不依赖 qemu-img 的转换器。
        """
        zero = bytes(self.cluster_size)
        # 真正需要的簇数。最后不足一簇的按一簇算。
        need = (self.size + self.cluster_size - 1) // self.cluster_size
        produced = 0
        for l1_entry in self._l1:
            l2_offset = l1_entry & 0x00FFFFFFFFFFFE00
            if l2_offset == 0:
                for i in range(self.l2_entries):
                    if produced >= need:
                        return
                    produced += 1
                    yield zero
                continue

            l2 = self._read_l2(l2_offset)
            for entry in l2:
                if produced >= need:
                    return
                produced += 1
                if entry & L2_FLAG_COMPRESSED:
                    yield self._decompress(entry)
                elif entry & L2_FLAG_ALL_ZEROS:
                    yield zero
                elif (entry & 0x00FFFFFFFFFFFE00) == 0:
                    yield zero
                else:
                    offset = entry & 0x00FFFFFFFFFFFE00
                    self.fh.seek(offset)
                    data = self.fh.read(self.cluster_size)
                    if len(data) != self.cluster_size:
                        raise Qcow2Error(
                            f"数据簇读取不完整：偏移 {offset}，"
                            f"期望 {self.cluster_size}，实得 {len(data)}"
                        )
                    yield data

    def close(self) -> None:
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def convert(src: str, dst: str, sparse: bool = True) -> tuple[int, int]:
    """转换，返回 (输出字节数, 非零簇数)。"""
    with Qcow2Reader(src) as q:
        total = q.size
        written = 0
        nonzero = 0
        with open(dst, "wb") as out:
            if sparse:
                out.truncate(total)
            for data in q.iter_clusters():
                if written + len(data) > total:
                    # 正常不会到这里（iter_clusters 已按 need 截断）。留着当
                    # 断言：镜像头里的 size 和 L1 表对不上时，宁可明说也不
                    # 悄悄产出一个长度不对的 raw 文件。
                    raise Qcow2Error(f"写入超出虚拟盘大小：{written} > {total}")
                if not sparse or any(data):
                    out.seek(written)
                    out.write(data)
                    if any(data):
                        nonzero += 1
                written += len(data)
            # 最后一段可能不足一簇，把文件裁到精确的虚拟大小。
            # 原来这里写了两遍同样的 truncate（if/else 两个分支同一句），已合并。
            out.truncate(total)
        return written, nonzero


def main() -> int:
    ap = argparse.ArgumentParser(description="qcow2 转 raw，纯 Python 零依赖")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--no-sparse", action="store_true", help="不使用稀疏输出")
    args = ap.parse_args()

    if not os.path.isfile(args.src):
        print(f"错误：源文件不存在：{args.src}", file=sys.stderr)
        return 1
    if os.path.abspath(args.src) == os.path.abspath(args.dst):
        print("错误：源与目标相同", file=sys.stderr)
        return 1

    try:
        written, nonzero = convert(args.src, args.dst, sparse=not args.no_sparse)
    except Qcow2Error as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    actual = os.path.getsize(args.dst)
    print(f"ok  虚拟盘大小 {written / 1e9:.2f} GB")
    print(f"ok  输出文件 {actual / 1e6:.0f} MB（{nonzero} 个非零簇，稀疏输出）")
    if actual != written:
        print(f"warn 输出文件大小 {actual} 与虚拟盘 {written} 不一致", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
