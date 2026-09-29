# SPDX-License-Identifier: GPL-3.0-only
"""按 plan 执行导出。

默认 dry-run：只有 apply=True 才创建目标文件。
源设备全程只读；目标已存在一律拒绝。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .gpt import Partition, build_gpt
from .plan import Plan, PlanError, Segment
from .safeio import DEFAULT_SECTOR, open_physical_drive, open_shadow
from .vmdk import SparseVmdkWriter

SECTOR = DEFAULT_SECTOR

MIB = 1024 * 1024
DEFAULT_CHUNK_MIB = 4
MIN_CHUNK_MIB = 1
MAX_CHUNK_MIB = 256


def resolve_chunk_bytes(chunk_mib=None) -> int:
    """把 --chunk-mib 解析为字节数并校验边界。块越大，Python 层循环与 grain 切分次数越少。"""
    mib = DEFAULT_CHUNK_MIB if chunk_mib is None else int(chunk_mib)
    if not (MIN_CHUNK_MIB <= mib <= MAX_CHUNK_MIB):
        raise PlanError("chunk size must be within %d..%d MiB, got %d"
                        % (MIN_CHUNK_MIB, MAX_CHUNK_MIB, mib))
    return mib * MIB


def _readable_length(dev, declared: int) -> int:
    """探测源设备实际可读上限。

    VSS 卷影副本只覆盖文件系统部分：例如 C 分区是 137440002048 字节，
    但快照可读长度只有 137438953472（= 卷大小 137439997952 按 1 MiB 对齐）。
    直接按分区大小读会在末尾撞短读。这里从 declared 按 1 MiB 向下回退探测。
    """
    end = (declared // MIB) * MIB
    while end > 0:
        try:
            dev.read_at(end - DEFAULT_SECTOR, DEFAULT_SECTOR)
            return end
        except Exception:
            end -= MIB
    return 0


MANUAL_POST_STEPS = [
    "产物已导出，但还不能直接开机：本工具只做拷贝，不做引导修复与驱动清理。",
    "请在 PE 里用 Dism++ 点两下完成收尾（本机已两次独立验证）：",
    "  1) Dism++ → 引导修复        # 不做会报 0xc000000e（File: \\Windows\\system32\\winload.efi）",
    "  2) Dism++ → 驱动管理 → 删除所有后装驱动（保留 in-box）",
    "                              # 不做可能因与原机硬件绑定的驱动在过引导后出问题",
    "命令行等价物（不喜欢点击时用）： bcdboot C:\\Windows /s <ESP盘符>: /f UEFI",
    "参考： Notes\\env\\env-vmware-p2v.md（黑屏 / 0xc000000e 排查）",
]


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
            attributes=seg.attributes,
            name=seg.name,
            sector_size=SECTOR,
        ))
    return parts


def run_export(plan: Plan, apply: bool = False, progress=None, chunk_mib=None) -> dict:
    """执行导出。apply=False 时仅预检并返回将要做的动作。

    chunk_mib: 读写块大小（MiB）。调大可减少 Python 层循环与 grain 切分次数；
    None 表示用 DEFAULT_CHUNK_MIB。
    """
    chunk_bytes = resolve_chunk_bytes(chunk_mib)
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
            "chunk_mib": chunk_bytes // MIB,
            "message": "dry-run only; pass apply=True (CLI --apply) to write",
            "manual_steps": MANUAL_POST_STEPS,
        }

    if os.path.exists(plan.target_path):
        raise PlanError("refusing to overwrite existing target: %s" % plan.target_path)
    parent = os.path.dirname(os.path.abspath(plan.target_path))
    if parent and not os.path.isdir(parent):
        raise PlanError("target directory does not exist: %s" % parent)

    started = time.time()
    stats = None
    warnings: list = []
    with SparseVmdkWriter(plan.target_path, plan.target_capacity) as w:
        # generated: 保护性 MBR + 主备 GPT（沿用源盘 disk GUID 与分区 GUID）
        parts = _partitions_from_plan(plan)
        built = build_gpt(plan.target_capacity // SECTOR,
                          uuid.UUID(plan.target_disk_guid), parts, SECTOR,
                          disk_signature=plan.disk_signature,
                          pmbr_end_lba=plan.pmbr_end_lba)
        layout = built["layout"]
        w.write_at(0, built["mbr"])
        w.write_at(SECTOR, built["primary_header"])
        w.write_at(layout["entries_lba"] * SECTOR, built["primary_entries"])
        w.write_at(layout["backup_entries_lba"] * SECTOR, built["backup_entries"])
        w.write_at(layout["alt_lba"] * SECTOR, built["backup_header"])

        physical = open_physical_drive(plan.source_disk)
        shadows: dict = {}
        try:
            for seg in data_segments:
                if seg.source_kind == "physical":
                    src = physical
                    src_offset = seg.source_offset
                elif seg.source_kind == "shadow":
                    if seg.shadow_index not in shadows:
                        shadows[seg.shadow_index] = open_shadow(seg.shadow_index, seg.size)
                    src = shadows[seg.shadow_index]
                    src_offset = seg.source_offset
                else:
                    raise PlanError("unknown source kind: %s" % seg.source_kind)

                # 卷影副本的可读长度可能小于分区大小，末段需补零（NTFS 尾部对齐区）
                readable = _readable_length(src, seg.size) if seg.source_kind == "shadow" else seg.size
                padded = max(0, seg.size - readable)

                copied = 0
                while copied < seg.size:
                    if copied >= readable:
                        # 卷影副本尾部补零：全零 buf 不占 grain（thin）
                        buf = b"\x00" * min(chunk_bytes, seg.size - copied)
                    else:
                        want = min(chunk_bytes, readable - copied)
                        try:
                            buf = src.read_at(src_offset + copied, want)
                        except Exception as exc:
                            raise PlanError("read failed at partition %s offset %d: %s"
                                            % (seg.partition_index, copied, exc))
                    w.write_at(seg.dest_offset + copied, buf)
                    copied += len(buf)
                    if progress:
                        progress(seg, copied)
                if padded:
                    warnings.append(
                        "partition %s: 卷影副本可读长度比分区少 %d 字节，已零填充（thin 不占空间）"
                        % (seg.partition_index, padded))
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
        "chunk_mib": chunk_bytes // MIB,
        "warnings": warnings,
        "manual_steps": MANUAL_POST_STEPS,
    }
    return out