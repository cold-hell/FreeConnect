@echo off
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist "C:\FreeConnect\logs" mkdir "C:\FreeConnect\logs"
set "LOG=C:\FreeConnect\logs\app.log"

echo === FreeConnect GUI start (SAFE / no GPU) === > "%LOG%"
where python >nul 2>&1
if %errorlevel%==0 (
    python -m freeconnect.app --safe >> "%LOG%" 2>&1
) else (
    py -3 -m freeconnect.app --safe >> "%LOG%" 2>&1
)
echo === app closed === >> "%LOG%"
