# Register the MCB Data Refresh Windows Scheduled Task.
# Runs twice daily at 6:30 AM and 7:00 PM local time:
#   1. `python run_digest.py --dry-run`   -- scrape MCB, refresh event store
#   2. `python push_digest.py`            -- post the digest to Telegram group
# Email is skipped (paused per user request).
#
# Usage (from the project root, in Administrator PowerShell):
#     powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Data Refresh' -Confirm:$false

$ErrorActionPreference = 'Stop'

$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $IsAdmin) {
    throw 'Run this script from an Administrator PowerShell window.'
}

$TaskName    = 'MCB Data Refresh'
$ProjectRoot = $PSScriptRoot
$Python      = (Get-Command python).Source
$PlaywrightBrowsers = Join-Path $env:LOCALAPPDATA 'ms-playwright'
$Scrape      = Join-Path $ProjectRoot 'run_digest.py'
$Push        = Join-Path $ProjectRoot 'push_digest.py'
# The Python apps write structured logs to mcb_scheduler.log via their own
# FileHandler; the shell wrapper captures anything printed outside logging
# (uncaught tracebacks, prints) into a separate file to avoid a file-lock clash.
$WrapperLog  = Join-Path $ProjectRoot 'mcb_wrapper.log'

foreach ($f in @($Scrape, $Push)) {
    if (-not (Test-Path $f)) { throw "$f not found" }
}
if (-not (Test-Path $PlaywrightBrowsers)) { throw "$PlaywrightBrowsers not found" }

# Two daily triggers: 6:30 AM and 7:00 PM local time
$Triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 6:30AM),
    (New-ScheduledTaskTrigger -Daily -At 7:00PM)
)

# Task Scheduler runs multiple Actions sequentially. Wrap each in cmd.exe so
# stdout/stderr redirects into the wrapper log; use `&` (not `&&`) on the push
# so a transient scrape hiccup doesn't block the reminder from going out.
$Cmd = "cmd.exe"
$ScrapeArgs = "/d /s /c set `"PLAYWRIGHT_BROWSERS_PATH=$PlaywrightBrowsers`" && cd /d `"$ProjectRoot`" && `"$Python`" `"$Scrape`" --dry-run >> `"$WrapperLog`" 2>&1"
$PushArgs   = "/d /s /c cd /d `"$ProjectRoot`" && `"$Python`" `"$Push`" >> `"$WrapperLog`" 2>&1"

$Actions = @(
    (New-ScheduledTaskAction -Execute $Cmd -Argument $ScrapeArgs -WorkingDirectory $ProjectRoot),
    (New-ScheduledTaskAction -Execute $Cmd -Argument $PushArgs   -WorkingDirectory $ProjectRoot)
)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

# Clean up any legacy task from the old single-trigger design
foreach ($old in @('MCB Daily Digest')) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Write-Host "Removing legacy task '$old'..."
        Unregister-ScheduledTask -TaskName $old -Confirm:$false
    }
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Trigger $Triggers `
    -Action $Actions `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Scrape CHIREC MCB then push the digest to Telegram at 6:30 AM and 7:00 PM daily." | Out-Null

Write-Host ""
Write-Host "OK: Task '$TaskName' registered."
Write-Host "   Runs      : 06:30 and 19:00 local time, daily"
Write-Host "   Account   : SYSTEM"
Write-Host "   Python    : $Python"
Write-Host "   Action 1  : $Scrape --dry-run"
Write-Host "   Action 2  : $Push"
Write-Host "   App log   : $ProjectRoot\mcb_scheduler.log"
Write-Host "   Wrapper   : $WrapperLog"
Write-Host ""
Write-Host "Run manually with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View next runs   :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List NextRunTime,LastRunTime,LastTaskResult"

