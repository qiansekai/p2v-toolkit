"""按 plan 执行导出。

默认 dry-run：只有 apply=True 才创建目标文件。
源设备全程只读；目标已存在一律拒绝。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .gpt import ENTRY_SIZE, Partition, build_gpt, build_protective_mbr
from .plan import Plan, PlanError, Segment
from .safeio import DEFAULT_SECTOR as SECTOR, open_physical_drive, open_shadow
from .vmdk import SparseVmdkWriter

CHUNK = 4 * 1024 * 1024


def _partitions_from_plan(plan: Plan) -> list:
    parts = []
    for i, seg in enumerate(s for s in plan.segments if s.role == "partition"):
        first = seg.dest_offset // SECTOR
        count = seg.size // SECTOR
        parts.append(Partition(
            index=i + 1,
            type_guid=uuid.UUID(seg.type_guid),
            part_guid=uuid.UUID(seg.part_guid),
            first_lba=first,
            last_lba=first + count - 1,
            attributes=0,
            name=seg.name,
            sector_size=SECTOR,
        ))
    return parts


def run_export(plan: Plan, apply: bool = False, progress=None) -> dict:
    """执行导出。apply=False 时仅预检并返回将要做的动作。"""
    data_segments = [s for s in plan.segments if s.role == "partition"]
    total_bytes = sum(s.size for s in data_segments)

    if not apply:
        return {
            "ok": True,
            "dry_run": True,
            "target_path": plan.target_path,
            "target_capacity": plan.target_capacity,
            "data_segments": len(data_segments),
            "data_bytes": total_bytes,
            "data_gib": round(total_bytes / 1024 ** 3, 2),
            "message": "dry-run only; pass apply=True (CLI --apply) to write",
        }

    if os.path.exists(plan.target_path):
        raise PlanError("refusing to overwrite existing target: %s" % plan.target_path)
    parent = os.path.dirname(os.path.abspath(plan.target_path))
    if parent and not os.path.isdir(parent):
        raise PlanError("target directory does not exist: %s" % parent)

    started = time.time()
    stats = None
    with SparseVmdkWriter(plan.target_path, plan.target_capacity) as w:
        # generated: 保护性 MBR + 主备 GPT（沿用源盘 disk GUID 与分区 GUID）
        parts = _partitions_from_plan(plan)
        built = build_gpt(plan.target_capacity // SECTOR,
                          uuid.UUID(plan.target_disk_guid), parts, SECTOR)
        layout = built["layout"]
        w.write_at(0, built["mbr"])
        w.write_at(SECTOR, built["primary_header"])
        w.write_at(layout["entries_lba"] * SECTOR, built["primary_entries"])
        w.write_at(layout["backup_entries_lba"] * SECTOR, built["backup_entries"])
        w.write_at(layout["alt_lba"] * SECTOR, built["backup_header"])

        physical = open_physical_drive(plan.source_disk, SECTOR)
        shadows: dict = {}
        try:
            for seg in data_segments:
                if seg.source_kind == "physical":
                    src = physical
                    src_offset = seg.source_offset
                elif seg.source_kind == "shadow":
                    if seg.shadow_index not in shadows:
                        shadows[seg.shadow_index] = open_shadow(seg.shadow_index, seg.size, SECTOR)
                    src = shadows[seg.shadow_index]
                    src_offset = seg.source_offset
                else:
                    raise PlanError("unknown source kind: %s" % seg.source_kind)

                copied = 0
                while copied < seg.size:
                    take = min(CHUNK, seg.size - copied)
                    try:
                        chunk = src.read_at(src_offset + copied, take)
                    except Exception as exc:
                        raise PlanError("read failed at partition %s offset %d: %s"
                                        % (seg.partition_index, copied, exc))
                    w.write_at(seg.dest_offset + copied, chunk)
                    copied += take
                    if progress:
                        progress(seg, copied)
        finally:
            physical.close()
            for dev in shadows.values():
                dev.close()
        stats = w.stats()

    elapsed = time.time() - started
    out = {
        "ok": True,
        "dry_run": False,
        "target_path": plan.target_path,
        "file_bytes": os.path.getsize(plan.target_path),
        "file_gib": round(os.path.getsize(plan.target_path) / 1024 ** 3, 2),
        "allocated_grains": stats["allocated_grains"],
        "allocated_bytes": stats["allocated_bytes"],
        "elapsed_sec": round(elapsed, 1),
        "throughput_mb_s": round(total_bytes / 1024 ** 2 / max(elapsed, 1e-6), 1),
    }
    return out
