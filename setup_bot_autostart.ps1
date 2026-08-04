# Register a Windows Scheduled Task that keeps the MCB Telegram bot running.
# Trigger : at user logon
# Restart : on failure, up to 3 times with 1-minute delay
# Window  : hidden (no visible console)
#
# Usage (from project root):
#     powershell -ExecutionPolicy Bypass -File .\setup_bot_autostart.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'MCB Bot' -Confirm:$false

$ErrorActionPreference = 'Stop'

$TaskName    = 'MCB Bot'
$ProjectRoot = $PSScriptRoot
$Pythonw     = Join-Path (Split-Path (Get-Command python).Source -Parent) 'pythonw.exe'
if (-not (Test-Path $Pythonw)) {
    # Fall back to python.exe if pythonw.exe isn't present.
    $Pythonw = (Get-Command python).Source
}
$Script      = Join-Path $ProjectRoot 'run_telegram_bot.py'
$BotLog      = Join-Path $ProjectRoot 'mcb_bot.log'

if (-not (Test-Path $Script)) {
    throw "run_telegram_bot.py not found in $ProjectRoot"
}

# Run at user logon
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# Wrap in cmd so any stderr/stdout goes to the bot log
$Cmd  = "cmd.exe"
$Args = "/c `"cd /d `"$ProjectRoot`" && `"$Pythonw`" `"$Script`" >> `"$BotLog`" 2>&1`""

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
    -Description "Run the MCB Telegram bot (polling) at user logon." | Out-Null

Write-Host ""
Write-Host "OK: Task '$TaskName' registered."
Write-Host "   Trigger   : At logon of $env:USERDOMAIN\$env:USERNAME"
Write-Host "   Python    : $Pythonw"
Write-Host "   Script    : $Script"
Write-Host "   Log       : $BotLog"
Write-Host ""
Write-Host "Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Stop with     :  Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Host "Status        :  Get-ScheduledTaskInfo -TaskName '$TaskName' | Format-List *"
