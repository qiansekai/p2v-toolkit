# Changelog

本文件记录 p2v-toolkit 的可见变更。格式参考 Keep a Changelog，版本号遵循语义化版本。

## [Unreleased]

### 功能

- `export --resume`：中断后从检查点续传。导出时每 `--checkpoint-mib`（默认 256 MiB）
  落一次检查点 —— 脏 grain table 增量写盘 + GD/RGD 全量写 + header 置
  `uncleanShutdown=1`，水位记在同名 `.p2v-resume.json`（原子替换）。恢复时按水位回退
  分配器，清掉水位之后的分配并重写，最终产物与一次性导出逐字节相同
- `--checkpoint-mib N`（1..8192）：检查点间隔
- `verify` 新增 `vmdk_completed` 断言：把未 finalize 的半成品认出来，
  而不是让它看起来像正常产物

### 安全

- 续传的四条强校验，任一不符都直接拒绝、不静默降级：计划指纹、源盘身份
  （序列号 / UniqueId / 容量）、快照身份（`Shadow Copy ID` + 创建时间，**不认会被复用的
  `HarddiskVolumeShadowCopyN` 序号**）、物理源盘必须整盘只读
- 快照在两次运行之间被回收或换号时拒绝续传：前 30% 来自快照 A、后 70% 来自物理盘，
  这种"看起来成功"的撕裂镜像比重新导出一遍糟糕得多

### 修复

- 初始检查点漏记分配器状态（`next_free_sector` / `allocated_grains` 记成 0），
  会让续传回退时把 GPT 元数据所在的 grain 一起清掉

### 测试

- 新增 `tests/test_resume.py`（14 项）：每个检查点边界各中断一次后逐字节比对、
  变间隔续传、快照被拉回原始序号，以及六条拒绝路径（缺检查点 / 计划变化 / 只读丢失 /
  换盘 / 快照消失 / 产物已完成）+ 半成品被 verify 认出；合计 39 项

### 文档

- README 新增「格式转换（VHDX ↔ VMDK）」：qemu-img 双向命令、中间文件与峰值空间说明、
  跨 hypervisor 的三个坑（控制器驱动 / 引导代次 / adapterType），以及为什么不自研 VHDX writer

## [0.1.0] - 2026-09-29

首个公开版本。

### 功能

- `probe`：只读枚举磁盘 GPT 布局，结果带主 / 备 CRC 自检
- `plan`：生成可审阅的导出计划（纯只读，不创建任何文件）
- `export`：按计划导出 monolithicSparse vmdk（默认 dry-run，`--apply` 才落盘）
- `verify`：自包含解析产物 vmdk，校验 GPT 主备结构与 GD / RGD / GT 布局，
  可抽样比对源盘或卷影副本
- `scripts/p2v-from-usb.ps1`：USB 拆机盘导出包装（安全闸 + 可选整盘只读 + 顺序跑四步）
- `scripts/expand-system-in-pe.cmd`：PE 内可选的系统分区扩容（**未实机验证**）

### 安全

- 源设备只以 `GENERIC_READ` 打开，`p2v/safeio.py` 不提供任何写接口
- 目标文件已存在一律拒绝；`export` 默认 dry-run
- `vol:` 选择器的盘符限定为单个字母：此前 `vol:C:; <命令>; #` 这类输入可注入 PowerShell
- `verify --source-shadow` 缺少 `--source-disk` 时报错，不再静默回落到 `PhysicalDrive3`

### 修复

- `export --apply` 在第一个分块之后必定崩溃：`chunk` 变量同时被当作字节数与数据缓冲
  复用，第二轮迭代抛 `TypeError: '<' not supported between instances of 'int' and 'bytes'`
- `SparseVmdkReader` / `ReadOnlyDevice` 构造失败时泄漏文件 / 设备句柄
- 产物结构损坏时 `verify` 返回失败明细（退出码 2），不再抛成通用错误（退出码 1）
- 文档与实现对齐：`__main__` 不再声称子命令「待实现」，`vss` 不再声称存在 `--create-shadow`

### 测试

- 新增 `tests/`（标准库 `unittest`，零外部依赖，无需真实磁盘）：
  GPT 构造 / 解析往返、vmdk 读写往返、导出多轮分块与卷影补零、
  verify 结构检查与守卫，共 25 项
