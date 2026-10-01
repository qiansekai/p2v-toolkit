# SPDX-License-Identifier: GPL-3.0-only
"""从产物 vmdk 生成 VMware Workstation 的 .vmx。

为什么需要
--------
P2V 的产物只是一块磁盘，没有虚拟机定义。手写 vmx 最容易漏掉的是 **PCIe 根端口**
（pciBridge0/4/5/6/7 以及显式的 pciSlotNumber）—— 漏掉后 VMware 拒绝加电，报：

    SCSI0 没有可用的 PCIe 插槽 / 无法分配 PCI SCSI 适配器 / 配置的 PCI 设备过多

2026-10-01 实测踩过：vmx 里只写了 scsi0 + ethernet0 + usb，VMware 把 SCSI 分到槽位 21，
而 21..24 本该是那四个根端口的位置，于是「没有可用的 PCIe 插槽」。本模块把验证过的
配置固化成生成器，槽位映射沿用本机可正常启动的 CentOS 母本。

固件类型由产物本身决定、不靠猜：GPT 里存在 ESP 分区就用 efi（选错会让 EFI 盘黑屏打转）。
"""

from __future__ import annotations

import os

from .gpt import TYPE_ESP, GptError, parse_gpt
from .vmdk import SparseVmdkReader, VmdkError


class VmxError(RuntimeError):
    pass


DEFAULT_GUEST_OS = "windows9-64"      # Win10/11 通用；只影响 VMware 的优化提示，不影响能否启动
DEFAULT_HW_VERSION = "19"             # VMware 16/17 都能开；17.6 会自动升到 21
DEFAULT_MEMSIZE_MB = 4096
DEFAULT_VCPUS = 2
DEFAULT_BOOT_DELAY_MS = 3000
DEFAULT_CONTROLLER = "lsisas1068"     # 本机实测通过的 P2V 引导控制器
DEFAULT_NIC = "e1000e"                # guest 没装 Tools，必须用 Windows 内置驱动的网卡
DEFAULT_CONNECTION = "nat"

CONTROLLERS = ("lsisas1068", "lsilogic", "buslogic")
CONNECTIONS = ("nat", "bridged", "hostonly")

# PCI 槽位分配（沿用本机可正常启动的母本）。
# scsi0 必须避开 21..24 —— 那四格留给 PCIe 根端口，抢了就是「没有可用的 PCIe 插槽」。
SLOT_SCSI0 = 16
SLOT_BRIDGES = (("0", 17), ("4", 21), ("5", 22), ("6", 23), ("7", 24))
SLOT_USB = 32
SLOT_ETHERNET0 = 33


def detect_firmware(dev) -> str:
    """由产物本身判断引导固件：GPT 里存在 ESP 分区就是 efi，否则 bios。"""
    try:
        gpt = parse_gpt(dev)
    except GptError as exc:
        raise VmxError("无法解析产物 GPT：%s" % exc)
    for p in gpt.partitions:
        if p.type_guid == TYPE_ESP:
            return "efi"
    return "bios"


def _safe_name(name: str) -> str:
    if not name or any(ch in name for ch in '"\\\r\n'):
        raise VmxError("非法名称 %r：不能为空，且不能含引号、反斜杠或换行" % name)
    return name


def _check_options(firmware, vcpus, memsize_mb, controller, connection, hw_version):
    if firmware not in ("efi", "bios"):
        raise VmxError("firmware 只能是 efi 或 bios，收到 %r" % firmware)
    if vcpus < 1:
        raise VmxError("vcpus 至少为 1，收到 %r" % vcpus)
    if memsize_mb < 512:
        raise VmxError("memsize 至少 512 MiB，收到 %r" % memsize_mb)
    if controller not in CONTROLLERS:
        raise VmxError("controller 只能是 %s，收到 %r" % ("/".join(CONTROLLERS), controller))
    if connection not in CONNECTIONS:
        raise VmxError("connection 只能是 %s，收到 %r" % ("/".join(CONNECTIONS), connection))
    if not str(hw_version).isdigit():
        raise VmxError("hw_version 必须是数字，收到 %r" % hw_version)


