# Installs or removes Uniagent's autostart on Windows.
#
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Install
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Install -Port 8790
#
# THE WORK IS IN ..\attach.ps1 - this is a forwarder and nothing else.
#
# It was the other way round once: this script did the job and attach.sh was the
# Linux equivalent with no counterpart here. That left the two platforms doing
# genuinely different things under the same name - this one never wrote the
# uniagentcli shim, never made a .env, and never waited for the server to answer
# before claiming it had started - so a folder attached on Windows came out with
# less than the same folder attached on Linux.
#
# One script does it now, at the top of the repo where attach.sh is, so there is
# one description of what attaching a folder means. This name is kept because
# the README and setup.md have pointed at it since the first Windows release,
# and a documented path that stops working is its own kind of bug.

param(
    [switch]$Install,
    [switch]$Remove,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

$attach = Join-Path (Split-Path -Parent $PSScriptRoot) "attach.ps1"
if (-not (Test-Path $attach)) {
    Write-Host "attach.ps1 is missing from $(Split-Path -Parent $PSScriptRoot)." -ForegroundColor Red
    exit 1
}

if ($Install) {
    if ($Port -gt 0) { & $attach -Port $Port } else { & $attach }
    exit $LASTEXITCODE
}
elseif ($Remove) {
    & $attach -Remove
    exit $LASTEXITCODE
}
else {
    Write-Host "Usage: install-autostart.ps1 -Install [-Port <n>] | -Remove"
    Write-Host "       (both are forwarded to attach.ps1, which does the work)"
    exit 1
}
