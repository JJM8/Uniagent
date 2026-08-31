# Uniagent - one-line installer for Windows 10/11.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/JJM8/Uniagent/main/install.ps1 | iex"
#
# What it does:
#   1. installs git and Python 3.12 if they are missing (winget where it is
#      available, otherwise a direct download - see Install-Git / Install-Python)
#   2. clones (or updates) Uniagent into %USERPROFILE%\Uniagent
#      - override with $env:UNIAGENT_HOME before running
#   3. makes a venv and installs the Python dependencies (voice extras are
#      best-effort; the browser microphone works without them)
#   4. writes .env from .env.example if it does not exist yet
#   5. asks the three first-run questions - a password, a port, and one provider
#      to talk to (scripts\setup_wizard.py, the same three install.sh asks)
#   6. hands over to attach.ps1, which puts `uniagentcli` on your PATH, installs
#      the logon task, starts the server and the cron watcher, waits for the
#      server to answer and prints the password
#   7. adds a firewall rule if elevated, and opens the web UI
#
# Steps 5 and 6 are where this used to differ from install.sh, and it mattered:
# Windows never asked the first-run questions, so an install finished with a
# password nobody had been shown and no provider configured. Both scripts now
# ask the same questions and end in the same place.
#
# Nothing here needs administrator rights: every fallback installs per-user.
# Running elevated only adds the firewall rule.
#
# To uninstall: attach.ps1 -Remove, then delete the folder.
#
# Works when piped straight in with `irm | iex`, or run as a downloaded file.
# Note that it must not use $PSScriptRoot or $MyInvocation - both are empty when
# a script arrives down a pipe.

$ErrorActionPreference = "Stop"

# Windows 10's PowerShell 5.1 can still default to TLS 1.0, which every download
# below would refuse. Harmless to set on newer hosts.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$Repo      = "https://github.com/JJM8/Uniagent.git"
$Root      = if ($env:UNIAGENT_HOME) { $env:UNIAGENT_HOME } else { Join-Path $HOME "Uniagent" }
$ToolsDir  = Join-Path $env:LOCALAPPDATA "Uniagent\tools"
$PyVersion = "3.12.10"
# Only the fallback. The real one is read out of .env after the setup wizard has
# had its say, the same way install.sh does it - otherwise every message below
# names a port the server is not actually listening on.
$Port      = 8764

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host ""
    Write-Host $msg -ForegroundColor Red
    exit 1
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

function Get-Arch {
    $a = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($a -eq "ARM64") { "arm64" } else { "amd64" }
}

function Add-ToUserPath($dir) {
    # Returns $true if it actually added anything. Reads the raw User value,
    # which is $null on an account that has never had one - hence no method
    # calls until it has been defaulted.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { $userPath = "" }
    $parts = $userPath.Split(@(";"), [StringSplitOptions]::RemoveEmptyEntries)
    if ($parts -contains $dir) { return $false }
    [Environment]::SetEnvironmentVariable("Path", ((@($parts) + $dir) -join ";"), "User")
    return $true
}

function Invoke-Native {
    # Runs an external program and hands back its exit code. Two things this
    # buys us over a bare call: the output goes to the host instead of into the
    # function's return value, and $ErrorActionPreference is dropped to Continue
    # for the duration, so a tool that chats on stderr (git and pip both do)
    # cannot turn into a terminating error in this "Stop" script.
    param([Parameter(Mandatory=$true)][string]$Exe, [string[]]$Arguments = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Invoke-Download($url, $dest) {
    $prev = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"   # the progress bar makes IWR very slow
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    } finally {
        $ProgressPreference = $prev
    }
}

function Get-PythonCmd {
    # The first Python 3.10+ we can find, as @{ exe; args; version }, or $null.
    # It is returned as a program plus an argument array and never as one string:
    # "py -3" in a single string makes PowerShell look for a program with a
    # space in its name, which is not a thing.
    $candidates = @(
        @{ exe = "py";      args = @("-3") },
        @{ exe = "python";  args = @()     },
        @{ exe = "python3"; args = @()     }
    )
    foreach ($c in $candidates) {
        $exe = $c.exe
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $probe = @($c.args) + @("-c", "import sys;print('%d.%d' % sys.version_info[:2])")
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            # A bare `python` on a machine without Python is the Microsoft Store
            # stub: it prints an advert to stderr and exits non-zero, so it falls
            # out here rather than being mistaken for an interpreter.
            $out = & $exe @probe 2>$null | Select-Object -First 1
        } catch {
            $out = $null
        } finally {
            $ErrorActionPreference = $prev
        }
        if (-not $out) { continue }
        try { $v = [version]($out.ToString().Trim()) } catch { continue }
        if ($v -ge [version]"3.10") { return @{ exe = $exe; args = @($c.args); version = $v } }
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  installing Python 3.12 with winget..."
        Invoke-Native winget @("install", "-e", "--id", "Python.Python.3.12",
                               "--accept-package-agreements", "--accept-source-agreements") | Out-Null
        Refresh-Path
        if (Get-PythonCmd) { return }
        Write-Host "  winget did not leave a usable Python - downloading from python.org instead." -ForegroundColor Yellow
    } else {
        Write-Host "  winget is not on this machine - downloading Python from python.org." -ForegroundColor Yellow
    }

    $file = "python-$PyVersion-$(Get-Arch).exe"
    $url  = "https://www.python.org/ftp/python/$PyVersion/$file"
    $exe  = Join-Path $env:TEMP $file
    Write-Host "  downloading $url"
    Invoke-Download $url $exe

    # InstallAllUsers=0 keeps it per-user, which is what avoids a UAC prompt.
    # PrependPath and Include_launcher are what put `python` and `py` on PATH.
    Write-Host "  running the installer (this takes a minute)..."
    $p = Start-Process $exe -Wait -PassThru -ArgumentList @(
        "/passive", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "Include_test=0")
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {   # 3010 = installed, wants a reboot
        throw "the Python installer exited with code $($p.ExitCode)"
    }
    Refresh-Path
}

