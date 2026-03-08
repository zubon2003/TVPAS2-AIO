@echo off
setlocal
cd /d %~dp0

if not exist "venv" (
    echo [ERROR] venv environment not found. Please run setup.bat first.
    pause
    exit /b 1
)

echo [INFO] Starting TVPAS2-AIO via venv...
".\venv\Scripts\python.exe" main.py

if errorlevel 1 (
    echo [ERROR] Application crashed.
    pause
)