def render_vmx(*, disk_ref: str, base_name: str, guest_os: str = DEFAULT_GUEST_OS,
               firmware: str = "efi", hw_version: str = DEFAULT_HW_VERSION,
               memsize_mb: int = DEFAULT_MEMSIZE_MB, vcpus: int = DEFAULT_VCPUS,
               nic: str = DEFAULT_NIC, connection: str = DEFAULT_CONNECTION,
               controller: str = DEFAULT_CONTROLLER,
               boot_delay_ms: int = DEFAULT_BOOT_DELAY_MS) -> str:
    """渲染 vmx 文本。纯函数，不碰文件系统（便于回归测试逐行断言）。"""
    _safe_name(base_name)
    _check_options(firmware, vcpus, memsize_mb, controller, connection, hw_version)

    out: list = []
    a = out.append
    a('.encoding = "UTF-8"')
    a('config.version = "8"')
    a('virtualHW.version = "%s"' % hw_version)
    a('virtualHW.productCompatibility = "hosted"')
    a('')
    a('# ---- identity ----')
    a('displayName = "%s"' % base_name)
    a('guestOS = "%s"' % guest_os)
    if firmware == "efi":
        a('# the exported GPT carries an ESP -> EFI firmware is mandatory')
    a('firmware = "%s"' % firmware)
    a('nvram = "%s.nvram"' % base_name)
    a('')
    a('# ---- resources (lower memsize if the host starts swapping) ----')
    a('memsize = "%d"' % memsize_mb)
    a('numvcpus = "%d"' % vcpus)
    a('cpuid.coresPerSocket = "%d"' % vcpus)
    a('mem.hotadd = "TRUE"')
    a('')
    a('# ---- PCIe root ports (REQUIRED) ----')
    a('# Without them VMware cannot place the SCSI controller and refuses to power on:')
    a('#   "SCSI0 has no available PCIe slot / too many configured PCI devices"')
    a('# Slot numbers follow a base VM that boots on this machine; the explicit')
    a('# pciSlotNumber lines keep them from colliding.')
    for bridge, slot in SLOT_BRIDGES:
        a('pciBridge%s.present = "TRUE"' % bridge)
        a('pciBridge%s.virtualDev = "pcieRootPort"' % bridge)
        a('pciBridge%s.functions = "8"' % bridge)
    a('vmci0.present = "TRUE"')
    a('hpet0.present = "TRUE"')
    a('')
    a('# ---- disk ----')
    a('# Guests that came from a physical machine have no VMware Tools, so the NIC')
    a('# below is an in-box driver (e1000e) on purpose; vmxnet3 would only appear as')
    a('# an unknown device. Keep the controller at a driver Windows already ships.')
    a('scsi0.present = "TRUE"')
    a('scsi0.virtualDev = "%s"' % controller)
    a('scsi0:0.present = "TRUE"')
    a('scsi0:0.fileName = "%s"' % disk_ref)
    a('scsi0:0.deviceType = "disk"')
    a('')
    a('# ---- network ----')
    a('ethernet0.present = "TRUE"')
    a('ethernet0.virtualDev = "%s"' % nic)
    a('ethernet0.connectionType = "%s"' % connection)
    a('ethernet0.addressType = "generated"')
    a('ethernet0.startConnected = "TRUE"')
    a('')
    a('# ---- other devices ----')
    a('usb.present = "TRUE"')
    a('usb:0.present = "TRUE"')
    a('usb:0.deviceType = "hid"')
    a('usb:0.port = "0"')
    a('usb:0.parent = "-1"')
    a('usb:1.speed = "2"')
    a('usb:1.present = "TRUE"')
    a('usb:1.deviceType = "hub"')
    a('usb:1.port = "1"')
    a('usb:1.parent = "-1"')
    a('svga.present = "TRUE"')
    a('svga.vramSize = "268435456"')
    a('svga.guestBackedPrimaryAware = "TRUE"')
    a('sound.present = "FALSE"')
    a('floppy0.present = "FALSE"')
    a('serial0.present = "FALSE"')
    a('')
    a('# ---- PCI slot assignment (must not collide) ----')
    a('scsi0.pciSlotNumber = "%d"' % SLOT_SCSI0)
    for bridge, slot in SLOT_BRIDGES:
        a('pciBridge%s.pciSlotNumber = "%d"' % (bridge, slot))
    a('usb.pciSlotNumber = "%d"' % SLOT_USB)
    a('ethernet0.pciSlotNumber = "%d"' % SLOT_ETHERNET0)
    a('')
    a('# ---- boot behaviour ----')
    a('# a short window to hit ESC and pick another boot device (e.g. a PE ISO)')
    a('bios.bootDelay = "%d"' % boot_delay_ms)
    a('tools.syncTime = "FALSE"')
    a('powerType.powerOff = "soft"')
    a('powerType.powerOn = "soft"')
    a('powerType.suspend = "soft"')
    a('powerType.reset = "soft"')
    a('')
    a('# ---- generated / runtime ----')
    a('extendedConfigFile = "%s.vmxf"' % base_name)
    a('vmxstats.filename = "%s.scoreboard"' % base_name)
    a('scsi0:0.redo = ""')
    return "\n".join(out) + "\n"


