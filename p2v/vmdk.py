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

        self.entry_table_sectors = max(1, (self.num_gts * 4 + SECTOR - 1) // SECTOR)
        self.gt_area_sectors = self.num_gts * GT_SECTORS

        # 布局必须与 qemu-img 的成品一致，否则 VMware 的 DISKLIB-SPARSECHK 会报
        #   Invalid GD or RGD ...
        # 并直接判定 "The specified virtual disk needs repair"。
        #
        # 实测（qemu q1g: gts=8 -> rgd=21 gd=54；starwind t: gts=1 -> rgd=21 gd=26）：
        #   RGD 区紧跟着一份【冗余 GT 区】，RGD[i] 指向冗余 GT 区的第 i 张 GT，
        #   主 GD[i] 指向主 GT 区的第 i 张 GT，两区内容相同。
        #   gd_offset = rgd_offset + rgd_need + gts*GT_SECTORS   (21+1+32=54 / 21+1+4=26)
        self.rgd_offset = 1 + DESCRIPTOR_SECTORS                  # 21
        self.rgd_sectors = self.entry_table_sectors
        self.redundant_gt_offset = self.rgd_offset + self.rgd_sectors
        self.redundant_gt_sectors = self.gt_area_sectors
        self.gd_offset = self.redundant_gt_offset + self.redundant_gt_sectors
        self.gd_sectors = self.entry_table_sectors
        self.gt_offset = self.gd_offset + self.gd_sectors
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

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 有异常时**不** finalize：半成品不该看起来像一个正常的 vmdk
        if self._f is not None:
            if exc_type is None and not self._finalized:
                self.finalize()
            self._f.close()
            self._f = None
        return False

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

        def _pack32(vals: list) -> bytes:
            raw = struct.pack("<%dI" % len(vals), *vals)
            pad = self.entry_table_sectors * SECTOR - len(raw)
            return raw + b"\x00" * pad

        # 主 GD -> 主 GT 区；RGD -> 冗余 GT 区（两台 GT 内容相同，互为备份）
        gd_main = [self.gt_offset + i * GT_SECTORS if self.gd[i] else 0
                   for i in range(self.num_gts)]
        rgd_vals = [self.redundant_gt_offset + i * GT_SECTORS if self.gd[i] else 0
                    for i in range(self.num_gts)]
        self._f.seek(self.gd_offset * SECTOR)
        self._f.write(_pack32(gd_main))
        self._f.seek(self.rgd_offset * SECTOR)
        self._f.write(_pack32(rgd_vals))

        for i, gt in enumerate(self.gts):
            blob = struct.pack("<%dI" % NUM_GTES_PER_GT, *gt)
            self._f.seek((self.gt_offset + i * GT_SECTORS) * SECTOR)
            self._f.write(blob)
            self._f.seek((self.redundant_gt_offset + i * GT_SECTORS) * SECTOR)
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

class SparseVmdkReader:
    """读取 monolithicSparse vmdk（自包含，供 verify 使用，不依赖外部工具）。

    接口与 ReadOnlyDevice 一致：size / read_at。
    """

    __slots__ = ("path", "sector_size", "capacity_sectors", "grain_sectors",
                 "num_gtes_per_gt", "flags", "gd_offset", "overhead",
                 "_f", "_gd", "_gt_cache")

    def __init__(self, path: str, sector_size: int = SECTOR) -> None:
        self.path = path
        self.sector_size = sector_size
        self._f = open(path, "rb")
        self._gt_cache = {}

        header = self._f.read(512)
        (magic, version, flags, capacity, grain, desc_off, desc_size,
         num_gtes, rgd_off, gd_off, overhead) = struct.unpack_from("<IIIQQQQIQQQ", header, 0)
        if magic != VMDK_MAGIC:
            raise VmdkError("not a sparse vmdk (magic=0x%08X)" % magic)
        if version != VMDK_VERSION:
            raise VmdkError("unsupported vmdk version %d" % version)
        if grain != GRAIN_SECTORS or num_gtes != NUM_GTES_PER_GT:
            raise VmdkError("unsupported geometry: grain=%d numGTEsPerGT=%d" % (grain, num_gtes))

        self.capacity_sectors = capacity
        self.grain_sectors = grain
        self.num_gtes_per_gt = num_gtes
        self.flags = flags
        self.gd_offset = gd_off
        self.overhead = overhead

        num_gts = (capacity + grain - 1) // grain
        num_gd_entries = (num_gts + num_gtes - 1) // num_gtes
        self._f.seek(gd_off * sector_size)
        raw_gd = self._f.read(num_gd_entries * 4)
        self._gd = list(struct.unpack("<%dI" % num_gd_entries, raw_gd))

    @property
    def size(self) -> int:
        return self.capacity_sectors * self.sector_size

    def _grain_sector(self, grain_index: int) -> int:
        gt_i, gte_i = divmod(grain_index, self.num_gtes_per_gt)
        if gt_i >= len(self._gd):
            return 0
        gt_sector = self._gd[gt_i]
        if gt_sector == 0:
            return 0
        if gt_i not in self._gt_cache:
            self._f.seek(gt_sector * self.sector_size)
            self._gt_cache[gt_i] = struct.unpack("<%dI" % self.num_gtes_per_gt,
                                                 self._f.read(self.num_gtes_per_gt * 4))
        return self._gt_cache[gt_i][gte_i]

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("negative offset/size")
        if offset + size > self.size:
            raise ValueError("read out of range: %d+%d > %d" % (offset, size, self.size))
        grain_bytes = self.grain_sectors * self.sector_size
        out = bytearray()
        pos = offset
        remaining = size
        while remaining > 0:
            grain_index = pos // grain_bytes
            inner = pos % grain_bytes
            take = min(grain_bytes - inner, remaining)
            sector = self._grain_sector(grain_index)
            if sector == 0:
                out.extend(b"\x00" * take)
            else:
                self._f.seek(sector * self.sector_size + inner)
                out.extend(self._f.read(take))
            pos += take
            remaining -= take
        return bytes(out)

    def read_sectors(self, lba: int, count: int) -> bytes:
        return self.read_at(lba * self.sector_size, count * self.sector_size)

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self) -> "SparseVmdkReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()