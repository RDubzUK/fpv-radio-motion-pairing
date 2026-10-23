@echo off
setlocal
cd /d "%~dp0"

where ffmpeg.exe >nul 2>nul
if errorlevel 1 goto :missing_ffmpeg
where ffprobe.exe >nul 2>nul
if errorlevel 1 goto :missing_ffmpeg

start "FPV Matcher" "%~dp0FPV Matcher.exe" --host 127.0.0.1 --port 8767
powershell.exe -NoLogo -NoProfile -Command "$deadline=(Get-Date).AddSeconds(45); while ((Get-Date) -lt $deadline) { try { Invoke-RestMethod -Uri 'http://127.0.0.1:8767/api/system' -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"
if errorlevel 1 goto :server_not_ready
start "" "http://127.0.0.1:8767/"
exit /b 0

:missing_ffmpeg
echo FFmpeg and ffprobe are required and must be available on PATH.
echo Windows download links: https://ffmpeg.org/download.html#build-windows
pause
exit /b 1

:server_not_ready
echo FPV Matcher did not become ready within 45 seconds.
echo Check the FPV Matcher console window for a startup error, then try again.
pause
exit /b 1
