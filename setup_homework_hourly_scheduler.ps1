# Register the MCB Homework Hourly Windows Scheduled Task.
# Runs Monday-Friday at 4, 5, 6, 8, and 9 PM local time (5 runs/day):
#   python run_diary.py --days 1 --push-if-new
# `--push-if-new` re-scrapes today's diary; a consolidated homework digest
# is only pushed if at least one homework entry_id was not present in the
# previous run's push. State is kept in `data/homework_last_push.json`.
#
# Usage (from the project root, in Administrator PowerShell):
#     powershell -ExecutionPolicy Bypass -File .\setup_homework_hourly_scheduler.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Homework Hourly' -Confirm:$false

$ErrorActionPreference = 'Stop'

$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $IsAdmin) {
    throw 'Run this script from an Administrator PowerShell window.'
}

$TaskName    = 'MCB Homework Hourly'
$LegacyName  = 'MCB Homework Push'   # the old 4:15 PM once-a-day task
$ProjectRoot = $PSScriptRoot
$Python      = (Get-Command python).Source
$PlaywrightBrowsers = Join-Path $env:LOCALAPPDATA 'ms-playwright'
$Runner      = Join-Path $ProjectRoot 'run_diary.py'
$WrapperLog  = Join-Path $ProjectRoot 'mcb_wrapper.log'

if (-not (Test-Path $Runner)) { throw "$Runner not found" }
if (-not (Test-Path $PlaywrightBrowsers)) { throw "$PlaywrightBrowsers not found" }

# The 19:00 slot is reserved for the evening digest task.
$Times   = @('4:00PM','5:00PM','6:00PM','8:00PM','9:00PM')
$Triggers = @()
foreach ($t in $Times) {
    $Triggers += New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $t
}

$Cmd   = "cmd.exe"
$Args  = "/d /s /c set `"PLAYWRIGHT_BROWSERS_PATH=$PlaywrightBrowsers`" && cd /d `"$ProjectRoot`" && `"$Python`" `"$Runner`" --days 1 --push-if-new >> `"$WrapperLog`" 2>&1"
$Action = New-ScheduledTaskAction -Execute $Cmd -Argument $Args -WorkingDirectory $ProjectRoot

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$Principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

# Remove the legacy once-a-day task if it's still around — the hourly task
# supersedes it. Silent-ok if it isn't registered.
if (Get-ScheduledTask -TaskName $LegacyName -ErrorAction SilentlyContinue) {
    Write-Host "Removing legacy task '$LegacyName'..."
    Unregister-ScheduledTask -TaskName $LegacyName -Confirm:$false
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Trigger $Triggers `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Re-scrape today's class diary at 4, 5, 6, 8, and 9 PM Mon-Fri and push a consolidated homework digest only when a new entry appears." | Out-Null

Write-Host ""
Write-Host "OK: Task '$TaskName' registered."
Write-Host "   Runs      : 16:00, 17:00, 18:00, 20:00, 21:00 local, Mon-Fri"
Write-Host "   Account   : SYSTEM"
Write-Host "   Python    : $Python"
Write-Host "   Command   : $Runner --days 1 --push-if-new"
Write-Host "   Wrapper   : $WrapperLog"
Write-Host ""
Write-Host "Run manually with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View next runs   :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List NextRunTime,LastRunTime,LastTaskResult"
