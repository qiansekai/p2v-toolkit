"""产物自检：直接解析 vmdk 内部结构，不依赖外部工具。

校验项：
  1. vmdk header 合法（magic / version / geometry，由 reader 构造时把关）
  2. 主 GPT：签名 + header CRC + partition array CRC
  3. 备份 GPT：签名 + CRC（UEFI 会交叉校验主备）
  4. 内容一致性：
     - ESP / MSR：与源盘全量采样比对（静态分区）
     - BasicData（NTFS）：**必须与导出时所用的卷影副本比对**。若只给了
       --source-disk，则仅比对引导扇区（VBR）——因为 NTFS 的活动元数据
       （$LogFile / $MFT）在系统运行时会变，拿"当前物理盘"比会误报。
"""

from __future__ import annotations

import struct

from .gpt import ENTRY_SIZE, GPT_SIGNATURE, TYPE_BASIC, _crc32, parse_gpt
from .safeio import open_physical_drive, open_shadow
from .vmdk import SparseVmdkReader

SECTOR = 512


def _parse_backup_gpt(reader) -> dict:
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
                source_shadow: int | None = None,
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

        if source_disk is not None or source_shadow is not None:
            with open_physical_drive(source_disk if source_disk is not None else 3) as src:
                src_gpt = parse_gpt(src)
            src_by_guid = {str(p.part_guid): p for p in src_gpt.partitions}
            missing = [str(p.part_guid) for p in gpt.partitions
                       if str(p.part_guid) not in src_by_guid]
            checks.append({"name": "partition_guids_present_on_source",
                           "ok": not missing, "detail": "missing=%s" % missing})

            shadow_dev = None
            if source_shadow is not None:
                part = next((p for p in gpt.partitions if p.type_guid == TYPE_BASIC), None)
                shadow_dev = open_shadow(source_shadow, part.size_bytes if part else reader.size)
            try:
                for p in gpt.partitions:
                    src_p = src_by_guid.get(str(p.part_guid))
                    if src_p is None:
                        continue
                    if p.type_guid == TYPE_BASIC:
                        if shadow_dev is not None:
                            take = min(sample_bytes, p.size_bytes)
                            a = reader.read_at(p.offset, take)
                            b = shadow_dev.read_at(0, take)
                            checks.append({
                                "name": "content_sample_part%d" % p.index,
                                "ok": a == b,
                                "detail": "first %d bytes vs shadow#%d" % (take, source_shadow),
                            })
                        else:
                            take = min(4096, p.size_bytes)
                            with open_physical_drive(source_disk) as src:
                                b = src.read_at(src_p.offset, take)
                            a = reader.read_at(p.offset, take)
                            checks.append({
                                "name": "vbr_only_part%d" % p.index,
                                "ok": a == b,
                                "detail": ("first %d bytes (VBR) vs physical; NTFS 元数据会变，"
                                           "如需全量比对请用 --source-shadow") % take,
                            })
                    else:
                        take = min(sample_bytes, p.size_bytes)
                        with open_physical_drive(source_disk if source_disk is not None else 3) as src:
                            b = src.read_at(src_p.offset, take)
                        a = reader.read_at(p.offset, take)
                        checks.append({
                            "name": "content_sample_part%d" % p.index,
                            "ok": a == b,
                            "detail": "first %d bytes vs physical" % take,
                        })
            finally:
                if shadow_dev is not None:
                    shadow_dev.close()

        result["layout"] = {"gts": None}

    # 布局自洽性（不依赖 reader 的封装，直接读文件头）
    try:
        checks.extend(_layout_checks(path))
    except Exception as exc:
        checks.append({"name": "layout_checks", "ok": False, "detail": str(exc)})

    result["ok"] = all(c["ok"] for c in checks)
    return result

def _layout_checks(path: str) -> list:
    """校验 sparse vmdk 的 GD / RGD / GT 位置关系是否与 qemu 成品一致。

    这是为了不让 "VMware: needs repair" 成为唯一的发现途径：
    RGD 指向的是紧跟其后的【冗余 GT 区】，主 GD 指向主 GT 区。
    """
    from .vmdk import GT_SECTORS

    checks = []
    with open(path, "rb") as f:
        header = f.read(512)
    (magic, ver, flags, cap, grain, doff, dsize, numg,
     rgd, gd, over) = struct.unpack_from("<IIIQQQQIQQQ", header, 0)
    num_grains = (cap + grain - 1) // grain
    num_gts = (num_grains + numg - 1) // numg
    need = max(1, (num_gts * 4 + 511) // 512)
    gt_area = num_gts * GT_SECTORS

    exp_rgt = rgd + need
    exp_gd = exp_rgt + gt_area
    exp_gt = exp_gd + need

    checks.append({
        "name": "layout_gd_offset",
        "ok": gd == exp_gd,
        "detail": "gd=%d expected=%d (rgd=%d rgd_need=%d gts*4=%d)" % (gd, exp_gd, rgd, need, gt_area),
    })
    checks.append({
        "name": "layout_overhead_covers_gt",
        "ok": over >= exp_gt + gt_area,
        "detail": "overhead=%d gt_end=%d" % (over, exp_gt + gt_area),
    })

    with open(path, "rb") as f:
        f.seek(rgd * SECTOR)
        rgd_vals = struct.unpack("<%dI" % num_gts, f.read(num_gts * 4))
        f.seek(gd * SECTOR)
        gd_vals = struct.unpack("<%dI" % num_gts, f.read(num_gts * 4))

    # GD / RGD 层不稀疏：每一项都必须等于对应 GT 的位置（0 也是错误）
    bad = []
    for i in range(num_gts):
        gdv, rgv = gd_vals[i], rgd_vals[i]
        if gdv != exp_gt + i * GT_SECTORS:
            bad.append(("GD", i, gdv))
        if rgv != exp_rgt + i * GT_SECTORS:
            bad.append(("RGD", i, rgv))
    checks.append({
        "name": "layout_gd_rgd_values",
        "ok": not bad,
        "detail": "mismatches=%d first=%s" % (len(bad), bad[:3]),
    })
    return checks