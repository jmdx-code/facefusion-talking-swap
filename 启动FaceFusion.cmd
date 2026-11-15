@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\scripts\start-facefusion-background.ps1"
if errorlevel 1 (
    echo.
    echo 启动未完成，请根据上方提示检查网络或显卡驱动后重试。
    pause
)
