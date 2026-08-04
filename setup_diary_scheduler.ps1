# Register the MCB Diary Refresh Windows Scheduled Task.
# Runs Saturday at 4:30 PM local time:
#   python run_diary.py --days 3
# (Weekday scrapes are handled by the 'MCB Homework Push' task at 4:15 PM Mon-Fri,
# which invokes run_diary.py itself before pushing.)
#
# Usage (from the project root, as the user who will own the task):
#     powershell -ExecutionPolicy Bypass -File .\setup_diary_scheduler.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Diary Refresh' -Confirm:$false

$ErrorActionPreference = 'Stop'

$TaskName    = 'MCB Diary Refresh'
$ProjectRoot = $PSScriptRoot
$Python      = (Get-Command python).Source
$Runner      = Join-Path $ProjectRoot 'run_diary.py'
$WrapperLog  = Join-Path $ProjectRoot 'mcb_wrapper.log'

if (-not (Test-Path $Runner)) { throw "$Runner not found" }

# Weekly trigger, Saturday at 4:30 PM local
$Trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Saturday `
    -At 4:30PM

$Cmd     = "cmd.exe"
$Args    = "/c `"cd /d `"$ProjectRoot`" && `"$Python`" `"$Runner`" >> `"$WrapperLog`" 2>&1`""
$Action  = New-ScheduledTaskAction -Execute $Cmd -Argument $Args -WorkingDirectory $ProjectRoot

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Scrape CHIREC MCB class-diary (homework) at 4:30 PM Saturday. Mon-Fri covered by MCB Homework Push." | Out-Null

Write-Host ""
Write-Host "OK: Task '$TaskName' registered."
Write-Host "   Runs      : 16:30 local time, Saturday"
Write-Host "   Python    : $Python"
Write-Host "   Action    : $Runner"
Write-Host "   Wrapper   : $WrapperLog"
Write-Host ""
Write-Host "Run manually with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View next runs   :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List NextRunTime,LastRunTime,LastTaskResult"
