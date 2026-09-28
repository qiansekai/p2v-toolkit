"""CLI 入口：python -m p2v <command> [options]

子命令
------
probe   只读枚举磁盘分区布局
plan    生成导出计划（dry-run，待实现）
export  按计划导出（需 --apply，待实现）
verify  校验产物（待实现）

约定：支持 --json；退出码 0=成功 / 2=校验失败 / 1=错误。
"""

from __future__ import annotations

import argparse
import json
import sys

from .gpt import GptError, parse_gpt
from .safeio import DeviceError, open_physical_drive


def _cmd_probe(args: argparse.Namespace) -> int:
    try:
        with open_physical_drive(args.disk) as dev:
            gpt = parse_gpt(dev, args.sector_size)
            payload = {
                "disk": args.disk,
                "device": dev.path,
                **gpt.as_dict(),
            }
    except (DeviceError, GptError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    payload["ok"] = bool(gpt.header_crc_ok and gpt.entries_crc_ok)

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("disk %d  %s  %.2f GiB  sector=%d" % (
            args.disk, dev.path, dev.size / 1024 ** 3, gpt.sector_size))
        print("disk_guid %s   header_crc=%s  entries_crc=%s" % (
            gpt.disk_guid,
            "OK" if gpt.header_crc_ok else "BAD",
            "OK" if gpt.entries_crc_ok else "BAD"))
        for p in gpt.partitions:
            print("  #%d  %-11s %10.2f GiB  @LBA %-10d off=%-12d %-12s {%s}" % (
                p.index, p.type_name, p.size_bytes / 1024 ** 3, p.first_lba,
                p.offset, p.name or "-", p.part_guid))

    return 0 if payload["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="p2v", description="agent 友好的 P2V 工具链（源设备只读）")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="只读枚举磁盘分区布局")
    p.add_argument("--disk", type=int, required=True, help="物理盘号，3 表示 PhysicalDrive3")
    p.add_argument("--sector-size", type=int, default=512)
    p.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p.set_defaults(func=_cmd_probe)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
