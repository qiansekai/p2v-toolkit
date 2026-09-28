# p2v-toolkit

agent 友好的 P2V 工具链：把物理盘上的「系统卷 + ESP」做成可引导 vmdk，
并可重组目标盘分区布局（等价 DiskGenius 的「在线克隆 + 布局重组」，但更省空间、不动原 GUID）。

## 定位

- 替代 DiskGenius 的**技术能力**，不是替代它的 GUI。核心差异：
  1. **定向只取**（ESP + 指定系统卷），**不复制数据盘**（源盘 932GB / 数据盘 588GB 被跳过）
  2. **沿用原分区 GUID 与偏移**，避免 `MountedDevices` 失配导致的 C: -> V: 盘符错乱
  3. 全程 CLI + JSON，可断点续传，可被 agent 驱动

## 安全红线（最高优先级，改代码前必读）

- **源设备只以 GENERIC_READ 打开**；`p2v/safeio.py` 不提供任何写 API，物理盘永不写。
- **不安装任何内核驱动**（不跑 StarWind 的 `[Install].bat`），不修改宿主机分区表 / BCD / VSS 配置。
- 产物只写新文件；目标已存在一律拒绝（除非显式 --force）。
- 长任务默认 `--dry-run`，必须显式 `--apply` 才落盘。
- 每步可验证：GPT CRC32 自检、产物用 `qemu-img info` / `dissect` 交叉验证。

## 目录

```
p2v/
  safeio.py    只读设备访问（物理盘 / VSS 快照）+ 安全护栏
  gpt.py       GPT 解析与构造（含 CRC32）
  vmdk.py      sparse vmdk 读写
  vss.py       VSS 快照创建与卸载
  cli.py       子命令：probe / plan / export / verify
```

## 已验证的关键事实（2026-09-28）

- `qemu-img` **打不开** VSS 快照设备（它依赖 IOCTL 查容量，VSS 设备不响应）
- 但用 Win32 `CreateFile` + `ReadFile` **可以**顺序读 VSS 设备 —— 实测读到 C 卷 NTFS VBR
- VSS 是**卷级**技术，物理设备层面不存在「整盘快照」；
  所谓「在线一致整盘克隆」= 逐卷 VSS + 分区表重组
- StarWind V2V Converter 9.0.0.202 的**整盘 CLI 路径**为裸读（日志无 VSS 调用）
- 本机 `diskshadow` 缺失，`vssadmin` / `wbadmin` 可用