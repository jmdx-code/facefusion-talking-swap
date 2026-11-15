@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%~dp0runtime\python\python.exe" "%~dp0create-portable-zip.py" --update-only
echo.
echo 按任意键关闭窗口。
pause >nul
