# SPDX-License-Identifier: GPL-3.0-only
"""vmx 生成（纯函数 + 真实 sparse vmdk 往返，不需要真盘、不需要 VMware）。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
import uuid

from tests.support import (BASIC_GUID, ESP_GUID, MIB, SECTOR, FakeDevice,
                           assemble_disk_image)

from p2v.gpt import Partition, build_gpt
from p2v.vmdk import SparseVmdkWriter
from p2v.vmx import (SLOT_BRIDGES, SLOT_ETHERNET0, SLOT_SCSI0, SLOT_USB,
                     VmxError, detect_firmware, render_vmx, write_vmx)

CAPACITY = 64 * MIB
BRIDGE_IDS = tuple(b for b, _ in SLOT_BRIDGES)
BRIDGE_SLOTS = {s for _, s in SLOT_BRIDGES}


def _built(with_esp: bool) -> dict:
    parts = []
    if with_esp:
        parts.append(Partition(1, uuid.UUID(ESP_GUID),
                               uuid.UUID("11111111-1111-1111-1111-111111111111"),
                               34, 2047, 0, "EFI system partition", SECTOR))
        parts.append(Partition(2, uuid.UUID(BASIC_GUID),
                               uuid.UUID("22222222-2222-2222-2222-222222222222"),
                               2048, 8191, 0, "Basic data", SECTOR))
    else:
        parts.append(Partition(1, uuid.UUID(BASIC_GUID),
                               uuid.UUID("22222222-2222-2222-2222-222222222222"),
                               34, 8191, 0, "Basic data", SECTOR))
    return build_gpt(CAPACITY // SECTOR, uuid.UUID("33333333-3333-3333-3333-333333333333"),
                     parts, SECTOR, disk_signature=0x12345678, pmbr_end_lba=0xFFFFFFFF)


def _image(with_esp: bool) -> bytes:
    return assemble_disk_image(_built(with_esp), CAPACITY)


class DetectFirmwareTest(unittest.TestCase):
    def test_gpt_with_esp_means_efi(self):
        self.assertEqual(detect_firmware(FakeDevice(_image(True))), "efi")

    def test_gpt_without_esp_means_bios(self):
        self.assertEqual(detect_firmware(FakeDevice(_image(False))), "bios")


class RenderVmxTest(unittest.TestCase):
    def _text(self, **kw):
        kw.setdefault("disk_ref", "prod.vmdk")
        kw.setdefault("base_name", "prod")
        return render_vmx(**kw)

    def test_pcie_root_ports_are_present(self):
        """回归：漏掉这五个根端口，VMware 报「SCSI0 没有可用的 PCIe 插槽」拒绝加电。"""
        text = self._text()
        for bridge in BRIDGE_IDS:
            self.assertIn('pciBridge%s.present = "TRUE"' % bridge, text)
            self.assertIn('pciBridge%s.virtualDev = "pcieRootPort"' % bridge, text)
            self.assertIn('pciBridge%s.functions = "8"' % bridge, text)

    def test_pci_slots_are_unique_and_avoid_bridge_slots(self):
        text = self._text()
        pairs = re.findall(r'^(\S+)\.pciSlotNumber = "(\d+)"$', text, re.M)
        self.assertTrue(pairs, '没有任何 pciSlotNumber')
        slots = [int(s) for _, s in pairs]
        self.assertEqual(len(slots), len(set(slots)), '槽位重复：%r' % pairs)
        got = dict(pairs)
        self.assertEqual(int(got['scsi0']), SLOT_SCSI0)
        self.assertNotIn(SLOT_SCSI0, BRIDGE_SLOTS)
        self.assertEqual(int(got['usb']), SLOT_USB)
        self.assertEqual(int(got['ethernet0']), SLOT_ETHERNET0)

    def test_firmware_disk_reference_and_names(self):
        text = self._text(disk_ref='my disk.vmdk', base_name='myvm', firmware='efi')
        self.assertIn('firmware = "efi"', text)
        self.assertIn('scsi0:0.fileName = "my disk.vmdk"', text)
        self.assertIn('displayName = "myvm"', text)
        self.assertIn('nvram = "myvm.nvram"', text)

    def test_options_are_parameterized(self):
        text = self._text(guest_os='windows11-64', memsize_mb=8192, vcpus=4,
                          nic='e1000', connection='bridged', controller='lsilogic')
        self.assertIn('guestOS = "windows11-64"', text)
        self.assertIn('memsize = "8192"', text)
        self.assertIn('numvcpus = "4"', text)
        self.assertIn('ethernet0.virtualDev = "e1000"', text)
        self.assertIn('ethernet0.connectionType = "bridged"', text)
        self.assertIn('scsi0.virtualDev = "lsilogic"', text)

    def test_invalid_options_are_rejected(self):
        bad = ({'firmware': 'uefi'}, {'vcpus': 0}, {'memsize_mb': 128},
               {'controller': 'nvme'}, {'connection': 'vpn'},
               {'hw_version': 'latest'}, {'base_name': 'a"b'},
               {'base_name': 'a\\b'}, {'base_name': ''})
        for kw in bad:
            with self.subTest(**kw):
                with self.assertRaises(VmxError):
                    self._text(**kw)


class WriteVmxTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='p2v-vmx-')
        self.vmdk = os.path.join(self.dir, 'prod.vmdk')
        with SparseVmdkWriter(self.vmdk, CAPACITY) as w:
            w.write_at(0, _image(True))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_next_to_vmdk_and_detects_efi(self):
        res = write_vmx(self.vmdk)
        self.assertEqual(res['vmx'], os.path.join(self.dir, 'prod.vmx'))
        self.assertEqual(res['firmware'], 'efi')
        self.assertEqual(res['firmware_detected'], 'efi')
        self.assertEqual(res['disk_ref'], 'prod.vmdk')
        self.assertIsNone(res['warning'])
        with open(res['vmx'], encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('pciBridge4.virtualDev = "pcieRootPort"', text)

    def test_existing_target_is_refused_then_forced(self):
        write_vmx(self.vmdk)
        with self.assertRaises(VmxError):
            write_vmx(self.vmdk)
        res = write_vmx(self.vmdk, force=True)
        self.assertTrue(os.path.isfile(res['vmx']))

    def test_missing_vmdk_is_rejected(self):
        with self.assertRaises(VmxError):
            write_vmx(os.path.join(self.dir, 'nope.vmdk'))

    def test_explicit_firmware_overrides_detection_with_warning(self):
        res = write_vmx(self.vmdk, firmware='bios', force=True)
        self.assertEqual(res['firmware'], 'bios')
        self.assertEqual(res['firmware_detected'], 'efi')
        self.assertIn('强制', res['warning'])


if __name__ == '__main__':
    unittest.main()
