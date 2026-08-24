@echo off
rem Uniagent command-line interface (Windows launcher).
rem
rem Copied to <repo>\bin\uniagentcli.cmd by install.ps1 / attach.ps1 and put on
rem PATH, so the command is spelled the same here as it is on Linux
rem (scripts\uniagentcli). Usage:
rem
rem   uniagentcli                  -> the full-screen chat interface
rem   uniagentcli "a question"     -> one turn, then exit
rem   echo text | uniagentcli      -> one line in, one turn out
rem
rem PYTHONUTF8 is not optional. Windows decides text encoding from the system
rem codepage - cp1252 on a Western install - and a reply with an em dash in it
rem would otherwise fail on the way to the screen. scripts\_term.py switches the
rem console over as well; this covers the streams that are already open by then.
setlocal
set "PYTHONUTF8=1"
for %%I in ("%~dp0..") do set "ROOT=%%~fI"

rem The venv's interpreter if an installer built one - that is where requests
rem and cryptography live - and whatever python is on PATH otherwise, so a
rem checkout used without an installer still has a working command.
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%ROOT%\scripts\cli.py" %*
exit /b %errorlevel%
