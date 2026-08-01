# uniagent update - pulls the latest code and restarts the running server.
#   powershell -ExecutionPolicy Bypass -File update.ps1
#
# The running supervisor and its python children are stopped via the scheduled
# task (Task Scheduler kills the whole process tree), the code is pulled, the
# python deps are re-applied, and the task is started again.

$ErrorActionPreference = "Stop"

$root      = Split-Path -Parent $PSScriptRoot
$taskName  = "Uniagent"
$taskExists = [bool](Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)

Write-Host "==> Stopping Uniagent..." -ForegroundColor Cyan
if ($taskExists) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3   # let the old python exit and drop its file locks
}

Write-Host "==> Pulling latest code..." -ForegroundColor Cyan
git -C $root pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host "git pull failed - the server is stopped. Fix the problem and re-run update.ps1." -ForegroundColor Red
    exit 1
}

Write-Host "==> Re-applying dependencies..." -ForegroundColor Cyan
& (Join-Path $root ".venv\Scripts\python.exe") -m pip install -r (Join-Path $root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install failed - see the message above. The server is stopped." -ForegroundColor Red
    exit 1
}

Write-Host "==> Restarting Uniagent..." -ForegroundColor Cyan
if ($taskExists) {
    Start-ScheduledTask -TaskName $taskName
} else {
    # Server was being run by hand - bring it back the same way.
    Start-Process powershell.exe -ArgumentList (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\run-server.ps1`"")
}

Write-Host "Done. Uniagent is back up at https://localhost:8764" -ForegroundColor Green
