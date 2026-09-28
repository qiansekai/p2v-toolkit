@echo off
REM ============================================================
REM  expand-system-in-pe.cmd
REM  在 FirPE 里把系统分区扩展到整盘（NTFS 会同步扩容）。
REM
REM  用途：p2v-toolkit 默认导出"与源盘同布局"的镜像（分区 GUID 与偏移
REM  完全沿用，所以盘符不会错乱）。若你想要 DiskGenius 那种"单分区"
REM  效果，就在导出后跑本脚本 —— 动作显式、可审阅、可回滚。
REM
REM  安全：只对"含 \Windows\System32\config\SYSTEM 的那个卷"做 extend，
REM  不删任何分区、不动分区 GUID。执行前请确认目标盘是对的。
REM ============================================================
setlocal enabledelayedexpansion

set SYS=
for %%d in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
  if exist "%%d:\Windows\System32\config\SYSTEM" set SYS=%%d
)

if "%SYS%"=="" (
  echo [FAIL] 未找到系统分区（没有哪个卷含 \Windows\System32\config\SYSTEM）
  echo        请确认已从本工具导出的 vmdk 启动到 PE。
  pause
  exit /b 1
)

echo [INFO] 系统分区 = %SYS%:
echo.
echo [INFO] 当前布局：
(echo list disk& echo list volume) | diskpart
echo.
echo [WARN] 即将对 %SYS%: 执行 diskpart "extend"（扩展到其后相邻的未分配空间）。
echo        不会删除任何分区，不会修改分区 GUID。
echo.
choice /c YN /m "继续吗"
if errorlevel 2 goto :cancelled

set SCRIPT=%TEMP%\p2v-extend.txt
> "%SCRIPT%" (
  echo select volume %SYS%
  echo extend
  echo exit
)
echo [INFO] diskpart 脚本：
type "%SCRIPT%"
echo.
diskpart /s "%SCRIPT%"
set RC=%ERRORLEVEL%
del "%SCRIPT%" >nul 2>&1

echo.
if "%RC%"=="0" (
  echo [DONE] extend 成功。可关机后改回从硬盘启动验证。
) else (
  echo [FAIL] diskpart 退出码 %RC%。常见原因：系统分区后面没有相邻的未分配空间。
)
echo.
pause
exit /b %RC%

:cancelled
echo [CANCELLED] 用户取消。
pause
exit /b 0
