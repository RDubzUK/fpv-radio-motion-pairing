@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "FPV_START_EXIT=%errorlevel%"
if not "%FPV_START_EXIT%"=="0" pause
exit /b %FPV_START_EXIT%
