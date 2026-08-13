@echo off
rem ============================================================
rem  stock-forecast one-click updater (Windows cloud server)
rem
rem  FLOW:
rem   1. Backup current DB (C:\stock-app\data\ -> data\backups\db\)
rem   2. Download latest code ZIP from GitHub
rem   3. Extract to temp dir
rem   4. Replace C:\stock-app\stock-forecast-main (data dir untouched)
rem   5. Done - relaunch start_server.bat
rem
rem  USAGE: stop the old service first, then double-click this file.
rem ============================================================

setlocal
chcp 437 >nul

set APP_DIR=C:\stock-app\stock-forecast-main
set DATA_DIR=C:\stock-app\data
set ZIP_URL=https://codeload.github.com/Hypnotized-zbc/stock-forecast/zip/refs/heads/main
set TMP_DIR=%TEMP%\stock_update
set ZIP_FILE=%TMP_DIR%\stock.zip

echo [1/5] Backing up database ...
if not exist "%DATA_DIR%\backups\db" mkdir "%DATA_DIR%\backups\db"
set BAK_TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set BAK_TS=%BAK_TS: =0%
if exist "%DATA_DIR%\stock_forecast.db" (
  copy /y "%DATA_DIR%\stock_forecast.db" "%DATA_DIR%\backups\db\stock_forecast_%BAK_TS%.db" >nul
  echo    backup done: backups\db\stock_forecast_%BAK_TS%.db
) else (
  echo    no db file found, skip backup
)

echo [2/5] Downloading latest code ...
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
mkdir "%TMP_DIR%"
curl -L -o "%ZIP_FILE%" "%ZIP_URL%"
if errorlevel 1 (
  echo    download failed, check network and retry
  pause
  exit /b 1
)

echo [3/5] Extracting code ...
cd /d "%TMP_DIR%"
tar -xf "%ZIP_FILE%"
rem extracted dir is usually stock-forecast-main
if not exist "%TMP_DIR%\stock-forecast-main" (
  echo    extract failed: stock-forecast-main dir not found
  pause
  exit /b 1
)

echo [4/5] Replacing code dir (data dir %DATA_DIR% untouched)...
if exist "%APP_DIR%" rmdir /s /q "%APP_DIR%"
mkdir "%APP_DIR%"
xcopy /e /y /q "%TMP_DIR%\stock-forecast-main" "%APP_DIR%\"

echo [5/5] Cleaning temp files ...
rmdir /s /q "%TMP_DIR%"

echo.
echo ============================================================
echo  Update finished!
echo  DB is at %DATA_DIR% (not touched by update, auto-backed-up)
echo.
echo  Open a NEW window and run:
echo    %APP_DIR%\deploy\start_server.bat
echo ============================================================
pause
