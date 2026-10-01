<#
    WorkBuddy 多账号自动签到 · Windows 计划任务安装脚本（v1.0.3）

    注册两个任务，完全不依赖 WorkBuddy 客户端是否运行/登录：

      1) WorkBuddy-MultiCheckin —— 每日 09:00 跑完整流程
         （签到 + 派小猫 + 活跃自检 + 每日问候的「补发」）
      2) WorkBuddy-Greet        —— 每日 03:00–08:00 每 15 分钟轮询一次
         到当天随机时刻才真正发问候；不到点则静默退出（不弹通知）。

    为什么问候要单独一个任务：随机时刻只能在「窗口内反复问一句『到点没』」，
    而签到只需要每天固定跑一次。两者节奏不同，混在一起会互相拖累。

    用法（普通用户权限即可，无需管理员）：
      powershell -ExecutionPolicy Bypass -File install_task.ps1
      powershell -ExecutionPolicy Bypass -File install_task.ps1 -Time 09:10
      powershell -ExecutionPolicy Bypass -File install_task.ps1 -SkipGreet        # 只装签到
      powershell -ExecutionPolicy Bypass -File install_task.ps1 -OnlyGreet       # 只装问候
      powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall       # 两个一起删

    注意：-Uninstall 需要 TaskName / GreetTaskName 与安装时一致（默认值即为默认安装）。
#>

param(
    [string]$TaskName = "WorkBuddy-MultiCheckin",
    [string]$GreetTaskName = "WorkBuddy-Greet",
    [string]$Time = "09:00",
    [string]$GreetStart = "03:00",
    [string]$GreetEnd = "08:00",
    [int]$GreetIntervalMinutes = 15,
    [string]$PythonPath = "",
    [switch]$SkipGreet,
    [switch]$OnlyGreet,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir "wb_checkin_multi.py"

function Remove-TaskIfExists([string]$name) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "[OK] 已删除计划任务：$name"
    } else {
        Write-Host "[--] 计划任务不存在：$name"
    }
}

if ($Uninstall) {
    Remove-TaskIfExists $TaskName
    Remove-TaskIfExists $GreetTaskName
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

# 问候轮询用 pythonw.exe（无控制台窗口）——凌晨触发时不该在屏幕上闪黑框。
$pythonw = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $PythonPath }
Write-Host "      无窗口解释器：$pythonw"

# 2) 组装动作（结果明细由脚本自身写入 logs\ 目录，无需重定向）
#    注意：不要用 $args 作变量名——它是 PowerShell 自动变量，赋值会与脚本入参冲突。
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

$installed = @()

# 3) 签到任务（每日固定时间跑完整流程）
if (-not $OnlyGreet) {
    $t = [datetime]::ParseExact($Time, "HH:mm", $null)
    $action = New-ScheduledTaskAction -Execute $PythonPath `
        -Argument "`"$target`"" -WorkingDirectory $scriptDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $t
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Write-Host "[2/4] 已注册签到任务：$TaskName（每日 $($t.ToString('HH:mm')))"
    $installed += $TaskName
} else {
    Write-Host "[2/4] 已跳过签到任务（-OnlyGreet）"
}

# 4) 问候任务（窗口内每 N 分钟轮询一次，到当天随机时刻才发）
#    New-ScheduledTaskTrigger -Daily 在 PS 5.1 下不直接支持 Repetition*，
#    故先用 -Once 造出 Repetition 对象，再挂到 -Daily 触发器上。
if (-not $SkipGreet) {
    $start = [datetime]::ParseExact($GreetStart, "HH:mm", $null)
    $end = [datetime]::ParseExact($GreetEnd, "HH:mm", $null)
    $span = New-TimeSpan -Minutes ([int]([math]::Max(15, ($end - $start).TotalMinutes)))
    $interval = New-TimeSpan -Minutes $GreetIntervalMinutes

    $gAction = New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$target`" --greet-window" -WorkingDirectory $scriptDir
    $gTrigger = New-ScheduledTaskTrigger -Daily -At $start
    $rep = (New-ScheduledTaskTrigger -Once -At $start `
                -RepetitionInterval $interval -RepetitionDuration $span).Repetition
    if ($rep) { $gTrigger.Repetition = $rep }

    $gSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $GreetTaskName -Action $gAction -Trigger $gTrigger `
        -Settings $gSettings `
        -Description "WorkBuddy 每日问候：$GreetStart-$GreetEnd 内每 $GreetIntervalMinutes 分钟轮询，到当天随机时刻发一条问候" `
        -Force | Out-Null
    Write-Host "[3/4] 已注册问候任务：$GreetTaskName（$GreetStart-$GreetEnd 每 $GreetIntervalMinutes 分钟轮询一次）"
    $installed += $GreetTaskName
} else {
    Write-Host "[3/4] 已跳过问候任务（-SkipGreet）"
}

Write-Host "[4/4] 完成。已注册：$($installed -join '，')"
Write-Host ""
Write-Host "查看：Get-ScheduledTask -TaskName `"$TaskName`" | Get-ScheduledTaskInfo"
Write-Host "试跑签到：Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "试跑问候：Start-ScheduledTask -TaskName `"$GreetTaskName`""
Write-Host "看今天发没发：python `"$target`" --greet-status"
Write-Host "立即强制发一条：python `"$target`" --greet-only --greet"
Write-Host "删除全部：powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall"
Write-Host ""
Write-Host "提示：任务在当前用户上下文注册（不需要管理员、不存密码），仅在用户已登录时可触发；"
Write-Host "      借助 -StartWhenAvailable，错过的执行会在下次可用时补跑。"
Write-Host "      本机常在 08:14-10:04 才开机，故 03:00-08:00 窗口经常整体错过 ——"
Write-Host "      09:00 的签到任务会按 greet_catchup 设置自动补发问候。"
