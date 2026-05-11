@echo off
REM Veritas IDV – zagonska skripta za Windows

SET "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

echo.
echo  ==========================================
echo   Veritas IDV – zaganjam storitve...
echo  ==========================================
echo   Delovni imenik: %PROJECT_DIR%
echo.

REM --- Docker / Redis ---
echo  [REDIS] Zaganjam Redis kontejner (docker compose up -d)...
docker compose up -d >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo  [NAPAKA] Docker Compose ni uspel.
    echo  Preveri, ali je Docker Desktop zagnan, in poskusi znova.
    echo.
    pause
    exit /b 1
)
echo  [REDIS] Kontejner zagnan.
echo.

REM --- Terminal 1: FastAPI ---
echo  [API] Zaganjam FastAPI streznik...
start "Veritas API" /min cmd /k "cd /d "%PROJECT_DIR%" && python -m uvicorn main:app --host 0.0.0.0 --port 8000"

echo  [API] Cakam 3 sekunde, da se API zazene...
timeout /t 3 /nobreak >nul

REM --- Terminal 2: Celery ---
echo  [CELERY] Zaganjam Celery delavca...
start "Veritas AI Engine" /min cmd /k "cd /d "%PROJECT_DIR%" && python -m celery -A worker worker --pool=solo --loglevel=info"

echo  [CELERY] Cakam 3 sekunde...
timeout /t 3 /nobreak >nul

REM --- Terminal 3: Streamlit ---
echo  [UI] Zaganjam Streamlit vmesnik...
start "Veritas Dashboard" /min cmd /k "cd /d "%PROJECT_DIR%" && python -m streamlit run frontend.py"

echo.
echo  ==========================================
echo   Vsi sistemi so zagnani!
echo   Lahko zapres to okno.
echo  ==========================================
echo.
echo  API docs:   http://localhost:8000/docs
echo  Dashboard:  http://localhost:8501
echo.
exit
