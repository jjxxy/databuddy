@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-native.ps1" %*
set "atlas_exit=%errorlevel%"
exit /b %atlas_exit%
