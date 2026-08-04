# Register the MCB Homework Push Windows Scheduled Task.
# Runs Monday-Friday at 4:15 PM local time. Two actions:
#   1. `python run_diary.py --days 1`             -- refresh today's homework
#   2. `python push_digest.py --slot homework`    -- push homework-only digest
# The push is skipped silently if no homework was posted today.
#
# Usage (from the project root, as the user who will own the task):
#     powershell -ExecutionPolicy Bypass -File .\setup_homework_push_scheduler.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Homework Push' -Confirm:$false

$ErrorActionPreference = 'Stop'

$TaskName    = 'MCB Homework Push'
$ProjectRoot = $PSScriptRoot
$Python      = (Get-Command python).Source
$Diary       = Join-Path $ProjectRoot 'run_diary.py'
$Push        = Join-Path $ProjectRoot 'push_digest.py'
$WrapperLog  = Join-Path $ProjectRoot 'mcb_wrapper.log'

foreach ($f in @($Diary, $Push)) {
    if (-not (Test-Path $f)) { throw "$f not found" }
}

$Trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 4:15PM

$Cmd        = "cmd.exe"
$DiaryArgs  = "/c `"cd /d `"$ProjectRoot`" && `"$Python`" `"$Diary`" --days 1 >> `"$WrapperLog`" 2>&1`""
$PushArgs   = "/c `"cd /d `"$ProjectRoot`" && `"$Python`" `"$Push`" --slot homework >> `"$WrapperLog`" 2>&1`""

$Actions = @(
    (New-ScheduledTaskAction -Execute $Cmd -Argument $DiaryArgs -WorkingDirectory $ProjectRoot),
    (New-ScheduledTaskAction -Execute $Cmd -Argument $PushArgs  -WorkingDirectory $ProjectRoot)
)

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
    -Action $Actions `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Scrape today's class-diary and push homework digest to Telegram at 4:15 PM Mon-Fri." | Out-Null

Write-Host ""
Write-Host "OK: Task '$TaskName' registered."
Write-Host "   Runs      : 16:15 local time, Mon-Fri"
Write-Host "   Python    : $Python"
Write-Host "   Action 1  : $Diary --days 1"
Write-Host "   Action 2  : $Push --slot homework"
Write-Host "   Wrapper   : $WrapperLog"
Write-Host ""
Write-Host "Run manually with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View next runs   :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List NextRunTime,LastRunTime,LastTaskResult"
