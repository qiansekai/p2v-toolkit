# p2v-toolkit

agent 友好的 P2V 工具链：把物理盘上的「系统卷 + ESP」做成可引导 vmdk。
许可证 **GPL-3.0-only** —— `LICENSE` 不得删除或替换。

## 定位

- 替代 DiskGenius 的**技术能力**，不是替代它的 GUI。核心差异：
  1. **定向只取**（ESP + 指定系统卷），**不复制数据盘**（源盘 932GB / 数据盘 588GB 被跳过）
  2. **沿用原分区 GUID 与偏移**，避免 `MountedDevices` 失配导致的 C: -> V: 盘符错乱
  3. 全程 CLI + JSON，可被 agent 驱动（`export --resume` 支持中断续传，见「外部契约」）

## 安全红线（最高优先级，改代码前必读）

- **源设备只以 GENERIC_READ 打开**；`p2v/safeio.py` 不提供任何写 API，物理盘永不写。
- **不安装任何内核驱动**（不跑 StarWind 的 `[Install].bat`），不修改宿主机分区表 / BCD / VSS 配置。
- 产物只写新文件；目标已存在一律拒绝（除非显式 --force）。
- 长任务默认 `--dry-run`，必须显式 `--apply` 才落盘。
- 每步可验证：GPT CRC32 自检、产物用 `qemu-img info` / `dissect` 交叉验证。
- **整盘只读化不属于本工具**：需要时由 `scripts/p2v-from-usb.ps1` 用 `Set-Disk -IsReadOnly`
  完成（partmgr 层，不写源盘扇区），工具进程本身永远不碰宿主状态；不要为它给 `safeio` 开写权限口子。

## 目录

```
p2v/
  safeio.py    只读设备访问（物理盘 / VSS 快照 / 文件）+ 安全护栏
  gpt.py       GPT 解析与构造（含 CRC32）
  vmdk.py      sparse vmdk 读写
  vss.py       卷影副本枚举与只读打开（工具自身不创建快照）
  plan.py      导出计划（纯只读）
  export.py    按计划导出（默认 dry-run，支持 --resume）
  resume.py    续传检查点（水位 / 源身份 / 原子落盘）
  verify.py    产物自检
  __main__.py  CLI 入口：probe / plan / export / verify
tests/         单测（标准库 unittest，无需真盘）
scripts/
  p2v-from-usb.ps1          USB 拆机盘导出包装（安全闸 + 可选整盘只读 + 顺序跑四步）
  expand-system-in-pe.cmd   PE 内可选的系统分区扩容（未实机验证）
  grain-fit.py              只读测源盘空洞分布，回答"该用多大 grain"（见「grain 尺寸」）
pyproject.toml / CHANGELOG.md / LICENSE / .github/workflows/ci.yml
```

## 开发约定

- 测试：`python -m unittest discover -s tests -t .`（52 项，无需真盘、无需管理员权限 —— 设备层被内存替身替换）
- **改 `export` / `vmdk` / `gpt` 必须跑测试**：分块循环曾因变量复用出现「首轮后必崩」，
  而当时没有任何自动化回归，只能靠真盘手工跑
- 新增文件保留 `# SPDX-License-Identifier: GPL-3.0-only` 头
- 未经实机验证的能力必须在 README 与 `CHANGELOG.md` 标注，不要写成可用功能
- 提交信息用中文 + 语义前缀（fix / feat / docs / test / chore）

## grain 尺寸（不要改成"可调旋钮"）

`GRAIN_SECTORS = 128`（一个 grain = 128 个 512B 扇区 = 64 KiB）不是随手选的默认值，而是**实测下的最优点**，
且受格式约束限定为离散集合。

- **约束**：vmdk 把 grain table 固定为 1 个扇区（512 项 x 4B），所以 `GRAIN_SECTORS`
  只能是 128 的整数倍 —— 候选是 64 KiB / 128 KiB / 256 KiB / ...，不能连续调
- **两笔对冲的成本**：元数据 = 12 字节/grain = 12 x 容量 / grain（grain 越大越省）；
  空洞边界的对齐浪费 = 空洞边界数 x grain / 2（grain 越大越亏）。
  1 TiB 容量下元数据总共才 128 MiB（0.0122%），而放大 grain 会直接胀产物
- **实测（`scripts/grain-fit.py`，两台真机）**：真实卷的空洞不是少量大块，而是大量
  中等块（零占比 25~37%，却有成百上千个 64 KiB 级零 run）。因此产物最小的尺寸就是
  候选集合的最小值 64 KiB；放大到 1 MiB 产物胀 0.6%~4%，2048 KiB 胀 1.2%~6.4%
- **换源盘要重算**：`python scripts/grain-fit.py --disk N`。近乎全零的脏盘是反例
  （空洞边界少，大 grain 反而更省），届时结论会变；脚本会直接给出该盘的答案

