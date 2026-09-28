"""导出计划：把"复制什么、放到哪"显式化成可审阅的数据结构。

plan 阶段**纯只读**，不创建任何文件；执行阶段（export）才落盘。
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass, field

from .gpt import (
    TYPE_BASIC, TYPE_ESP, TYPE_MSR, GptDisk, Partition, build_gpt,
    build_protective_mbr, parse_gpt,
)
from .safeio import DEFAULT_SECTOR as SECTOR, open_physical_drive

PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


class PlanError(RuntimeError):
    pass


@dataclass
class Segment:
    role: str                 # mbr | gpt-primary | gpt-entries | gpt-backup-entries | gpt-backup | partition
    dest_offset: int
    size: int
    source_kind: str          # generated | physical | shadow
    source_offset: int = 0
    shadow_index: int | None = None
    partition_index: int | None = None
    type_guid: str = ""
    part_guid: str = ""
    name: str = ""


@dataclass
class Plan:
    source_disk: int
    source_device: str
    source_capacity: int
    source_disk_guid: str
    target_path: str
    target_capacity: int
    target_disk_guid: str
    segments: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_disk": self.source_disk,
            "source_device": self.source_device,
            "source_capacity": self.source_capacity,
            "source_capacity_gib": round(self.source_capacity / 1024 ** 3, 2),
            "source_disk_guid": self.source_disk_guid,
            "target_path": self.target_path,
            "target_capacity": self.target_capacity,
            "target_capacity_gib": round(self.target_capacity / 1024 ** 3, 2),
            "target_disk_guid": self.target_disk_guid,
            "segments": [asdict(s) for s in self.segments],
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 只读探测辅助
# ---------------------------------------------------------------------------

def _ps_json(script: str):
    proc = subprocess.run(PS + [script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise PlanError("powershell failed: %s" % (proc.stderr or "").strip()[:300])
    text = (proc.stdout or "").strip()
    return json.loads(text) if text else None


def partition_for_volume(letter: str) -> dict:
    """查盘符对应的分区（只读）。返回 {disk, partition, guid}。"""
    lt = letter.rstrip(":\\")
    data = _ps_json(
        "Get-Partition -DriveLetter %s | Select-Object DiskNumber,PartitionNumber,Guid | "
        "ConvertTo-Json -Compress" % lt
    )
    if not data:
        raise PlanError("cannot resolve volume %s:" % letter)
    return {
        "disk": int(data.get("DiskNumber", -1)),
        "partition": int(data.get("PartitionNumber", -1)),
        "guid": str(data.get("Guid", "")).strip("{}").lower(),
    }


def latest_shadow_index_for_volume(letter: str) -> int | None:
    """取该卷最新的可用卷影副本序号（只读枚举，不创建）。"""
    from .vss import list_shadows, volume_size_bytes  # 局部导入避免环依赖

    lt = letter.rstrip(":\\")
    vol_guid = _ps_json(
        "Get-Volume -DriveLetter %s | Select-Object -ExpandProperty UniqueId | "
        "ConvertTo-Json -Compress" % lt
    )
    shadows = [s for s in list_shadows() if s.get("shadow_index") is not None]
    if not shadows:
        return None
    if vol_guid:
        want = str(vol_guid).strip()
        same = [s for s in shadows if s.get("volume", "").rstrip("\\").endswith(want.strip("\\"))]
        if same:
            shadows = same
    shadows.sort(key=lambda s: str(s.get("install_date") or ""))
    return shadows[-1]["shadow_index"]


# ---------------------------------------------------------------------------
# 计划构建
# ---------------------------------------------------------------------------

def resolve_selector(gpt: GptDisk, selector: str) -> Partition:
    """选择器 -> 源盘分区。

    支持：ESP / MSR / part:N / vol:C:
    """
    sel = selector.strip()
    upper = sel.upper()
    if upper == "ESP":
        for p in gpt.partitions:
            if p.type_guid == TYPE_ESP:
                return p
        raise PlanError("no ESP partition found")
    if upper == "MSR":
        for p in gpt.partitions:
            if p.type_guid == TYPE_MSR:
                return p
        raise PlanError("no MSR partition found")
    if sel.lower().startswith("part:"):
        want = int(sel.split(":", 1)[1])
        for p in gpt.partitions:
            if p.index == want:
                return p
        raise PlanError("no partition #%d" % want)
    if sel.lower().startswith("vol:"):
        letter = sel.split(":", 1)[1]
        info = partition_for_volume(letter)
        for p in gpt.partitions:
            if p.index == info["partition"]:
                return p
        raise PlanError("volume %s maps to partition #%d which is not in this disk's GPT"
                        % (letter, info["partition"]))
    raise PlanError("unsupported selector: %r (use ESP / MSR / part:N / vol:C:)" % selector)


def build_plan(disk: int, take: list, out_path: str, sector_size: int = SECTOR,
               keep_disk_guid: bool = True) -> Plan:
    """只读探测并生成计划。

    目标盘容量 = 源盘容量；被选中的分区**沿用原偏移与 GUID**（这样 BCD 与
    MountedDevices 的引用天然继续有效，不会再出现 C: -> V: 的盘符错乱）。
    未被选中的分区在目标盘上留作未分配空间（thin vmdk 下不占空间）。
    """
    with open_physical_drive(disk, sector_size) as dev:
        gpt = parse_gpt(dev, sector_size)
        source_capacity = dev.size
        source_device = dev.path
        if not (gpt.header_crc_ok and gpt.entries_crc_ok):
            raise PlanError("source GPT failed CRC self-check; refusing to plan")

    picked: list = []
    for sel in take:
        part = resolve_selector(gpt, sel)
        if part not in picked:
            picked.append(part)
    if not picked:
        raise PlanError("nothing selected")

    # 目标盘的 GPT：沿用源盘 disk GUID（或新生成）+ 选中分区的原 GUID/偏移
    target_disk_guid = gpt.disk_guid if keep_disk_guid else uuid.uuid4()
    target_parts = [
        Partition(p.index, p.type_guid, p.part_guid, p.first_lba, p.last_lba,
                  p.attributes, p.name, sector_size)
        for p in picked
    ]
    cap_sectors = source_capacity // sector_size
    built = build_gpt(cap_sectors, target_disk_guid, target_parts, sector_size)

    plan = Plan(
        source_disk=disk,
        source_device=source_device,
        source_capacity=source_capacity,
        source_disk_guid=str(gpt.disk_guid),
        target_path=out_path,
        target_capacity=source_capacity,
        target_disk_guid=str(target_disk_guid),
    )

    plan.segments.append(Segment("mbr", 0, sector_size, "generated"))
    plan.segments.append(Segment("gpt-primary", 1 * sector_size, 92, "generated"))
    layout = built["layout"]
    plan.segments.append(Segment("gpt-entries", layout["entries_lba"] * sector_size,
                                 layout["entries_sectors"] * sector_size, "generated"))
    plan.segments.append(Segment("gpt-backup-entries",
                                 layout["backup_entries_lba"] * sector_size,
                                 layout["entries_sectors"] * sector_size, "generated"))
    plan.segments.append(Segment("gpt-backup", layout["alt_lba"] * sector_size,
                                 sector_size, "generated"))

    for p in picked:
        source_kind = "physical"
        shadow_index = None
        source_offset = p.offset
        if p.type_guid == TYPE_BASIC:
            # 系统/数据卷：优先用 VSS 快照保证一致性
            idx = None
            try:
                info = _ps_json(
                    "Get-Partition -DiskNumber %d -PartitionNumber %d | "
                    "Select-Object -ExpandProperty DriveLetter | ConvertTo-Json -Compress"
                    % (disk, p.index)
                )
                if info:
                    idx = latest_shadow_index_for_volume(str(info))
            except Exception:
                idx = None
            if idx is not None:
                source_kind = "shadow"
                shadow_index = idx
                source_offset = 0
                plan.notes.append(
                    "partition #%d (%s) 将从卷影副本 %d 读取以保证一致性"
                    % (p.index, p.name or p.type_name, idx))
            else:
                plan.notes.append(
                    "partition #%d 未找到可用卷影副本，将直读物理盘（crash-consistent）"
                    % p.index)

        plan.segments.append(Segment(
            role="partition",
            dest_offset=p.offset,
            size=p.size_bytes,
            source_kind=source_kind,
            source_offset=source_offset,
            shadow_index=shadow_index,
            partition_index=p.index,
            type_guid=str(p.type_guid),
            part_guid=str(p.part_guid),
            name=p.name,
        ))

    plan.notes.append("目标盘容量与源盘一致；未选中的分区保留为未分配空间（thin 不占空间）")
    plan.notes.append("选中分区沿用原 GUID 与偏移，BCD / MountedDevices 引用无需改动")
    plan.notes.append("如需把系统分区扩展到整盘，导出后在 PE 里用 diskpart extend（NTFS 同步扩容）")
    return plan