function Install-Git {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  installing Git with winget..."
        Invoke-Native winget @("install", "-e", "--id", "Git.Git",
                               "--accept-package-agreements", "--accept-source-agreements") | Out-Null
        Refresh-Path
        if (Get-Command git -ErrorAction SilentlyContinue) { return }
        Write-Host "  winget did not leave a usable git - falling back to MinGit." -ForegroundColor Yellow
    } else {
        Write-Host "  winget is not on this machine - falling back to MinGit." -ForegroundColor Yellow
    }

    # MinGit is the trimmed-down build git-for-windows publishes for exactly this
    # (it is what the GitHub tooling embeds). A plain zip, so there is no
    # installer and no UAC prompt - we unpack it under LOCALAPPDATA and add its
    # cmd\ directory to PATH. Enough for clone, pull and everything update.ps1 does.
    $want = if ((Get-Arch) -eq "arm64") { "arm64" } else { "64-bit" }
    $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" `
        -Headers @{ "User-Agent" = "uniagent-installer" }
    $asset = $rel.assets |
        Where-Object { $_.name -like "MinGit-*-$want.zip" -and $_.name -notlike "*busybox*" } |
        Select-Object -First 1
    if (-not $asset) { throw "no MinGit download published for $want" }

    $zip = Join-Path $env:TEMP $asset.name
    $dir = Join-Path $ToolsDir "git"
    Write-Host "  downloading $($asset.name) (about 40 MB)..."
    Invoke-Download $asset.browser_download_url $zip
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dir -Force
    Remove-Item $zip -Force -ErrorAction SilentlyContinue

    $gitCmd = Join-Path $dir "cmd"
    if (-not (Test-Path (Join-Path $gitCmd "git.exe"))) { throw "MinGit unpacked but git.exe is not where it should be" }
    [void](Add-ToUserPath $gitCmd)       # future terminals
    $env:Path = $gitCmd + ";" + $env:Path # and this one
    Write-Host "  git installed to $dir"
}

# Reading the password back out of .env is attach.ps1's job now, since that is
# the script that starts the server and so the one that knows when there is a
# password to read.

function Test-ServerUp($port) {
    $client = New-Object Net.Sockets.TcpClient
    try   { $client.Connect("127.0.0.1", $port); return $true }
    catch { return $false }
    finally { $client.Close() }
}

# --- 1. git ----------------------------------------------------------------
Step "Checking git..."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git not found." -ForegroundColor Yellow
    try { Install-Git } catch {
        Fail ("Could not install git automatically: $($_.Exception.Message)`n" +
              "Install Git for Windows from https://git-scm.com/download/win, then re-run this installer.")
    }
    Refresh-Path
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Fail ("git still is not on PATH after installing it.`n" +
              "Close this window, open a new one, and re-run the installer.")
    }
}
Write-Host ("git " + (git --version).Split(" ")[-1])

# --- 2. Python -------------------------------------------------------------
Step "Checking Python 3.10+..."
$pyCmd = Get-PythonCmd
if (-not $pyCmd) {
    Write-Host "Python 3.10+ not found." -ForegroundColor Yellow
    try { Install-Python } catch {
        Fail ("Could not install Python automatically: $($_.Exception.Message)`n" +
              "Install Python 3.12 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then re-run this installer.")
    }
    $pyCmd = Get-PythonCmd
    if (-not $pyCmd) {
        Fail ("Python was installed but is not on PATH yet.`n" +
              "Close this window, open a new one, and re-run the installer.")
    }
}
$pyExe  = $pyCmd.exe
$pyArgs = @($pyCmd.args)
Write-Host ("Python $($pyCmd.version) OK (" + ((@($pyExe) + $pyArgs) -join " ") + ")")

