@echo off
cd /d "%~dp0"
echo ============================================================
echo  Bot Setup — Installing dependencies
echo ============================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org and check "Add to PATH".
    pause
    exit /b 1
)

python -m venv venv
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

call venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt

echo.
echo ============================================================
echo  Setup complete! Run run.bat to start the dashboard.
echo ============================================================
pause
