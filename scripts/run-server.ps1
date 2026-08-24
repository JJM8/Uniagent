# Supervises the Uniagent server and cron watcher on Windows.
#
# Started by the "Uniagent" scheduled task at logon (see attach.ps1), and the
# equivalent of Restart=always in the Linux systemd units: if either process
# dies, it is started again. Gives up on a process that crashes within seconds
# of starting over and over (a port conflict, say) rather than spinning forever.
#
# THIS IS ALSO HOW AN UPDATE RESTARTS. Both processes know they are supervised
# (UNIAGENT_SUPERVISED, set below and read by scripts/service.py) and a restart
# is therefore an exit: they stop, this loop starts them again seconds later,
# and the new process reads the new code, because Python reads a .py once at
# startup and never looks again. Nothing here needs to know an update happened.
#
# Stop with:  schtasks /End /TN Uniagent   (kills the whole process tree)
#
# Logs go to <repo>\logs\server.out.log / server.err.log / cron.*.log.

$ErrorActionPreference = "Continue"

$root       = Split-Path -Parent $PSScriptRoot            # scripts\.. -> repo root
$scriptsDir = Join-Path $root "scripts"
$logDir     = Join-Path $root "logs"
$py         = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }              # fall back to PATH

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location $scriptsDir                                   # same as systemd WorkingDirectory

# Inherited by everything started below, which is the point of setting them here
# rather than passing them per process.
#
#   UNIAGENT_SUPERVISED  tells the two processes something will restart them if
#                        they exit, so "restart" can mean "stop". Without it
#                        they would have to launch their own replacement, and
#                        this loop would start a second one on the same port.
#   PYTHONUTF8           Windows decides text encoding from the system codepage,
#                        which is cp1252 on a Western install. This makes Python
#                        read and write UTF-8 regardless - the chats, the .env
#                        and these logs all contain characters cp1252 has no
#                        room for.
#   PYTHONUNBUFFERED     so the log has the line in it when the thing happened,
#                        not when the buffer happened to fill.
$env:UNIAGENT_SUPERVISED = "1"
$env:PYTHONUTF8          = "1"
$env:PYTHONUNBUFFERED    = "1"

$server = @{ name = "server"; args = "server.py";  proc = $null; start = $null; quick = 0 }
$cron   = @{ name = "cron";   args = "cron.py";    proc = $null; start = $null; quick = 0 }
$MAX_QUICK_CRASHES = 15
$MIN_UPTIME_SECS   = 15

function Start-One($job) {
    $out = Join-Path $logDir ($job.name + ".out.log")
    $err = Join-Path $logDir ($job.name + ".err.log")
    # -WindowStyle is Windows-only; harmless to include there, excluded elsewhere
    # so the same script is testable (and runnable) on PowerShell Core.
    $sp = @{
        FilePath               = $py
        ArgumentList           = $job.args
        WorkingDirectory       = $scriptsDir
        RedirectStandardOutput = $out
        RedirectStandardError  = $err
        PassThru               = $true
    }
    if ($env:OS -eq "Windows_NT") { $sp.WindowStyle = "Hidden" }
    $job.proc = Start-Process @sp
    $job.start = Get-Date
    Write-Host ("[{0}] {1} started (pid {2})" -f (Get-Date -Format HH:mm:ss), $job.name, $job.proc.Id)
}

function Check-One($job) {
    if ($job.proc -eq $null) { return }
    $job.proc.Refresh()
    if (-not $job.proc.HasExited) { return }

    $uptime = ((Get-Date) - $job.start).TotalSeconds
    if ($uptime -lt $MIN_UPTIME_SECS) {
        $job.quick++
        Write-Host ("[{0}] {1} died after {2:N0}s (crash {3}/{4})" -f `
            (Get-Date -Format HH:mm:ss), $job.name, $uptime, $job.quick, $MAX_QUICK_CRASHES)
        if ($job.quick -ge $MAX_QUICK_CRASHES) {
            $msg = ("[{0}] {1} keeps dying at startup - giving up on it. " +
                "Check {2}\{1}.err.log and fix the cause, then restart the task.")
            Write-Host ($msg -f (Get-Date -Format HH:mm:ss), $job.name, $logDir)
            $job.proc = $null
            return
        }
    } else {
        $job.quick = 0   # it lived a while - whatever killed it was a one-off
        Write-Host ("[{0}] {1} exited (code {2}) - restarting" -f `
            (Get-Date -Format HH:mm:ss), $job.name, $job.proc.ExitCode)
    }
    Start-One $job
}

# The port the server will actually use, so this line is right on an install
# that answered anything other than the default to the wizard's port question.
$port = 8764
try {
    $port = [int](& $py -c "import sys; sys.path.insert(0, sys.argv[1]); import provider; print(provider.port('UNIAGENT_HTTPS_PORT', 8764))" $scriptsDir)
} catch { }

Write-Host "Uniagent supervisor running - server on https://localhost:$port"
Start-One $server
Start-One $cron

while ($true) {
    Start-Sleep -Seconds 5
    Check-One $server
    Check-One $cron
}
