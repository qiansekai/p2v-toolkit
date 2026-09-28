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
from .export import run_export
from .plan import PlanError, build_plan
from .verify import verify_vmdk
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


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan = build_plan(args.disk, args.take, args.out, args.sector_size)
    except (DeviceError, GptError, PlanError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.json:
        print(plan.to_json())
    else:
        d = plan.as_dict()
        print("source disk %d  %s  %.2f GiB" % (
            d["source_disk"], d["source_device"], d["source_capacity_gib"]))
        print("target %s  %.2f GiB  (disk_guid %s)" % (
            d["target_path"], d["target_capacity_gib"], d["target_disk_guid"]))
        print("segments:")
        for s in d["segments"]:
            src = s["source_kind"]
            if src == "shadow":
                src += "#%s" % s["shadow_index"]
            elif src == "physical":
                src += "@%d" % s["source_offset"]
            print("  %-18s dest=%-14d size=%-14d src=%s" % (
                s["role"], s["dest_offset"], s["size"], src))
        for n in d["notes"]:
            print("note: " + n)

    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    try:
        plan = build_plan(args.disk, args.take, args.out, args.sector_size)
        result = run_export(plan, apply=args.apply)
    except (DeviceError, GptError, PlanError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for k, v in result.items():
            print("%-16s %s" % (k, v))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        result = verify_vmdk(args.vmdk, args.source_disk, args.source_shadow, args.sample_bytes)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("%s  capacity=%.2f GiB  flags=%s" % (
            result["path"], result.get("capacity_gib", 0), result.get("flags")))
        for c in result["checks"]:
            print("  [%s] %-34s %s" % ("OK " if c["ok"] else "FAIL", c["name"], c["detail"][:110]))
        print("verdict: %s" % ("PASS" if result["ok"] else "FAIL"))
    return 0 if result["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="p2v", description="agent 友好的 P2V 工具链（源设备只读）")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="只读枚举磁盘分区布局")
    p.add_argument("--disk", type=int, required=True, help="物理盘号，3 表示 PhysicalDrive3")
    p.add_argument("--sector-size", type=int, default=512)
    p.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p.set_defaults(func=_cmd_probe)

    q = sub.add_parser("plan", help="生成导出计划（纯只读，不创建文件）")
    q.add_argument("--disk", type=int, required=True)
    q.add_argument("--take", required=True,
                   type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                   help="选择器，逗号分隔：ESP / MSR / part:N / vol:C:")
    q.add_argument("--out", required=True, help="目标 vmdk 路径（仅写入计划，不创建）")
    q.add_argument("--sector-size", type=int, default=512)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_plan)

    e = sub.add_parser("export", help="按计划导出（默认 dry-run，--apply 才写盘）")
    e.add_argument("--disk", type=int, required=True)
    e.add_argument("--take", required=True,
                   type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    e.add_argument("--out", required=True)
    e.add_argument("--sector-size", type=int, default=512)
    e.add_argument("--apply", action="store_true", help="真正写盘（不加则只预检）")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=_cmd_export)

    vp = sub.add_parser("verify", help="校验产物 vmdk（自包含解析）")
    vp.add_argument("--vmdk", required=True)
    vp.add_argument("--source-disk", type=int, default=None,
                    help="对比源盘（只读），如 3")
    vp.add_argument("--source-shadow", type=int, default=None,
                    help="对比该卷影副本序号（NTFS 全量比对应使用导出时所用的快照）")
    vp.add_argument("--sample-bytes", type=int, default=4 * 1024 * 1024)
    vp.add_argument("--json", action="store_true")
    vp.set_defaults(func=_cmd_verify)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())