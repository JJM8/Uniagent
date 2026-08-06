# uniagent update - pulls the latest code and restarts the running server.
#   powershell -ExecutionPolicy Bypass -File update.ps1
#
# A wrapper, and deliberately nothing more. The update itself is update.py,
# which is shared with Linux and with the "update now" button on the settings
# page, so there is exactly one description of what an update does and what it
# is careful not to touch. Read that file's docstring, not this one.
#
# Anything after -- is passed straight through, so:
#   update.ps1 -- --check        say what an update would bring, change nothing
#   update.ps1 -- --no-restart   update, but leave Uniagent on the old code

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

# The venv's interpreter if the installer built one - that is where requests and
# cryptography live - and whatever python is on PATH otherwise.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source }
    if (-not $py) {
        Write-Host "No python found. Re-run install.ps1." -ForegroundColor Red
        exit 1
    }
}

& $py (Join-Path $root "scripts\update.py") @args
exit $LASTEXITCODE
