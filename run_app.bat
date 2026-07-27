@echo off
setlocal

rem Always run from this script's own folder, regardless of where it was launched from.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Creating it now...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Failed to create the virtual environment. Is Python installed and on PATH?
        goto :end
    )

    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Failed to install dependencies. See the error above.
        goto :end
    )
)

if not exist ".env" (
    echo.
    echo WARNING: .env not found. Copy .env.example to .env and fill it in first.
    goto :end
)

echo.
echo Starting Weekly Report Importer... (close this window to stop it)
echo.
".venv\Scripts\python.exe" app.py

:end
echo.
pause
