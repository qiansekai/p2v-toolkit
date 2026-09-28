"""GPT 解析与构造（含 CRC32 自检）。

分区的 GUID 在 GPT 里是 mixed-endian，统一用 uuid.UUID(bytes_le=...) 处理。
"""

from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass, field

GPT_SIGNATURE = b"EFI PART"
GPT_REVISION = 0x00010000
ENTRY_SIZE = 128

TYPE_ESP = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
TYPE_MSR = uuid.UUID("e3c9e316-0b5c-4db8-817d-f92df00215ae")
TYPE_BASIC = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")
TYPE_LDM_META = uuid.UUID("5808c8aa-7e8f-42e0-85d2-e1e90434cfb3")
TYPE_LDM_DATA = uuid.UUID("af9b60a0-1431-4f62-bc68-3311714a69ad")
TYPE_RECOVERY = uuid.UUID("de94bba4-06d1-4d40-a16a-bfd50179d6ac")

TYPE_NAMES = {
    TYPE_ESP: "ESP",
    TYPE_MSR: "MSR",
    TYPE_BASIC: "BasicData",
    TYPE_LDM_META: "LDM-Meta",
    TYPE_LDM_DATA: "LDM-Data",
    TYPE_RECOVERY: "Recovery",
}


class GptError(RuntimeError):
    """GPT 结构非法。"""


@dataclass
class Partition:
    index: int
    type_guid: uuid.UUID
    part_guid: uuid.UUID
    first_lba: int
    last_lba: int
    attributes: int = 0
    name: str = ""
    sector_size: int = 512

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type_guid, str(self.type_guid))

    @property
    def offset(self) -> int:
        return self.first_lba * self.sector_size

    @property
    def sectors(self) -> int:
        return self.last_lba - self.first_lba + 1

    @property
    def size_bytes(self) -> int:
        return self.sectors * self.sector_size

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "type": self.type_name,
            "attributes": "0x%016x" % self.attributes,
            "type_guid": str(self.type_guid),
            "part_guid": str(self.part_guid),
            "first_lba": self.first_lba,
            "last_lba": self.last_lba,
            "offset": self.offset,
            "size_bytes": self.size_bytes,
            "size_gib": round(self.size_bytes / 1024 ** 3, 2),
            "name": self.name,
        }


