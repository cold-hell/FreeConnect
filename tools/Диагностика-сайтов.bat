@echo off
chcp 65001 >nul
set PYTHONUTF8=1
echo FreeConnect - diagnostika saitov. Ostav obhod VKLYUCHENNYM.
echo Proverka zaimet do minuty...
echo.
where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0diag_sites.py" %*
) else (
    py -3 "%~dp0diag_sites.py" %*
)
echo.
echo Gotovo. Otchet lezhit v C:\FreeConnect\logs\  (fail site_diag_...txt)
pause
