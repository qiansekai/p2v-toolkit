# SPDX-License-Identifier: GPL-3.0-only
"""grain-fit 的纯逻辑单测（不碰真实磁盘）。

被测函数都以"64 KiB 单元的零/非零序列"为输入，所以可以完全用构造数据驱动。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importlib import import_module

# 文件名带连字符，不能直接 import；用 importlib 从路径加载
import importlib.util

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "grain-fit.py")
_spec = importlib.util.spec_from_file_location("grain_fit", _PATH)
grain_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grain_fit)


def units_from(pattern: str) -> list:
    """pattern 用 '.' 表示数据单元，'z' 表示零单元。"""
    return [ch == "z" for ch in pattern]


class EvaluateTest(unittest.TestCase):
    def test_all_zero_gives_empty_product(self):
        """整段全零 -> 没有任何 grain 被分配，产物只有元数据。"""
        r = grain_fit.evaluate(units_from("z" * 64), (64, 128))
        self.assertEqual(r["zero_fraction"], 1.0)
        for row in r["candidates"]:
            self.assertEqual(row["allocated_grains"], 0)
            self.assertEqual(row["file_bytes"], 0)

    def test_all_data_ratio_is_one_at_min_grain(self):
        """全数据时，最小 grain（64 KiB）的产物倍率必然是 1.000。"""
        r = grain_fit.evaluate(units_from("." * 16), (64,))
        self.assertEqual(r["candidates"][0]["overhead_ratio"], 1.0)

    def test_bigger_grain_absorbs_hole_and_grows_file(self):
        """一个夹在数据中间的 64 KiB 空洞：64 KiB grain 跳过它，128 KiB 只能整块分配。"""
        units = units_from("..." + "z" + "...")
        r = grain_fit.evaluate(units, (64, 128))
        small, big = r["candidates"]
        self.assertEqual(small["allocated_grains"], 6)      # 零单元被跳过
        self.assertEqual(big["allocated_grains"], 4)        # 128 KiB 窗口：2 个完整 + 首尾各半
        self.assertLess(small["file_bytes"], big["file_bytes"])
        self.assertEqual(r["best_grain_kib"], 64)

    def test_trailing_hole_is_free_at_any_grain(self):
        """末尾的空洞不产生边界代价：任何 grain 都不会为它分配。"""
        units = units_from("." * 8 + "z" * 8)
        r = grain_fit.evaluate(units, (64, 128, 256))
        for row in r["candidates"]:
            self.assertEqual(row["file_units"], 8)          # 只分配前 8 个单元

    def test_last_partial_window_counts_as_allocated(self):
        """尾部不足一个 grain 的窗口：只要含数据就必须分配。"""
        r = grain_fit.evaluate(units_from("." * 3), (256,))   # 256 KiB = 4 个单元
        row = r["candidates"][0]
        self.assertEqual(row["allocated_grains"], 1)
        self.assertEqual(row["file_units"], 4)                # 写出整个 256 KiB grain

    def test_empty_sample_is_rejected(self):
        with self.assertRaises(grain_fit.FitError):
            grain_fit.evaluate([], (64,))


class BreakEvenTest(unittest.TestCase):
    def test_best_grain_has_no_break_even(self):
        rows = grain_fit.evaluate(units_from("." * 32), (64, 128))["candidates"]
        be = {r["grain_kib"]: r["holes_needed"] for r in grain_fit.break_even_holes(rows)}
        self.assertIsNone(be[64])
        self.assertIsNotNone(be[128])

    def test_break_even_shrinks_as_grain_grows(self):
        """grain 越大，元数据省得越多但每边界亏得越多 -> 翻盘门槛单调下降。"""
        rows = grain_fit.evaluate(units_from(("." * 8 + "z" * 8) * 32),
                                  (64, 128, 256, 512))["candidates"]
        vals = [r["holes_needed"] for r in grain_fit.break_even_holes(rows)
                if r["holes_needed"] is not None]
        self.assertEqual(vals, sorted(vals, reverse=True))


class CandidateValidationTest(unittest.TestCase):
    def test_candidates_must_be_multiples_of_64_kib(self):
        """vmdk 的 grain table 固定 1 个扇区，所以 grain 只能是 64 KiB 的整数倍。"""
        bad = [c for c in (96, 100, 1000) if c % 64]
        self.assertEqual(bad, [96, 100, 1000])
        for good in grain_fit.DEFAULT_CANDIDATES:
            self.assertEqual(good % 64, 0)


if __name__ == "__main__":
    unittest.main()
