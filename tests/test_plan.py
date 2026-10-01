# SPDX-License-Identifier: GPL-3.0-only
"""plan / 选择器 / 参数校验（需要 Windows：plan 导入 safeio）。"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from tests.support import BASIC_GUID, ESP_GUID, MSR_GUID, MIB, SECTOR

NT = unittest.skipUnless(os.name == "nt", "需要 Windows 设备层（safeio 走 ctypes）")

if os.name == "nt":
    import p2v.plan as plan
    from p2v import vss
    from p2v.export import MAX_CHUNK_MIB, resolve_chunk_bytes
    from p2v.gpt import GptDisk, Partition
    from p2v.plan import (PlanError, build_plan, latest_shadow_for_volume,
                          resolve_selector)


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


@NT
class ShadowVolumeMatchingTest(unittest.TestCase):
    """卷影副本选择：只能取目标卷自己的快照。

    回归点：没有快照的卷必须拿到 None，而不是"全部快照里最新的那个"。
    真实触发场景 —— C 卷无快照、D 卷有两张，于是 C 盘的数据段被指向 D 卷快照：
    产物结构合法、verify 全绿，但盘里装的是另一个卷的内容。
    """

    C_VOL = "\\\\?\\Volume{3b7ed9b1-2613-487a-ad57-423b41194600}\\"
    D_VOL = "\\\\?\\Volume{dddd0b95-123e-4954-bd11-8de28b4e3463}\\"

    @staticmethod
    def _shadow(volume, index, install_date, sid):
        return {
            "id": sid,
            "volume": volume,
            "device_object": ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy%s"
                              % index),
            "shadow_index": index,
            "install_date": install_date,
        }

    def _pick(self, vol_guid, shadows):
        with mock.patch.object(plan, "_ps_json", return_value=vol_guid), \
                mock.patch.object(vss, "list_shadows", return_value=shadows):
            return latest_shadow_for_volume("C")

    def test_other_volume_shadow_is_not_substituted(self):
        """别的卷的快照绝不能顶给本卷（旧实现会退化成"全局取最新"）。"""
        shadows = [self._shadow(self.D_VOL, 3, "2026/9/28 23:23:21", "{SID-D}")]
        self.assertIsNone(self._pick(self.C_VOL, shadows))

    def test_matching_volume_picks_newest_of_its_own(self):
        shadows = [
            self._shadow(self.C_VOL, 5, "/Date(1000)/", "{SID-C-OLD}"),
            self._shadow(self.C_VOL, 7, "/Date(2000)/", "{SID-C-NEW}"),
            self._shadow(self.D_VOL, 3, "/Date(3000)/", "{SID-D}"),
        ]
        got = self._pick(self.C_VOL, shadows)
        self.assertIsNotNone(got)
        self.assertEqual(got["shadow_index"], 7)
        self.assertEqual(got["id"], "{SID-C-NEW}")
        self.assertEqual(got["volume"], self.C_VOL)

    def test_unknown_volume_identity_returns_none(self):
        """拿不到本卷 UniqueId 就不猜：宁可不走快照。"""
        shadows = [self._shadow(self.D_VOL, 3, "/Date(3000)/", "{SID-D}")]
        self.assertIsNone(self._pick(None, shadows))

    def test_no_shadows_at_all_returns_none(self):
        self.assertIsNone(self._pick(self.C_VOL, []))

    def test_shadow_without_index_is_ignored(self):
        """WMI 里解析不出序号的条目不可用。"""
        bad = self._shadow(self.C_VOL, None, "/Date(9000)/", "{SID-NOINDEX}")
        good = self._shadow(self.C_VOL, 4, "/Date(1000)/", "{SID-C}")
        got = self._pick(self.C_VOL, [bad, good])
        self.assertIsNotNone(got)
        self.assertEqual(got["shadow_index"], 4)


if __name__ == "__main__":
    unittest.main()