# --- 3. clone / update -----------------------------------------------------
Step "Getting the code into $Root ..."
if (Test-Path (Join-Path $Root ".git")) {
    if ((Invoke-Native git @("-C", $Root, "pull", "--ff-only")) -ne 0) {
        Fail "git pull failed. See the message above."
    }
} elseif (Test-Path $Root) {
    Fail "$Root already exists but is not a Uniagent checkout - refusing to touch it."
} else {
    if ((Invoke-Native git @("clone", "--quiet", $Repo, $Root)) -ne 0) {
        Fail "git clone failed. See the message above."
    }
}

# --- 4. venv + dependencies ------------------------------------------------
Step "Creating a virtualenv and installing dependencies..."
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    if ((Invoke-Native $pyExe ($pyArgs + @("-m", "venv", (Join-Path $Root ".venv")))) -ne 0) {
        Fail "Could not create the virtualenv."
    }
}
if (-not (Test-Path $venvPy)) { Fail "The virtualenv was created but $venvPy is missing." }

Invoke-Native $venvPy @("-m", "pip", "install", "--upgrade", "pip", "--quiet") | Out-Null
if ((Invoke-Native $venvPy @("-m", "pip", "install", "-r", (Join-Path $Root "requirements.txt"), "--quiet")) -ne 0) {
    Fail "Dependency install failed. See the message above."
}

if ((Invoke-Native $venvPy @("-m", "pip", "install", "-r", (Join-Path $Root "requirements-voice.txt"), "--quiet")) -ne 0) {
    Write-Host ("Voice extras skipped (pyaudio/pynput) - the web page's microphone still works; " +
                "only the local hold-to-talk key won't. To add later: pip install -r requirements-voice.txt") -ForegroundColor Yellow
}

# --- 5. .env ---------------------------------------------------------------
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Root ".env.example") $envFile
    Write-Host "Created $envFile - add your API keys there (OPENAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY), or add them later on the settings page." -ForegroundColor Yellow
} else {
    Write-Host "Found an existing .env - leaving it alone."
}

# --- 6. first-run questions ------------------------------------------------
# Before anything starts, not after: the wizard sets the password and the port,
# and both have to be settled before the server reads them. Exactly what
# install.sh does at this point, with the same script.
Step "First-run setup..."
& $venvPy (Join-Path $Root "scripts\setup_wizard.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host ("Setup did not finish - run it again whenever you like with:`n" +
                "  `"$venvPy`" `"$Root\scripts\setup_wizard.py`"") -ForegroundColor Yellow
}

# Whatever the wizard settled on. Read back from .env rather than remembered, so
# this is right whether the wizard ran, was skipped, or the file already had a
# port in it.
try {
    $Port = [int](& $venvPy -c "import sys; sys.path.insert(0, sys.argv[1]); import provider; print(provider.port('UNIAGENT_HTTPS_PORT', 8764))" (Join-Path $Root "scripts"))
} catch { }

# --- 7. firewall (only helps if we're elevated) ----------------------------
# Before the server starts rather than after, so the rule is already there the
# first time it binds and Windows has no reason to pop its own dialog.
if (Test-IsAdmin) {
    try {
        New-NetFirewallRule -DisplayName "Uniagent (port $Port)" -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort $Port -Profile Private `
            -ErrorAction Stop | Out-Null
        Write-Host "Firewall rule added for https://<this-pc-ip>:$Port on private networks."
    } catch {
        Write-Host "Could not add a firewall rule (not fatal): $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "Not elevated - Windows will ask to allow Uniagent through the firewall on first start. Click 'Allow'."
}

# --- 8. CLI on PATH, autostart, and start it -------------------------------
# attach.ps1 is the half of this that belongs to the machine rather than to the
# download, and it is the same script someone runs by hand after moving the
# folder to a new PC. -Port so it does not ask the port question the wizard has
# just asked.
Step "Installing the command, the logon task, and starting Uniagent..."
try {
    & (Join-Path $Root "attach.ps1") -Port $Port
} catch {
    Fail ("Could not finish the install: $($_.Exception.Message)`n" +
          "Everything is downloaded. Start Uniagent by hand with:`n" +
          "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\run-server.ps1`"")
}

# --- 9. done ---------------------------------------------------------------
# attach.ps1 has already waited for the server, printed the password and listed
# the day-to-day commands. Only the things that belong to a DOWNLOAD rather than
# to this machine are left to say.
$up = Test-ServerUp $Port

Write-Host ""
Write-Host "Install complete:" -ForegroundColor Green
Write-Host "  Installed to: $Root"
Write-Host "  Update:       powershell -ExecutionPolicy Bypass -File `"$Root\scripts\update.ps1`""
Write-Host "                (or the 'update now' button on the settings page)"
Write-Host "  Uninstall:    powershell -ExecutionPolicy Bypass -File `"$Root\attach.ps1`" -Remove"
Write-Host "                then delete $Root (and $ToolsDir if the installer put git there)" -ForegroundColor DarkGray
if ($up) { try { Start-Process "https://localhost:$Port" } catch { } }
