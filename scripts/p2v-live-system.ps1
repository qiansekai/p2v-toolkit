# SPDX-License-Identifier: GPL-3.0-only
<#
.SYNOPSIS
    把本机正在运行的系统卷导出为可引导 vmdk（活系统盘 + 卷影副本）。

.DESCRIPTION
    与 p2v-from-usb.ps1 互补：
      p2v-from-usb.ps1     离线拆机盘（源盘不是本机系统盘）-> 直读物理盘，不需要快照
      p2v-live-system.ps1  活系统盘（源盘就是本机系统盘）    -> 必须先建卷影副本

    p2v-toolkit 本身刻意不提供创建快照的能力（p2v/vss.py 只枚举与只读打开现有快照，
    安全红线要求工具永远不改变宿主 VSS 状态），所以「建快照 -> 导出 -> 清理快照」
    这个生命周期由本脚本承担：

      1. 安全闸：源盘必须是本机系统盘（离线拆机盘请改用 p2v-from-usb.ps1）
      2. 为 Take 里出现的每个 vol:X: 建一份卷影副本（CIM Win32_ShadowCopy.Create）
      3. plan -> export --apply -> verify，全部走 --source-mode shadow
      4. finally 清理：成功默认删掉自己建的那份；失败默认保留（续传必须用同一份
         快照，删掉检查点就作废）并打印手工清理命令。快照创建阶段的失败同样清理。

    注意：卷影副本是写时复制（COW）的，占用源卷空间（默认上限 = 卷的 10%），
    跑完不删会一直挂着；导出期间源卷写入量越大，占用越高。

.PARAMETER Out
    目标 vmdk 路径。通常必须不存在；配 -Resume 时指向既有半成品。

.PARAMETER Disk
    源物理盘号。省略则自动取本机系统盘。

.PARAMETER Take
    p2v 选择器，逗号分隔。省略则默认 ESP,MSR,vol:<系统盘符>:

.PARAMETER ChunkMiB
    读写块大小（MiB，1..256，默认 4）。

.PARAMETER CheckpointMiB
    检查点间隔（MiB，1..8192，默认 256）。

.PARAMETER Resume
    续传既有半成品。此时不新建快照（新建会让快照身份校验失败），只复用该卷现有快照。

.PARAMETER Json
    export 改用 JSON 输出（默认关闭，以保留实时进度）。

.PARAMETER SkipVerify
    跳过 verify。

.PARAMETER KeepShadow
    跑完保留快照（默认导出成功即删）。

.PARAMETER DeleteShadowOnFailure
    失败时也删快照（默认保留，便于 --resume 续传）。

.PARAMETER DryRun
    只做检查并打印将要执行的步骤：不建快照、不写盘。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$Disk = 0,
    [string]$Take = '',
    [ValidateRange(1, 256)][int]$ChunkMiB = 4,
    [ValidateRange(1, 8192)][int]$CheckpointMiB = 256,
    [switch]$Resume,
    [switch]$Json,
    [switch]$SkipVerify,
    [switch]$KeepShadow,
    [switch]$DeleteShadowOnFailure,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Fail 只用于「还没有任何副作用」的前置检查；进入 try 之后一律用 throw，
# 否则 exit 会绕过 finally，把已经建好的快照留在系统里。
function Fail($msg) { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }
function Info($msg) { Write-Host "[*] $msg" }
function Ok($msg)   { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }

$repo = Split-Path -Parent $PSScriptRoot

# ---------- 0) 权限 ----------
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail '需要管理员权限（读 \\.\PhysicalDriveN 与创建卷影副本都要求）' }

# ---------- 1) 安全闸：源盘必须是本机系统盘 ----------
$sysLetter = $env:SystemDrive.TrimEnd(':')
$sysPart = Get-Partition -DriveLetter $sysLetter -ErrorAction SilentlyContinue
if ($null -eq $sysPart) { Fail "无法确定本机系统盘（$env:SystemDrive 没有对应分区）" }
$sysDisk = $sysPart.DiskNumber

if ($Disk -le 0) {
    $Disk = $sysDisk
    Info "自动识别源盘：#$Disk（本机系统盘 $env:SystemDrive）"
} elseif ($Disk -ne $sysDisk) {
    Fail "盘 #$Disk 不是本机系统盘（系统盘是 #$sysDisk）。离线/拆机盘请用 scripts\p2v-from-usb.ps1。"
}
Ok "安全闸通过：源盘 #$Disk 就是本机系统盘"

