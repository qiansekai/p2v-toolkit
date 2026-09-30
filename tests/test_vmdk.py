# SPDX-License-Identifier: GPL-3.0-only
"""sparse vmdk 读写往返（跨平台，不需要真实磁盘）。"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import MIB, pattern_bytes

from p2v.vmdk import SparseVmdkReader, SparseVmdkWriter, VmdkError


class SparseVmdkTest(unittest.TestCase):
    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".vmdk")
        os.close(handle)
        os.remove(self.path)          # writer 要求目标不存在

    def tearDown(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_write_read_roundtrip_with_thin_hole(self):
        capacity = 4 * MIB
        data = pattern_bytes(capacity)
        with SparseVmdkWriter(self.path, capacity) as writer:
            writer.write_at(0, data[:2 * MIB])
            writer.write_at(3 * MIB, data[3 * MIB:])
            allocated = writer.allocated_grains
        with SparseVmdkReader(self.path) as reader:
            self.assertEqual(reader.size, capacity)
            self.assertEqual(reader.read_at(0, 2 * MIB), data[:2 * MIB])
            self.assertEqual(reader.read_at(2 * MIB, MIB), b"\x00" * MIB)   # 空洞
            self.assertEqual(reader.read_at(3 * MIB, MIB), data[3 * MIB:])
        self.assertEqual(allocated, 48)          # 3 MiB / 64 KiB

    def test_partial_grain_write_merges_with_existing(self):
        capacity = 1 * MIB
        with SparseVmdkWriter(self.path, capacity) as writer:
            writer.write_at(0, b"A" * 1024)
            writer.write_at(100, b"B" * 10)
        with SparseVmdkReader(self.path) as reader:
            blob = reader.read_at(0, 1024)
        self.assertEqual(blob[:100], b"A" * 100)
        self.assertEqual(blob[100:110], b"B" * 10)
        self.assertEqual(blob[110:], b"A" * (1024 - 110))

    def test_write_beyond_capacity_is_rejected(self):
        with SparseVmdkWriter(self.path, MIB) as writer:
            with self.assertRaises(VmdkError):
                writer.write_at(MIB - 10, b"x" * 20)

    def test_refuses_to_overwrite_existing_file(self):
        with open(self.path, "wb") as handle:
            handle.write(b"already here")
        with self.assertRaises(VmdkError):
            with SparseVmdkWriter(self.path, MIB):
                pass

    def test_reader_rejects_non_vmdk(self):
        with open(self.path, "wb") as handle:
            handle.write(b"\x00" * 4096)
        with self.assertRaises(VmdkError):
            SparseVmdkReader(self.path)


if __name__ == "__main__":
    unittest.main()


def _read_gd(path):
    """读回产物的 header 几何与 grain directory（测试内自用）。"""
    import struct
    with open(path, "rb") as handle:
        header = handle.read(512)
        (_m, _v, _f, capacity, grain, _doff, _dsize, entries_per_gt,
         _rgd, gd_off, overhead) = struct.unpack_from("<IIIQQQQIQQQ", header, 0)
        gd_count = (capacity + grain * entries_per_gt - 1) // (grain * entries_per_gt)
        handle.seek(gd_off * 512)
        gd = struct.unpack("<%dI" % gd_count, handle.read(gd_count * 4))
    num_gts = gd_count
    return {"capacity": capacity, "grain": grain, "num_gts": num_gts,
            "gd": gd, "gd_off": gd_off, "overhead": overhead,
            "entries_per_gt": entries_per_gt}


class HotPathTest(unittest.TestCase):
    """热路径改动的语义回归。

    这几条不是"性能测试"（跑得快慢受机器影响），而是锁住优化不能改变的东西：
    零判据必须全量、grain 表惰性分配不得丢数据、检查点只落脏表。
    """

    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".vmdk")
        os.close(handle)
        os.remove(self.path)

    def tearDown(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_nonzero_payload_in_grain_middle_is_kept(self):
        """回归：零判据**不能**抽样。

        曾经的实现只看首尾采样区间，于是"数据落在 grain 中段、首尾全是零"的写
        被当成空洞丢弃 —— 真实触发点是 GPT 备份分区表：它落在 64 KiB grain 的
        48640 偏移处，而首尾各 4 KiB 都是零。
        """
        payload = b"\xa2\xa0\xd0\xeb" + b"\x11" * 44
        with SparseVmdkWriter(self.path, 4 * MIB) as writer:
            writer.write_at(48640, payload)
        with SparseVmdkReader(self.path) as reader:
            self.assertEqual(reader.read_at(48640, len(payload)), payload)
            self.assertEqual(reader.read_at(0, 4096), b"\x00" * 4096)

    def test_checkpoint_reports_only_dirty_runs(self):
        """检查点只写脏表，且把**相邻**脏表合并成一个区间。

        这是 1 TiB 下把一次检查点从几百毫秒压下来的关键：GT 区有 64 MiB，
        按"每张表两次 write"落盘等于把检查点变成主要开销。这里同时验证
        "相邻必须合并"，否则合并逻辑退化就没人发现。
        """
        with SparseVmdkWriter(self.path, 128 * MIB) as writer:
            self.assertEqual(writer.num_gts, 4)             # 128 MiB / 32 MiB 每张 GT
            writer.write_at(0, pattern_bytes(512))
            self.assertEqual(writer._dirty_runs(), [(0, 0)])
            writer.write_at(64 * MIB, pattern_bytes(512))    # 跳过 GT1 -> 区间不连续
            self.assertEqual(writer._dirty_runs(), [(0, 0), (2, 2)])
            writer.write_at(96 * MIB, pattern_bytes(512))    # 补上 GT2 的邻居 GT3
            self.assertEqual(writer._dirty_runs(), [(0, 0), (2, 3)])
    def test_checkpoint_preserves_undirtied_tables(self):
        """未脏的 GT 必须原样保留：检查点不能退化成"把全部表当待写清单"。

        GTE 的高 4 位是 vmdk 的 flag 位（qemu-img 写 0x80000000），
        所以比较前要掩掉 0xF0000000，只看低 28 位的扇区号。
        """
        import struct
        with SparseVmdkWriter(self.path, 64 * MIB) as writer:
            writer.write_at(0, pattern_bytes(512))
            writer.checkpoint()
        geom = _read_gd(self.path)
        count = geom["entries_per_gt"]
        gt_bytes = count * 4
        with open(self.path, "rb") as handle:
            handle.seek(geom["gd"][0] * 512)
            dirty = struct.unpack("<%dI" % count, handle.read(gt_bytes))
            handle.seek(geom["gd"][1] * 512)
            clean = struct.unpack("<%dI" % count, handle.read(gt_bytes))
        sectors = lambda row: [v & 0x0FFFFFFF for v in row]
        self.assertNotEqual(sectors(dirty), [0] * count)   # GT0 有过分配
        self.assertEqual(sectors(clean), [0] * count)      # GT1 没人碰过
    def test_lazy_grain_tables_survive_finalize(self):
        """惰性分配的 GT 在 finalize 时按全零表回写：容量内任意位置的写都要能读回。"""
        capacity = 64 * MIB
        head = pattern_bytes(1 * MIB)
        tail = pattern_bytes(1 * MIB, mul=11, add=5)
        with SparseVmdkWriter(self.path, capacity) as writer:
            writer.write_at(0, head)                       # GT0
            writer.write_at(capacity - MIB, tail)          # GT1 —— 另一张表
        with SparseVmdkReader(self.path) as reader:
            self.assertEqual(reader.read_at(0, MIB), head)
            self.assertEqual(reader.read_at(capacity - MIB, MIB), tail)
            self.assertEqual(reader.read_at(MIB, 4096), b"\x00" * 4096)


if __name__ == "__main__":
    unittest.main()