def _assert_slots_unique() -> None:
    """自检：槽位重复会让 VMware 静默改配置，必须在生成前就发现。"""
    slots = [SLOT_SCSI0, SLOT_USB, SLOT_ETHERNET0] + [s for _, s in SLOT_BRIDGES]
    if len(set(slots)) != len(slots):
        raise VmxError("内部错误：PCI 槽位分配有重复 %r" % slots)


def write_vmx(vmdk_path: str, *, out_path: str | None = None, name: str | None = None,
              force: bool = False, **options) -> dict:
    """读产物 GPT 判固件，然后写 vmx。已存在一律拒绝（除非 force）。"""
    _assert_slots_unique()
    vmdk = os.path.abspath(vmdk_path)
    if not os.path.isfile(vmdk):
        raise VmxError("找不到产物 vmdk：%s" % vmdk)
    base = name or os.path.splitext(os.path.basename(vmdk))[0]
    _safe_name(base)
    target = os.path.abspath(out_path) if out_path else os.path.splitext(vmdk)[0] + ".vmx"
    if os.path.exists(target) and not force:
        raise VmxError("目标已存在：%s（确认要覆盖就加 --force）" % target)

    try:
        with SparseVmdkReader(vmdk) as dev:
            detected = detect_firmware(dev)
    except VmdkError as exc:
        raise VmxError("打不开产物 vmdk：%s" % exc)

    firmware = options.pop("firmware", None) or "auto"
    warning = None
    if firmware == "auto":
        firmware = detected
    elif firmware != detected:
        # 不阻断，但要说清楚：产物里有 ESP 却按 BIOS 配，几乎必然起不来
        warning = ("产物 GPT 里%s ESP，但你强制用了 %s（自动探测结果是 %s）"
                   % ("有" if detected == "efi" else "没有", firmware, detected))

    same_dir = os.path.dirname(target) == os.path.dirname(vmdk)
    disk_ref = os.path.basename(vmdk) if same_dir else vmdk
    text = render_vmx(disk_ref=disk_ref, base_name=base, firmware=firmware, **options)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return {
        "ok": True,
        "vmx": target,
        "vmdk": vmdk,
        "disk_ref": disk_ref,
        "firmware": firmware,
        "firmware_detected": detected,
        "guest_os": options.get("guest_os", DEFAULT_GUEST_OS),
        "memsize_mb": options.get("memsize_mb", DEFAULT_MEMSIZE_MB),
        "vcpus": options.get("vcpus", DEFAULT_VCPUS),
        "warning": warning,
    }
