# Changelog

本文件记录 p2v-toolkit 的可见变更。格式参考 Keep a Changelog，版本号遵循语义化版本。

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
