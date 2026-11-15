@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\scripts\start-facefusion-background.ps1"
if errorlevel 1 pause
