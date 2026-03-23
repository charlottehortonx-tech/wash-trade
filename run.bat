@echo off
cd /d "%~dp0"
echo ============================================================
echo  Starting Bot Dashboard at http://localhost:5000
echo  Press Ctrl+C to stop.
echo ============================================================

if not exist venv (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate
python app.py
pause
