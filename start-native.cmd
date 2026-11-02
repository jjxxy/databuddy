@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-native.ps1" %*
set "nexus_exit=%errorlevel%"
exit /b %nexus_exit%
