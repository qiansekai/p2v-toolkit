<#
.SYNOPSIS
    把 USB 硬盘盒里的拆机盘（离线系统盘）安全导出为 vmdk。

.DESCRIPTION
    在 p2v-toolkit 之外包一层，只负责工具本身不该管的三件事：
      1. 安全闸：拒绝把本机系统盘当源盘（对它设只读会让系统当场故障）
      2. 可选把源盘整盘设为只读（partmgr 层，不写源盘任何扇区），并在 finally 里恢复 + 核对
      3. 顺序执行 probe -> plan -> export --apply -> verify

    只读属性持久保存在注册表里，程序异常退出也不会自动清除，所以恢复失败会明确报警。

.PARAMETER Disk
    源物理盘号（先跑 Get-Disk 确认，别选错）。

.PARAMETER Out
    目标 vmdk 路径（必须不存在，已存在会被拒绝）。

.PARAMETER Take
    p2v 选择器，逗号分隔。跨机拆盘请用 part:N —— vol:C: 解析的是本机盘符。

.PARAMETER ChunkMiB
    读写块大小（MiB，1..256，默认 4）。

.PARAMETER DryRun
    只做检查并打印将要执行的命令；不设只读、不写盘。

.PARAMETER NoLock
    跳过只读加锁（安全闸与检查仍然执行）。

.PARAMETER KeepReadOnly
    跑完保留只读状态（默认一定恢复）。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$Disk,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Take = 'ESP,MSR,part:3',
    [int]$ChunkMiB = 4,
    [switch]$DryRun,
    [switch]$NoLock,
    [switch]$KeepReadOnly
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }
function Info($msg) { Write-Host "[*] $msg" }
function Ok($msg)   { Write-Host "[+] $msg" -ForegroundColor Green }

$repo = Split-Path -Parent $PSScriptRoot

# ---------- 1) 安全闸：源盘不能是本机系统盘 ----------
$sysLetter = $env:SystemDrive.TrimEnd(':')
$sysPart = Get-Partition -DriveLetter $sysLetter -ErrorAction SilentlyContinue
$sysDisk = $null
if ($null -ne $sysPart) { $sysDisk = $sysPart.DiskNumber }
if ($null -ne $sysDisk -and $Disk -eq $sysDisk) {
    Fail "盘 #$Disk 就是本机系统盘（$env:SystemDrive）。对它设只读会让系统当场故障，已拒绝。"
}
Ok "安全闸通过：盘 #$Disk 不是本机系统盘（本机系统盘是 #$sysDisk）"

# ---------- 2) 盘存在性与基本状态 ----------
$d = Get-Disk -Number $Disk -ErrorAction SilentlyContinue
if ($null -eq $d) { Fail "找不到物理盘 #$Disk。先跑 Get-Disk 看盘号。" }
Info ("盘 #$Disk  {0}  {1}  {2:N1} GiB  {3}" -f $d.FriendlyName, $d.BusType, ($d.Size / 1GB), $d.PartitionStyle)
Info ("当前状态：IsReadOnly=$($d.IsReadOnly)  IsOffline=$($d.IsOffline)")
if ($d.PartitionStyle -ne 'GPT') { Fail "只支持 GPT，当前是 $($d.PartitionStyle)" }
if ($d.IsOffline) { Info '盘处于 offline；读取一般仍可用，若 probe 失败可先 Set-Disk -IsOffline false' }

# ---------- 3) BitLocker 检查 ----------
try {
    $enc = @(Get-BitLockerVolume -ErrorAction Stop | Where-Object { $_.VolumeStatus -ne 'FullyDecrypted' })
    if ($enc.Count -gt 0) {
        Write-Host '[!] 检测到 BitLocker 卷（ESP 不加密，只有 Windows 卷是密文）' -ForegroundColor Yellow
        $enc | ForEach-Object { Write-Host ("      {0}  {1}" -f $_.MountPoint, $_.VolumeStatus) -ForegroundColor Yellow }
        Write-Host '    克隆照常进行，产物结构完整；但 VM 的虚拟 TPM 与源机不是同一个，' -ForegroundColor Yellow
        Write-Host '    首次启动会停在 BitLocker 恢复界面，需要 48 位恢复密钥（或启动密码）。' -ForegroundColor Yellow
        Write-Host '    有密钥：输入即可进系统，进去后建议 manage-bde -off 关掉加密。' -ForegroundColor Yellow
        Write-Host '    没密钥：产物无法开机；可把盘装回源机先 manage-bde -off 解密再拆。' -ForegroundColor Yellow
        Write-Host '    密钥通常在 account.microsoft.com/devices/recoverykey / 公司 AD / 自存文本。' -ForegroundColor Yellow
    } else { Ok 'BitLocker：无加密卷' }
} catch { Info 'BitLocker 状态查询不可用（缺模块或权限），请自行确认' }

