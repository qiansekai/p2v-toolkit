# p2v-toolkit

[![CI](https://github.com/qiansekai/p2v-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/qiansekai/p2v-toolkit/actions/workflows/ci.yml)

把物理盘上的「系统卷 + ESP」导出为可引导 vmdk 的 Windows 命令行工具。
**只读、定向、沿用源盘分区 GUID**，全程 CLI + JSON，可被脚本与 agent 驱动。

> **English** — `p2v-toolkit` exports the Windows system volume and ESP from a physical
> disk into a bootable monolithicSparse VMDK. Source devices are opened with
> `GENERIC_READ` only (the code exposes no write API), and the partition GUIDs, offsets,
> disk signature and protective MBR are copied from the source so `MountedDevices` and
> BCD references keep working. Windows 10/11, Python 3.9+, standard library only.
> Licensed GPL-3.0-only.

| | |
|---|---|
| 平台 | Windows 10 / 11（设备层走 Win32 API，其他平台不适用） |
| 运行时依赖 | 仅 Python 标准库（3.9+）+ 系统自带 PowerShell |
| 许可证 | GPL-3.0-only（见 `LICENSE`） |
| 状态 | 0.1.0 Beta；导出的 vmdk 仍需在 PE 里做两步人工收尾 |
| 续传 | `export --resume`：中断后从检查点继续，强校验计划指纹 / 源身份 / 快照身份 |

## 它解决什么问题

DiskGenius 一类工具能完成克隆，但会在克隆时**重建分区 GUID**，于是 Windows 的
`MountedDevices` 里 `\DosDevices\C:` 指向的旧 GUID 失效，系统盘被挂成 `V:`，
`ProfileList` 里的用户目录引用一并失效（作者踩过这个坑：盘符错乱 + 悬空设备实例）。
本工具不做这件事 —— 分区的 GUID、偏移、属性与磁盘签名全部逐项沿用源盘。

| | DiskGenius | p2v-toolkit |
|---|---|---|
| 源盘 | 只读克隆 | **只读**（代码层不存在写路径） |
| 分区 GUID | 重建（→ 盘符错乱） | **沿用原 GUID 与偏移** |
| 磁盘签名 / 保护性 MBR / 分区 attributes | 重写 | **逐项沿用源盘**（`verify` 会断言） |
| 目标布局 | 克隆时重组 | 默认不动布局；要扩容就在 PE 里跑 `scripts/expand-system-in-pe.cmd` |
| 数据盘 | 一并处理 | **跳过**（示例：源盘 931 GB 中的 588 GB 数据分区不复制） |
| 一致性 | VSS | 系统卷从 **VSS 快照**读；ESP 直读 |
| 接口 | GUI | CLI + JSON，计划可审阅、可被 agent 驱动 |

## 安装

```powershell
git clone https://github.com/qiansekai/p2v-toolkit.git
cd p2v-toolkit

# 方式一：直接跑，不安装
python -m p2v --help

# 方式二：装成命令 p2v
pip install -e .
p2v --help
```

读取 `\\.\PhysicalDriveN` 需要**管理员权限**，普通权限打不开物理盘。

## 安全模型

- 源设备**只以 `GENERIC_READ` 打开**，`p2v/safeio.py` 不提供任何写 API
  （没有 write / flush / set-length），调用方即使想写也无接口可用
- **不安装内核驱动**，不修改宿主机分区表 / BCD / VSS 配置
- `plan` 纯只读、零副作用；`export` 默认 dry-run，**必须 `--apply` 才落盘**
- 目标文件已存在一律拒绝；`--resume` 是唯一例外，它会逐项校验计划指纹、源盘身份与
  快照身份，任一不符直接报错（混合两个时刻的数据比重新导出更糟）
- 工具进程本身永远不碰宿主机状态；需要「整盘设为只读」时由
  `scripts/p2v-from-usb.ps1` 用 `Set-Disk -IsReadOnly` 完成（partmgr 层，不写源盘扇区）

## 快速开始

```powershell
# 1) 看源盘布局（只读；--json 给结构化输出）
python -m p2v probe --disk 3

# 2) 生成计划（纯只读，不创建文件；输出可审阅的 JSON）
python -m p2v plan --disk 3 --take "ESP,MSR,vol:C:" --out 'H:\sys-p2v.vmdk'

# 3) 导出（默认 dry-run；--apply 才写盘）
python -m p2v export --disk 3 --take "ESP,MSR,vol:C:" --out 'H:\sys-p2v.vmdk' --apply --json

# 4) 自检产物（结构 + 与源盘 / 快照抽样比对）
python -m p2v verify --vmdk 'H:\sys-p2v.vmdk' --source-disk 3
```

选择器：`ESP` / `MSR` / `part:N` / `vol:C:`。
`vol:` 只对本机在线的卷有效；跨机拆下来的盘请用 `part:N`。

退出码：`0` 成功 / `2` 校验失败 / `1` 错误。

读写块大小可调：`--chunk-mib N`（1..256，默认 4）。

### 中断续传

长任务（拆机盘动辄数小时）中断后不必从头再来：

```powershell
# 第一次跑到一半中断（断电 / 拔盘 / Ctrl+C 都可以）
python -m p2v export --disk 5 --take "ESP,MSR,part:3" --out 'H:\sys.vmdk' --apply --source-mode physical

# 接着跑：从检查点继续
python -m p2v export --disk 5 --take "ESP,MSR,part:3" --out 'H:\sys.vmdk' --apply --source-mode physical --resume
```

导出过程每 `--checkpoint-mib N`（1..8192，默认 256）字节落一次检查点：脏 grain table 与
GD/RGD 增量写盘，header 标成 `uncleanShutdown=1`，进度水位、计划指纹与源身份记在同名的
`<out>.p2v-resume.json` 里（原子替换）。成功收尾时检查点会被清掉。

**四条前提，任一不成立都直接拒绝续传（不会静默降级）：**

1. 计划未变 —— `--take` / 容量 / 分区 GUID 一致（比指纹，不比"看起来差不多"）
2. 源未变 —— 盘序列号 / UniqueId / 容量一致
3. 源盘只读 —— 物理源必须整盘只读（`Set-Disk -Number N -IsReadOnly $true`），
   否则中断期间的任意写入会让前后两段来自不同时刻
4. 快照未变 —— 快照源必须仍是同一个 `Shadow Copy ID`。注意
   `HarddiskVolumeShadowCopyN` 的**序号会被系统复用**，只看序号会接到另一个快照上

崩溃后最多重做一份检查点的数据（默认 256 MiB）。半成品能被 `verify` 认出来：
它会报 `vmdk_completed` 失败并说明这是未 finalize 的中间态。

### 导出后必须人工收尾（不可跳过）

**导出的 vmdk 不能直接开机。** 必须在 PE 里做两步（作者机器上两次独立复现）：

1. **引导修复** —— Dism++ → 引导修复
   （等价命令 `bcdboot C:\Windows /s <ESP盘符>: /f UEFI`）；不做会报 `0xc000000e`
   （`File: \Windows\system32\winload.efi`）
2. **删除所有后装驱动** —— Dism++ → 驱动管理 → 删除所有后装驱动（保留 in-box）

`export` 结束时会把这套提示同时打到 stdout 与 `--json` 的 `manual_steps` 字段。
原因与排查过程见项目作者的 P2V 排查笔记（黑屏分层取证 / 盘符错乱两层根因）。

## USB 硬盘盒里的拆机盘

跨机拆盘有两个坑：`vol:C:` 解析的是**本机**盘符；Windows 会在插上的瞬间挂载并回写
NTFS 日志。

```powershell
mountvol /N     # 接入【之前】执行：禁止自动挂载新卷
# …插盘… 用 Get-Disk 确认盘号
.\scripts\p2v-from-usb.ps1 -Disk 5 -Take "ESP,MSR,part:3" -Out 'H:\game-p2v.vmdk'
mountvol /E     # 收尾恢复自动挂载
```

脚本负责：拒绝把本机系统盘当源盘、检查 BitLocker、用 `part:N` + `--source-mode physical`
顺序跑 probe / plan / export / verify，并可选把源盘整盘设为只读（在 `finally` 里恢复并核对）。
开关：`-DryRun`（只检查与打印）、`-NoLock`、`-KeepReadOnly`。

## 格式转换（VHDX ↔ VMDK）

本工具**只输出 monolithicSparse vmdk**。要上 Hyper-V（或从 Hyper-V 迁回来）时用 qemu-img
做块级转换 —— 它是块级拷贝、**不做分区重组**，所以分区 GUID / 磁盘签名 / 保护性 MBR
都原样保留，不会重演 DiskGenius 那种盘符错乱。

```powershell
# 本机位置（注意不在 PATH 里）
$Q = 'D:\Kita-Tools\DevEnv\qemu-img\qemu-img.exe'

# vmdk -> vhdx（迁去 Hyper-V）
& $Q convert -p -f vmdk -O vhdx -o block_size=32M 'H:\sys-p2v.vmdk' 'H:\sys-p2v.vhdx'

# vhdx -> vmdk（从 Hyper-V 迁回来）
& $Q convert -p -f vhdx -O vmdk -o subformat=monolithicSparse,adapter_type=lsilogic `
    'H:\sys-p2v.vhdx' 'H:\sys-back.vmdk'
```

**中间文件**：`convert` 是流式的（读源写目标，不落 raw 中间盘），但**源文件必须保留**，
峰值空间约等于「源 + 目标」（示例：91 GB 的 vmdk 转 vhdx，峰值约 180 GB）。
确认产物可用后再删源。要彻底不产生中间文件只能自研 VHDX writer，但 VHDX 规范
（BAT / 元数据 / 日志区）比 sparse vmdk 复杂一个量级，不值得为省一份中间文件去写。

**转完不等于能开机**，跨 hypervisor 还有三件事：

1. **磁盘控制器 / 驱动**：VMware 的 SCSI 驱动与 Hyper-V 的合成驱动（`storvsc` / `netvsc`）不同，
   与 P2V 那两步收尾里的「删除所有后装驱动」是同类问题
2. **引导代次**：Hyper-V **Gen2 = UEFI**（与本工具的 ESP 产物一致），Gen1 = BIOS/MBR，选错不进引导
3. **adapterType**：反向转回 vmdk 时 qemu-img 用默认值，需要 `-o adapter_type=lsilogic`
   才与 VMware 原生一致

工具边界：`vmware-vdiskmanager` 只做 vmdk 内部转换、**不认 vhdx**；Hyper-V 自带的
`Convert-VHD` **不认 vmdk**；StarWind V2V 可双向但要装内核驱动（撞本项目的安全红线）。
双向都走 qemu-img。

## 已知边界

- **只支持 512 字节逻辑扇区**：扇区大小默认向设备查询，探到 4Kn 直接拒绝
  （GPT 的 LBA 与产物 vmdk 都按 512 解释，4Kn 会整体错位）
- `vol:C:` 只对本机在线卷有效，跨盘会被直接拒绝
- 源盘不是本机系统盘时（拆机盘 / 外接盘），`--source-mode auto` 会自动跳过 VSS 直读物理盘
- 只负责拷贝：**不重建引导引用、不清理后装驱动、不做驱动注入**
- 目标盘容量与源盘一致，未选中的分区保留为未分配空间（thin vmdk 不占空间）
- **续传未在真盘验证**：逻辑由 13 项内存替身回归覆盖（含"每个检查点边界各中断一次、
  续传产物与一次性导出逐字节相同"），但 USB 盒拔插换号、中断期间被系统自动挂载写入
  这两类真实场景尚未实测
- 续传要求物理源盘处于只读状态；离线拆机盘请先执行
  `Set-Disk -Number N -IsReadOnly $true`（`scripts/p2v-from-usb.ps1` 已经会做）
- `scripts/expand-system-in-pe.cmd` **未实机验证**：PE 内做系统分区扩容，使用前请自行确认
- VSS 是卷级技术，物理设备层不存在整盘快照；本工具按「ESP 直读 + 系统卷走快照」组合
- 吞吐实测（早期版本，128 GB 系统卷）：读 128 GB / 写 91 GB / 343 s / 382 MB/s；
  裸盘读写可达 1.2-1.4 GB/s，瓶颈在 VSS 快照路径与 Python 单线程循环

## 测试

```powershell
python -m unittest discover -s tests -t .
```

39 项单测，**不需要真实磁盘、不需要管理员权限**：设备层被内存替身替换，
因此 GPT / vmdk / export / verify / 续传的核心逻辑可以在任何机器上回归。
`tests/test_gpt.py` 与 `tests/test_vmdk.py` 跨平台，其余需要 Windows。
其中 `tests/test_resume.py` 的主回归是「在每一个检查点边界各中断一次，续传产物与
一次性导出逐字节相同」。

## 目录

```
p2v/
  safeio.py    只读设备层（物理盘 / VSS 快照 / 文件），无写 API
  gpt.py       GPT 解析与构造（保护性 MBR、主备表、CRC32）
  vmdk.py      sparse vmdk 读写（monolithicSparse）
  vss.py       卷影副本枚举与只读打开（本版本不创建快照）
  plan.py      导出计划（纯只读）
  export.py    按计划导出（默认 dry-run，支持 --resume 续传）
  resume.py    续传检查点（水位 / 源身份 / 原子落盘）
  verify.py    产物自检
  __main__.py  CLI 入口
tests/         单测（标准库 unittest）
scripts/
  p2v-from-usb.ps1          USB 拆机盘导出包装（安全闸 + 可选整盘只读）
  expand-system-in-pe.cmd   PE 内可选的系统分区扩容（未实机验证）
```

## 排错

- `0xc000000e`（`winload.efi`）：引导引用问题，在 PE 里做 `bcdboot` 引导修复
- 黑屏但 VM 有 CPU 占用且持续写盘：查盘符错乱（`MountedDevices`）与
  `Enum\ROOT\DISPLAY` / `GraphicsDrivers\Configuration` 残留
- VMware 报 `The specified virtual disk needs repair` / `Invalid GD or RGD`：
  sparse vmdk 元数据布局问题；先跑 `verify`，它对 GD / RGD / GT 位置关系有断言
- BitLocker 卷：克隆照常，但虚拟 TPM 与源机不是同一个，首次启动需 48 位恢复密钥

## 贡献

- 提交信息用中文，语义前缀（`fix:` / `feat:` / `docs:` / `test:` / `chore:`）
- 改 `export` / `vmdk` / `gpt` 后请跑 `python -m unittest discover -s tests -t .`
- 未经实机验证的能力必须在 README 与 `CHANGELOG.md` 里标注
- 新增文件保留 `SPDX-License-Identifier: GPL-3.0-only` 头

## 许可证

GPL-3.0-only，全文见 `LICENSE`。

本工具直接操作物理磁盘与系统卷，因此选择 GPL：衍生作品必须同样开源，
使用者能够审计它究竟对磁盘做了什么。
