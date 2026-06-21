@echo off
cd /d "%~dp0"
echo 清理端口占用...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
    echo 已终止旧进程 PID %%a
)
echo 启动服务器...
start "" python main.py
timeout /t 5 /nobreak >nul
start http://127.0.0.1:5000
