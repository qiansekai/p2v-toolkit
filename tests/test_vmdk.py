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
