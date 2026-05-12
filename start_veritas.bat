@echo off
REM Veritas IDV – zagonska skripta za Windows
SETLOCAL

SET "PROJECT_DIR=%~dp0"
SET "API_KEY=vrt_dacaccedd3067322ebd824eb76433bfe3ead5be5d574f65720c2ac98d6bc4c14"
SET "NGROK_EXE=echo"

cd /d "%PROJECT_DIR%"

echo.
echo  ==========================================
echo   Veritas IDV – zaganjam storitve...
echo  ==========================================
echo.

REM --- Zapisi API kljuc v config datoteko ---
echo  [CONFIG] Nastavljam API kljuc...
echo {"api_key": "%API_KEY%"} > "%PROJECT_DIR%.veritas_config.json"
echo  [CONFIG] OK
echo.

REM --- Docker / Redis ---
echo  [REDIS] Zaganjam Redis kontejner...
docker compose up -d >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  [NAPAKA] Docker Compose ni uspel. Preveri Docker Desktop.
    pause
    exit /b 1
)
echo  [REDIS] OK
echo.

REM --- FastAPI ---
echo  [API] Zaganjam FastAPI streznik na portu 8000...
start "Veritas API" /min cmd /k "cd /d "%PROJECT_DIR%" && python -m uvicorn main:app --host 0.0.0.0 --port 8000"
echo  [API] Cakam da se zazene...
timeout /t 5 /nobreak >nul
echo  [API] OK
echo.

REM --- Celery ---
echo  [CELERY] Zaganjam AI Engine...
start "Veritas AI Engine" /min cmd /k "cd /d "%PROJECT_DIR%" && python -m celery -A worker worker --pool=solo --loglevel=info"
timeout /t 3 /nobreak >nul
echo  [CELERY] OK
echo.

REM --- ngrok (direktno z exe, ne prek pyngrok) ---
echo  [NGROK] Zaganjam ngrok tunel na portu 8000...
start "Veritas ngrok" /min "%NGROK_EXE%" http 8000
echo  [NGROK] Cakam da se tunel vzpostavi...
timeout /t 6 /nobreak >nul
echo  [NGROK] OK
echo.

REM --- Odpri dashboard ---
echo  [BROWSER] Odpiranje dashboarda...
start "" "http://localhost:8000/"

echo.
echo  ==========================================
echo   Vsi sistemi so zagnani!
echo  ==========================================
echo.
echo  Dashboard:  http://localhost:8000/
echo  API docs:   http://localhost:8000/docs
echo  ngrok UI:   http://localhost:4040
echo.
echo  Lahko zapres to okno.
exit
