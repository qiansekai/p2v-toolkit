# SPDX-License-Identifier: GPL-3.0-only
"""export 端到端（内存设备，覆盖多轮分块与卷影补零两条路径）。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from tests.support import BASIC_GUID, FakeDevice, MIB, pattern_bytes

NT = unittest.skipUnless(os.name == "nt", "需要 Windows 设备层（safeio 走 ctypes）")

if os.name == "nt":
    from p2v import export as E
    from p2v.plan import Plan, Segment
    from p2v.verify import verify_vmdk
    from p2v.vmdk import SparseVmdkReader

DISK_GUID = "33333333-3333-3333-3333-333333333333"


def _segment(dest, size, kind, source_offset=0, index=1, shadow=None):
    return Segment(role="partition", dest_offset=dest, size=size, source_kind=kind,
                   source_offset=source_offset, shadow_index=shadow, partition_index=index,
                   type_guid=BASIC_GUID,
                   part_guid="aaaaaaaa-bbbb-cccc-dddd-00000000000%d" % index,
                   name="p%d" % index, attributes=0)


def _plan(out, capacity, segments):
    return Plan(source_disk=99, source_device=r"\\.\FAKE", source_capacity=capacity,
                source_disk_guid=DISK_GUID, target_path=out, target_capacity=capacity,
                target_disk_guid=DISK_GUID, disk_signature=0xDEADBEEF,
                pmbr_end_lba=0xFFFFFFFF, segments=segments)


@NT
class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2v-test-")
        self._orig_physical = E.open_physical_drive
        self._orig_shadow = E.open_shadow

    def tearDown(self):
        E.open_physical_drive = self._orig_physical
        E.open_shadow = self._orig_shadow
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _out(self, name="out.vmdk"):
        return os.path.join(self.tmp, name)

    def test_physical_export_copies_every_chunk(self):
        """回归：分块循环第二轮曾因 int/bytes 复用直接 TypeError。"""
        source = pattern_bytes(32 * MIB)
        E.open_physical_drive = lambda *a, **k: FakeDevice(source)
        out = self._out()
        plan = _plan(out, 64 * MIB, [_segment(1 * MIB, 32 * MIB, "physical")])

        ticks = []
        result = E.run_export(plan, apply=True, chunk_mib=4,
                              progress=lambda seg, copied: ticks.append(copied))

        self.assertTrue(result["ok"])
        self.assertEqual(result["chunk_mib"], 4)
        self.assertGreater(len(ticks), 1)                 # 确实走了多轮
        with SparseVmdkReader(out) as reader:
            self.assertEqual(reader.read_at(1 * MIB, 32 * MIB), source)
            self.assertEqual(reader.read_at(1 * MIB + 32 * MIB, 4096), b"\x00" * 4096)
        self.assertTrue(verify_vmdk(out)["ok"])

    def test_shadow_shorter_than_partition_is_zero_padded(self):
        snapshot = pattern_bytes(8 * MIB)
        E.open_physical_drive = lambda *a, **k: FakeDevice(b"\x00" * (64 * MIB))
        E.open_shadow = lambda index, size, **k: FakeDevice(snapshot)
        out = self._out("shadow.vmdk")
        plan = _plan(out, 64 * MIB, [_segment(1 * MIB, 16 * MIB, "shadow", shadow=1)])

        result = E.run_export(plan, apply=True, chunk_mib=1)

        self.assertTrue(result["warnings"])
        with SparseVmdkReader(out) as reader:
            self.assertEqual(reader.read_at(1 * MIB, 8 * MIB), snapshot)
            self.assertEqual(reader.read_at(1 * MIB + 8 * MIB, 8 * MIB),
                             b"\x00" * (8 * MIB))
        self.assertTrue(verify_vmdk(out)["ok"])

    def test_dry_run_writes_nothing(self):
        E.open_physical_drive = lambda *a, **k: FakeDevice(b"\x00" * (64 * MIB))
        out = self._out("dry.vmdk")
        plan = _plan(out, 64 * MIB, [_segment(1 * MIB, MIB, "physical")])

        result = E.run_export(plan, apply=False)

        self.assertTrue(result["dry_run"])
        self.assertFalse(os.path.exists(out))
        self.assertTrue(result["manual_steps"])           # 收尾提示必须始终带上

    def test_existing_target_is_refused_untouched(self):
        E.open_physical_drive = lambda *a, **k: FakeDevice(b"\x00" * (64 * MIB))
        out = self._out("exists.vmdk")
        with open(out, "wb") as handle:
            handle.write(b"keep me")
        plan = _plan(out, 64 * MIB, [_segment(1 * MIB, MIB, "physical")])

        with self.assertRaises(Exception):
            E.run_export(plan, apply=True)
        with open(out, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")


if __name__ == "__main__":
    unittest.main()
