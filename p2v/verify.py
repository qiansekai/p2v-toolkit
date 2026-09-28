"""产物自检：直接解析 vmdk 内部结构，不依赖外部工具。

校验项：
  1. vmdk header 合法（magic / version / geometry，由 reader 构造时把关）
  2. 主 GPT：签名 + header CRC + partition array CRC
  3. 备份 GPT：签名 + CRC（UEFI 会交叉校验主备）
  4. 与源盘对比（可选 --source-disk）：分区 GUID 集合、数据采样一致性
"""

from __future__ import annotations

import struct

from .gpt import ENTRY_SIZE, GPT_SIGNATURE, _crc32, parse_gpt
from .safeio import open_physical_drive
from .vmdk import SparseVmdkReader

SECTOR = 512


def _parse_backup_gpt(reader) -> dict:
    """解析备份 GPT（位于最后一个 LBA）。"""
    alt_lba = reader.size // SECTOR - 1
    header = reader.read_at(alt_lba * SECTOR, 92)
    if header[:8] != GPT_SIGNATURE:
        return {"signature_ok": False}
    (_rev, header_size, header_crc, _res, _my, _alt, _fu, _lu,
     _guid, entries_lba, num_entries, entry_size, entries_crc) = struct.unpack_from(
        "<IIIIQQQQ16sQIII", header, 8)

    header_full = bytearray(reader.read_at(alt_lba * SECTOR, header_size or 92))
    header_full[16:20] = b"\x00\x00\x00\x00"
    header_ok = _crc32(bytes(header_full)) == header_crc

    table = reader.read_at(entries_lba * SECTOR, num_entries * entry_size)
    entries_ok = _crc32(table) == entries_crc
    return {
        "signature_ok": True,
        "header_crc_ok": header_ok,
        "entries_crc_ok": entries_ok,
        "entries_lba": entries_lba,
        "num_entries": num_entries,
    }


def verify_vmdk(path: str, source_disk: int | None = None,
                sample_bytes: int = 4 * 1024 * 1024) -> dict:
    checks = []
    result = {"path": path, "ok": False, "checks": checks}

    with SparseVmdkReader(path) as reader:
        result["capacity_bytes"] = reader.size
        result["capacity_gib"] = round(reader.size / 1024 ** 3, 2)
        result["flags"] = reader.flags
        checks.append({"name": "vmdk_header", "ok": True,
                       "detail": "monolithicSparse, capacity=%d sectors" % reader.capacity_sectors})

        gpt = parse_gpt(reader)
        result["gpt"] = gpt.as_dict()
        checks.append({
            "name": "gpt_primary",
            "ok": bool(gpt.header_crc_ok and gpt.entries_crc_ok),
            "detail": "header_crc=%s entries_crc=%s partitions=%d" % (
                gpt.header_crc_ok, gpt.entries_crc_ok, len(gpt.partitions)),
        })

        backup = _parse_backup_gpt(reader)
        result["gpt_backup"] = backup
        checks.append({
            "name": "gpt_backup",
            "ok": bool(backup.get("signature_ok") and backup.get("header_crc_ok")
                       and backup.get("entries_crc_ok")),
            "detail": str(backup),
        })

        if source_disk is not None:
            with open_physical_drive(source_disk) as src:
                src_gpt = parse_gpt(src)
            src_by_guid = {str(p.part_guid): p for p in src_gpt.partitions}
            missing = [str(p.part_guid) for p in gpt.partitions if str(p.part_guid) not in src_by_guid]
            checks.append({
                "name": "partition_guids_present_on_source",
                "ok": not missing,
                "detail": "missing=%s" % missing,
            })
            with open_physical_drive(source_disk) as src:
                for p in gpt.partitions:
                    sp = src_by_guid.get(str(p.part_guid))
                    if sp is None:
                        continue
                    take = min(sample_bytes, p.size_bytes)
                    a = reader.read_at(p.offset, take)
                    b = src.read_at(sp.offset, take)
                    checks.append({
                        "name": "content_head_sample_part%d" % p.index,
                        "ok": a == b,
                        "detail": "first %d bytes of %s" % (take, p.type_name),
                    })

    result["ok"] = all(c["ok"] for c in checks)
    return result
