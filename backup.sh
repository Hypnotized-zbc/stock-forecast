#!/bin/bash
# 备份脚本：把关键源码备份到 backups/<时间戳>/，并写一份 NOTE.txt 说明
# 用法：./backup.sh "备份原因说明"
set -e

TS=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="backups/$TS"
mkdir -p "$BACKUP_DIR"

# 要备份的文件（新增源码文件记得加到这里）
FILES=(
  app.py
  backend.py
  db.py
  static/index.html
  static/login.html
  crawler/core.py
  crawler/parsers.py
  crawler/__init__.py
  examples/demo.py
  tools/upload_github.py
  tests/test_backend.py
  requirements.txt
  README.md
  UPDATES.md
)

for f in "${FILES[@]}"; do
  if [ -f "$f" ]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$f")"
    cp "$f" "$BACKUP_DIR/$f"
  fi
done

NOTE="${1:-无说明}"
echo "$(date '+%Y-%m-%d %H:%M:%S') | $NOTE" > "$BACKUP_DIR/NOTE.txt"

echo "备份完成: $BACKUP_DIR"
