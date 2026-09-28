"""Sparse VMDK（monolithicSparse）写出。

格式（逆向自 qemu-img 产出的同类型文件，实测字段）：
  sector 0            : SparseExtentHeader (512B)
  sector 1..20        : descriptor 文本（20 扇区）
  sector 21..         : 冗余 grain directory (RGD)
  之后                : 主 grain directory (GD)
  之后                : grain table 区（每张 GT = 512 项 x 4B = 4 扇区）
  overHead            : 数据 grain 起点（每 grain = 128 扇区 = 64 KiB）

写入策略：源数据顺序读，非零 grain 顺序追加到文件尾，GT/GD 在内存里累积，
结束时回写 header + RGD + GD + GT。全零 grain 不分配 -> thin。
"""

from __future__ import annotations

import os
import struct

VMDK_MAGIC = 0x564D444B
VMDK_VERSION = 1
VMDK_FLAGS = 3                      # 0x1 newline detection | 0x2 redundant GT
GRAIN_SECTORS = 128                 # 64 KiB
NUM_GTES_PER_GT = 512
GT_SECTORS = 4                      # 512 entries * 4B / 512B
DESCRIPTOR_SECTORS = 20
SECTOR = 512


class VmdkError(RuntimeError):
    pass


def _align_up(value: int, unit: int) -> int:
    return ((value + unit - 1) // unit) * unit


class SparseVmdkWriter:
    """流式写 monolithicSparse vmdk。

    用法::

        with SparseVmdkWriter(path, capacity_bytes, adapter="lsilogic") as w:
            w.write_at(offset, data)    # 任意偏移，内部按 grain 对齐
            w.finalize()
    """

    def __init__(self, path: str, capacity_bytes: int, adapter: str = "lsilogic",
                 geometry_heads: int = 255, geometry_sectors: int = 63) -> None:
        if capacity_bytes % SECTOR:
            raise VmdkError("capacity must be a multiple of %d" % SECTOR)
        self.path = path
        self.capacity_sectors = capacity_bytes // SECTOR
        self.adapter = adapter
        self.geometry_heads = geometry_heads
        self.geometry_sectors = geometry_sectors

        self.num_grains = (self.capacity_sectors + GRAIN_SECTORS - 1) // GRAIN_SECTORS
        self.num_gts = (self.num_grains + NUM_GTES_PER_GT - 1) // NUM_GTES_PER_GT
        self.gd_sectors = max(1, (self.num_gts * 4 + SECTOR - 1) // SECTOR)

        self.rgd_offset = 1 + DESCRIPTOR_SECTORS                  # 21
        self.gd_offset = self.rgd_offset + self.gd_sectors
        self.gt_offset = self.gd_offset + self.gd_sectors
        self.gt_area_sectors = self.num_gts * GT_SECTORS
        self.data_offset = _align_up(self.gt_offset + self.gt_area_sectors, 128)
        self.overhead = self.data_offset

        # 内存里的 GD / GT（24 位足够：最多 2^24 个 grain 槽）
        self.gd = [0] * self.num_gts
        self.gts = [[0] * NUM_GTES_PER_GT for _ in range(self.num_gts)]
        self.next_free_sector = self.data_offset
        self.allocated_grains = 0
        self._f = None
        self._finalized = False

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "SparseVmdkWriter":
        if os.path.exists(self.path):
            raise VmdkError("refusing to overwrite existing file: %s" % self.path)
        self._f = open(self.path, "w+b")
        self._reserve_metadata()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._f is not None and not self._finalized:
            try:
                self.finalize()
            finally:
                pass
        if self._f is not None:
            self._f.close()
            self._f = None

    def _reserve_metadata(self) -> None:
        """把 header/descriptor/RGD/GD/GT 区域先占位成零，保证后续随机写不越界。"""
        self._f.seek(0)
        self._f.write(b"\x00" * (self.data_offset * SECTOR))
        self._f.flush()

    # -- data -------------------------------------------------------------
    def write_zeros_until(self, offset: int) -> None:
        """显式跳空洞（语义上等于写零，thin 下不落盘）。"""
        if offset < 0:
            raise ValueError("negative offset")

    def write_grain(self, grain_index: int, data: bytes) -> None:
        """写入第 grain_index 个 grain（64 KiB，允许最后一块短）。"""
        expected = GRAIN_SECTORS * SECTOR
        if len(data) > expected:
            raise VmdkError("grain payload too large: %d" % len(data))
        if not any(data):                      # 全零 -> thin 跳过
            return
        payload = data + b"\x00" * (expected - len(data))
        sector = self.next_free_sector
        self.next_free_sector += GRAIN_SECTORS
        self._f.seek(sector * SECTOR)
        self._f.write(payload)
        gt_i, gte_i = divmod(grain_index, NUM_GTES_PER_GT)
        self.gts[gt_i][gte_i] = sector
        if self.gd[gt_i] == 0:
            self.gd[gt_i] = self.gt_offset + gt_i * GT_SECTORS
        self.allocated_grains += 1

    def write_at(self, offset: int, data: bytes) -> None:
        """按字节偏移写入（内部按 grain 切分；跨 grain 自动拆分）。"""
        if offset < 0:
            raise ValueError("negative offset")
        if offset + len(data) > self.capacity_sectors * SECTOR:
            raise VmdkError("write out of capacity")
        pos = 0
        while pos < len(data):
            absolute = offset + pos
            grain_index = absolute // (GRAIN_SECTORS * SECTOR)
            inner = absolute % (GRAIN_SECTORS * SECTOR)
            take = min(GRAIN_SECTORS * SECTOR - inner, len(data) - pos)
            chunk = data[pos:pos + take]

            if inner == 0 and take == GRAIN_SECTORS * SECTOR:
                if any(chunk):
                    self.write_grain(grain_index, chunk)
            else:
                # 部分 grain：读改写（已分配则读出原 grain，否则零）
                sector = self.gts[grain_index // NUM_GTES_PER_GT][grain_index % NUM_GTES_PER_GT]
                current = bytearray(GRAIN_SECTORS * SECTOR)
                if sector:
                    self._f.seek(sector * SECTOR)
                    current[:] = self._f.read(GRAIN_SECTORS * SECTOR)
                current[inner:inner + take] = chunk
                if sector:
                    self._f.seek(sector * SECTOR)
                    self._f.write(bytes(current))
                elif any(current):
                    self.write_grain(grain_index, bytes(current))
            pos += take

    # -- finalize ---------------------------------------------------------
    def _descriptor_text(self) -> bytes:
        cyl = self.capacity_sectors // max(1, self.geometry_heads * self.geometry_sectors)
        lines = [
            "# Disk DescriptorFile",
            "version=1",
            'encoding="UTF-8"',
            "CID=fffffffe",
            "parentCID=ffffffff",
            'createType="monolithicSparse"',
            "",
            "# Extent description",
            'RW %d SPARSE "%s"' % (self.capacity_sectors, os.path.basename(self.path)),
            "",
            "# The Disk Data Base",
            "#DDB",
            'ddb.adapterType = "%s"' % self.adapter,
            'ddb.geometry.cylinders = "%d"' % cyl,
            'ddb.geometry.heads = "%d"' % self.geometry_heads,
            'ddb.geometry.sectors = "%d"' % self.geometry_sectors,
            'ddb.virtualHWVersion = "4"',
            "",
        ]
        return "\n".join(lines).encode("utf-8")

    def finalize(self) -> None:
        """回写 descriptor / RGD / GD / GT / header。"""
        if self._f is None:
            raise VmdkError("writer is not open")

        desc = self._descriptor_text()
        self._f.seek(1 * SECTOR)
        self._f.write(desc + b"\x00" * (DESCRIPTOR_SECTORS * SECTOR - len(desc)))

        gd_bytes = struct.pack("<%dI" % self.num_gts, *self.gd)
        gd_padded = gd_bytes + b"\x00" * (self.gd_sectors * SECTOR - len(gd_bytes))
        self._f.seek(self.gd_offset * SECTOR)
        self._f.write(gd_padded)
        self._f.seek(self.rgd_offset * SECTOR)
        self._f.write(gd_padded)

        for i, gt in enumerate(self.gts):
            blob = struct.pack("<%dI" % NUM_GTES_PER_GT, *gt)
            self._f.seek((self.gt_offset + i * GT_SECTORS) * SECTOR)
            self._f.write(blob)

        header = bytearray(512)
        struct.pack_into(
            "<IIIQQQQIQQQ", header, 0,
            VMDK_MAGIC, VMDK_VERSION, VMDK_FLAGS,
            self.capacity_sectors, GRAIN_SECTORS,
            1, DESCRIPTOR_SECTORS, NUM_GTES_PER_GT,
            self.rgd_offset, self.gd_offset, self.overhead,
        )
        header[72] = 0                     # uncleanShutdown = 0
        header[73] = ord("\n")
        header[74] = ord(" ")
        header[75] = ord("\r")
        header[76] = ord("\n")
        struct.pack_into("<H", header, 77, 0)   # compressAlgorithm = none
        self._f.seek(0)
        self._f.write(bytes(header))

        self._f.flush()
        os.fsync(self._f.fileno())
        self._finalized = True

    # -- info -------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "path": self.path,
            "capacity_bytes": self.capacity_sectors * SECTOR,
            "num_grains": self.num_grains,
            "num_gts": self.num_gts,
            "gd_sectors": self.gd_sectors,
            "data_offset_sector": self.data_offset,
            "allocated_grains": self.allocated_grains,
            "allocated_bytes": self.allocated_grains * GRAIN_SECTORS * SECTOR,
        }
