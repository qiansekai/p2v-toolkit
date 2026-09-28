# p2v-toolkit

把物理盘上的系统卷 + ESP 导出为可引导 vmdk，并可显式重组目标盘布局。

## 为什么不是 DiskGenius

DG 能干这活，但它在克隆时**重建分区 GUID**，于是 Windows 的 `MountedDevices`
里 `\DosDevices\C:` 指向的旧 GUID 失效，系统盘被挂成 `V:` —— 这正是本项目作者
踩过的坑（盘符错乱 + 悬空设备实例）。本工具的做法：

| | DiskGenius | p2v-toolkit |
|---|---|---|
| 源盘 | 只读克隆 | **只读**（代码层不存在写路径） |
| 分区 GUID | 重建（→ 盘符错乱） | **沿用原 GUID 与偏移** |
| 目标布局 | 克隆时重组 | **v1 不动布局**；要扩容就在 PE 里跑 `scripts/expand-system-in-pe.cmd` |
| 数据盘 | 一并处理 | **跳过**（例：源盘 931GB 中的 588GB 数据分区不复制） |
| 一致性 | VSS | 系统卷从 **VSS 快照**读；ESP 直读（几乎不变） |
| 接口 | GUI | CLI + JSON，可断点审阅、可被 agent 驱动 |

## 安全约束

- 源设备**只以 GENERIC_READ 打开**，`p2v/safeio.py` 不提供任何写 API
- **不安装内核驱动**、不改宿主机分区表 / BCD / VSS 配置
- `plan` 纯只读、零副作用；`export` 默认 dry-run，`--apply` 才落盘
- 目标已存在一律拒绝
- 每个阶段都有自检：GPT 双 CRC、`verify` 子命令（可对比源盘内容采样）

## 完整流程

```powershell
# 1) 看源盘布局（只读）
python -m p2v probe --disk 3

# 2) 生成计划（只读，输出可审阅的 JSON；不会创建文件）
python -m p2v plan --disk 3 --take "ESP,MSR,vol:C:" --out 'H:\sys-p2v.vmdk'

# 3) 导出（默认 dry-run；加 --apply 才写盘）
python -m p2v export --disk 3 --take "ESP,MSR,vol:C:" --out 'H:\sys-p2v.vmdk' --apply --json
#    读写块大小可调：--chunk-mib N（1..256，默认 4）；块越大，Python 层循环与
#    grain 切分次数越少（128 GiB 在 4 MiB 下约 3.3 万次循环，64 MiB 下约 2 千次）
#    拆机盘 / USB 硬盘盒里的离线系统盘：用 part:N，并加 --source-mode physical
#      python -m p2v export --disk 5 --take "ESP,MSR,part:3" --out 'H:\game.vmdk' \
#          --source-mode physical --apply --json

# 4) 自检产物（结构 + 与源盘内容抽样比对）
python -m p2v verify --vmdk 'H:\sys-p2v.vmdk' --source-disk 3
```

## 导出后必须人工收尾（不可跳过）

**导出的 vmdk 不能直接开机。** 必须在 PE 里做两步（本机两次独立复现验证）：

1. **引导修复** —— Dism++ → 引导修复
   （等价命令：`bcdboot C:\Windows /s <ESP盘符>: /f UEFI`）
   不做会报 `0xc000000e`（`File: \Windows\system32\winload.efi`）
2. **删除所有后装驱动** —— Dism++ → 驱动管理 → 删除所有后装驱动（保留 in-box）
   不做可能因与原机硬件绑定的驱动在过引导后出问题

`export` 结束时会在 stdout 与 `--json` 的 `manual_steps` 字段里重复这段提示。
排查过程与根因记录见 `Notes\env\env-vmware-p2v.md`、`Notes\env\env-vmware-p2v-toolkit.md`。

可选收尾（需要 DiskGenius 那种「单分区」效果时）：

1. 用导出的 vmdk 建 VM 并从 FirPE 启动；
2. 在 PE 里运行 `scripts\expand-system-in-pe.cmd`（只对系统卷做 `diskpart extend`，
   不删分区、不改 GUID）；
3. 关机，改回从硬盘启动验证。

## 依赖

运行：仅 Python 标准库（ctypes / struct / zlib / json）+ Windows 自带 `powershell`。
`qemu-img`、`dissect.target` 只用于**交叉验证**，不是运行依赖。

## 目录

```
p2v/
  safeio.py   只读设备层（物理盘 / VSS 快照 / 文件），无写 API
  gpt.py      GPT 解析与构造（保护性 MBR、主备表、CRC32）
  vmdk.py     sparse vmdk 读写（monolithicSparse）
  vss.py      卷影副本枚举 / 卷容量（默认不改 VSS 状态）
  plan.py     导出计划（纯只读）
  export.py   按计划导出（默认 dry-run）
  verify.py   产物自检
scripts/
  expand-system-in-pe.cmd   PE 内可选的系统分区扩容
```

## 已知边界

- 目标盘容量与源盘一致，未选中的分区保留为未分配空间（thin vmdk 不占空间）
- **只支持 512 字节逻辑扇区**：扇区大小默认向设备查询（`--sector-size` 只是覆盖），
  探到 4Kn(4096) 直接拒绝——GPT 的 LBA 与产物 vmdk 都按 512 解释，4Kn 会整体错位
- `vol:C:` 只对**本机在线的卷**有效；USB 盒里的离线系统盘请用 `part:N`
  （`vol:` 拿到的是本机盘符，跨盘会被直接拒绝）
- 源盘**不是本机系统盘**时（拆机盘、外接盘）`--source-mode auto` 会自动跳过 VSS
  直读物理盘：这类盘没有并发写入，VSS 多余且会误用盘上残留的卷影副本
- 只负责拷贝：**不重建引导引用、不清理后装驱动**（见上节，必须在 PE 里人工完成两次收尾）
- 不做驱动注入（与 DG 相同）；换硬件后仍需处理驱动适配
- 仅支持 GPT + 512B 扇区
- VSS 为卷级技术，物理设备层面不存在整盘快照；本项目按「ESP 直读 + 系统卷走快照」组合
