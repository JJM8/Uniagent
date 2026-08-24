@echo off
rem Uniagent: make THIS folder a service on THIS machine. (Windows)
rem
rem Double-click this file, or run it from a Command Prompt. It is a wrapper
rem around attach.ps1 and does nothing else - all it adds is -ExecutionPolicy
rem Bypass, which is what stops Windows refusing to run a .ps1 that arrived
rem from somewhere other than this machine, and a pause at the end so a
rem double-clicked window does not vanish before you have read the password.
rem
rem Anything you type after it is passed straight through:
rem
rem   attach.cmd -Port 8790     register on that port without being asked
rem   attach.cmd -Remove        unhook it again
rem
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0attach.ps1" %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
