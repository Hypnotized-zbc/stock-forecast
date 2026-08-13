@echo off
rem ============================================================
rem  stock-forecast 服务器启动脚本（Windows 云服务器）
rem  用法：双击运行，或在 cmd 中执行本文件
rem
rem  设计要点：
rem   1. 数据库放在项目目录【外】的 C:\stock-app\data\ 下，
rem      与代码目录完全分离 -> 更新代码时永远不会覆盖数据库
rem   2. 每次启动自动把旧数据库备份到 C:\stock-app\data\backups\db\
rem   3. 公网监听 0.0.0.0:8000，关闭窗口 = 停止服务
rem ============================================================

setlocal

rem ---- 基础配置（按需修改）----
set APP_DIR=C:\stock-app\stock-forecast-main
set DATA_DIR=C:\stock-app\data
set PORT=8000

rem ---- 环境变量（传给 app.py / backend.py）----
set STOCK_HOST=0.0.0.0
set STOCK_PORT=%PORT%
set STOCK_DB=%DATA_DIR%\stock_forecast.db

rem ---- 确保数据目录存在 ----
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%DATA_DIR%\backups\db" mkdir "%DATA_DIR%\backups\db"

echo ============================================================
echo  股票历史数据查询 - 服务器模式
echo  数据库: %STOCK_DB%
echo  更新代码时请勿覆盖 C:\stock-app\data\ 目录！
echo ============================================================
echo.

cd /d "%APP_DIR%"
python app.py

pause
