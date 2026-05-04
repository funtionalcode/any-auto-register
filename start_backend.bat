@echo off
setlocal

set "HOST=%HOST%"
if "%HOST%"=="" set "HOST=0.0.0.0"
set "PORT=%PORT%"
if "%PORT%"=="" set "PORT=8000"
set "RESTART_EXISTING=%RESTART_EXISTING%"
if "%RESTART_EXISTING%"=="" set "RESTART_EXISTING=1"

where uv >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 uv 命令。请先安装 uv: https://docs.astral.sh/uv/getting-started/installation/
  exit /b 1
)

cd /d "%~dp0"
echo [INFO] 项目目录: %CD%
echo [INFO] 启动后端: http://localhost:%PORT%
echo [INFO] 按 Ctrl+C 可停止服务

if "%RESTART_EXISTING%"=="1" (
  echo [INFO] 启动前先清理旧的后端 / Solver 进程
  powershell -ExecutionPolicy Bypass -File "%~dp0stop_backend.ps1" -BackendPort %PORT% -SolverPort 8889 -FullStop 0
)

set "HOST=%HOST%"
set "PORT=%PORT%"
uv run main.py
