@echo off
REM One-time local setup. Run from the project root: setup.bat
setlocal

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher 'py' not found. Install Python 3.11 from python.org and retry.
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment with Python 3.11...
    py -3.11 -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install --upgrade pip
pip install -r requirements.txt

if not exist .env (
    echo .env not found - copying from .env.example. Edit it with real values before running.
    copy .env.example .env
)

echo Initializing data files (users.csv, chat_logs/ weekly + monthly logs, logs/, chroma_store/, cache/)...
python scripts\init_data_files.py

echo.
echo Setup complete. Next steps:
echo   1. Choose your data source in .env: DATA_SOURCE=sqlserver or DATA_SOURCE=local
echo   2. Ensure Ollama is running and models are pulled (see README.md)
echo   3. python scripts\test_db_connection.py       (prints which source is active)
echo   4. python scripts\build_column_aliases.py     (column shortcut dictionary)
echo   5. python scripts\setup_chromadb.py           (semantic schema index)
echo   6. run_backend.bat   (in one terminal)
echo   7. run_frontend.bat  (in another terminal)

endlocal
