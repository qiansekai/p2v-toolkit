# SPDX-License-Identifier: GPL-3.0-only
"""verify：结构自检与两道守卫。"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
import unittest

from tests.support import BASIC_GUID, FakeDevice, MIB, SECTOR, pattern_bytes

NT = unittest.skipUnless(os.name == "nt", "需要 Windows 设备层（safeio 走 ctypes）")

if os.name == "nt":
    from p2v import export as E
    from p2v.plan import Plan, Segment
    from p2v.verify import _layout_checks, verify_vmdk

DISK_GUID = "33333333-3333-3333-3333-333333333333"


@NT
class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2v-verify-")
        self._orig_physical = E.open_physical_drive
        self.path = os.path.join(self.tmp, "out.vmdk")
        E.open_physical_drive = lambda *a, **k: FakeDevice(pattern_bytes(8 * MIB))
        segment = Segment(role="partition", dest_offset=1 * MIB, size=4 * MIB,
                          source_kind="physical", source_offset=0, shadow_index=None,
                          partition_index=1, type_guid=BASIC_GUID,
                          part_guid="aaaaaaaa-bbbb-cccc-dddd-000000000001",
                          name="p1", attributes=0)
        plan = Plan(source_disk=99, source_device=r"\\.\FAKE", source_capacity=64 * MIB,
                    source_disk_guid=DISK_GUID, target_path=self.path,
                    target_capacity=64 * MIB, target_disk_guid=DISK_GUID,
                    disk_signature=0xDEADBEEF, pmbr_end_lba=0xFFFFFFFF,
                    segments=[segment])
        E.run_export(plan, apply=True, chunk_mib=4)

    def tearDown(self):
        E.open_physical_drive = self._orig_physical
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_artifact_passes_all_checks(self):
        result = verify_vmdk(self.path)
        self.assertTrue(result["ok"], result["checks"])
        names = [c["name"] for c in result["checks"]]
        for expected in ("vmdk_header", "gpt_primary", "gpt_backup",
                         "layout_gd_offset", "layout_overhead_covers_gt",
                         "layout_gd_rgd_values"):
            self.assertIn(expected, names)

    def test_zeroed_gd_entry_is_detected(self):
        """GD/RGD 层不稀疏：GD 项写成 0 必须被抓到（否则只有 VMware 报 needs repair）。"""
        with open(self.path, "r+b") as handle:
            header = handle.read(512)
            (_magic, _ver, _flags, _cap, _grain, _doff, _dsize, _numg,
             _rgd, gd_off, _over) = struct.unpack_from("<IIIQQQQIQQQ", header, 0)
            handle.seek(gd_off * SECTOR)
            handle.write(b"\x00\x00\x00\x00")

        checks = {c["name"]: c["ok"] for c in _layout_checks(self.path)}

        self.assertFalse(checks["layout_gd_rgd_values"])
        self.assertTrue(checks["layout_gd_offset"])       # 位置关系本身仍然正确

    def _grain0_host_sector(self) -> int:
        """经 GD -> GT 找到虚拟盘第 0 个 grain 在文件里的位置。"""
        with open(self.path, "rb") as handle:
            header = handle.read(512)
            (_magic, _ver, _flags, _cap, _grain, _doff, _dsize, _numg,
             _rgd, gd_off, _over) = struct.unpack_from("<IIIQQQQIQQQ", header, 0)
            handle.seek(gd_off * SECTOR)
            gt_offset = struct.unpack("<I", handle.read(4))[0]
            handle.seek(gt_offset * SECTOR)
            return struct.unpack("<I", handle.read(4))[0]

    def test_broken_gpt_is_reported_not_raised(self):
        """产物损坏应返回 FAIL 明细（exit 2），不能抛成通用错误（exit 1）。"""
        grain0 = self._grain0_host_sector()
        with open(self.path, "r+b") as handle:
            handle.seek(grain0 * SECTOR + SECTOR)          # 虚拟盘 LBA1 = 主 GPT header
            handle.write(b"XXXXXXXX")

        result = verify_vmdk(self.path)

        self.assertFalse(result["ok"])
        primary = [c for c in result["checks"] if c["name"] == "gpt_primary"]
        self.assertEqual(len(primary), 1)
        self.assertFalse(primary[0]["ok"])

    def test_source_shadow_without_source_disk_is_rejected(self):
        """卷影副本不含分区表：缺 --source-disk 时必须报错，不能回落去读 PhysicalDrive3。"""
        with self.assertRaises(ValueError):
            verify_vmdk(self.path, source_shadow=1)


if __name__ == "__main__":
    unittest.main()
