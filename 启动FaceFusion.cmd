@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-facefusion-foreground.ps1"
set "FACEFUSION_EXIT=%ERRORLEVEL%"
echo.
if not "%FACEFUSION_EXIT%"=="0" echo FaceFusion 启动失败，错误代码：%FACEFUSION_EXIT%
echo 按任意键关闭窗口。
pause >nul
