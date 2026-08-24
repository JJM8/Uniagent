# Uniagent: make THIS folder a service on THIS machine. (Windows)
#
#   .\attach.ps1                  register and start, asking which port
#   .\attach.ps1 -Port 8790       the same without the question
#   .\attach.ps1 -NoStart         write the autostart but don't start it
#   .\attach.ps1 -Remove          unhook it again
#
# Or just double-click attach.cmd, which runs this with the execution policy
# already dealt with.
#
# This is attach.sh, for Windows - the same script, doing the same job, in the
# form this platform needs. install.ps1 downloads Uniagent and walks you through
# a password, a port and a first provider; this assumes all of that is long done
# and does only the part that belongs to the machine you are standing at: find a
# Python that can run the code, put the uniagentcli command on your PATH, and
# register the scheduled task that starts the server and the cron watcher at
# every logon - pointed at THIS folder.
#
# It never clones, never pulls, never asks about API keys, and never touches
# your .env beyond the port. Copy the whole folder somewhere else - a second
# machine, a USB stick - run this, and the copy is a running service there too,
# with the same chats, settings, skills and keys it had at home.
#
# Nothing inside the folder records where the folder is, so the same copy can be
# attached on several machines at once, each on its own port. Move the folder
# afterwards and the task will point at where it used to be: run this again from
# the new place and it takes over.
#
# No administrator rights are needed. A scheduled task at logon is as far as
# that gets you: "running before anyone logs in" needs a real Windows service,
# which does need admin - see setup.md.

[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoStart,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# The folder this script is sitting in. Every path below is built from it, which
# is the whole point: there is no baked-in install location.
$Root     = $PSScriptRoot
$TaskName = "Uniagent"
$Runner   = Join-Path $Root "scripts\run-server.ps1"
$EnvFile  = Join-Path $Root ".env"
$BinDir   = Join-Path $Root "bin"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "xx  $msg" -ForegroundColor Red; exit 1 }
function Dim($msg)  { Write-Host "    $msg" -ForegroundColor DarkGray }

# --- the uniagentcli command ------------------------------------------------

function Add-ToUserPath($dir) {
    # $true if it actually added anything. The raw User value is $null on an
    # account that has never had one, hence no method calls until it is a string.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { $userPath = "" }
    $parts = $userPath.Split(@(";"), [StringSplitOptions]::RemoveEmptyEntries)
    if ($parts -contains $dir) { return $false }
    [Environment]::SetEnvironmentVariable("Path", ((@($parts) + $dir) -join ";"), "User")
    $env:Path = $env:Path + ";" + $dir      # and this window, so it works now
    return $true
}

function Remove-FromUserPath($dir) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { return }
    $parts = $userPath.Split(@(";"), [StringSplitOptions]::RemoveEmptyEntries) |
             Where-Object { $_ -ne $dir }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

# --- detaching --------------------------------------------------------------