@dataclass
class GptDisk:
    sector_size: int
    disk_guid: uuid.UUID
    first_usable_lba: int
    last_usable_lba: int
    capacity: int
    partitions: list = field(default_factory=list)
    header_crc_ok: bool = False
    entries_crc_ok: bool = False

    def as_dict(self) -> dict:
        return {
            "sector_size": self.sector_size,
            "disk_guid": str(self.disk_guid),
            "capacity_bytes": self.capacity,
            "capacity_gib": round(self.capacity / 1024 ** 3, 2),
            "first_usable_lba": self.first_usable_lba,
            "last_usable_lba": self.last_usable_lba,
            "header_crc_ok": self.header_crc_ok,
            "entries_crc_ok": self.entries_crc_ok,
            "partitions": [p.as_dict() for p in self.partitions],
        }


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def parse_gpt(dev, sector_size: int = 512) -> GptDisk:
    """从只读设备解析 GPT 主表。CRC 结果作为字段返回，不静默通过。"""
    raw_header = dev.read_at(sector_size, 92)
    if raw_header[:8] != GPT_SIGNATURE:
        raise GptError("not a GPT disk: signature mismatch at LBA1")

    (
        revision, header_size, header_crc, _reserved, _my_lba, _alt_lba,
        first_usable, last_usable, disk_guid_bytes,
        entries_lba, num_entries, entry_size, entries_crc,
    ) = struct.unpack_from("<IIIIQQQQ16sQIII", raw_header, 8)

    header_size = header_size or 92
    entry_size = entry_size or ENTRY_SIZE

    header_full = bytearray(dev.read_at(sector_size, header_size))
    header_full[16:20] = b"\x00\x00\x00\x00"
    header_ok = _crc32(bytes(header_full)) == header_crc

    table = dev.read_at(entries_lba * sector_size, num_entries * entry_size)
    entries_ok = _crc32(table) == entries_crc

    partitions = []
    for i in range(num_entries):
        entry = table[i * entry_size:(i + 1) * entry_size]
        type_guid = uuid.UUID(bytes_le=bytes(entry[0:16]))
        if type_guid.int == 0:
            continue
        part_guid = uuid.UUID(bytes_le=bytes(entry[16:32]))
        first_lba, last_lba, attrs = struct.unpack_from("<QQQ", entry, 32)
        name = entry[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        partitions.append(
            Partition(i + 1, type_guid, part_guid, first_lba, last_lba, attrs, name, sector_size)
        )

    return GptDisk(
        sector_size=sector_size,
        disk_guid=uuid.UUID(bytes_le=disk_guid_bytes),
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        capacity=dev.size,
        partitions=partitions,
        header_crc_ok=header_ok,
        entries_crc_ok=entries_ok,
    )

# ---------------------------------------------------------------------------
# GPT 构造
# ---------------------------------------------------------------------------

PROTECTIVE_MBR_TYPE = 0xEE


def build_protective_mbr(capacity_sectors: int, sector_size: int = 512,
                         disk_signature: int = 0,
                         end_lba: int | None = None) -> bytes:
    """保护性 MBR：单个 0xEE 分区项覆盖整盘。

    disk_signature: LBA0 @0x1B8 的磁盘签名。克隆场景应沿用源盘值——它不是
    装饰：MBR 盘上 UEFI 的 HD() 设备路径与 Windows 的 MountedDevices 都用到它。
    end_lba: 0xEE 项覆盖的末 LBA。None 时按 UEFI 规范取
    min(capacity-1, 0xFFFFFFFF)；源盘若按 Windows 惯例写满 0xFFFFFFFF，
    克隆时应显式传入源盘值，保证产物与源盘逐字节一致。
    """
    mbr = bytearray(sector_size)
    struct.pack_into("<I", mbr, 0x1B8, int(disk_signature) & 0xFFFFFFFF)
    entry = 0x1BE
    mbr[entry + 0] = 0x00            # boot flag
    mbr[entry + 1:entry + 4] = b"\x00\x02\x00"   # start CHS (0/0/2)
    mbr[entry + 4] = PROTECTIVE_MBR_TYPE
    mbr[entry + 5:entry + 8] = b"\xff\xff\xff"   # end CHS
    struct.pack_into("<I", mbr, entry + 8, 1)
    if end_lba is None:
        end_lba = min(capacity_sectors - 1, 0xFFFFFFFF)
    struct.pack_into("<I", mbr, entry + 12, int(end_lba) & 0xFFFFFFFF)
    mbr[510] = 0x55
    mbr[511] = 0xAA
    return bytes(mbr)


def build_gpt(capacity_sectors: int, disk_guid: uuid.UUID, partitions: list,
              sector_size: int = 512, num_entries: int = 128,
              disk_signature: int = 0, pmbr_end_lba: int | None = None) -> dict:
    """构造 GPT 主/备结构与保护性 MBR。

    partitions: list[Partition]（用其 type_guid / part_guid / first_lba / last_lba /
                attributes / name；part_guid 建议沿用源盘值，避免 MountedDevices 失配）
    返回 dict，键为各结构在本盘内的字节位置 -> 内容。
    """
    entries_sectors = (num_entries * ENTRY_SIZE) // sector_size
    entries_lba = 2
    alt_lba = capacity_sectors - 1
    backup_entries_lba = alt_lba - entries_sectors   # 必须紧邻备份 header，再减 1 会与它重叠
    first_usable = entries_lba + entries_sectors
    last_usable = backup_entries_lba - 1
    my_lba = 1

    for p in partitions:
        if p.first_lba < first_usable or p.last_lba > last_usable:
            raise GptError("partition %d out of usable range" % p.index)

    table = bytearray(num_entries * ENTRY_SIZE)
    for slot, p in enumerate(partitions):
        off = slot * ENTRY_SIZE
        table[off:off + 16] = p.type_guid.bytes_le
        table[off + 16:off + 32] = p.part_guid.bytes_le
        struct.pack_into("<QQQ", table, off + 32, p.first_lba, p.last_lba, p.attributes)
        name = p.name.encode("utf-16-le")
        table[off + 56:off + 56 + len(name)] = name
    entries_crc = _crc32(bytes(table))

    def _header(this_lba: int, other_lba: int, part_lba: int) -> bytes:
        hdr = bytearray(92)
        hdr[0:8] = GPT_SIGNATURE
        struct.pack_into("<I", hdr, 8, GPT_REVISION)
        struct.pack_into("<I", hdr, 12, 92)          # header size
        struct.pack_into("<I", hdr, 16, 0)           # crc placeholder
        struct.pack_into("<I", hdr, 20, 0)           # reserved
        struct.pack_into("<Q", hdr, 24, this_lba)
        struct.pack_into("<Q", hdr, 32, other_lba)
        struct.pack_into("<Q", hdr, 40, first_usable)
        struct.pack_into("<Q", hdr, 48, last_usable)
        hdr[56:72] = disk_guid.bytes_le
        struct.pack_into("<Q", hdr, 72, part_lba)
        struct.pack_into("<I", hdr, 80, num_entries)
        struct.pack_into("<I", hdr, 84, ENTRY_SIZE)
        struct.pack_into("<I", hdr, 88, entries_crc)
        crc = _crc32(bytes(hdr))
        struct.pack_into("<I", hdr, 16, crc)
        return bytes(hdr)

    primary = _header(my_lba, alt_lba, entries_lba)
    backup = _header(alt_lba, my_lba, backup_entries_lba)

    return {
        "mbr": build_protective_mbr(capacity_sectors, sector_size,
                                    disk_signature=disk_signature,
                                    end_lba=pmbr_end_lba),
        "primary_header": primary,
        "primary_entries": bytes(table),
        "backup_entries": bytes(table),
        "backup_header": backup,
        "layout": {
            "entries_lba": entries_lba,
            "entries_sectors": entries_sectors,
            "backup_entries_lba": backup_entries_lba,
            "first_usable_lba": first_usable,
            "last_usable_lba": last_usable,
            "alt_lba": alt_lba,
        },
    }
