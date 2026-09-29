# SPDX-License-Identifier: GPL-3.0-only
"""GPT 构造 / 解析往返与自检（跨平台，不需要真实磁盘）。"""

from __future__ import annotations

import struct
import unittest
import uuid

from tests.support import (BASIC_GUID, ESP_GUID, MIB, SECTOR,
                           FakeDevice, assemble_disk_image)

from p2v.gpt import GptError, Partition, build_gpt, parse_gpt

CAPACITY = 64 * MIB
DISK_SIG = 0xDEADBEEF


def _partitions() -> list:
    return [
        Partition(1, uuid.UUID(ESP_GUID), uuid.UUID("11111111-1111-1111-1111-111111111111"),
                  2048, 4095, 0, "EFI system partition", SECTOR),
        Partition(2, uuid.UUID(BASIC_GUID), uuid.UUID("22222222-2222-2222-2222-222222222222"),
                  4096, 8191, 0x8000000000000000, "Basic data", SECTOR),
    ]


def _built() -> dict:
    return build_gpt(CAPACITY // SECTOR, uuid.UUID("33333333-3333-3333-3333-333333333333"),
                     _partitions(), SECTOR, disk_signature=DISK_SIG,
                     pmbr_end_lba=0xFFFFFFFF)


class GptRoundTripTest(unittest.TestCase):
    def test_build_then_parse_is_lossless(self):
        gpt = parse_gpt(FakeDevice(assemble_disk_image(_built(), CAPACITY)))
        self.assertTrue(gpt.header_crc_ok)
        self.assertTrue(gpt.entries_crc_ok)
        self.assertEqual(gpt.sector_size, SECTOR)
        self.assertEqual(str(gpt.disk_guid), "33333333-3333-3333-3333-333333333333")
        self.assertEqual([p.index for p in gpt.partitions], [1, 2])
        self.assertEqual([p.name for p in gpt.partitions],
                         ["EFI system partition", "Basic data"])
        self.assertEqual(gpt.partitions[1].attributes, 0x8000000000000000)
        self.assertEqual(gpt.partitions[0].first_lba, 2048)
        self.assertEqual(gpt.partitions[0].size_bytes, 2048 * SECTOR)

    def test_backup_entries_lba_is_immediately_before_backup_header(self):
        """回归：备份分区表必须紧邻备份 header（写成 capacity - entries 会多占一扇区）。"""
        layout = _built()["layout"]
        self.assertEqual(layout["backup_entries_lba"],
                         layout["alt_lba"] - layout["entries_sectors"])
        self.assertEqual(layout["last_usable_lba"], layout["backup_entries_lba"] - 1)

    def test_protective_mbr_fields_follow_source(self):
        mbr = _built()["mbr"]
        self.assertEqual(struct.unpack_from("<I", mbr, 0x1B8)[0], DISK_SIG)
        self.assertEqual(struct.unpack_from("<I", mbr, 0x1BE + 12)[0], 0xFFFFFFFF)
        self.assertEqual(mbr[0x1BE + 4], 0xEE)
        self.assertEqual(mbr[510:512], b"\x55\xaa")

    def test_tampered_entry_table_fails_crc(self):
        built = _built()
        img = bytearray(assemble_disk_image(built, CAPACITY))
        img[built["layout"]["entries_lba"] * SECTOR + 40] ^= 0xFF
        gpt = parse_gpt(FakeDevice(bytes(img)))
        self.assertFalse(gpt.entries_crc_ok)
        self.assertTrue(gpt.header_crc_ok)

    def test_partition_outside_usable_range_is_rejected(self):
        bad = [Partition(1, uuid.UUID(BASIC_GUID), uuid.uuid4(), 1, 100, 0, "bad", SECTOR)]
        with self.assertRaises(GptError):
            build_gpt(CAPACITY // SECTOR, uuid.uuid4(), bad, SECTOR)

    def test_non_gpt_signature_is_rejected(self):
        with self.assertRaises(GptError):
            parse_gpt(FakeDevice(b"\x00" * (2 * SECTOR)))


if __name__ == "__main__":
    unittest.main()
