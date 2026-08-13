@echo off
rem ============================================================
rem  stock-forecast server launcher (Windows cloud server)
rem  USAGE: double-click or run in cmd
rem
rem  KEY POINTS:
rem   1. Database lives OUTSIDE the code dir:
rem      C:\stock-app\data\stock_forecast.db (STOCK_DB env var)
rem      -> code updates can never overwrite user data
rem   2. Every start auto-backs-up the db to C:\stock-app\data\backups\db\
rem   3. Listens on 0.0.0.0:8000. Closing this window stops the service.
rem ============================================================

setlocal

rem ---- config (edit if needed) ----
set APP_DIR=C:\stock-app\stock-forecast-main
set DATA_DIR=C:\stock-app\data
set PORT=8000

rem ---- env vars (read by app.py / backend.py) ----
set STOCK_HOST=0.0.0.0
set STOCK_PORT=%PORT%
set STOCK_DB=%DATA_DIR%\stock_forecast.db

rem ---- ensure data dir exists ----
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%DATA_DIR%\backups\db" mkdir "%DATA_DIR%\backups\db"

echo ============================================================
echo  stock-forecast - server mode
echo  DB: %STOCK_DB%
echo  Do NOT overwrite C:\stock-app\data\ when updating code!
echo ============================================================
echo.

cd /d "%APP_DIR%"
python app.py

pause
