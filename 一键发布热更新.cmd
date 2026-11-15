@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ====================================================================
echo   FaceFusion 一键打包并发布热更新到 GitHub Releases
echo   （自动递增版本号 + 生成 0.4MB 热更新包与 14MB 初始包 + 上传发布）
echo ====================================================================
echo.
set /p REL_MSG=请输入本次更新简短说明（直接回车则使用默认说明）：
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish-update.ps1" -Message "%REL_MSG%"
echo.
echo 按任意键关闭窗口。
pause >nul
