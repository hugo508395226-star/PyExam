@echo off
cd /d "%~dp0"
echo 正在关闭服务器...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
    echo 已终止进程 PID %%a
)
echo 端口 5000 已释放
pause
