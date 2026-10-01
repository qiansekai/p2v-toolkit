# Changelog

本文件记录 p2v-toolkit 的可见变更。格式参考 Keep a Changelog，版本号遵循语义化版本。

## [Unreleased]

### 工具

- `scripts/grain-fit.py`：只读测量源盘的空洞分布，给出"该卷用多大 grain 产物最小"。
  等距采样（默认 8 段 x 32 MiB，末段贴分区尾）、按 64 KiB 单元聚合到各候选 grain，
  输出产物倍率 / 元数据 / 翻盘门槛，支持 `--json`。**grain 尺寸是实测最优点，不是旋钮**：
  1 TiB 容量下元数据总共 128 MiB（0.0122%），放大 grain 只会让跨在数据边缘的空洞被
  整块分配。两台真机实测（零占比 25~37%）产物最小的都是 64 KiB；1 MiB 胀 0.6%~4%，
  2048 KiB 胀 1.2%~6.4%
- `scripts/p2v-live-system.ps1`：本机**在运行的系统卷**导出包装。工具刻意不建快照
  （`vss.py` 只枚举与只读打开现有快照），所以建 / 删快照由脚本承担：为 `--take` 里的每个
  `vol:X:` 建卷影副本（CIM `Win32_ShadowCopy.Create`）-> `--source-mode shadow` 跑
  plan / export / verify -> `finally` 清理（成功即删；失败默认保留，因为 `--resume` 要求
  快照身份不变）。闸门与 `-DryRun` 已实测（源盘非系统盘 / 目标已存在 /
  `-Resume` 缺检查点均正确拒绝）；建 / 删快照两条命令路径均已在本机实机跑通（删除路径是把
  脚本里的真实函数取出来跑：建临时快照 -> 删除 -> 复核为 0），**端到端一次跑通待下一次真实
  转换验证**。三个实测坑记在这里：
  ① 客户端版 Windows 的 `vssadmin` **没有 create 子命令**（`vssadmin create shadow` 报
  `Invalid command`），`diskshadow` 本机也不存在 —— 创建只能走 CIM 静态方法
  `Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create`；
  ② **删除不能用 `Invoke-CimMethod -MethodName Delete`**：`Win32_ShadowCopy` 的 CIM 方法表
  只有 `Create` / `Revert`，实测报「找不到方法 Delete」；正确做法是 `Remove-CimInstance`（走提供
  程序的实例删除路径），脚本在删除后**复核**快照是否真的消失，而不是相信返回值；
  ③ 快照 COW 上限默认是卷的 10%（本机 C 盘 12.8 GB），跑完不删会一直挂着。
- `scripts/p2v-live-system.ps1` 新增 BCD 判据预检：导出前只读挂载源机 ESP，按
  `\EFI\Microsoft\Boot\BCD` 的大小直接告诉你产物要不要进 PE（36864 需要 / 40960 不需要），
  并可用 `-PreflightBcdboot` 在导出前于源机重写一遍（默认关闭 —— 它改写源机引导配置）；
  `-SkipBcdCheck` 可跳过。**同时修掉一处误删**：早先修 `Remove-VolumeShadow` 时把
  `# 5) 命令拼装` 段的前 5 行一并替换掉了，脚本从那时起无法运行（用 AST 只提函数跑的
  删除路径测试察觉不到；这次靠整体语法检查抓到）。
- 新增 `p2v vmx` 子命令：从产物 vmdk 生成 VMware Workstation 的 `.vmx`。把 2026-10-01 踩过的
  「手写 vmx 漏掉 PCIe 根端口 -> `SCSI0 没有可用的 PCIe 插槽 / 配置的 PCI 设备过多`」
  固化成生成器：`pciBridge0/4/5/6/7`（`pcieRootPort`，functions=8）+ 显式 `pciSlotNumber`（scsi0=16、
  根端口=17/21/22/23/24、usb=32、ethernet0=33；槽位沿用本机可正常启动的 CentOS 母本，生成前
  自检槽位不重复）。固件类型由产物 GPT 里有没有 ESP 决定（efi/bios），不靠猜；网卡默认
  `e1000e`（guest 没有 VMware Tools，只有内置驱动可用）；`--firmware` 与自动探测不符时给出警告。

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

### 性能

