@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
set "atlas_exit=%ERRORLEVEL%"
if not "%atlas_exit%"=="0" echo Start did not finish. Follow the message above, then run start.cmd again.
rem Keep the local address and generated access token visible after double-click.
if "%~1"=="" pause
exit /b %atlas_exit%
