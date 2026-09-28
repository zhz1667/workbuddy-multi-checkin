<#
    WorkBuddy 多账号自动签到 · Windows 计划任务安装脚本
    作用：把 wb_checkin_multi.py 注册为每日定时任务，完全不依赖 WorkBuddy 客户端是否运行/登录。

    用法（普通用户权限即可，无需管理员）：
      powershell -ExecutionPolicy Bypass -File install_task.ps1
      powershell -ExecutionPolicy Bypass -File install_task.ps1 -Time 09:10 -TaskName "WB多账号签到"
      powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall
#>

param(
    [string]$TaskName = "WorkBuddy-MultiCheckin",
    [string]$Time = "09:00",
    [string]$PythonPath = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir "wb_checkin_multi.py"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] 已删除计划任务：$TaskName"
    } else {
        Write-Host "[--] 计划任务不存在：$TaskName"
    }
    exit 0
}

if (-not (Test-Path $target)) {
    Write-Host "[错误] 未找到主程序：$target" -ForegroundColor Red
    exit 1
}

# 1) 解析 Python 解释器：优先用户指定 → 托管 Python → PATH 中的 python
if (-not $PythonPath) {
    $cands = @(
        (Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions\3.13.12\python.exe"),
        "D:\Python 3.12\python.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { $PythonPath = $c; break } }
}
if (-not $PythonPath) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $PythonPath = $cmd.Source }
}
if (-not $PythonPath -or -not (Test-Path $PythonPath)) {
    Write-Host "[错误] 未找到 python.exe，请用 -PythonPath 指定绝对路径" -ForegroundColor Red
    exit 1
}
Write-Host "[1/4] Python 解释器：$PythonPath"

# 2) 校验参数
$t = [datetime]::ParseExact($Time, "HH:mm", $null)
Write-Host "[2/4] 每日执行时间：$($t.ToString('HH:mm'))"

# 3) 组装动作（结果明细由脚本自身写入 logs\ 目录，无需重定向；加 --no-notify 可静默）
#    注意：不要用 $args 作变量名——它是 PowerShell 自动变量，赋值会与脚本入参冲突。
$argLine = "`"$target`""
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument $argLine -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At $t
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

# 4) 注册（当前用户上下文；不需要管理员、不需要存密码）
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "WorkBuddy 多账号自动签到 + 派小猫旅行（独立运行）" -Force | Out-Null

Write-Host "[3/4] 计划任务已注册：$TaskName"
Write-Host "[4/4] 完成。"
Write-Host ""
Write-Host "查看：Get-ScheduledTask -TaskName `"$TaskName`" | Get-ScheduledTaskInfo"
Write-Host "试跑：Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "删除：powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall"
Write-Host ""
Write-Host "提示：任务在当前用户上下文注册（不需要管理员、不存密码），仅在用户已登录时可触发；"
Write-Host "      借助 -StartWhenAvailable，错过的执行会在下次可用时补跑。"