# ---------- 2) 选择器与目标路径 ----------
if ([string]::IsNullOrWhiteSpace($Take)) { $Take = 'ESP,MSR,vol:' + $sysLetter + ':' }
Info "选择器：$Take"

$vols = @([regex]::Matches($Take, 'vol:([A-Za-z]):') | ForEach-Object { $_.Groups[1].Value.ToUpper() })
if ($vols.Count -eq 0) {
    Warn '选择器里没有 vol:X: —— 工具会按 auto 规则自行判断是否需要快照；'
    Warn '本脚本只负责为 vol:X: 建快照，这种写法请自行确认快照状态。'
} else {
    Info ('将为这些卷建卷影副本：' + ($vols -join ', '))
}

$outExists = Test-Path -LiteralPath $Out
$ckptPath  = $Out + '.p2v-resume.json'
if ($Resume) {
    if (-not $outExists) { Fail "-Resume 需要既有半成品，但 $Out 不存在" }
    if (-not (Test-Path -LiteralPath $ckptPath)) { Fail "-Resume 需要同目录检查点 $ckptPath，但它不存在" }
    Info "续传目标：$Out（检查点在位）"
} else {
    if ($outExists) { Fail "目标已存在：$Out（换路径，或确认要续传就加 -Resume）" }
}

# ---------- 3) BitLocker ----------
try {
    $enc = @(Get-BitLockerVolume -ErrorAction Stop | Where-Object { $_.VolumeStatus -ne 'FullyDecrypted' })
    if ($enc.Count -gt 0) {
        Warn '检测到 BitLocker 卷：产物结构完整，但 VM 的虚拟 TPM 与源机不同，首次启动会停在恢复界面。'
        $enc | ForEach-Object { Warn ("      {0}  {1}" -f $_.MountPoint, $_.VolumeStatus) }
        Warn '有 48 位恢复密钥：输入即可进系统；没有则产物无法开机。'
    } else { Ok 'BitLocker：无加密卷' }
} catch { Info 'BitLocker 状态查询不可用（缺模块或权限），请自行确认' }

# ---------- 4) 卷影副本辅助 ----------
function Get-LatestShadowForVolume([string]$letter) {
    $guid = (Get-Volume -DriveLetter $letter -ErrorAction SilentlyContinue).UniqueId
    if (-not $guid) { return $null }
    $want = ([string]$guid).Trim().Trim('"') -replace '\\+$', ''
    $cands = @(Get-CimInstance -ClassName Win32_ShadowCopy -ErrorAction SilentlyContinue |
        Where-Object {
            $v = [string]$_.VolumeName
            $v -and (($v.Trim() -replace '\\+$', '').ToLower().EndsWith($want.ToLower()))
        })
    if ($cands.Count -eq 0) { return $null }
    return ($cands | Sort-Object InstallDate | Select-Object -Last 1)
}

