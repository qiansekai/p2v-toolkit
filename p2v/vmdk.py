# SPDX-License-Identifier: GPL-3.0-only
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

续传（resume=True）
------------------
分配位置由"第 k 个被分配的非零 grain"唯一决定（overhead + k*GRAIN_SECTORS），
所以中断后重跑会落到完全相同的扇区，不会错位。但 GT/GD 原本只在 finalize 时
回写，中途退出等于没有任何状态 —— 因此引入 checkpoint()：把脏 GT 与 GD/RGD
增量落盘，并把 header 的 uncleanShutdown 置 1 标记"未完成"。
**恢复时 header / GD / GT 是权威事实，水位由 sidecar 提供**（GTE 表分不清
"全零"与"还没读到"）。
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
                 geometry_heads: int = 255, geometry_sectors: int = 63,
                 resume: bool = False) -> None:
        if capacity_bytes % SECTOR:
            raise VmdkError("capacity must be a multiple of %d" % SECTOR)
        self.path = path
        self.resume = bool(resume)
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
        self._dirty_gts: set = set()
        self.unclean = False

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "SparseVmdkWriter":
        if self.resume:
            return self._open_existing()
        if os.path.exists(self.path):
            raise VmdkError("refusing to overwrite existing file: %s" % self.path)
        self._f = open(self.path, "w+b")
        self._reserve_metadata()
        return self

    def _open_existing(self) -> "SparseVmdkWriter":
        """打开半成品并恢复分配器状态；任何不一致都直接拒绝，不做猜测。"""
        if not os.path.exists(self.path):
            raise VmdkError("resume 需要已存在的半成品，但目标不存在：%s" % self.path)
        self._f = open(self.path, "r+b")
        try:
            self._load_existing()
        except BaseException:
            # 打开失败不要把句柄留在外面（Windows 上会一直占住文件）
            self._f.close()
            self._f = None
            raise
        return self

    def _load_existing(self) -> None:
        raw = self._f.read(512)
        if len(raw) < 512:
            raise VmdkError("目标文件小于一个扇区，不是可续传的半成品：%s" % self.path)
        (magic, version, _flags, capacity, grain, _desc_off, _desc_size,
         num_gtes, rgd_off, gd_off, overhead) = struct.unpack_from("<IIIQQQQIQQQ", raw, 0)
        if magic != VMDK_MAGIC:
            raise VmdkError(
                "目标文件没有 vmdk header，无法续传（上一次导出在第一个检查点之前就"
                "中断了），请删除后重跑：%s" % self.path)
        if version != VMDK_VERSION:
            raise VmdkError("unsupported vmdk version %d" % version)
        if grain != GRAIN_SECTORS or num_gtes != NUM_GTES_PER_GT:
            raise VmdkError("半成品几何参数不受支持：grain=%d numGTEsPerGT=%d"
                            % (grain, num_gtes))
        if capacity != self.capacity_sectors:
            raise VmdkError("半成品虚拟容量 %d 扇区与本次计划 %d 扇区不一致"
                            % (capacity, self.capacity_sectors))
        if (overhead != self.overhead or gd_off != self.gd_offset
                or rgd_off != self.rgd_offset):
            raise VmdkError("半成品元数据布局与本次计划不一致：overhead %d/%d gd %d/%d"
                            % (overhead, self.overhead, gd_off, self.gd_offset))
        self.unclean = bool(raw[72])

        self._f.seek(gd_off * SECTOR)
        gd_raw = self._f.read(self.num_gts * 4)
        if len(gd_raw) < self.num_gts * 4:
            raise VmdkError("半成品的 grain directory 不完整")
        gd = list(struct.unpack("<%dI" % self.num_gts, gd_raw))
        for i, gt_sector in enumerate(gd):
            expected = self.gt_offset + i * GT_SECTORS
            if gt_sector != expected:
                raise VmdkError("GD[%d]=%d 与预期 %d 不符，目标文件不是本工具写的半成品"
                                % (i, gt_sector, expected))

        gts = []
        allocated = 0
        highest_sector = 0
        for i in range(self.num_gts):
            self._f.seek((self.gt_offset + i * GT_SECTORS) * SECTOR)
            blob = self._f.read(NUM_GTES_PER_GT * 4)
            if len(blob) < NUM_GTES_PER_GT * 4:
                raise VmdkError("grain table #%d 不完整" % i)
            row = list(struct.unpack("<%dI" % NUM_GTES_PER_GT, blob))
            gts.append(row)
            for value in row:
                if value:
                    allocated += 1
                    if value > highest_sector:
                        highest_sector = value
        if allocated:
            expected_free = highest_sector + GRAIN_SECTORS
            if expected_free != self.data_offset + allocated * GRAIN_SECTORS:
                raise VmdkError(
                    "半成品分配不连续（已分配 %d 个 grain，最高位置在 %d 扇区）；"
                    "文件可能被外部改动过，拒绝续传" % (allocated, highest_sector))
            self.next_free_sector = expected_free
        else:
            self.next_free_sector = self.data_offset
        self.gd = gd
        self.gts = gts
        self.allocated_grains = allocated

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
        self._dirty_gts.add(gt_i)
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

    # -- 检查点 / 续传 -----------------------------------------------------
    def checkpoint(self) -> None:
        """把元数据增量落盘，并把 header 标记为「未完成」，供中断后续传。

        只写**脏**的 grain table：1 TiB 计划全量落一次是 128 MiB，按 256 MiB
        数据落一次就是 50% 写放大。GD / RGD 每张只有 4 字节且必须整区一致，
        所以按全量写（1 TiB 计划两张各 128 KiB，可忽略）。
        """
        if self._f is None:
            raise VmdkError("writer is not open")
        self._write_gd_rgd()
        for index in sorted(self._dirty_gts):
            self._write_gt(index)
        self._write_header(unclean=True)
        self._flush()
        self._dirty_gts.clear()

    def rewind(self, next_free_sector: int) -> int:
        """把分配器回退到检查点水位，丢弃水位之后的分配，返回丢弃的 grain 数。

        判据是**数据位置**而不是 grain 下标：grain 被分配的先后等于"写入顺序"，
        而写入顺序由 plan 的段顺序决定（--take 完全可以写成 "vol:C:,ESP" 这种
        非递增顺序），所以 grain_index 与分配先后没有对应关系。凡是指向
        >= 水位的 GTE 都是检查点之后才产生的分配，必须清掉 —— 否则它们会一直
        指向陈旧数据（该 grain 不会再被重新分配时就永远错下去）。
        """
        limit = int(next_free_sector)
        dropped = 0
        for index, row in enumerate(self.gts):
            if not any(row):                     # 空行用 C 级 any 快速跳过
                continue
            changed = False
            for position, sector in enumerate(row):
                if sector and sector >= limit:
                    row[position] = 0
                    dropped += 1
                    changed = True
            if changed:
                self._dirty_gts.add(index)
        self.next_free_sector = limit
        self.allocated_grains = max(0, self.allocated_grains - dropped)
        return dropped

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

        self._write_gd_rgd()
        for i in range(self.num_gts):
            self._write_gt(i)
        self._write_header(unclean=False)
        self._flush()
        self._dirty_gts.clear()
        self._finalized = True

    def _pack32(self, vals: list) -> bytes:
        raw = struct.pack("<%dI" % len(vals), *vals)
        pad = self.entry_table_sectors * SECTOR - len(raw)
        return raw + b"\x00" * pad

    def _write_gd_rgd(self) -> None:
        # 主 GD -> 主 GT 区；RGD -> 冗余 GT 区（两区内容相同，互为备份）
        # 注意：GD / RGD 这一层**不稀疏** —— 每一项都必须指向对应的 GT 位置
        # （稀疏性只体现在 GTE 层：GTE == 0 表示该 grain 未分配）。
        # qemu-img 的成品同样如此（q1g.vmdk 的 8 个 GD 项全非零）。
        # 若把未分配 GT 的 GD 项写成 0，VMware 的 SPARSECHK 会对每一项报
        #   Invalid GD or RGD [i]: <gt pos>,<rgt pos> vs. 0,0
        gd_main = [self.gt_offset + i * GT_SECTORS for i in range(self.num_gts)]
        rgd_vals = [self.redundant_gt_offset + i * GT_SECTORS for i in range(self.num_gts)]
        self._f.seek(self.gd_offset * SECTOR)
        self._f.write(self._pack32(gd_main))
        self._f.seek(self.rgd_offset * SECTOR)
        self._f.write(self._pack32(rgd_vals))

    def _write_gt(self, index: int) -> None:
        blob = struct.pack("<%dI" % NUM_GTES_PER_GT, *self.gts[index])
        self._f.seek((self.gt_offset + index * GT_SECTORS) * SECTOR)
        self._f.write(blob)
        self._f.seek((self.redundant_gt_offset + index * GT_SECTORS) * SECTOR)
        self._f.write(blob)

    def _write_header(self, unclean: bool) -> None:
        header = bytearray(512)
        struct.pack_into(
            "<IIIQQQQIQQQ", header, 0,
            VMDK_MAGIC, VMDK_VERSION, VMDK_FLAGS,
            self.capacity_sectors, GRAIN_SECTORS,
            1, DESCRIPTOR_SECTORS, NUM_GTES_PER_GT,
            self.rgd_offset, self.gd_offset, self.overhead,
        )
        header[72] = 1 if unclean else 0    # uncleanShutdown：半成品必须为 1
        header[73] = ord("\n")
        header[74] = ord(" ")
        header[75] = ord("\r")
        header[76] = ord("\n")
        struct.pack_into("<H", header, 77, 0)   # compressAlgorithm = none
        self._f.seek(0)
        self._f.write(bytes(header))
        self.unclean = bool(unclean)

    def _flush(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())

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
                 "num_gtes_per_gt", "flags", "gd_offset", "overhead", "unclean",
                 "_f", "_gd", "_gt_cache")

    def __init__(self, path: str, sector_size: int = SECTOR) -> None:
        self.path = path
        self.sector_size = sector_size
        self._f = open(path, "rb")
        self._gt_cache = {}
        try:
            self._read_header()
        except Exception:
            # 构造失败时不要泄漏句柄（Windows 上会一直占住文件）
            self._f.close()
            self._f = None
            raise

    def _read_header(self) -> None:
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
        # uncleanShutdown：1 表示这是一份中途检查点过的半成品，还没 finalize
        self.unclean = bool(header[72])

        num_gts = (capacity + grain - 1) // grain
        num_gd_entries = (num_gts + num_gtes - 1) // num_gtes
        self._f.seek(gd_off * self.sector_size)
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

    def grain_sector(self, grain_index: int) -> int:
        """该 grain 的数据区起始扇区；0 表示未分配（thin 空洞）。"""
        return self._grain_sector(grain_index)

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