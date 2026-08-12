@echo off
setlocal
title Lucky Bingo Launcher

cd /d "%~dp0"

echo.
echo ========================================
echo             Lucky Bingo
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python virtual environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Python 3.12 or newer is not installed.
            echo Download Python from https://www.python.org/downloads/
            pause
            exit /b 1
        )
        python -m venv .venv
    )

    if errorlevel 1 (
        echo ERROR: Could not create the virtual environment.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo Creating .env from .env.example...
    copy /Y ".env.example" ".env" >nul
)

echo Checking dependencies...
".venv\Scripts\python.exe" -c "import fastapi, httpx, psycopg, uvicorn, dotenv" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies. This may take a few minutes...
    ".venv\Scripts\python.exe" -m pip install -e "."
    if errorlevel 1 (
        echo ERROR: Dependency installation failed.
        echo Check your internet connection and try again.
        pause
        exit /b 1
    )
)

echo.
echo Starting Lucky at http://127.0.0.1:8000
echo Press Ctrl+C in this window to stop the server.
echo.

rem The Telegram bot runs inside this same process now (webhook mode) and
rem registers itself automatically on startup when BOT_TOKEN is set in .env.

start "" "http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo Lucky has stopped.
pause
