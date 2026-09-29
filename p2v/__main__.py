# SPDX-License-Identifier: GPL-3.0-only
"""CLI 入口：python -m p2v <command> [options]

子命令
------
probe   只读枚举磁盘布局（GPT 解析 + CRC 自检）
plan    生成导出计划（纯只读，不创建任何文件）
export  按计划导出（默认 dry-run，需 --apply 才落盘；中断后 --resume 续传）
verify  校验产物（自包含解析 vmdk，可选与源盘 / 卷影副本比对）

约定：支持 --json；退出码 0=成功 / 2=校验失败 / 1=错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .gpt import GptError, parse_gpt
from .export import (DEFAULT_CHECKPOINT_MIB, DEFAULT_CHUNK_MIB,
                     MAX_CHECKPOINT_MIB, MAX_CHUNK_MIB, MIN_CHECKPOINT_MIB,
                     MIN_CHUNK_MIB, run_export)
from .plan import PlanError, build_plan
from .resume import ResumeError
from .verify import verify_vmdk
from .safeio import DeviceError, open_physical_drive


def _cmd_probe(args: argparse.Namespace) -> int:
    try:
        with open_physical_drive(args.disk) as dev:
            gpt = parse_gpt(dev, args.sector_size or dev.sector_size)
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
        plan = build_plan(args.disk, args.take, args.out, args.sector_size,
                          source_mode=args.source_mode)
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
    # 人类可读模式给个进度：6 分钟静默对交互式使用太不友好（--json 时不打扰管道）
    progress = None
    if not args.json:
        tick = {"last": 0.0}

        def progress(seg, copied):                    # noqa: F811
            now = time.time()
            done = copied >= seg.size
            if not done and now - tick["last"] < 1.0:
                return
            tick["last"] = now
            sys.stderr.write("\r  partition #%s  %5.1f%%  %.1f/%.1f GiB   "
                             % (seg.partition_index,
                                100.0 * copied / max(seg.size, 1),
                                copied / 1024 ** 3, seg.size / 1024 ** 3))
            sys.stderr.flush()
            if done:
                sys.stderr.write("\n")

    try:
        plan = build_plan(args.disk, args.take, args.out, args.sector_size,
                          source_mode=args.source_mode)
        result = run_export(plan, apply=args.apply, chunk_mib=args.chunk_mib,
                            progress=progress, resume=args.resume,
                            checkpoint_mib=args.checkpoint_mib)
    except (DeviceError, GptError, PlanError, ResumeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:                          # 写盘中途的 IO 错误等
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        if args.apply:
            print("hint: %s 是未完成的半成品（检查点仍在）；修好原因后加 --resume 继续，"
                  "或连同它与同名 <out>.p2v-resume.json 一起删除后重跑"
                  % args.out, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for k, v in result.items():
            if k == "manual_steps":
                continue
            print("%-16s %s" % (k, v))
        steps = result.get("manual_steps") or []
        if steps:
            print("")
            print("!" * 74)
            for line in steps:
                print(line)
            print("!" * 74)
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
    p.add_argument("--sector-size", type=int, default=None,
                   help="逻辑扇区大小；默认向设备查询（4Kn 盘必须靠它拿到 4096）")
    p.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p.set_defaults(func=_cmd_probe)

    q = sub.add_parser("plan", help="生成导出计划（纯只读，不创建文件）")
    q.add_argument("--disk", type=int, required=True)
    q.add_argument("--take", required=True,
                   type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                   help="选择器，逗号分隔：ESP / MSR / part:N / vol:C:")
    q.add_argument("--out", required=True, help="目标 vmdk 路径（仅写入计划，不创建）")
    q.add_argument("--source-mode", choices=("auto", "physical", "shadow"), default="auto",
                   help="auto=源盘是本机系统盘才走 VSS；physical=强制直读；shadow=强制快照")
    q.add_argument("--sector-size", type=int, default=None)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_plan)

    e = sub.add_parser("export", help="按计划导出（默认 dry-run，--apply 才写盘）")
    e.add_argument("--disk", type=int, required=True)
    e.add_argument("--take", required=True,
                   type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    e.add_argument("--out", required=True)
    e.add_argument("--source-mode", choices=("auto", "physical", "shadow"), default="auto",
                   help="auto=源盘是本机系统盘才走 VSS；physical=强制直读；shadow=强制快照")
    e.add_argument("--sector-size", type=int, default=None)
    e.add_argument("--apply", action="store_true", help="真正写盘（不加则只预检）")
    e.add_argument("--chunk-mib", type=int, default=None,
                   help="读写块大小（MiB，%d..%d，默认 %d）；调大可减少 Python 层循环开销"
                        % (MIN_CHUNK_MIB, MAX_CHUNK_MIB, DEFAULT_CHUNK_MIB))
    e.add_argument("--resume", action="store_true",
                   help="续用已存在的半成品（需要同目录的 <out>.p2v-resume.json 检查点；"
                        "会校验计划指纹、源身份与快照身份，不一致直接拒绝）")
    e.add_argument("--checkpoint-mib", type=int, default=None,
                   help="每提交这么多数据落一次检查点（%d..%d，默认 %d）；崩溃后最多重做这一份"
                        % (MIN_CHECKPOINT_MIB, MAX_CHECKPOINT_MIB, DEFAULT_CHECKPOINT_MIB))
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