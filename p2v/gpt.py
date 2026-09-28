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
