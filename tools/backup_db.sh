#!/bin/bash
# 数据库备份脚本：把 stock_forecast.db 备份到 backups/db/<时间戳>/
# 用法：./tools/backup_db.sh [备份说明]
# 建议配合 crontab：0 3 * * * /path/to/stock-forecast/tools/backup_db.sh nightly
set -e
cd "$(dirname "$0")/.."

DB="stock_forecast.db"
if [ ! -f "$DB" ]; then
  echo "数据库不存在: $DB"
  exit 0
fi

TS=$(date +%Y%m%d_%H%M%S)
DIR="backups/db/$TS"
mkdir -p "$DIR"

# 用 sqlite3 在线备份（若无 sqlite3 命令则直接 cp；WAL 模式下 cp 可能不含未 checkpoint 数据）
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$DIR/stock_forecast.db'"
  echo "sqlite3 在线备份完成: $DIR/stock_forecast.db"
else
  cp "$DB" "$DIR/stock_forecast.db"
  [ -f "$DB-wal" ] && cp "$DB-wal" "$DIR/stock_forecast.db-wal" 2>/dev/null || true
  echo "cp 备份完成: $DIR/stock_forecast.db"
fi

NOTE="${1:-无说明}"
echo "$(date '+%Y-%m-%d %H:%M:%S') | $NOTE" > "$DIR/NOTE.txt"

# 保留最近 30 份，删除更早的
ls -1d backups/db/*/ 2>/dev/null | head -n -30 | xargs -r rm -rf
echo "完成（保留最近 30 份）"