function New-VolumeShadow([string]$letter) {
    $vol = $letter + ':\'
    $res = Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create `
             -Arguments @{ Volume = $vol; Context = 'ClientAccessible' }
    if ($null -eq $res -or $res.ReturnValue -ne 0) {
        throw ("为 {0} 创建卷影副本失败：ReturnValue={1}（0 才表示成功）" -f $vol, $res.ReturnValue)
    }
    Ok ("已创建卷影副本 {0} <- {1}" -f $res.ShadowID, $vol)
    return [string]$res.ShadowID
}

function Remove-VolumeShadow([string]$id) {
    $sc = Get-CimInstance -ClassName Win32_ShadowCopy -ErrorAction SilentlyContinue |
          Where-Object { $_.ID -eq $id }
    if ($null -eq $sc) { Warn "快照 $id 已不存在（可能被存储上限回收）"; return $true }

    # Win32_ShadowCopy 的 CIM 方法表只有 Create / Revert —— 没有 Delete，
    # Invoke-CimMethod -MethodName Delete 会报「找不到方法 Delete」（实测）。
    # Remove-CimInstance 走提供程序的实例删除路径，实测可用；vssadmin 作回退。
    try { Remove-CimInstance -InputObject $sc -ErrorAction Stop }
    catch { & vssadmin delete shadows /shadow=$id /quiet 2>&1 | Out-Null }

    # 不信返回值，删除后复核：快照真的没了才算成功
    $after = Get-CimInstance -ClassName Win32_ShadowCopy -ErrorAction SilentlyContinue |
             Where-Object { $_.ID -eq $id }
    if ($null -eq $after) { Ok "已删除快照 $id"; return $true }
    Warn ("删除快照 {0} 失败，请手工：vssadmin delete shadows /shadow={0} /quiet" -f $id)
    return $false
}
                '--source-mode',$sourceMode,'--chunk-mib',"$ChunkMiB",
                '--checkpoint-mib',"$CheckpointMiB",'--apply')
if ($Resume) { $exportArgs += '--resume' }
if ($Json)   { $exportArgs += '--json' }
$pyVerify = @('-m','p2v','verify','--vmdk',$Out,'--source-disk',"$Disk")

if ($DryRun) {
    Info 'DryRun：只做检查，不建快照、不写盘。将要执行：'
    if ($Resume) { Info '  （-Resume：复用该卷现有快照，不新建）' }
    else { foreach ($v in $vols) { Info ("  建卷影副本 <- {0}:" -f $v) } }
    Write-Host ('      python ' + ($pyPlan     -join ' '))
    Write-Host ('      python ' + ($exportArgs -join ' '))
    if (-not $SkipVerify) { Write-Host ('      python ' + ($pyVerify -join ' ')) }
    Info ('库目录：' + $repo)
    exit 0
}

# ---------- 6) 建快照 -> 导出 -> 清理 ----------
Push-Location $repo
$created   = @()
$reused    = @()
$exitCode  = 0
$succeeded = $false
try {
    if ($Resume) {
        if ($vols.Count -eq 0) { Warn '-Resume 但没有 vol:X:，无法核对复用哪份快照' }
        foreach ($v in $vols) {
            $sc = Get-LatestShadowForVolume $v
            if ($null -eq $sc) {
                throw ("续传必须复用原来那份快照，但 {0}: 现在没有任何卷影副本：检查点已作废，请删掉半成品与检查点后重跑。" -f $v)
            }
            $reused += [string]$sc.ID
            Info ("续传复用既有快照 {0}（{1}）" -f $sc.ID, $sc.DeviceObject)
        }
    } else {
        foreach ($v in $vols) { $created += New-VolumeShadow $v }
    }

    & python @pyPlan
    if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE; throw "plan 失败（exit $LASTEXITCODE）" }

    & python @exportArgs
    if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE; throw "export 失败（exit $LASTEXITCODE）" }
    Ok "导出完成：$Out"

    if (-not $SkipVerify) {
        & python @pyVerify
        $vc = $LASTEXITCODE
        if ($vc -eq 0) { Ok 'verify PASS' }
        elseif ($vc -eq 2) { $exitCode = 2; throw 'verify FAIL（退出码 2）：看上面的 FAIL 行' }
        elseif ($vc -ne 0) { $exitCode = $vc; throw "verify 异常退出（$vc）" }
    }
    $succeeded = $true
}
catch {
    Write-Host ("[x] " + $_.Exception.Message) -ForegroundColor Red
    if ($exitCode -eq 0) { $exitCode = 1 }
}
finally {
    if ($created.Count -gt 0) {
        if ($KeepShadow) {
            Warn ('按 -KeepShadow 保留快照：' + ($created -join ', '))
        } elseif ($succeeded -or $DeleteShadowOnFailure) {
            foreach ($id in $created) { Remove-VolumeShadow $id | Out-Null }
        } else {
            Warn ('导出未成功，保留快照以便 -Resume 续传：' + ($created -join ', '))
            foreach ($id in $created) {
                Warn ("    确认不再续传后手工删除：vssadmin delete shadows /shadow={0} /quiet" -f $id)
            }
        }
    }
    if ($reused.Count -gt 0) {
        Warn ('续传复用的是既有快照，脚本不替你做主删除：' + ($reused -join ', '))
        foreach ($id in $reused) {
            Warn ("    确认导出无误后手工删除：vssadmin delete shadows /shadow={0} /quiet" -f $id)
        }
    }
    if ($succeeded) {
        Write-Host ''
        Warn '产物不能直接开机：进 PE 后做两步收尾 ——'
        Warn '  1) 引导修复：Dism++ -> 引导修复，或 bcdboot C:\Windows /s <ESP盘符>: /f UEFI'
        Warn '  2) Dism++ -> 驱动管理 -> 删除所有后装驱动（保留 in-box）'
    }
    Pop-Location -ErrorAction SilentlyContinue
}
exit $exitCode
