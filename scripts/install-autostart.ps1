# Installs or removes Uniagent's autostart on Windows.
#
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Install
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Remove
#
# Creates a scheduled task that runs the supervisor (run-server.ps1) at the
# current user's logon, so the server and cron watcher come up every time the
# machine is used - no admin needed, no service account, nothing left running
# when you log out. The task has no execution time limit, because the
# supervisor is meant to run for as long as the session does.
#
# "Always on, even before anyone logs in" needs a real Windows service, which
# means admin rights - see README for the NSSM option.

param(
    [switch]$Install,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$taskName = "Uniagent"
$root     = Split-Path -Parent $PSScriptRoot
$runner   = Join-Path $root "scripts\run-server.ps1"

if ($Install) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`"")
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Uniagent personal AI agent - web server and cron watcher" `
        -Force | Out-Null
    Write-Host "Scheduled task '$taskName' installed - Uniagent starts at every logon." -ForegroundColor Green

    # Start it now, so an install finishes with the server already up.
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Uniagent is starting now (https://localhost:8764)." -ForegroundColor Green
}
elseif ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Scheduled task '$taskName' removed." -ForegroundColor Yellow
    } else {
        Write-Host "No task named '$taskName' was installed." -ForegroundColor Yellow
    }
}
else {
    Write-Host "Usage: install-autostart.ps1 -Install | -Remove"
    exit 1
}
