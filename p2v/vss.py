# SPDX-License-Identifier: GPL-3.0-only
"""VSS 卷影副本访问。

安全姿态
--------
- 默认**只枚举与读取现有快照**，不改变系统 VSS 状态。
- 创建/删除快照会占用卷空间并改变系统状态，本版本**不提供**创建快照的能力
  （CLI 没有 --create-shadow 之类的开关）。需要一致性快照时，请先用系统工具
  （vssadmin / wbadmin 等）创建，再让本工具以只读方式打开。

注意：VSS 卷影副本是**卷级**的，物理设备层面不存在"整盘快照"。
本工具因此采用：ESP 从物理盘直读（几乎不变），系统卷从 VSS 快照读（一致）。
"""

from __future__ import annotations

import json
import re
import subprocess

from .safeio import ReadOnlyDevice, shadow_device_path

PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


class VssError(RuntimeError):
    pass


def _ps_json(script: str):
    proc = subprocess.run(PS + [script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise VssError("powershell failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()[:400]))
    text = (proc.stdout or "").strip()
    if not text:
        return []
    return json.loads(text)


def list_shadows() -> list:
    """枚举现有卷影副本（只读）。

    返回 [{id, volume, device_object, install_date}, ...]
    """
    script = (
        "Get-CimInstance Win32_ShadowCopy | "
        "Select-Object ID,VolumeName,DeviceObject,InstallDate | "
        "ConvertTo-Json -Compress"
    )
    data = _ps_json(script)
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data or []:
        device = item.get("DeviceObject") or ""
        # WMI 给的是 \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN，取其序号
        index = None
        if "HarddiskVolumeShadowCopy" in device:
            tail = device.rsplit("HarddiskVolumeShadowCopy", 1)[1]
            index = int("".join(ch for ch in tail if ch.isdigit()) or 0)
        out.append({
            "id": item.get("ID"),
            "volume": item.get("VolumeName"),
            "device_object": device,
            "shadow_index": index,
            "install_date": item.get("InstallDate"),
        })
    return out


def volume_size_bytes(drive_letter: str) -> int:
    """查卷容量（字节）。盘符限定为单个字母（会被拼进 PowerShell 脚本）。"""
    letter = (drive_letter or "").strip().rstrip(":\\").strip()
    if not re.fullmatch(r"[A-Za-z]", letter):
        raise VssError("非法盘符 %r：只接受单个字母（例如 C）" % drive_letter)
    script = (
        "Get-CimInstance Win32_Volume -Filter \"DriveLetter='%s:'\" | "
        "Select-Object -ExpandProperty Capacity" % letter
    )
    proc = subprocess.run(PS + [script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    text = (proc.stdout or "").strip()
    if not text.isdigit():
        raise VssError("cannot determine size of %s: %r" % (drive_letter, text[:200]))
    return int(text)


def open_shadow_readonly(shadow_index: int, size: int) -> ReadOnlyDevice:
    """以只读方式打开快照设备（qemu-img 打不开这个设备，我们自己实现读取流）。"""
    return ReadOnlyDevice(shadow_device_path(shadow_index), size=size)
