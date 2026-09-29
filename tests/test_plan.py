# SPDX-License-Identifier: GPL-3.0-only
"""plan / 选择器 / 参数校验（需要 Windows：plan 导入 safeio）。"""

from __future__ import annotations

import os
import unittest
import uuid

from tests.support import BASIC_GUID, ESP_GUID, MSR_GUID, MIB, SECTOR

NT = unittest.skipUnless(os.name == "nt", "需要 Windows 设备层（safeio 走 ctypes）")

if os.name == "nt":
    from p2v.export import MAX_CHUNK_MIB, resolve_chunk_bytes
    from p2v.gpt import GptDisk, Partition
    from p2v.plan import PlanError, build_plan, resolve_selector


def _gpt():
    parts = [
        Partition(1, uuid.UUID(ESP_GUID), uuid.uuid4(), 2048, 4095, 0, "ESP", SECTOR),
        Partition(2, uuid.UUID(MSR_GUID), uuid.uuid4(), 4096, 8191, 0, "MSR", SECTOR),
        Partition(3, uuid.UUID(BASIC_GUID), uuid.uuid4(), 8192, 20479, 0, "Basic", SECTOR),
    ]
    return GptDisk(sector_size=SECTOR, disk_guid=uuid.uuid4(), first_usable_lba=34,
                   last_usable_lba=20479, capacity=64 * MIB, partitions=parts,
                   header_crc_ok=True, entries_crc_ok=True)


@NT
class SelectorTest(unittest.TestCase):
    def test_esp_msr_and_part_selectors(self):
        gpt = _gpt()
        self.assertEqual(resolve_selector(gpt, "ESP").index, 1)
        self.assertEqual(resolve_selector(gpt, "msr").index, 2)
        self.assertEqual(resolve_selector(gpt, "part:3").index, 3)

    def test_unknown_selectors_are_rejected(self):
        for bad in ("part:99", "bogus", ""):
            with self.subTest(selector=bad):
                with self.assertRaises(PlanError):
                    resolve_selector(_gpt(), bad)

    def test_volume_selector_rejects_command_injection(self):
        """盘符会被拼进 PowerShell 脚本，非单个字母一律拒绝。"""
        for bad in ("vol:C:; calc; #", "vol:CC", "vol:", "vol:1", "vol:C$(calc)"):
            with self.subTest(selector=bad):
                with self.assertRaises(PlanError):
                    resolve_selector(_gpt(), bad, disk=3)


@NT
class ChunkSizeTest(unittest.TestCase):
    def test_bounds_and_default(self):
        self.assertEqual(resolve_chunk_bytes(None), 4 * MIB)
        self.assertEqual(resolve_chunk_bytes(1), 1 * MIB)
        self.assertEqual(resolve_chunk_bytes(MAX_CHUNK_MIB), MAX_CHUNK_MIB * MIB)

    def test_out_of_range_is_rejected(self):
        for bad in (0, -1, MAX_CHUNK_MIB + 1):
            with self.subTest(mib=bad):
                with self.assertRaises(PlanError):
                    resolve_chunk_bytes(bad)


@NT
class BuildPlanGuardTest(unittest.TestCase):
    def test_unknown_source_mode_is_rejected_before_touching_devices(self):
        with self.assertRaises(PlanError):
            build_plan(3, ["ESP"], "x.vmdk", source_mode="nope")


if __name__ == "__main__":
    unittest.main()
