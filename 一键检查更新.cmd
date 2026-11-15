@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-facefusion.ps1" >nul 2>&1
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%~dp0runtime\python\python.exe" -B "%~dp0auto_updater.py" --interactive
echo.
echo 按任意键关闭窗口（重新双击“后台启动FaceFusion.cmd”即可运行最新版本）。
pause >nul
