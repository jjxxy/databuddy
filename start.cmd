@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "nexus_exit=%ERRORLEVEL%"
if not "%nexus_exit%"=="0" echo Start did not finish. Follow the message above, then run start.cmd again.
rem Keep the local address and generated access token visible after double-click.
if "%~1"=="" pause
exit /b %nexus_exit%
