@echo off
setlocal
cd /d "%~dp0"
set "ATLAS_FIXTURE_PYTHON=%~dp0runtime\python\python.exe"
if exist "%ATLAS_FIXTURE_PYTHON%" goto run
set "ATLAS_FIXTURE_PYTHON=%~dp0..\..\_github-publish\databuddy\dist\r25clean\resources\app\runtime\python\python.exe"
if exist "%ATLAS_FIXTURE_PYTHON%" goto run
echo 未找到随包 Python。请将便携 Python 放入 web\runtime\python，或使用已有 Python 3 运行 web\server.py。
pause
exit /b 1
:run
"%ATLAS_FIXTURE_PYTHON%" -X utf8 "%~dp0server.py"
if errorlevel 1 pause
endlocal
