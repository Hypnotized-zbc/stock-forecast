@echo off
rem ============================================================
rem  stock-forecast 服务器一键更新脚本（Windows 云服务器）
rem
rem  流程：
rem   1. 备份当前数据库（C:\stock-app\data\ -> backups\db\时间戳\）
rem   2. 从 GitHub 下载最新代码 ZIP
rem   3. 解压到临时目录
rem   4. 替换 C:\stock-app\stock-forecast-main 代码（保留 data 目录不动）
rem   5. 重新启动服务
rem
rem  使用：先停止旧服务（关掉运行中的窗口），再双击本脚本。
rem ============================================================

setlocal
chcp 65001 >nul

set APP_DIR=C:\stock-app\stock-forecast-main
set DATA_DIR=C:\stock-app\data
set ZIP_URL=https://codeload.github.com/Hypnotized-zbc/stock-forecast/zip/refs/heads/main
set TMP_DIR=%TEMP%\stock_update
set ZIP_FILE=%TMP_DIR%\stock.zip

echo [1/5] 备份数据库 ...
if not exist "%DATA_DIR%\backups\db" mkdir "%DATA_DIR%\backups\db"
set BAK_TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set BAK_TS=%BAK_TS: =0%
if exist "%DATA_DIR%\stock_forecast.db" (
  copy /y "%DATA_DIR%\stock_forecast.db" "%DATA_DIR%\backups\db\stock_forecast_%BAK_TS%.db" >nul
  echo   备份完成: backups\db\stock_forecast_%BAK_TS%.db
) else (
  echo   未发现数据库文件，跳过备份
)

echo [2/5] 下载最新代码 ...
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
mkdir "%TMP_DIR%"
curl -L -o "%ZIP_FILE%" "%ZIP_URL%"
if errorlevel 1 (
  echo   下载失败，请检查网络后重试
  pause
  exit /b 1
)

echo [3/5] 解压代码 ...
cd /d "%TMP_DIR%"
tar -xf "%ZIP_FILE%"
rem 解压后目录名通常为 stock-forecast-main
if not exist "%TMP_DIR%\stock-forecast-main" (
  echo   解压失败：未找到 stock-forecast-main 目录
  pause
  exit /b 1
)

echo [4/5] 替换代码目录（保留 %DATA_DIR% 数据目录）...
if exist "%APP_DIR%" rmdir /s /q "%APP_DIR%"
mkdir "%APP_DIR%"
xcopy /e /y /q "%TMP_DIR%\stock-forecast-main" "%APP_DIR%\"

echo [5/5] 清理临时文件 ...
rmdir /s /q "%TMP_DIR%"

echo.
echo ============================================================
echo  更新完成！
echo  数据库在 %DATA_DIR%（不受更新影响，已自动备份）
echo.
echo  请打开新窗口运行启动脚本:
echo     %APP_DIR%\deploy\start_server.bat
echo ============================================================
pause