结论：**保持 64 KiB，且不提供 --grain 选项** —— 加了只会给人一个能把产物吹大的开关。

## 热路径约定（改 `p2v/vmdk.py` 的写入路径前必读）

导出是单线程顺序拷贝。在「源盘与目标盘都不是瓶颈」的前提下，CPU 全花在 Python 层，
所以下面每一条都不是"风格"，而是被基准量过、且被回归测试锁住的约束：

- **零判据必须全量**（`_nonzero`）：不许用"抽样若干段"判"是否全零"。零字节与数据字节
  的分布没有任何约束 —— GPT 备份分区表就落在 64 KiB grain 的 48640 偏移处，首尾各 4 KiB
  都是零，抽样会把它当 thin 空洞丢掉（真实踩过）。bytes/bytearray 上的 `any()` 本身就是
  C 循环，4 MiB 约 1.6 TB/s，不需要再优化
- **grain table 惰性分配**：`gts` 里未写到的表保持 `None`，写入时经 `_pad_gt()` 补齐。
  1 TiB 容量满建是 32768 张 × 512 项 ≈ 130 MiB，一次导出通常只用到四分之一。
  任何遍历 `gts` 的新代码都必须处理 `None`（`_write_gt` / `_write_gt_range` / `rewind` 已处理）
- **检查点只写脏表，且相邻脏表合并成一次写**（`_dirty_runs` + `_write_gt_range`）：
  GT 区在 1 TiB 下是 64 MiB，退化成"每张表两次 write"会让一次检查点从亚毫秒涨到几百毫秒
- **不许在写路径里共享可变的解包缓存**：曾经用 `values[0] = tuple(row)` 缓存，等于把缓存
  槽位改名指向最近一次写入的表，后续"空表"就会写出前一张表的内容（备份 GPT 被覆盖）
- **对齐整块写的零扫描只做一次**：`write_at` 对整块扫一次，结论经 `nonzero=True` 传给
  `_write_grain_at`，不要逐 64 KiB 重扫

## 外部契约（改代码前注意）

- **只支持 512B 逻辑扇区**：扇区大小向设备查询（`IOCTL_DISK_GET_DRIVE_GEOMETRY_EX`），
  探到 4Kn 直接拒绝——GPT 的 LBA 与产物 vmdk 都按 512 解释，4Kn 会整体错位
- **元数据逐项沿用源盘**：磁盘签名 / 保护性 MBR 末 LBA / 分区 attributes 都从源盘读入后原样写出，
  `verify` 有对应断言；不要改回「重新计算」
- **VSS 只在源盘就是本机系统盘时才用**（`--source-mode auto`）：拆机盘 / 外接盘直读物理盘，
  它们没有并发写入，走 VSS 反而会误用盘上残留的卷影副本
- **`vol:C:` 只对本机在线的卷有效**：跨机拆盘必须用 `part:N`（跨盘会被直接拒绝）；
  盘符限定为单个字母（会拼进 PowerShell，禁止放宽）
- **`verify --source-shadow` 必须同时给 `--source-disk`**：卷影副本是卷级的，不含分区表
- **续传（`export --resume`）的四条强校验不许放宽**：计划指纹、源盘身份、快照身份
  （`Shadow Copy ID`，**不是会被复用的 `HarddiskVolumeShadowCopyN` 序号**）、物理源盘只读。
  任何一条不符都必须报错而不是降级 —— "看起来成功"的撕裂镜像比重新导出更糟
- **分配位置由"写入顺序"决定，而写入顺序由 plan 的段顺序决定**（`--take` 可以是非递增的）。
  所以回退判据只能用数据位置（`next_free_sector`），不能用 grain 下标

## 已验证的关键事实（2026-09-28）

- `qemu-img` **打不开** VSS 快照设备（它依赖 IOCTL 查容量，VSS 设备不响应）
- 但用 Win32 `CreateFile` + `ReadFile` **可以**顺序读 VSS 设备 —— 实测读到 C 卷 NTFS VBR
- VSS 是**卷级**技术，物理设备层面不存在「整盘快照」；
  所谓「在线一致整盘克隆」= 逐卷 VSS + 分区表重组
- StarWind V2V Converter 9.0.0.202 的**整盘 CLI 路径**为裸读（日志无 VSS 调用）
- 本机 `diskshadow` 缺失，`vssadmin` / `wbadmin` 可用

## 未实机验证（不要当成可用功能）

- `scripts/expand-system-in-pe.cmd`：PE 内系统分区扩容，从未在真实 PE 里跑过
- 4Kn 盘、>2 TiB 源盘、多系统卷组合：只在 512B / 单系统卷场景实测
- 续传：只有内存替身回归（含逐字节等价），USB 盒拔插换号、中断期间被系统挂载写盘
  这两类真实场景未实测
