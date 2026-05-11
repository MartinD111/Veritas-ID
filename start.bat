@echo off
REM Veritas IDV – zagonska skripta za Windows razvoj
REM Odpri dva ločena terminala in zaženi vsak blok posebej.

echo =========================================
echo  TERMINAL 1 – FastAPI strežnik
echo =========================================
echo uvicorn main:app --host 0.0.0.0 --port 8000 --reload
echo.
echo =========================================
echo  TERMINAL 2 – Celery delavec (Windows)
echo =========================================
echo celery -A worker worker --pool=solo --loglevel=info
echo.
pause
