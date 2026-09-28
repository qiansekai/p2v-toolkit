# p2v-toolkit

把物理盘上的系统卷 + ESP 导出为可引导 vmdk，并可重组目标盘布局。

## 为什么

手工 P2V（克隆盘 + 删驱动）会同时引爆三个问题：分区 GUID 变化导致盘符错乱、
设备实例悬空、生成器不注入驱动。本工具用「定向取卷 + 保留 GUID」把前两个从源头掐掉。

## 安全

- 源设备**只读**（代码层无写路径）
- 默认 dry-run；`--apply` 才写盘
- 不装驱动、不改宿主机引导

## 用法（规划中）

```
python -m p2v probe  --disk 3 --json
python -m p2v plan   --disk 3 --take ESP,C --out H:\sys.vmdk --json
python -m p2v export --plan plan.json --apply --resume
python -m p2v verify --vmdk H:\sys.vmdk --json
```

## 依赖

仅标准库（ctypes / struct / zlib / json）。
`dissect.target`、`qemu-img` 仅用于**交叉验证**，不是运行依赖。