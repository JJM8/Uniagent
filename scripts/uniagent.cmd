# Uniagent command-line interface (Windows launcher).
# Installed to <repo>\bin\uniagent.cmd by install.ps1 and put on PATH.
# Usage:
#   uniagentcli                  -> the CLI (live keyboard mode is Unix-only for
#                                   now; Windows prints how to use it)
#   uniagentcli "a question"     -> one turn, then exit
#   echo text | uniagentcli      -> one line in, one turn out
@echo off
setlocal
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\scripts\cli.py" %*
exit /b %errorlevel%
