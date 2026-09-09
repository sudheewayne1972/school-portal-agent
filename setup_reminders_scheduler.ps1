# Register the MCB Deadline Reminder Windows Scheduled Task.
# Runs every day at 6:10 PM local time:
#   python push_reminders.py
# The script pushes only when at least one event is 5/4/3/2/1 days out,
# so this is a silent no-op on days with nothing pending.
#
# Usage (from the project root, in Administrator PowerShell):
#     powershell -ExecutionPolicy Bypass -File .\setup_reminders_scheduler.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Deadline Reminders' -Confirm:$false

$ErrorActionPreference = 'Stop'

$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $IsAdmin) {
    throw 'Run this script from an Administrator PowerShell window.'
}

$TaskName    = 'MCB Deadline Reminders'
$ProjectRoot = $PSScriptRoot
$Python      = (Get-Command python).Source
$Runner      = Join-Path $ProjectRoot 'push_reminders.py'
$WrapperLog  = Join-Path $ProjectRoot 'mcb_wrapper.log'

if (-not (Test-Path $Runner)) { throw "$Runner not found" }

$Trigger = New-ScheduledTaskTrigger -Daily -At 6:10PM

$Cmd    = "cmd.exe"
$Args   = "/c `"cd /d `"$ProjectRoot`" && `"$Python`" `"$Runner`" >> `"$WrapperLog`" 2>&1`""
$Action = New-ScheduledTaskAction -Execute $Cmd -Argument $Args -WorkingDirectory $ProjectRoot

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Send a Telegram nudge for events with deadlines 5/4/3/2/1 days out, daily at 6:10 PM." | Out-Null

Write-Host ""
Write-Host "OK: Task '$TaskName' registered."
Write-Host "   Runs      : 18:10 local time, daily"
Write-Host "   Account   : SYSTEM"
Write-Host "   Python    : $Python"
Write-Host "   Action    : $Runner"
Write-Host "   Wrapper   : $WrapperLog"
Write-Host ""
Write-Host "Run manually with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View next runs   :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List NextRunTime,LastRunTime,LastTaskResult"
