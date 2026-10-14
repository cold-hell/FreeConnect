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

:: Чистим зомби от прошлых зависших запусков (иначе новый старт может подвиснуть)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe' -or $_.Name -eq 'msedgewebview2.exe') -and $_.CommandLine -like '*freeconnect*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
taskkill /F /IM winws.exe >nul 2>&1
if exist "C:\FreeConnect\webview2" rmdir /s /q "C:\FreeConnect\webview2" >nul 2>&1

echo === FreeConnect GUI start === > "%LOG%"
where python >nul 2>&1
if %errorlevel%==0 (
    python -m freeconnect.app >> "%LOG%" 2>&1
) else (
    py -3 -m freeconnect.app >> "%LOG%" 2>&1
)
echo === app closed === >> "%LOG%"