# ---------- 4) 分区一览 ----------
Get-Partition -DiskNumber $Disk -ErrorAction SilentlyContinue |
    Select-Object PartitionNumber, DriveLetter, @{n='GiB';e={[math]::Round($_.Size/1GB,2)}}, Type |
    Format-Table -AutoSize | Out-Host

$pyProbe  = @('-m','p2v','probe','--disk',"$Disk")
$pyPlan   = @('-m','p2v','plan','--disk',"$Disk",'--take',$Take,'--out',$Out,'--source-mode','physical')
$pyExport = @('-m','p2v','export','--disk',"$Disk",'--take',$Take,'--out',$Out,
              '--source-mode','physical','--chunk-mib',"$ChunkMiB",'--apply','--json')
$pyVerify = @('-m','p2v','verify','--vmdk',$Out,'--source-disk',"$Disk")

if ($DryRun) {
    Info 'DryRun：只做检查，不设只读、不写盘。将要执行：'
    Write-Host ("      python " + ($pyProbe  -join ' '))
    Write-Host ("      python " + ($pyPlan   -join ' '))
    Write-Host ("      python " + ($pyExport -join ' '))
    Write-Host ("      python " + ($pyVerify -join ' '))
    if ($NoLock) { Info '（已指定 -NoLock：不会设只读）' }
    else { Info ("（会先把盘 {0} 设为只读，结束后恢复）" -f $Disk) }
    exit 0
}

# ---------- 5) 加锁 -> 导出 -> 恢复 ----------
Push-Location $repo
$locked = $false
$exitCode = 0
try {
    if (-not $NoLock) {
        Set-Disk -Number $Disk -IsReadOnly $true
        $locked = $true
        Ok "已把盘 #$Disk 设为只读（partmgr 层，不写源盘扇区）"
    } else { Info '按 -NoLock 跳过只读加锁' }

    & python @pyProbe
    if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE; throw "probe 失败（exit $LASTEXITCODE）" }

    & python @pyPlan
    if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE; throw "plan 失败（exit $LASTEXITCODE）" }

    & python @pyExport
    if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE; throw "export 失败（exit $LASTEXITCODE）" }
    Ok "导出完成：$Out"

    & python @pyVerify
    $vc = $LASTEXITCODE
    if ($vc -eq 0) { Ok 'verify PASS' }
    elseif ($vc -eq 2) { Write-Host '[!] verify FAIL（退出码 2）：看上面的 FAIL 行' -ForegroundColor Yellow }
}
catch {
    Write-Host ("[x] " + $_.Exception.Message) -ForegroundColor Red
    if ($exitCode -eq 0) { $exitCode = 1 }
}
finally {
    if ($locked -and -not $KeepReadOnly) {
        Set-Disk -Number $Disk -IsReadOnly $false
        $now = (Get-Disk -Number $Disk).IsReadOnly
        if ($now) {
            Write-Host '[!] 只读恢复失败！请手工执行：' -ForegroundColor Red
            Write-Host '      diskpart' -ForegroundColor Red
            Write-Host ("      select disk $Disk") -ForegroundColor Red
            Write-Host '      attributes disk clear readonly' -ForegroundColor Red
            if ($exitCode -eq 0) { $exitCode = 1 }
        } else { Ok "已恢复盘 #$Disk 为可写（核对 IsReadOnly=$now）" }
    } elseif ($locked) {
        Write-Host "[!] 按 -KeepReadOnly 保留只读：盘 #$Disk 仍是只读，用完记得清" -ForegroundColor Yellow
    }
    Pop-Location -ErrorAction SilentlyContinue
}
exit $exitCode
