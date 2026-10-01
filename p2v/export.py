# SPDX-License-Identifier: GPL-3.0-only
"""按 plan 执行导出（含中断续传）。

默认 dry-run：只有 apply=True 才创建目标文件。
源设备全程只读；目标已存在时——不带 resume 一律拒绝，带 resume 则从检查点续写。

续传的四条正确性前提（缺一不可，任一不成立都直接报错）
----------------------------------------------------
1. 计划未变 —— plan.fingerprint() 与检查点一致（--take / 容量 / 分区 GUID）
2. 源未变   —— 盘身份（序列号 / UniqueId / 容量）一致；物理源必须整盘只读
3. 快照未变 —— 快照源必须仍是同一个 Shadow Copy ID（序号会被系统复用）
4. 产物配对 —— 水位不能领先于 GT 表，且产物必须处于未完成态（uncleanShutdown=1）

**绝不静默回落**：前 30% 来自快照 A、后 70% 来自物理盘，这种"看起来成功"比
重新导出一遍糟糕得多。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .gpt import Partition, build_gpt
from .plan import Plan, PlanError, disk_identity, shadow_identity
from .resume import ResumeState, identity_mismatch, shadow_mismatch
from .resume import remove as remove_sidecar
from .safeio import DEFAULT_SECTOR, open_physical_drive, open_shadow
from .vmdk import GRAIN_SECTORS, SparseVmdkReader, SparseVmdkWriter

SECTOR = DEFAULT_SECTOR
GRAIN_BYTES = GRAIN_SECTORS * SECTOR

MIB = 1024 * 1024
DEFAULT_CHUNK_MIB = 4
MIN_CHUNK_MIB = 1
MAX_CHUNK_MIB = 256

DEFAULT_CHECKPOINT_MIB = 256
MIN_CHECKPOINT_MIB = 1
MAX_CHECKPOINT_MIB = 8192


def resolve_chunk_bytes(chunk_mib=None) -> int:
    """把 --chunk-mib 解析为字节数并校验边界。块越大，Python 层循环与 grain 切分次数越少。"""
    mib = DEFAULT_CHUNK_MIB if chunk_mib is None else int(chunk_mib)
    if not (MIN_CHUNK_MIB <= mib <= MAX_CHUNK_MIB):
        raise PlanError("chunk size must be within %d..%d MiB, got %d"
                        % (MIN_CHUNK_MIB, MAX_CHUNK_MIB, mib))
    return mib * MIB


def resolve_checkpoint_bytes(checkpoint_mib=None) -> int:
    """把 --checkpoint-mib 解析为字节数。

    检查点要写一次 header + GD/RGD（1 TiB 计划约 256 KiB）加若干脏 GT，
    间隔太小会白白增加 fsync 次数，太大则崩溃后重做得多。
    """
    mib = DEFAULT_CHECKPOINT_MIB if checkpoint_mib is None else int(checkpoint_mib)
    if not (MIN_CHECKPOINT_MIB <= mib <= MAX_CHECKPOINT_MIB):
        raise PlanError("checkpoint interval must be within %d..%d MiB, got %d"
                        % (MIN_CHECKPOINT_MIB, MAX_CHECKPOINT_MIB, mib))
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
    "产物已导出。能否直接开机，取决于源机 ESP 上 \\EFI\\Microsoft\\Boot\\BCD 的大小：",
    "  36864 = 出厂原始 hive                -> 首次开机报 0xc000000e",
    "                                        （File: \\Windows\\system32\\winload.efi），需要进 PE 修引导",
    "  40960 = 已被 bcdboot/Dism++ 重写过    -> 可直接开机（2026-10-01 实测，未进 PE）",
    "需要修引导时（PE 内）：Dism++ → 引导修复，或 bcdboot C:\\Windows /s <ESP盘符>: /f UEFI",
    "删除后装驱动是可选的稳定性措施：它与 0xc000000e 无关（失败发生在 winload 阶段，轮不到驱动）",
    "下一步：python -m p2v vmx --vmdk <产物路径> 生成 VMware 的 .vmx，然后双击启动",
    "参考： Notes\\env\\env-vmware-p2v.md（黑屏 / 0xc000000e / BCD 判据）",
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


def _prepare_resume(plan: Plan, data_segments: list) -> ResumeState:
    """校验检查点并把 plan 的源选择拉回"当初那个源"。

    注意本函数会**就地修改** plan 的 partition 段：快照源会被改回检查点记录的
    那个 shadow_index（本次 plan 可能已经选到了更新的快照，续传不能将就它）。

    任何不一致都抛 PlanError —— 这里没有"尽力而为"的选项。
    """
    if not os.path.exists(plan.target_path):
        raise PlanError("--resume 需要已存在的半成品，但找不到目标文件：%s"
                        % plan.target_path)
    state = ResumeState.load(plan.target_path)

    if state.fingerprint != plan.fingerprint():
        raise PlanError(
            "计划与检查点不匹配（--take / 容量 / 分区 GUID 有变化），拒绝续传；"
            "确认不再需要这份半成品后删除 %s 重跑" % plan.target_path)
    if int(state.target_capacity) != int(plan.target_capacity):
        raise PlanError("检查点容量 %d 与本次计划 %d 不一致"
                        % (state.target_capacity, plan.target_capacity))

    with SparseVmdkReader(plan.target_path) as reader:
        if not reader.unclean:
            raise PlanError(
                "检查点对应的产物已经 finalize（uncleanShutdown=0），无需续传；"
                "要重新导出请先删除 %s" % plan.target_path)
        if reader.size != int(state.target_capacity):
            raise PlanError("产物虚拟容量 %d 与检查点 %d 不一致，拒绝续传"
                            % (reader.size, state.target_capacity))

    current_disk = disk_identity(plan.source_disk)
    trouble = identity_mismatch(state.source_identity, current_disk)
    if trouble:
        raise PlanError("源身份与检查点不符：%s" % trouble)
    if (any(seg.source_kind == "physical" for seg in data_segments)
            and not current_disk.get("is_read_only")):
        raise PlanError(
            "续传要求源盘处于只读状态：两次运行之间任何写入都会让前后两段来自不同时刻。"
            "请先执行  Set-Disk -Number %d -IsReadOnly $true  再重试" % plan.source_disk)

    for index, seg in enumerate(data_segments):
        if seg.source_kind != "shadow":
            continue
        recorded = (state.segment_identities[index]
                    if index < len(state.segment_identities) else {})
        if not recorded:
            raise PlanError("检查点缺少 partition #%s 的快照身份，无法确认续用的是同一个快照"
                            % seg.partition_index)
        current = shadow_identity(int(recorded.get("shadow_index") or 0))
        trouble = shadow_mismatch(recorded, current)
        if trouble:
            raise PlanError("partition #%s 的卷影副本无法续用：%s"
                            % (seg.partition_index, trouble))
        # 拉回当初那个快照：本次 plan 选到的可能是更新的一个
        seg.shadow_index = int(recorded["shadow_index"])
        seg.source_identity = dict(recorded)
    return state


def run_export(plan: Plan, apply: bool = False, progress=None, chunk_mib=None,
               resume: bool = False, checkpoint_mib=None) -> dict:
    """执行导出。apply=False 时仅预检并返回将要做的动作。

    chunk_mib: 读写块大小（MiB）。调大可减少 Python 层循环与 grain 切分次数。
    resume:    续用已存在的半成品（必须同时有 sidecar 检查点）。
    checkpoint_mib: 每提交这么多数据落一次检查点（默认 %d MiB）。崩溃后最多重做这一份。
    """ % DEFAULT_CHECKPOINT_MIB
    chunk_bytes = resolve_chunk_bytes(chunk_mib)
    checkpoint_bytes = resolve_checkpoint_bytes(checkpoint_mib)
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
            "checkpoint_mib": checkpoint_bytes // MIB,
            "resume": bool(resume),
            "message": "dry-run only; pass apply=True (CLI --apply) to write",
            "manual_steps": MANUAL_POST_STEPS,
        }

    state = None
    if resume:
        state = _prepare_resume(plan, data_segments)
    elif os.path.exists(plan.target_path):
        raise PlanError("refusing to overwrite existing target: %s" % plan.target_path)
    parent = os.path.dirname(os.path.abspath(plan.target_path))
    if parent and not os.path.isdir(parent):
        raise PlanError("target directory does not exist: %s" % parent)

    started = time.time()
    warnings: list = []
    if state is not None and state.chunk_mib and state.chunk_mib != chunk_bytes // MIB:
        warnings.append("本次 --chunk-mib %d 与首次导出的 %d 不同；分块边界变化不影响正确性"
                        "（grain 落点由内容与段顺序决定）"
                        % (chunk_bytes // MIB, state.chunk_mib))

    with SparseVmdkWriter(plan.target_path, plan.target_capacity, resume=resume) as w:
        if state is None:
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

            state = ResumeState(
                fingerprint=plan.fingerprint(),
                source_identity=disk_identity(plan.source_disk),
                target_path=plan.target_path,
                target_capacity=plan.target_capacity,
                capacity_sectors=w.capacity_sectors,
                grain_sectors=GRAIN_SECTORS,
                overhead=w.overhead,
                chunk_mib=chunk_bytes // MIB,
                segment_identities=[dict(seg.source_identity) for seg in data_segments],
            )
            # 初始水位必须如实记录：GPT 元数据（MBR / 主备表）已经占用了低地址
            # 的 grain，漏记会让续传的回退把 grain 0 一起清掉（那份 GPT 就没了）。
            _save_watermark(state, w, 0, 0)
            # 先让文件自描述（header + GD/RGD），再记水位：顺序颠倒的话，
            # 第一次数据检查点之前崩溃会留下"有水位但文件没有 header"的死局。
            w.checkpoint()
            state.save()
        else:
            dropped = w.rewind(state.next_free_sector)
            if w.allocated_grains != state.allocated_grains:
                raise PlanError(
                    "半成品的分配计数 %d 与检查点 %d 不一致，文件可能被外部改动过，拒绝续传"
                    % (w.allocated_grains, state.allocated_grains))
            if dropped:
                warnings.append("回退检查点之后的 %d 个 grain 分配（崩溃窗口内的残留，"
                                "会被重新写入覆盖）" % dropped)
            w.checkpoint()

        physical = open_physical_drive(plan.source_disk)
        shadows: dict = {}
        try:
            for seg_index, seg in enumerate(data_segments):
                if seg_index < state.segment_index:
                    continue
                start = state.offset_in_segment if seg_index == state.segment_index else 0
                if start >= seg.size:
                    continue
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

                copied = start
                pending = 0
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
                    pending += len(buf)
                    if progress:
                        progress(seg, copied)
                    if pending >= checkpoint_bytes:
                        pending = 0
                        _save_watermark(state, w, seg_index, copied)
                        w.checkpoint()
                        state.save()
                # 段结束也落一次：整段短于检查点间隔时水位同样必须推进
                _save_watermark(state, w, seg_index + 1, 0)
                w.checkpoint()
                state.save()
                if padded:
                    warnings.append(
                        "partition %s: 卷影副本可读长度比分区少 %d 字节，已零填充（thin 不占空间）"
                        % (seg.partition_index, padded))
        finally:
            physical.close()
            for dev in shadows.values():
                dev.close()
        stats = w.stats()

    # 产物已 finalize，检查点失去意义
    remove_sidecar(plan.target_path)

    elapsed = time.time() - started
    return {
        "ok": True,
        "dry_run": False,
        "resumed": bool(resume),
        "target_path": plan.target_path,
        "file_bytes": os.path.getsize(plan.target_path),
        "file_gib": round(os.path.getsize(plan.target_path) / 1024 ** 3, 2),
        "allocated_grains": stats["allocated_grains"],
        "allocated_bytes": stats["allocated_bytes"],
        "elapsed_sec": round(elapsed, 1),
        "throughput_mb_s": round(total_bytes / 1024 ** 2 / max(elapsed, 1e-6), 1),
        "chunk_mib": chunk_bytes // MIB,
        "checkpoint_mib": checkpoint_bytes // MIB,
        "warnings": warnings,
        "manual_steps": MANUAL_POST_STEPS,
    }


def _save_watermark(state: ResumeState, writer, segment_index: int, offset: int) -> None:
    """把"已完整提交到哪"写进检查点对象（落盘由调用方随后执行）。"""
    state.segment_index = int(segment_index)
    state.offset_in_segment = int(offset)
    state.allocated_grains = writer.allocated_grains
    state.next_free_sector = writer.next_free_sector
