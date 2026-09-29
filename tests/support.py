# SPDX-License-Identifier: GPL-3.0-only
"""测试共用工具：把块设备抽象替换成内存设备，单测因此不需要真实磁盘。

GPT / vmdk / export / verify 的核心逻辑都能在没有管理员权限、没有物理盘的
机器上回归 —— 这也是这次「export 首轮后必崩」能被 CI 提前抓住的原因。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MIB = 1024 * 1024
SECTOR = 512

ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSR_GUID = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
BASIC_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"


class FakeDevice:
    """内存设备，接口与 safeio.ReadOnlyDevice / ReadOnlyFile 一致（只读）。"""

    def __init__(self, data: bytes, path: str = r"\\.\FAKEDISK") -> None:
        self.data = bytes(data)
        self.path = path

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def total_sectors(self) -> int:
        return len(self.data) // SECTOR

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("negative offset/size")
        if offset + size > len(self.data):
            raise ValueError("read out of range: %d+%d > %d"
                             % (offset, size, len(self.data)))
        return self.data[offset:offset + size]

    def read_sectors(self, lba: int, count: int) -> bytes:
        return self.read_at(lba * SECTOR, count * SECTOR)

    def close(self) -> None:
        pass


def pattern_bytes(size: int, mul: int = 7, add: int = 3) -> bytes:
    """可校验的非零数据（保证不会被 vmdk 的 thin 逻辑当空洞丢掉）。"""
    return bytes((i * mul + add) & 0xFF for i in range(size))


def assemble_disk_image(built: dict, capacity_bytes: int) -> bytes:
    """把 build_gpt() 的返回值拼成一整块盘镜像，供 parse_gpt 读回。"""
    img = bytearray(capacity_bytes)
    layout = built["layout"]

    def put(offset: int, blob: bytes) -> None:
        img[offset:offset + len(blob)] = blob

    put(0, built["mbr"])
    put(SECTOR, built["primary_header"])
    put(layout["entries_lba"] * SECTOR, built["primary_entries"])
    put(layout["backup_entries_lba"] * SECTOR, built["backup_entries"])
    put(layout["alt_lba"] * SECTOR, built["backup_header"])
    return bytes(img)
