# SPDX-License-Identifier: GPL-3.0-only
"""grain-fit：量出源盘的空洞分布，回答"这个卷该用多大的 grain"。

为什么需要它
------------
grain 尺寸是 monolithicSparse vmdk 的**分配粒度**：只有"含非零字节"的 grain 才在
文件里占位置。所以它同时决定两笔互相对冲的成本：

  元数据   = 12 字节 / grain（主 GTE 4 + 冗余 GTE 4 + GD/RGD 摊到每 grain 4）
           = 12 * 容量 / grain        -> grain 越大越省
  对齐浪费 = 每个"空洞边界"平均浪费半个 grain
           = 空洞边界数 * grain / 2   -> grain 越大越亏

实测过两台真机上的真实卷：空洞**不是**少量大块，而是大量中等块（零占比 25~37%，
却切出成百上千个 64 KiB 级零 run），所以第二项在真实负载里远大于第一项，
产物最小的尺寸落在候选集合的最小值。这个脚本把该结论做成可复跑的测量，
以后换源盘（尤其是"近乎全零的脏盘"这种反例）可以直接重算，不用重新推一遍。

只读保证
--------
与主工具同一套设备层（p2v.safeio）：GENERIC_READ 打开，无任何写 API。
本脚本不创建、不修改任何文件。

用法::

    python scripts/grain-fit.py --disk 3
    python scripts/grain-fit.py --disk 0 --partition 1 --samples 8 --sample-mib 32
    python scripts/grain-fit.py --disk 3 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p2v.gpt import TYPE_BASIC, TYPE_ESP, TYPE_MSR, parse_gpt       # noqa: E402
from p2v.safeio import open_physical_drive                          # noqa: E402

MIB = 1024 * 1024
SECTOR = 512
UNIT_BYTES = 64 * 1024          # 统计基准 = 最小的合法 grain
# vmdk 规范把 grain table 固定为 1 个扇区（512 项 x 4B），所以 grain 只能取
# 64 KiB 的整数倍 —— 候选集合是离散的，不能连续调
DEFAULT_CANDIDATES = (64, 128, 256, 512, 1024, 2048)        # KiB
META_BYTES_PER_GRAIN = 12       # 见模块 docstring 的推导

KIND = {TYPE_ESP: "ESP", TYPE_MSR: "MSR", TYPE_BASIC: "Basic"}


class FitError(RuntimeError):
    pass


def _kind(type_guid) -> str:
    return KIND.get(type_guid, "other")


def _progress(done: int, total: int, units: int) -> None:
    """采样进度写 stderr（stdout 留给结果，便于重定向）。"""
    print("\r  采样 %d/%d 段，已读 %.1f MiB" % (done, total, units * UNIT_BYTES / MIB),
          end="", file=sys.stderr, flush=True)
    if done >= total:
        print(file=sys.stderr)


def list_partitions(disk: int) -> list:
    """只读枚举源盘分区（返回轻量 dict，不持有设备）。"""
    with open_physical_drive(disk) as dev:
        gpt = parse_gpt(dev, dev.sector_size)
        parts = []
        for p in gpt.partitions:
            parts.append({
                "index": p.index,
                "kind": _kind(p.type_guid),
                "name": p.name or "",
                "first_lba": p.first_lba,
                "last_lba": p.last_lba,
                "size_bytes": (p.last_lba - p.first_lba + 1) * SECTOR,
            })
        return parts


def sample_units(dev, part: dict, samples: int, sample_mib: int, progress=None) -> list:
    """沿分区等距取 samples 段，返回每个 64 KiB 单元是否为零的布尔列表。

    末段贴着分区尾：大空洞通常在那里，不采它会系统性低估空洞。
    段长按分区大小收缩，保证各段不重叠（重叠会让同一区域被重复计入）。

    progress: 可选回调 progress(done, total, units_read)，仅用于输出进度。
    """
    total = part["size_bytes"]
    seg = sample_mib * MIB
    if samples > 1:
        seg = min(seg, max(UNIT_BYTES, total // samples))
    stride = (total - seg) // (samples - 1) if samples > 1 else 0
    units = []
    for seg_i in range(samples):
        start = part["first_lba"] * SECTOR + seg_i * stride
        if start + seg > (part["last_lba"] + 1) * SECTOR:
            start = max(part["first_lba"] * SECTOR,
                        (part["last_lba"] + 1) * SECTOR - seg)
        off = 0
        while off < seg:
            take = min(MIB, seg - off)
            buf = dev.read_at(start + off, take)
            limit = len(buf) - UNIT_BYTES
            for k in range(0, limit + 1, UNIT_BYTES):
                units.append(not any(buf[k:k + UNIT_BYTES]))
            off += take
        if progress:
            progress(seg_i + 1, samples, len(units))
    return units


def evaluate(units: list, candidates_kib, samples: int = 0) -> dict:
    """把 64 KiB 单元的零/非零序列聚合到各候选 grain，算产物大小与元数据。"""
    n = len(units)
    if n == 0:
        raise FitError("采样为空：分区太小或读取失败，无法给出结论")
    data_units = sum(1 for u in units if not u)
    rows = []
    for kib in candidates_kib:
        per = kib * 1024 // UNIT_BYTES
        alloc = 0
        for i in range(0, n, per):
            window = units[i:i + per]
            if len(window) < per or not all(window):      # 窗口内有任一非零单元
                alloc += 1
        file_units = alloc * per
        meta_bytes = alloc * META_BYTES_PER_GRAIN
        rows.append({
            "grain_kib": kib,
            "grain_bytes": kib * 1024,
            "allocated_grains": alloc,
            "file_units": file_units,
            "file_bytes": file_units * UNIT_BYTES,
            "metadata_bytes": meta_bytes,
            "overhead_ratio": round(file_units / max(data_units, 1), 4),
        })
    best = min(rows, key=lambda r: r["file_bytes"])
    return {
        "samples": samples,
        "sampled_units": n,
        "sampled_bytes": n * UNIT_BYTES,
        "data_units": data_units,
        "zero_units": n - data_units,
        "zero_fraction": round((n - data_units) / n, 4),
        "candidates": rows,
        "best_grain_kib": best["grain_kib"],
    }


def break_even_holes(rows: list) -> list:
    """对每个候选算"要比最优尺寸更省空间，需要多少个空洞边界"。

    判据：R * (g - g_best) / 2 > meta(g_best) - meta(g)
    数值越小说明该尺寸越容易在真实盘上胜出。
    """
    best = min(rows, key=lambda r: r["file_bytes"])
    out = []
    for r in rows:
        dg = (r["grain_bytes"] - best["grain_bytes"]) / 2.0
        dm = best["metadata_bytes"] - r["metadata_bytes"]
        if dg <= 0:
            out.append({"grain_kib": r["grain_kib"], "holes_needed": None})
        else:
            needed = round(dm / dg, 2) if dm > 0 else 0.0
            out.append({"grain_kib": r["grain_kib"], "holes_needed": needed})
    return out


def run(disk: int, partition: int | None, samples: int, sample_mib: int,
        candidates_kib) -> dict:
    parts = list_partitions(disk)
    picked = None
    if partition is not None:
        for p in parts:
            if p["index"] == partition:
                picked = p
                break
        if picked is None:
            raise FitError("磁盘 #%d 上没有分区 %d" % (disk, partition))
    else:
        basics = [p for p in parts if p["kind"] == "Basic"]
        if not basics:
            raise FitError("磁盘 #%d 上没有 Basic 数据分区；用 --partition 指定" % disk)
        picked = max(basics, key=lambda p: p["size_bytes"])

    with open_physical_drive(disk) as dev:
        units = sample_units(dev, picked, samples, sample_mib, progress=_progress)
        result = evaluate(units, candidates_kib, samples)
    result["disk"] = disk
    result["partition"] = {k: picked[k] for k in ("index", "kind", "name", "size_bytes")}
    result["break_even_holes"] = break_even_holes(result["candidates"])
    return result


def render(result: dict) -> str:
    p = result["partition"]
    lines = []
    lines.append("源盘 #%d  分区 #%d (%s)%s  容量 %.1f GiB"
                 % (result["disk"], p["index"], p["kind"],
                    " " + p["name"] if p["name"] else "",
                    p["size_bytes"] / 1024 ** 3))
    lines.append("采样 %.1f MiB（等距 %d 段 x %.0f MiB），零占比 %.1f%%"
                 % (result["sampled_bytes"] / MIB, result.get("samples", 0),
                    result["sampled_bytes"] / MIB / max(result.get("samples", 1), 1),
                    result["zero_fraction"] * 100))
    lines.append("")
    lines.append("  grain       产物/真实数据   元数据(样本)   翻盘所需空洞边界数")
    lines.append("  ---------   -------------   ------------   ------------------")
    be = {r["grain_kib"]: r["holes_needed"] for r in result["break_even_holes"]}
    for r in result["candidates"]:
        holes = be[r["grain_kib"]]
        holes_txt = "—（本样本最优）" if holes is None else ("%.2f" % holes)
        lines.append("  %-9s   %6.3fx        %8d B     %s"
                     % ("%d KiB" % r["grain_kib"], r["overhead_ratio"],
                        r["metadata_bytes"], holes_txt))
    lines.append("")
    lines.append("结论：本分区产物最小的 grain = %d KiB" % result["best_grain_kib"])
    lines.append("说明：产物大小 = 被分配 grain 数 x grain 大小；元数据按 12 字节/grain 计。")
    lines.append("      '翻盘所需空洞边界数'= 该尺寸的元数据节省 / 每个空洞边界多浪费的半个 grain。")
    lines.append("      数量远小于 1 表示「只要有一个空洞边界它就更亏」；数量很大才轮到它胜出。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="grain-fit",
        description="只读测量源盘的空洞分布，给出产物最小的 vmdk grain 尺寸")
    ap.add_argument("--disk", type=int, required=True, help="物理盘号（需管理员权限）")
    ap.add_argument("--partition", type=int, default=None,
                    help="分区号；默认选最大的 Basic 数据分区")
    ap.add_argument("--samples", type=int, default=8, help="等距采样段数（默认 8）")
    ap.add_argument("--sample-mib", type=int, default=32, help="每段读取的 MiB（默认 32）")
    ap.add_argument("--candidates", default=",".join(str(c) for c in DEFAULT_CANDIDATES),
                    help="候选 grain（KiB，逗号分隔；必须是 64 的倍数）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.samples < 1:
        print("--samples 至少为 1", file=sys.stderr)
        return 1
    if args.sample_mib < 1:
        print("--sample-mib 至少为 1", file=sys.stderr)
        return 1
    try:
        cands = tuple(int(x) for x in args.candidates.split(",") if x.strip())
    except ValueError:
        print("--candidates 需要逗号分隔的整数（KiB）", file=sys.stderr)
        return 1
    bad = [c for c in cands if c % 64 or c <= 0]
    if not cands or bad:
        print("候选 grain 必须是 64 KiB 的正整数倍，非法：%s" % bad, file=sys.stderr)
        return 1

    try:
        where = "--partition %d" % args.partition if args.partition else "自动选择最大 Basic 分区"
        print("grain-fit: 只读采样 磁盘 #%d（%s），%d 段 x %d MiB"
              % (args.disk, where, args.samples, args.sample_mib), file=sys.stderr)
        result = run(args.disk, args.partition, args.samples, args.sample_mib, cands)
    except FitError as exc:
        print("grain-fit: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("grain-fit: 设备读取失败（需要管理员权限）：%s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