导出热路径（`p2v/vmdk.py`）按"CPU 不受源盘/目标盘限制"重新打磨。改动都在 Python 内，
没有换语言、没有引入依赖；正确性由 43 项单测（新增 4 项热路径语义回归）与既有
续传逐字节等价断言共同守住：

- **检查点只落脏 grain table，并把相邻脏表合并成一次大块写**。此前 `checkpoint()`
  名字叫"增量"，实际每次无条件遍历全表：1 TiB 容量是 32768 张表、主区+冗余区各写一遍
  = 每次检查点固定写 64 MiB，与真正脏了几张表无关。按 256 MiB 的检查点间隔，这是
  50% 以上的时间税（实测单次 528 ms → 54 ms）
- **GT 惰性分配**：`gts` 里未用到的表保持 `None`，写到时才补齐。1 TiB 容量下省掉
  32768 × 512 项的预分配，元数据堆占用 386 MiB → 257 MiB
- **GD / RGD 载荷缓存**：这两区字节只由容量决定，整个生命周期是常量，不再逐次检查点现算
- **对齐整块写的零扫描只做一次**：`write_at` 先对整块做一次 C 级扫描，结论传给 grain
  写入，不再逐 64 KiB 重扫；grain 下标增量维护
- 非零数据吞吐（4 MiB 块）：约 400 → 1170 MB/s；block 越大收益越明显

同时修掉两个热路径打磨中引入、并被新增回归测试锁住的正确性缺陷（见下方"修复"）。

### 修复

- **「导出后必须进 PE」被修正为有判据的条件动作**：产物能否直接开机取决于源机 ESP 上
  `\EFI\Microsoft\Boot\BCD` 的大小 —— 36864（出厂原始 hive）需要在 PE 里 bcdboot；40960（已被
  Dism++ 引导修复重写过）可直接开机。2026-10-01 实测：源机 BCD 已在 PE 里被重写为 40960，
  同一套工具导出的产物一次点亮、未进 PE；两次的设备引用完全相同，起决定作用的是 **BCD hive
  本身**，不是设备引用、也不是 `{fwbootmgr}`。`MANUAL_POST_STEPS` 与 README 同步改写。
- **卷影副本必须正向匹配卷身份（曾产生错盘镜像）**：`latest_shadow_for_volume()` 原先在
  "本卷没有任何快照"时保留全部快照再取最新，于是没有快照的卷会拿到**别的卷**的快照当
  数据源 —— 实测 C 卷无快照、D 卷有两张，C 盘的数据段被指向 D 卷的
  `HarddiskVolumeShadowCopy3`。产物结构合法、`verify` 全绿，但盘里装的是另一个卷的内容，
  比"没有快照"糟糕得多。现在卷身份（`Get-Volume` 的 `UniqueId`）取不到、或与快照
  `VolumeName` 匹配为空时一律返回 `None`：auto 模式回落直读物理盘并在计划里说明，
  `--source-mode shadow` 仍然直接报错
- **零判据不得抽样**：`_nonzero` 一度只看首尾采样区间来判"是否全零"，于是"数据落在
  grain 中段、首尾皆零"的写被当成 thin 空洞丢弃 —— 真实触发点是 GPT 备份分区表，
  它落在 64 KiB grain 的 48640 偏移处。现在按全量 C 级扫描判定
- **空 GT 不得复用他人内容**：`_write_gt` 的共享解包缓存会把缓存槽位改名指向最近一次
  写入的表，导致后续所有"空表"写出前一张表的数据（备份 GPT 因此被覆盖）

- 初始检查点漏记分配器状态（`next_free_sector` / `allocated_grains` 记成 0），
  会让续传回退时把 GPT 元数据所在的 grain 一起清掉

### 测试

- 新增 `tests/test_resume.py`（14 项）：每个检查点边界各中断一次后逐字节比对、
- `tests/test_vmdk.py` 新增 4 项热路径语义回归：零判据不得抽样（数据落在 grain 中段）、
- `tests/test_grain_fit.py` 新增 9 项 grain-fit 纯逻辑回归（全零盘、中段空洞、
  尾部空洞免代价、末尾半窗、候选合法性、翻盘门槛单调性）
- `tests/test_plan.py` 新增 5 项卷影副本卷匹配回归（跨卷顶替必须为 None、本卷取最新、
  卷身份未知、无任何快照、无序号条目）；合计 57 项
  惰性 GT 必须正确回写、检查点只落脏表且相邻脏表合并、未脏表原样保留；合计 43 项
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
