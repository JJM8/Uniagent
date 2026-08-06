# Installs or removes Uniagent's autostart on Windows.
#
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Install
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Install -Port 8790
#
# Creates a scheduled task that runs the supervisor (run-server.ps1) at the
# current user's logon, so the server and cron watcher come up every time the
# machine is used - no admin needed, no service account, nothing left running
# when you log out. The task has no execution time limit, because the
# supervisor is meant to run for as long as the session does.
#
# This is Windows' attach.sh: the counterpart to install.ps1's second half, for
# a folder that is already set up. Copy the whole Uniagent folder anywhere - a
# second machine, a USB stick - run this, and the copy runs there too, with the
# password, providers and chats that travelled with it. Nothing here writes an
# install location anywhere: the task points at wherever this script is sitting
# when you run it, so a folder that moves is fixed by running it again.
#
# "Always on, even before anyone logs in" needs a real Windows service, which
# means admin rights - see README for the NSSM option.

param(
    [switch]$Install,
    [switch]$Remove,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

$taskName = "Uniagent"
$root     = Split-Path -Parent $PSScriptRoot
$runner   = Join-Path $root "scripts\run-server.ps1"

# Usable means 3.10 or newer and able to import what Uniagent needs. That second
# half is what makes a copied folder work: a .venv carried from another machine
# is all there on disk but names a Python that isn't on this one, so it fails at
# the first import rather than at a path check.
function Test-Py($exe) {
    if (-not $exe) { return $false }
    try {
        & $exe -c "import sys; assert sys.version_info >= (3, 10); import requests, cryptography" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-Py {
    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    foreach ($candidate in @($venvPy, "python", "py")) {
        if (Test-Py $candidate) { return $candidate }
    }

    # Nothing here can run it yet. Build a virtualenv inside the folder - the one
    # step that wants a network, and dependencies only: no download of Uniagent
    # itself, nothing of yours touched.
    Write-Host "No usable Python found - building $root\.venv" -ForegroundColor Cyan
    $bootstrap = $null
    foreach ($candidate in @("python", "py")) {
        try {
            & $candidate -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $bootstrap = $candidate; break }
        } catch { }
    }
    if (-not $bootstrap) {
        throw "No Python 3.10 or newer on this machine. Install one from python.org and run this again."
    }
    if (Test-Path $venvPy) { Remove-Item -Recurse -Force (Join-Path $root ".venv") }
    & $bootstrap -m venv (Join-Path $root ".venv")
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $root "requirements.txt")
    if (-not (Test-Py $venvPy)) {
        throw "The virtualenv was built but still cannot import requests and cryptography."
    }
    return $venvPy
}

if ($Install) {
    $py = Resolve-Py
    Write-Host "Using $py" -ForegroundColor DarkGray

    # The port, and only the port. The password and the providers are in the
    # .env that came with the folder; asking again would be asking you to
    # re-enter things you already have.
    $wizard = Join-Path $root "scripts\setup_wizard.py"
    if ($Port -gt 0) {
        & $py -c "import sys; sys.path.insert(0, sys.argv[1]); import provider; provider.set_env('UNIAGENT_HTTPS_PORT', sys.argv[2])" (Join-Path $root "scripts") "$Port"
    } else {
        & $py $wizard --port-only
    }
    # Read back rather than remembered, so this is right however the question went.
    $httpsPort = & $py -c "import sys; sys.path.insert(0, sys.argv[1]); import provider; print(provider.port('UNIAGENT_HTTPS_PORT', 8764))" (Join-Path $root "scripts")

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`"")
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable
    # Worth saying out loud when it changes: running this from a second copy
    # moves your autostart onto that copy, and finding that out by wondering why
    # your edits do nothing is a bad afternoon.
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        $was = $existing.Actions[0].Arguments
        if ($was -and ($was -notlike "*$runner*")) {
            Write-Host "This task used to run a different folder. It now runs $root." -ForegroundColor Yellow
        }
    }

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Uniagent personal AI agent - web server and cron watcher" `
        -Force | Out-Null
    Write-Host "Scheduled task '$taskName' installed - Uniagent starts at every logon." -ForegroundColor Green

    # Start it now, so an install finishes with the server already up.
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Uniagent is starting now, from $root (https://localhost:$httpsPort)." -ForegroundColor Green
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
    Write-Host "Usage: install-autostart.ps1 -Install [-Port <n>] | -Remove"
    exit 1
}