if ($Remove) {
    Step "Removing Uniagent's autostart from this machine"

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        # Only if it is OURS. A task registered from a different copy of
        # Uniagent points somewhere else and is not this folder's to unhook.
        # Not $args: that is one of PowerShell's own automatic variables and
        # cannot be assigned to.
        $taskArgs = ""
        if ($task.Actions -and $task.Actions[0].Arguments) { $taskArgs = $task.Actions[0].Arguments }
        if ($taskArgs -like "*$Runner*") {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Scheduled task '$TaskName' removed."
        } else {
            Warn "The '$TaskName' task runs a different folder - leaving it alone."
        }
    } else {
        Dim "No scheduled task was installed."
    }

    # And stop whatever is running right now, which the task being gone does not
    # do by itself.
    foreach ($name in @("server", "cron")) {
        $pidFile = Join-Path $Root ("logs\" + $name + ".pid")
        if (Test-Path $pidFile) {
            $processId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($processId) {
                Start-Process taskkill -ArgumentList "/T","/F","/PID",$processId `
                    -NoNewWindow -Wait -ErrorAction SilentlyContinue
            }
            Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        }
    }

    if (Test-Path (Join-Path $BinDir "uniagentcli.cmd")) {
        Remove-Item (Join-Path $BinDir "uniagentcli.cmd") -Force -ErrorAction SilentlyContinue
    }
    Remove-FromUserPath $BinDir

    Write-Host ""
    Write-Host "Done. The folder itself is untouched - your chats, settings and .env are all still in $Root." -ForegroundColor Green
    exit 0
}

# --- 1. a Python that can actually run this ---------------------------------

# Usable means: at least 3.10, and able to import the two things Uniagent needs.
# That second half is what makes a copied folder work. A .venv carried over from
# another machine looks perfectly fine - the directory is all there - but its
# pyvenv.cfg names a Python that isn't on this machine, so it fails at the first
# import rather than at a path check.
function Test-Py($exe) {
    if (-not $exe) { return $false }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $exe -c "import sys; assert sys.version_info >= (3, 10); import requests, cryptography" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Find-Bootstrap {
    # Any Python 3.10+ on the machine, good enough to build a venv with. A bare
    # `python` on a machine without one is the Microsoft Store stub: it prints an
    # advert and exits non-zero, so it falls out here rather than being mistaken
    # for an interpreter.
    foreach ($candidate in @("py", "python", "python3")) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $candidate -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { } finally { $ErrorActionPreference = $prev }
    }
    return $null
}

Step "Looking for a Python to run Uniagent with"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$py = $null
foreach ($candidate in @($venvPy, "python", "py")) {
    if (Test-Py $candidate) {
        $py = $candidate
        break
    }
}

if (-not $py) {
    # Nothing here can run it yet, so build a virtualenv inside the folder. This
    # is the one step that wants a network, and it is dependencies only - no
    # download of Uniagent itself, nothing of yours touched.
    $bootstrap = Find-Bootstrap
    if (-not $bootstrap) {
        Fail ("No Python 3.10 or newer on this machine. Install one from " +
              "https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH') " +
              "and run this again.")
    }
    Step "None of them had the dependencies - building $Root\.venv"
    if (Test-Path (Join-Path $Root ".venv")) {
        Remove-Item (Join-Path $Root ".venv") -Recurse -Force
    }
    & $bootstrap -m venv (Join-Path $Root ".venv")
    if (-not (Test-Path $venvPy)) { Fail "Could not create the virtualenv." }
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Fail "Installing the dependencies failed - check the network and run this again."
    }
    # Optional, and a failure here is not a failure: the browser's hold-to-talk
    # records in the page and never touches these.
    & $venvPy -m pip install --quiet -r (Join-Path $Root "requirements-voice.txt") 2>$null
    if ($LASTEXITCODE -ne 0) {
        Dim "(skipped the optional voice extras - browser hold-to-talk works regardless)"
    }
    if (-not (Test-Py $venvPy)) {
        Fail "The virtualenv was built but still cannot import requests and cryptography."
    }
    $py = $venvPy
}

# Spelled out in full, because the scheduled task and the CLI shim both bake
# this path in and a relative one would break the moment either is run from
# somewhere else.
$resolved = (Get-Command $py -ErrorAction SilentlyContinue).Source
if ($resolved) { $py = $resolved }
Dim "using $py"

# --- 2. .env ----------------------------------------------------------------

# Normally this exists, because the folder was copied from a working install and
# brought its keys with it. It only doesn't when someone deliberately left it
# behind, and then a blank one is the right thing: the server generates a
# password into it on first start and the settings page fills in the rest.
if (-not (Test-Path $EnvFile)) {
    Warn "No .env in this folder - starting a blank one. Add your API keys on the settings page."
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
}

# --- 3. which port ----------------------------------------------------------

$scriptsDir = Join-Path $Root "scripts"
if ($Port -gt 0) {
    if ($Port -lt 1 -or $Port -gt 65535) { Fail "A port has to be between 1 and 65535." }
    & $py -c "import sys; sys.path.insert(0, sys.argv[1]); import provider; provider.set_env('UNIAGENT_HTTPS_PORT', sys.argv[2])" $scriptsDir "$Port"
} else {
    # The wizard's port question, and only that one - the password is already in
    # the .env that came with the folder, and so are the providers. Asking again
    # would be asking you to re-enter things you have.
    & $py (Join-Path $scriptsDir "setup_wizard.py") --port-only
    if ($LASTEXITCODE -ne 0) {
        Warn "Port question skipped - keeping whatever .env already says."
    }
}

# Read back rather than remembered, so this is right whether the question was
# asked, answered with Enter, skipped, or never reached.
$httpsPort = 8764
try {
    $httpsPort = [int](& $py -c "import sys; sys.path.insert(0, sys.argv[1]); import provider; print(provider.port('UNIAGENT_HTTPS_PORT', 8764))" $scriptsDir)
} catch { }

# --- 4. the uniagentcli command ---------------------------------------------

Step "Pointing the uniagentcli command at this folder"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-Item (Join-Path $scriptsDir "uniagentcli.cmd") (Join-Path $BinDir "uniagentcli.cmd") -Force
if (Add-ToUserPath $BinDir) {
    Dim "Added $BinDir to your user PATH - open a NEW terminal to use 'uniagentcli'."
} else {
    Dim "'uniagentcli' is already on your PATH."
}

# --- 5. autostart -----------------------------------------------------------

Step "Registering the logon task"

# Worth saying out loud when it changes: running this from a second copy moves
# your autostart onto that copy, and finding that out by wondering why your edits
# do nothing is a bad afternoon.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Actions -and $existing.Actions[0].Arguments) {
    if ($existing.Actions[0].Arguments -notlike "*$Runner*") {
        Warn "This task used to run a different folder. It now runs $Root."
    }
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"")
$trigger = New-ScheduledTaskTrigger -AtLogOn
# No execution time limit: the supervisor is meant to run for as long as the
# session does, and the default hour would otherwise kill Uniagent every day.
# StartWhenAvailable covers the machine being asleep at the trigger.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description "Uniagent personal AI agent - web server and cron watcher" | Out-Null
} catch {
    Warn "Could not register the scheduled task: $($_.Exception.Message)"
    Warn "Start Uniagent by hand with:"
    Warn "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
    exit 0
}

if ($NoStart) {
    Write-Host ""
    Write-Host "Registered. Not started, as asked. Start it with: schtasks /Run /TN $TaskName" -ForegroundColor Green
    exit 0
}

# Stop whatever the previous copy left running, or the new server finds the port
# taken and dies in a restart loop nobody is watching.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
foreach ($name in @("server", "cron")) {
    $pidFile = Join-Path $Root ("logs\" + $name + ".pid")
    if (Test-Path $pidFile) {
        $processId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($processId) {
            Start-Process taskkill -ArgumentList "/T","/F","/PID",$processId `
                -NoNewWindow -Wait -ErrorAction SilentlyContinue
        }
    }
}
Start-ScheduledTask -TaskName $TaskName

# --- 6. wait for it, then say where it is -----------------------------------

Step "Waiting for the server to come up..."

function Test-ServerUp($port) {
    $client = New-Object Net.Sockets.TcpClient
    try   { $client.Connect("127.0.0.1", $port); return $true }
    catch { return $false }
    finally { $client.Close() }
}

function Get-EnvPassword($file) {
    # The same read auth.py does: first non-blank UNIAGENT_PASSWORD line, quotes
    # off - .env strips surrounding quotes when it is read, so a quoted password
    # is not the password that gets checked.
    if (-not (Test-Path $file)) { return $null }
    foreach ($line in (Get-Content $file -ErrorAction SilentlyContinue)) {
        $t = $line.Trim()
        if ($t.StartsWith("UNIAGENT_PASSWORD=")) {
            $v = $t.Substring("UNIAGENT_PASSWORD=".Length).Trim().Trim('"').Trim("'")
            if ($v) { return $v }
        }
    }
    return $null
}

$deadline = (Get-Date).AddSeconds(90)
$pass = $null
$up = $false
while ((Get-Date) -lt $deadline) {
    if (-not $up)   { $up = Test-ServerUp $httpsPort }
    if (-not $pass) { $pass = Get-EnvPassword $EnvFile }
    if ($up -and $pass) { break }
    Start-Sleep -Seconds 2
}

Write-Host ""
if ($up -and $pass) {
    Write-Host "==========================================================" -ForegroundColor Green
    Write-Host "  Uniagent is running from $Root" -ForegroundColor Green
    Write-Host "  Password:  $pass" -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Green
} elseif ($up) {
    Warn "Running, but no password turned up in $EnvFile."
    Warn "Look for it in $Root\logs\server.out.log"
} else {
    Warn "The server has not answered on port $httpsPort yet. It may still be starting."
    Warn "Check $Root\logs\server.err.log"
}

Write-Host ""
Write-Host "  Web UI:     https://localhost:$httpsPort   (other devices: https://<this-pc-ip>:$httpsPort)"
Write-Host "              First visit warns about the self-signed certificate - Advanced, then Proceed."
Write-Host "  CLI:        uniagentcli `"a question`"   (open a new terminal first if PATH changed)"
Write-Host "  Logs:       $Root\logs\server.out.log"
Write-Host "  Restart:    schtasks /End /TN $TaskName  then  schtasks /Run /TN $TaskName"
Write-Host "  Detach:     powershell -ExecutionPolicy Bypass -File `"$Root\attach.ps1`" -Remove"
