# SPDX-License-Identifier: GPL-3.0-only
"""续传端到端（内存设备，无需真盘、无需管理员）。

最有力的那条断言是：**在每一个检查点边界各中断一次，续传后的产物与一次性导出
逐字节相同**。它同时覆盖数据、分配顺序、GTE 表与回退逻辑。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from tests.support import BASIC_GUID, FakeDevice, MIB, pattern_bytes

NT = unittest.skipUnless(os.name == "nt", "需要 Windows 设备层（safeio 走 ctypes）")

if os.name == "nt":
    from p2v import export as E
    from p2v import resume as R
    from p2v.plan import Plan, PlanError, Segment
    from p2v.verify import verify_vmdk
    from p2v.vmdk import GRAIN_SECTORS, SparseVmdkReader, SparseVmdkWriter

DISK_GUID = "44444444-4444-4444-4444-444444444444"
CAPACITY = 32 * MIB
GRAIN = GRAIN_SECTORS * 512

DISK_IDENTITY = {
    "disk": 99,
    "serial_number": "FAKESERIAL01",
    "unique_id": "FAKEUNIQUE01",
    "size": CAPACITY,
    "bus_type": "SATA",
    "is_read_only": True,
    "model": "FakeDisk",
}

SHADOW_IDENTITY = {
    "shadow_index": 7,
    "id": "{11111111-2222-3333-4444-555555555555}",
    "install_date": "2026/9/28 23:23:21",
    "volume": "\\\\?\\Volume{deadbeef-0000-0000-0000-000000000000}\\",
}


class Interrupted(RuntimeError):
    """模拟断电 / 进程被杀。"""


def _segment(dest, size, kind, source_offset=0, index=1, shadow=None, identity=None):
    return Segment(role="partition", dest_offset=dest, size=size, source_kind=kind,
                   source_offset=source_offset, shadow_index=shadow, partition_index=index,
                   type_guid=BASIC_GUID,
                   part_guid="aaaaaaaa-bbbb-cccc-dddd-00000000000%d" % index,
                   name="p%d" % index, attributes=0,
                   source_identity=dict(identity or {}))


def _plan(out, segments, capacity=CAPACITY):
    return Plan(source_disk=99, source_device=r"\\.\FAKE", source_capacity=capacity,
                source_disk_guid=DISK_GUID, target_path=out, target_capacity=capacity,
                target_disk_guid=DISK_GUID, disk_signature=0xDEADBEEF,
                pmbr_end_lba=0xFFFFFFFF, segments=segments,
                source_identity=dict(DISK_IDENTITY))


@NT
class ResumeExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2v-resume-")
        self._orig = {name: getattr(E, name) for name in
                      ("open_physical_drive", "open_shadow", "disk_identity",
                       "shadow_identity")}

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(E, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 脚手架 -----------------------------------------------------------
    def _out(self, case="case"):
        """每个用例独占子目录、产物一律叫 out.vmdk。

        descriptor 里嵌了文件名（RW ... SPARSE "<basename>"），所以逐字节比对
        必须让两侧的 basename 相同。
        """
        directory = os.path.join(self.tmp, case)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, "out.vmdk")

    def _install(self, data, shadow=None, disk=None, shadow_identity=None):
        E.open_physical_drive = lambda *a, **k: FakeDevice(data)
        E.disk_identity = lambda *a, **k: dict(disk or DISK_IDENTITY)
        E.shadow_identity = lambda index: dict(shadow_identity or SHADOW_IDENTITY)
        if shadow is not None:
            E.open_shadow = lambda index, size, **k: FakeDevice(shadow)

    def _interrupt_at(self, plan, stop_at, chunk_mib=1, checkpoint_mib=1):
        state = {"fired": False}

        def progress(seg, copied):
            if not state["fired"] and copied >= stop_at:
                state["fired"] = True
                raise Interrupted("simulated power loss at %d" % copied)

        with self.assertRaises(Interrupted):
            E.run_export(plan, apply=True, chunk_mib=chunk_mib,
                         checkpoint_mib=checkpoint_mib, progress=progress)
        self.assertTrue(state["fired"], "中断点从未触发")

    # -- 主回归 -----------------------------------------------------------
    def test_resume_from_every_checkpoint_boundary(self):
        """每个检查点边界各中断一次，续传产物必须与一次性导出逐字节相同。"""
        source = pattern_bytes(6 * MIB)
        self._install(source)

        baseline = self._out("baseline")
        E.run_export(_plan(baseline, [_segment(MIB, 6 * MIB, "physical")]),
                     apply=True, chunk_mib=1, checkpoint_mib=1)
        self.assertFalse(os.path.exists(R.sidecar_path(baseline)),
                         "成功导出后检查点必须被清掉")
        with open(baseline, "rb") as handle:
            want = handle.read()

        for stop_at in range(MIB, 6 * MIB + 1, MIB):
            with self.subTest(stop_at=stop_at):
                out = self._out("resume-%d" % stop_at)
                self._interrupt_at(_plan(out, [_segment(MIB, 6 * MIB, "physical")]), stop_at)
                self.assertTrue(os.path.exists(R.sidecar_path(out)))

                result = E.run_export(_plan(out, [_segment(MIB, 6 * MIB, "physical")]),
                                      apply=True, chunk_mib=1, checkpoint_mib=1, resume=True)
                self.assertTrue(result["resumed"])
                with open(out, "rb") as handle:
                    self.assertEqual(handle.read(), want)
                self.assertTrue(verify_vmdk(out)["ok"])
                self.assertFalse(os.path.exists(R.sidecar_path(out)))

    def test_resume_mid_segment_with_finer_checkpoints(self):
        """检查点比 chunk 更密时（每 1 MiB 数据一次），中断点落在中间也要正确。"""
        source = pattern_bytes(6 * MIB)
        self._install(source)

        baseline = self._out("fine-baseline")
        E.run_export(_plan(baseline, [_segment(MIB, 6 * MIB, "physical")]),
                     apply=True, chunk_mib=1, checkpoint_mib=1)
        with open(baseline, "rb") as handle:
            want = handle.read()

        out = self._out("fine")
        self._interrupt_at(_plan(out, [_segment(MIB, 6 * MIB, "physical")]), 3 * MIB + MIB // 2)
        E.run_export(_plan(out, [_segment(MIB, 6 * MIB, "physical")]),
                     apply=True, chunk_mib=4, checkpoint_mib=1, resume=True)
        with open(out, "rb") as handle:
            self.assertEqual(handle.read(), want)

    def test_resume_shadow_source_pulls_back_to_original_snapshot(self):
        """本次 plan 选到了更新的快照时，续传必须拉回当初那个快照，而不是混用。"""
        snapshot = pattern_bytes(4 * MIB)
        self._install(b"\x00" * CAPACITY, shadow=snapshot)
        segment = _segment(MIB, 4 * MIB, "shadow", shadow=7, identity=SHADOW_IDENTITY)

        out = self._out("shadow")
        self._interrupt_at(_plan(out, [segment]), 2 * MIB)

        # 第二天重新 plan：卷上多了一个更新的快照（序号 8），auto 会选它
        newer = dict(SHADOW_IDENTITY)
        newer.update({"shadow_index": 8, "id": "{99999999-8888-7777-6666-555555555555}"})
        E.shadow_identity = lambda index: dict(SHADOW_IDENTITY if index == 7 else newer)
        plan2 = _plan(out, [_segment(MIB, 4 * MIB, "shadow", shadow=8, identity=newer)])

        result = E.run_export(plan2, apply=True, chunk_mib=1, checkpoint_mib=1, resume=True)
        self.assertTrue(result["resumed"])
        self.assertEqual(plan2.segments[-1].shadow_index, 7, "必须回到原始快照")
        with SparseVmdkReader(out) as reader:
            self.assertEqual(reader.read_at(MIB, 4 * MIB), snapshot)

    # -- 拒绝语义 ---------------------------------------------------------
    def test_missing_checkpoint_is_refused(self):
        self._install(b"\x00" * CAPACITY)
        out = self._out("nocheck")
        with open(out, "wb") as handle:
            handle.write(b"x" * 4096)
        with self.assertRaises(R.ResumeError):
            E.run_export(_plan(out, [_segment(MIB, MIB, "physical")]),
                         apply=True, resume=True)

    def test_changed_plan_is_refused(self):
        source = pattern_bytes(4 * MIB)
        self._install(source)
        out = self._out("changed")
        self._interrupt_at(_plan(out, [_segment(MIB, 4 * MIB, "physical")]), 2 * MIB)

        # 同样的输出路径，但多选了一段（--take 变了）=> 指纹不同
        plan2 = _plan(out, [_segment(MIB, 4 * MIB, "physical"),
                            _segment(8 * MIB, MIB, "physical", index=2)])
        with self.assertRaises(PlanError) as ctx:
            E.run_export(plan2, apply=True, resume=True)
        self.assertIn("计划与检查点不匹配", str(ctx.exception))

    def test_writable_source_is_refused(self):
        """离线盘的只读状态丢失（中断期间被系统挂载写入）必须拒绝续传。"""
        source = pattern_bytes(4 * MIB)
        self._install(source)
        out = self._out("writable")
        self._interrupt_at(_plan(out, [_segment(MIB, 4 * MIB, "physical")]), 2 * MIB)

        writable = dict(DISK_IDENTITY)
        writable["is_read_only"] = False
        E.disk_identity = lambda *a, **k: dict(writable)
        with self.assertRaises(PlanError) as ctx:
            E.run_export(_plan(out, [_segment(MIB, 4 * MIB, "physical")]),
                         apply=True, resume=True)
        self.assertIn("只读", str(ctx.exception))

    def test_different_disk_is_refused(self):
        source = pattern_bytes(4 * MIB)
        self._install(source)
        out = self._out("otherdisk")
        self._interrupt_at(_plan(out, [_segment(MIB, 4 * MIB, "physical")]), 2 * MIB)

        other = dict(DISK_IDENTITY)
        other["serial_number"] = "OTHERSERIAL99"
        E.disk_identity = lambda *a, **k: other
        with self.assertRaises(PlanError) as ctx:
            E.run_export(_plan(out, [_segment(MIB, 4 * MIB, "physical")]),
                         apply=True, resume=True)
        self.assertIn("源身份与检查点不符", str(ctx.exception))

    def test_snapshot_that_vanished_is_refused(self):
        snapshot = pattern_bytes(4 * MIB)
        self._install(b"\x00" * CAPACITY, shadow=snapshot)
        out = self._out("gonshadow")
        segment = _segment(MIB, 4 * MIB, "shadow", shadow=7, identity=SHADOW_IDENTITY)
        self._interrupt_at(_plan(out, [segment]), 2 * MIB)

        E.shadow_identity = lambda index: {}          # 快照被系统回收
        with self.assertRaises(PlanError) as ctx:
            E.run_export(_plan(out, [_segment(MIB, 4 * MIB, "shadow", shadow=7,
                                              identity=SHADOW_IDENTITY)]),
                         apply=True, resume=True)
        self.assertIn("无法续用", str(ctx.exception))

    def test_verify_flags_unfinalized_half_product(self):
        """半成品结构合法，但必须被 verify 认出来，而不是当成可用产物。"""
        self._install(b"\x00" * CAPACITY)
        out = self._out("half")
        self._interrupt_at(_plan(out, [_segment(MIB, 2 * MIB, "physical")]), MIB)

        result = verify_vmdk(out)
        self.assertFalse(result["ok"])
        checks = {item["name"]: item["ok"] for item in result["checks"]}
        self.assertIn("vmdk_completed", checks)
        self.assertFalse(checks["vmdk_completed"])
        self.assertTrue(checks["gpt_primary"], "GPT 应该已经落盘且结构完好")

    def test_finalized_artifact_is_not_resumable(self):
        """sidecar 删掉前崩溃的窗口：产物已经 finalize，不能再续。"""
        source = pattern_bytes(2 * MIB)
        self._install(source)
        out = self._out("done")
        plan = _plan(out, [_segment(MIB, 2 * MIB, "physical")])
        E.run_export(plan, apply=True, chunk_mib=1, checkpoint_mib=1)

        state = R.ResumeState(
            fingerprint=plan.fingerprint(), source_identity=dict(DISK_IDENTITY),
            target_path=out, target_capacity=CAPACITY,
            capacity_sectors=CAPACITY // 512, grain_sectors=GRAIN_SECTORS,
            overhead=0, chunk_mib=1)
        state.save()

        with self.assertRaises(PlanError) as ctx:
            E.run_export(_plan(out, [_segment(MIB, 2 * MIB, "physical")]),
                         apply=True, resume=True)
        self.assertIn("已经 finalize", str(ctx.exception))


@NT
class ResumeWriterTest(unittest.TestCase):
    """writer 层的回退与重开（不经过 export）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2v-writer-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self):
        return os.path.join(self.tmp, "w.vmdk")

    def test_rewind_drops_grain_written_after_watermark(self):
        path = self._path()
        watermark = {}

        def run():
            with SparseVmdkWriter(path, CAPACITY) as writer:
                writer.write_at(0, b"A" * GRAIN)
                writer.write_at(GRAIN, b"B" * GRAIN)
                writer.checkpoint()
                watermark["sector"] = writer.next_free_sector
                writer.write_at(2 * GRAIN, b"C" * GRAIN)   # 检查点之后才分配的
                writer.checkpoint()                        # GT 领先于水位
                raise Interrupted("simulated")

        with self.assertRaises(Interrupted):
            run()

        with SparseVmdkWriter(path, CAPACITY, resume=True) as writer:
            self.assertEqual(writer.allocated_grains, 3)
            self.assertTrue(writer.unclean)
            dropped = writer.rewind(watermark["sector"])
            self.assertEqual(dropped, 1)
            self.assertEqual(writer.allocated_grains, 2)
            self.assertEqual(writer.next_free_sector, watermark["sector"])

        with SparseVmdkReader(path) as reader:
            self.assertNotEqual(reader.grain_sector(0), 0)
            self.assertNotEqual(reader.grain_sector(1), 0)
            self.assertEqual(reader.grain_sector(2), 0, "水位之后的分配必须被清掉")

    def test_resume_refuses_file_without_header(self):
        path = self._path()
        with open(path, "wb") as handle:
            handle.write(b"not a vmdk" * 100)
        with self.assertRaises(Exception) as ctx:
            with SparseVmdkWriter(path, CAPACITY, resume=True):
                pass
        self.assertIn("没有 vmdk header", str(ctx.exception))

    def test_resume_refuses_capacity_mismatch(self):
        path = self._path()
        with SparseVmdkWriter(path, CAPACITY) as writer:
            writer.write_at(0, b"A" * GRAIN)
            writer.checkpoint()
        with self.assertRaises(Exception) as ctx:
            with SparseVmdkWriter(path, 64 * MIB, resume=True):
                pass
        self.assertIn("容量", str(ctx.exception))

    def test_missing_target_is_refused(self):
        with self.assertRaises(Exception) as ctx:
            with SparseVmdkWriter(self._path(), CAPACITY, resume=True):
                pass
        self.assertIn("resume 需要已存在的半成品", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
