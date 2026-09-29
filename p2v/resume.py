# SPDX-License-Identifier: GPL-3.0-only
"""续传检查点（sidecar）读写。

为什么必须有 sidecar
--------------------
vmdk 的 GTE 表能回答"哪些 grain 已分配"，但回答不了"哪些 grain 是还没读"——
未分配既可能是全零（thin 正常跳过），也可能是流程还没走到。只有持久化的水位
能区分这两者。**GTE 表（分配事实）+ 水位（进度事实）** 构成续传的全部状态。

落盘顺序与崩溃语义
------------------
数据 -> GT/GD -> sidecar。sidecar 落后于 GT 是安全的（重跑会覆盖到同一位置，
因为分配位置由内容唯一决定）；sidecar 领先于 GT 绝不出现。tmp + os.replace
保证断电后读到的要么是旧的完整 sidecar，要么是新的完整 sidecar，不会是半截 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields

RESUME_VERSION = 1
SIDECAR_SUFFIX = ".p2v-resume.json"


class ResumeError(RuntimeError):
    """检查点缺失 / 损坏 / 与产物或计划不匹配。"""


def sidecar_path(target_path: str) -> str:
    """检查点路径与产物同名同目录：产物被删或改名，检查点自然失配。"""
    return target_path + SIDECAR_SUFFIX


@dataclass
class ResumeState:
    """一次导出（或续传）的持久化进度。

    字段都是"可验证的事实"，不含任何猜测：
      fingerprint       plan 指纹，计划变了就必须拒绝续传
      source_identity   源盘身份（序列号 / UniqueId / 容量），拒绝"换了一块盘接着写"
      segment_identities 每段的源身份（快照源记 Shadow Copy ID + 创建时间）
      segment_index     已完成到 plan 的第几个 partition 段（0-based）
      offset_in_segment 该段内已**完整提交**的字节偏移
      next_free_sector  分配器水位；恢复时以它为准回退，丢弃检查点之后的分配
    """

    fingerprint: str
    source_identity: dict
    target_path: str
    target_capacity: int
    capacity_sectors: int
    grain_sectors: int
    overhead: int
    chunk_mib: int
    segment_identities: list = field(default_factory=list)
    segment_index: int = 0
    offset_in_segment: int = 0
    allocated_grains: int = 0
    next_free_sector: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = RESUME_VERSION

    def as_dict(self) -> dict:
        return asdict(self)

    def save(self) -> str:
        """原子落盘：先写同目录临时文件并 fsync，再 os.replace 顶替旧文件。"""
        self.updated_at = time.time()
        path = sidecar_path(self.target_path)
        payload = json.dumps(self.as_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        directory = os.path.dirname(os.path.abspath(path)) or "."
        handle_fd, tmp = tempfile.mkstemp(prefix=".p2v-resume-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(handle_fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    @classmethod
    def load(cls, target_or_sidecar: str) -> "ResumeState":
        path = (target_or_sidecar if target_or_sidecar.endswith(SIDECAR_SUFFIX)
                else sidecar_path(target_or_sidecar))
        if not os.path.exists(path):
            raise ResumeError("找不到续传检查点：%s" % path)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError) as exc:
            raise ResumeError("续传检查点不可读 %s：%s" % (path, exc))
        if not isinstance(raw, dict):
            raise ResumeError("续传检查点结构不是对象：%s" % path)
        version = int(raw.get("version") or 0)
        if version != RESUME_VERSION:
            raise ResumeError("续传检查点版本 %d 不受支持（当前 %d），请删除后重跑"
                              % (version, RESUME_VERSION))
        known = {item.name for item in fields(cls)}
        missing = [key for key in ("fingerprint", "source_identity", "target_path",
                                   "target_capacity", "capacity_sectors",
                                   "grain_sectors", "overhead", "chunk_mib")
                   if key not in raw]
        if missing:
            raise ResumeError("续传检查点缺字段 %s：%s" % (missing, path))
        return cls(**{key: value for key, value in raw.items() if key in known})


def remove(target_or_sidecar: str) -> bool:
    """删除检查点（导出成功收尾时调用）。不存在返回 False。"""
    path = (target_or_sidecar if target_or_sidecar.endswith(SIDECAR_SUFFIX)
            else sidecar_path(target_or_sidecar))
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def identity_mismatch(recorded: dict, current: dict) -> str | None:
    """比对源盘身份。一致返回 None，否则返回人话原因。

    锚点优先级：序列号 / UniqueId（任一非空且相等即视为同一块盘）。两者都取不到时
    回退比容量。**盘号不参与判定** —— USB 盒重新插拔经常换 PhysicalDrive 号，
    而库里的 Number 又可能与另一块盘撞上。
    """
    if not recorded:
        return "检查点里没有源身份记录"
    if not current:
        return "当前无法读取源身份（权限或盘已离线）"
    if int(recorded.get("size") or 0) != int(current.get("size") or 0):
        return "源容量 %s -> %s" % (recorded.get("size"), current.get("size"))
    for key in ("serial_number", "unique_id"):
        want = str(recorded.get(key) or "").strip().upper()
        got = str(current.get(key) or "").strip().upper()
        if want and got:
            if want != got:
                return "源 %s 不匹配：%s -> %s" % (key, want, got)
            return None
    return None


def shadow_mismatch(recorded: dict, current: dict) -> str | None:
    """比对卷影副本身份。快照序号会被系统复用，只有 GUID + 创建时间可信。"""
    if not recorded:
        return "检查点里没有快照身份记录"
    if not current:
        return "导出所用的卷影副本已不存在（快照被回收 / 删除）"
    want = str(recorded.get("id") or "").strip().upper()
    got = str(current.get("id") or "").strip().upper()
    if want and got and want != got:
        return "快照 ID 不匹配：%s -> %s（序号被复用到了另一个快照）" % (want, got)
    want_time = str(recorded.get("install_date") or "").strip()
    got_time = str(current.get("install_date") or "").strip()
    if want_time and got_time and want_time != got_time:
        return "快照创建时间不匹配：%s -> %s" % (want_time, got_time)
    return None
