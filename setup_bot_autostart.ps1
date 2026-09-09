# Register a Windows Scheduled Task that keeps the MCB Telegram bot running.
# Trigger : at Windows startup
# Restart : on failure, up to 3 times with 1-minute delay
# Window  : hidden (no visible console)
#
# Usage (from project root):
#     powershell -ExecutionPolicy Bypass -File .\setup_bot_autostart.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Bot' -Confirm:$false

$ErrorActionPreference = 'Stop'

$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $IsAdmin) {
    throw 'Run this script from an Administrator PowerShell window.'
}

$TaskName    = 'MCB Bot'
$ProjectRoot = $PSScriptRoot
$Python      = (Get-Command python).Source
$Script      = Join-Path $ProjectRoot 'run_telegram_bot.py'
$BotLog      = Join-Path $ProjectRoot 'mcb_bot.log'

if (-not (Test-Path $Script)) {
    throw "run_telegram_bot.py not found in $ProjectRoot"
}

# Run as soon as Windows starts, without waiting for an interactive logon.
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Wrap in cmd so any stderr/stdout goes to the bot log
$Cmd  = "cmd.exe"
$Args = "/c `"cd /d `"$ProjectRoot`" && `"$Python`" `"$Script`" >> `"$BotLog`" 2>&1`""

$Action = New-ScheduledTaskAction -Execute $Cmd -Argument $Args -WorkingDirectory $ProjectRoot

# Restart-on-failure and never expire; hidden window; single instance only.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Write-Host "Removing existing task '$TaskName'..."
    if ($ExistingTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$BotProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Script) }
foreach ($BotProcess in $BotProcesses) {
    Write-Host "Stopping existing bot process $($BotProcess.ProcessId)..."
    Stop-Process -Id $BotProcess.ProcessId -Force
}

Register-ScheduledTask -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Run the MCB Telegram bot (polling) at Windows startup." | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "OK: Task '$TaskName' registered and started."
Write-Host "   Trigger   : At Windows startup"
Write-Host "   Account   : SYSTEM"
Write-Host "   Python    : $Python"
Write-Host "   Script    : $Script"
Write-Host "   Log       : $BotLog"
Write-Host ""
Write-Host "Stop with     :  Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Host "Status        :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List *"
