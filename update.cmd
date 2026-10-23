@echo off
setlocal
cd /d "%~dp0"
git pull --ff-only
if errorlevel 1 (
    echo Update failed. Review the Git message above before retrying.
    pause
    exit /b 1
)
call "%~dp0start.cmd" %*
exit /b %errorlevel%
