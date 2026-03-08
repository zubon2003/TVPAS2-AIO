@echo off
setlocal enabledelayedexpansion
cd /d %~dp0

echo [INFO] TVPAS2-AIO Setup started...

REM Python Check
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10 or later and try again.
    pause
    exit /b 1
)

REM Create venv if not exists
if not exist "venv" (
    echo [INFO] Creating virtual environment: venv
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
) else (
    echo [INFO] venv already exists. Skipping creation.
)

REM Upgrade pip
echo [INFO] Upgrading pip...
".\venv\Scripts\python.exe" -m pip install --upgrade pip

REM Install requirements
if exist "requirements.txt" (
    echo [INFO] Installing requirements from requirements.txt...
    ".\venv\Scripts\pip" install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Installation failed.
        pause
        exit /b 1
    )
) else (
    echo [ERROR] requirements.txt not found.
    pause
    exit /b 1
)

echo.
echo [SUCCESS] Setup completed successfully!
echo You can now start the application by running start.bat.
echo.
pause
