# SPDX-License-Identifier: GPL-3.0-only
r"""只读设备访问层（Windows）。

安全契约
--------
本模块只以 GENERIC_READ 打开设备，并且不暴露任何写接口
（没有 write / flush / set-length / ioctl-write）。这是"绝不损坏源系统"这条
红线的代码落点：调用方即使想写也无 API 可用。

块设备约束
----------
对 \\.\PhysicalDriveN 这类裸设备，ReadFile 要求 **offset 与 size 都是扇区
（512B）整数倍**，否则报 ERROR_INVALID_PARAMETER(87)。本模块在 read_at 内部
自动做"向上/向下对齐 -> 整扇区读 -> 裁剪"，调用方可以按字节任意读。
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
DEFAULT_SECTOR = 512

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_k32.ReadFile.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
_k32.SetFilePointerEx.restype = wintypes.BOOL
_k32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]


class DeviceError(RuntimeError):
    """设备打开/读取失败。"""


class ReadOnlyDevice:
    r"""对块设备的只读访问。

    Parameters
    ----------
    path:
        Windows 设备路径，例如 \\.\PhysicalDrive3 或
        \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1。
    size:
        字节数。为 None 时用 IOCTL_DISK_GET_LENGTH_INFO 查询；
        VSS 卷影副本设备不支持该 IOCTL，必须显式传入 size。
    sector_size:
        扇区大小。None（默认）表示向设备查询 DISK_GEOMETRY_EX 的真实逻辑扇区；
        查询不到时回落 512。裸设备读取按它对齐，所以 4Kn 盘必须拿到真值，
        否则偏移全部错位。
    """

    __slots__ = ("path", "sector_size", "_h", "_size")

    def __init__(self, path: str, size: int | None = None,
                 sector_size: int | None = None) -> None:
        handle = _k32.CreateFileW(
            path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise DeviceError(
                "CreateFileW failed for %r (err=%d)" % (path, ctypes.get_last_error())
            )
        self.path = path
        self._h = handle
        try:
            self.sector_size = int(sector_size) if sector_size else self._query_sector_size()
            self._size = int(size) if size is not None else self._query_size()
        except Exception:
            # 探询失败同样要关句柄：否则会一直占住物理盘
            self.close()
            raise

    # -- metadata ---------------------------------------------------------
    @property
    def size(self) -> int:
        """设备字节数。"""
        return self._size

    @property
    def total_sectors(self) -> int:
        return self._size // self.sector_size

    def _query_sector_size(self) -> int:
        """查询设备逻辑扇区大小；不支持该 IOCTL 时回落 512。

        DISK_GEOMETRY_EX = DISK_GEOMETRY(24B) + DiskSize(8B) + Data[]，
        BytesPerSector 是 DISK_GEOMETRY 的第 5 个 DWORD（偏移 0x14）。
        VSS 卷影副本设备不响应此 IOCTL，会走到回落分支。
        """
        buf = ctypes.create_string_buffer(64)
        ret = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(
            self._h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 0,
            buf, ctypes.sizeof(buf), ctypes.byref(ret), None,
        )
        if not ok or ret.value < 0x18:
            return DEFAULT_SECTOR
        value = struct.unpack_from("<I", buf.raw, 0x14)[0]
        return value if value in (512, 4096) else DEFAULT_SECTOR

    def _query_size(self) -> int:
        buf = ctypes.c_longlong(0)
        ret = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(
            self._h, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
            ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(ret), None,
        )
        if not ok:
            raise DeviceError(
                "cannot query length of %r (err=%d); pass size= explicitly "
                "(VSS shadow devices do not answer this IOCTL)"
                % (self.path, ctypes.get_last_error())
            )
        return int(buf.value)

    # -- io (read only) ---------------------------------------------------
    def _read_raw(self, offset: int, size: int) -> bytes:
        """实际读取；要求 offset/size 已扇区对齐。"""
        newpos = ctypes.c_longlong(0)
        if not _k32.SetFilePointerEx(self._h, ctypes.c_longlong(offset), ctypes.byref(newpos), 0):
            raise DeviceError("seek to %d failed (err=%d)" % (offset, ctypes.get_last_error()))
        out = ctypes.create_string_buffer(size)
        got = wintypes.DWORD(0)
        if not _k32.ReadFile(self._h, out, size, ctypes.byref(got), None):
            raise DeviceError("read at %d (%d bytes) failed (err=%d)"
                              % (offset, size, ctypes.get_last_error()))
        if got.value != size:
            raise DeviceError("short read at %d: got %d/%d" % (offset, got.value, size))
        return out.raw

    def read_at(self, offset: int, size: int) -> bytes:
        """按字节读任意区间；内部自动做扇区对齐与裁剪。越界即报错。"""
        if offset < 0 or size < 0:
            raise ValueError("negative offset/size")
        if size == 0:
            return b""
        if offset + size > self._size:
            raise ValueError("read out of range: %d+%d > %d" % (offset, size, self._size))

        ss = self.sector_size
        start = (offset // ss) * ss
        end = min(((offset + size + ss - 1) // ss) * ss, self._size)
        raw = self._read_raw(start, end - start)
        begin = offset - start
        return raw[begin:begin + size]

    def read_sectors(self, lba: int, count: int) -> bytes:
        """按扇区读，最贴合块设备的语义。"""
        return self.read_at(lba * self.sector_size, count * self.sector_size)

    def close(self) -> None:
        if self._h:
            _k32.CloseHandle(wintypes.HANDLE(self._h))
            self._h = None

    def __enter__(self) -> "ReadOnlyDevice":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<ReadOnlyDevice %s size=%d sector=%d>" % (self.path, self._size, self.sector_size)


def physical_drive_path(n: int) -> str:
    r"""返回 \\.\PhysicalDriveN 形式的路径。"""
    return "\\\\.\\PhysicalDrive%d" % int(n)


def open_physical_drive(n: int, sector_size: int | None = None) -> ReadOnlyDevice:
    """以只读方式打开物理盘 N。sector_size=None 时向设备查询（USB 盒 / 4Kn 盘必需）。"""
    return ReadOnlyDevice(physical_drive_path(n), sector_size=sector_size)


def shadow_device_path(n: int) -> str:
    """返回 VSS 卷影副本设备路径。"""
    return "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy%d" % int(n)


def open_shadow(n: int, size: int, sector_size: int | None = None) -> ReadOnlyDevice:
    """以只读方式打开卷影副本 N（必须显式给 size；扇区大小查不到时回落 512）。"""
    return ReadOnlyDevice(shadow_device_path(n), size=size, sector_size=sector_size)

class ReadOnlyFile:
    """只读文件/镜像设备（用于校验产物）。接口与 ReadOnlyDevice 一致。"""

    __slots__ = ("path", "sector_size", "_f", "_size")

    def __init__(self, path: str, sector_size: int = DEFAULT_SECTOR) -> None:
        self.path = path
        self.sector_size = int(sector_size)
        self._f = open(path, "rb")
        self._size = os.path.getsize(path)

    @property
    def size(self) -> int:
        return self._size

    @property
    def total_sectors(self) -> int:
        return self._size // self.sector_size

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("negative offset/size")
        if offset + size > self._size:
            raise ValueError("read out of range: %d+%d > %d" % (offset, size, self._size))
        self._f.seek(offset)
        data = self._f.read(size)
        if len(data) != size:
            raise DeviceError("short read at %d: %d/%d" % (offset, len(data), size))
        return data

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self) -> "ReadOnlyFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
