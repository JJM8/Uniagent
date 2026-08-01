# Uniagent - one-line installer for Windows 10/11.
#
#   powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/JJM8/Uniagent/main/install.ps1 | iex"
#
# What it does:
#   1. installs git and Python 3.12 if they are missing (via winget)
#   2. clones (or updates) Uniagent into %USERPROFILE%\Uniagent
#      - override with $env:UNIAGENT_HOME before running
#   3. makes a venv and installs the Python dependencies (voice extras are
#      best-effort; the browser microphone works without them)
#   4. writes .env from .env.example if it does not exist yet
#   5. puts a `uniagentcli` command on your PATH
#   6. installs a scheduled task so the server and cron watcher start at every
#      logon, and starts them right now
#   7. opens https://localhost:8764 and shows the first-run password
#
# Works when piped straight in with `irm | iex`, or run as a downloaded file.

$ErrorActionPreference = "Stop"

$Repo = "https://github.com/JJM8/Uniagent.git"
$Root = if ($env:UNIAGENT_HOME) { $env:UNIAGENT_HOME } else { Join-Path $HOME "Uniagent" }

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --- 1. git ----------------------------------------------------------------
Step "Checking git..."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git not found - installing Git via winget..." -ForegroundColor Yellow
    winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget could not install Git. Install Git for Windows manually, then re-run." -ForegroundColor Red
        exit 1
    }
    Refresh-Path
}
Write-Host ("git " + (git --version).Split(" ")[-1])

# --- 2. Python -------------------------------------------------------------
Step "Checking Python 3.10+..."
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $ver = & py -3 -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($ver -and [version]$ver -ge [version]"3.10") { $py = "py -3" }
}
if (-not $py) {
    Write-Host "Python 3.10+ not found - installing Python 3.12 via winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget could not install Python. Install Python 3.12 from python.org (tick 'Add to PATH'), then re-run." -ForegroundColor Red
        exit 1
    }
    Refresh-Path
    $py = "py -3"
}
& $py -c "import sys; assert sys.version_info >= (3,10); print('Python ' + sys.version.split()[0] + ' OK')"
if ($LASTEXITCODE -ne 0) { exit 1 }

# --- 3. clone / update -----------------------------------------------------
Step "Getting the code into $Root ..."
if (Test-Path (Join-Path $Root ".git")) {
    git -C $Root pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "git pull failed. See the message above." -ForegroundColor Red; exit 1 }
} elseif (Test-Path $Root) {
    Write-Host "$Root already exists but is not a Uniagent checkout - refusing to touch it." -ForegroundColor Red
    exit 1
} else {
    git clone --quiet $Repo $Root
    if ($LASTEXITCODE -ne 0) { Write-Host "git clone failed. See the message above." -ForegroundColor Red; exit 1 }
}

# --- 4. venv + dependencies ------------------------------------------------
Step "Creating a virtualenv and installing dependencies..."
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $py -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not create the virtualenv." -ForegroundColor Red; exit 1 }
}
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $Root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "Dependency install failed. See the message above." -ForegroundColor Red; exit 1 }

& $venvPy -m pip install -r (Join-Path $Root "requirements-voice.txt") --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "Voice extras skipped (pyaudio/pynput) - the web page's microphone still works; only the local hold-to-talk key won't. To add later: pip install -r requirements-voice.txt" -ForegroundColor Yellow
}

# --- 5. .env ---------------------------------------------------------------
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Root ".env.example") $envFile
    Write-Host "Created $envFile - add your API keys there (OPENAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY), or add them later on the settings page." -ForegroundColor Yellow
} else {
    Write-Host "Found an existing .env - leaving it alone."
}

# --- 6. CLI on PATH --------------------------------------------------------
Step "Installing the 'uniagentcli' command..."
$binDir = Join-Path $Root "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Copy-Item (Join-Path $Root "scripts\uniagent.cmd") (Join-Path $binDir "uniagent.cmd") -Force
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $binDir), "User")
    Write-Host "Added $binDir to your user PATH. Open a NEW terminal to use 'uniagentcli'."
} else {
    Write-Host "'uniagentcli' is already on your PATH."
}

# --- 7. autostart ----------------------------------------------------------
Step "Setting up autostart (server + cron watcher start at every logon)..."
& (Join-Path $Root "scripts\install-autostart.ps1") -Install

# --- 8. firewall (only helps if we're elevated) ----------------------------
if (Test-IsAdmin) {
    try {
        New-NetFirewallRule -DisplayName "Uniagent (port 8764)" -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort 8764 -Profile Private `
            -ErrorAction Stop | Out-Null
        Write-Host "Firewall rule added for https://<this-pc-ip>:8764 on private networks."
    } catch {
        Write-Host "Could not add a firewall rule (not fatal): $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "Not elevated - Windows will ask to allow Uniagent through the firewall on first start. Click 'Allow'."
}

# --- 9. show the password + open the page ----------------------------------
Start-Sleep -Seconds 6   # let the server print its first-run password
$passLine = $null
$logFile = Join-Path $Root "logs\server.out.log"
if (Test-Path $logFile) {
    $passLine = Select-String -Path $logFile -Pattern "([a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})" |
        Select-Object -First 1 | ForEach-Object { $_.Matches[0].Value }
}
if ($passLine) {
    Write-Host ""
    Write-Host "==========================================================" -ForegroundColor Green
    Write-Host "  Uniagent is running." -ForegroundColor Green
    Write-Host "  First-run password:  $passLine" -ForegroundColor Green
    Write-Host "  (keep it - or read it anytime from $envFile as UNIAGENT_PASSWORD)" -ForegroundColor DarkGray
    Write-Host "==========================================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Uniagent is running. If this is the first run, its generated" -ForegroundColor Green
    Write-Host "password is in $logFile" -ForegroundColor Green
}

Write-Host ""
Write-Host "Install complete:" -ForegroundColor Green
Write-Host "  Web UI:      https://localhost:8764   (from any device: https://<this-pc-ip>:8764)"
Write-Host "  CLI:         uniagentcli \"a question\"   (new terminal)"
Write-Host "  Update:      powershell -ExecutionPolicy Bypass -File `"$Root\scripts\update.ps1`""
Write-Host "  Uninstall:   powershell -ExecutionPolicy Bypass -File `"$Root\scripts\install-autostart.ps1`" -Remove"
try { Start-Process "https://localhost:8764" } catch { }
