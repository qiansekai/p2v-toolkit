# p2v-toolkit

agent 友好的 P2V 工具链：把物理盘上的「系统卷 + ESP」做成可引导 vmdk，
并可重组目标盘分区布局（等价 DiskGenius 的「在线克隆 + 布局重组」，但更省空间、不动原 GUID）。

## 定位

- 替代 DiskGenius 的**技术能力**，不是替代它的 GUI。核心差异：
  1. **定向只取**（ESP + 指定系统卷），**不复制数据盘**（源盘 932GB / 数据盘 588GB 被跳过）
  2. **沿用原分区 GUID 与偏移**，避免 `MountedDevices` 失配导致的 C: -> V: 盘符错乱
  3. 全程 CLI + JSON，可被 agent 驱动（断点续传**尚未实现**，别照抄旧说法）

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
  safeio.py    只读设备访问（物理盘 / VSS 快照）+ 安全护栏
  gpt.py       GPT 解析与构造（含 CRC32）
  vmdk.py      sparse vmdk 读写
  vss.py       卷影副本枚举与只读打开（工具自身不创建快照）
  __main__.py  CLI 入口：probe / plan / export / verify
scripts/
  p2v-from-usb.ps1          USB 拆机盘导出包装（安全闸 + 可选整盘只读 + 顺序跑四步）
  expand-system-in-pe.cmd   PE 内可选的系统分区扩容（未实机验证）
```

## 外部契约（改代码前注意）

- **只支持 512B 逻辑扇区**：扇区大小向设备查询（`IOCTL_DISK_GET_DRIVE_GEOMETRY_EX`），
  探到 4Kn 直接拒绝——GPT 的 LBA 与产物 vmdk 都按 512 解释，4Kn 会整体错位
- **元数据逐项沿用源盘**：磁盘签名 / 保护性 MBR 末 LBA / 分区 attributes 都从源盘读入后原样写出，
  `verify` 有对应断言；不要改回「重新计算」
- **VSS 只在源盘就是本机系统盘时才用**（`--source-mode auto`）：拆机盘 / 外接盘直读物理盘，
  它们没有并发写入，走 VSS 反而会误用盘上残留的卷影副本
- **`vol:C:` 只对本机在线的卷有效**：跨机拆盘必须用 `part:N`（跨盘会被直接拒绝）

## 已验证的关键事实（2026-09-28）

- `qemu-img` **打不开** VSS 快照设备（它依赖 IOCTL 查容量，VSS 设备不响应）
- 但用 Win32 `CreateFile` + `ReadFile` **可以**顺序读 VSS 设备 —— 实测读到 C 卷 NTFS VBR
- VSS 是**卷级**技术，物理设备层面不存在「整盘快照」；
  所谓「在线一致整盘克隆」= 逐卷 VSS + 分区表重组
- StarWind V2V Converter 9.0.0.202 的**整盘 CLI 路径**为裸读（日志无 VSS 调用）
- 本机 `diskshadow` 缺失，`vssadmin` / `wbadmin` 可用