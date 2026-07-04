@echo off
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%~1' -Verb RunAs"
    exit /b
)
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist "C:\FreeConnect\logs" mkdir "C:\FreeConnect\logs"
set "LOG=C:\FreeConnect\logs\admin_search.log"

echo === FreeConnect search %~1 === > "%LOG%"

where python >nul 2>&1
if %errorlevel%==0 (
    python -m freeconnect.cli search %~1 >> "%LOG%" 2>&1
) else (
    py -3 -m freeconnect.cli search %~1 >> "%LOG%" 2>&1
)
echo. >> "%LOG%"
echo === DONE === >> "%LOG%"

type "%LOG%"
echo.
echo ---------------------------------------------
echo Done. Log saved to: %LOG%
echo ---------------------------------------------
pause
