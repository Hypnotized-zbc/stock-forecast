#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库模块（SQLite，标准库，零依赖）
=====================================
三张表：
- watchlist : 自选股列表（user_id, secid, name, added_at）
- ai_cache  : AI 解读缓存（user_id, secid, period, text, ts）
- history   : 查询历史（user_id, secid, name, ts）

现阶段单用户（user_id 默认 "default"），字段预留多用户扩展。
数据库文件默认项目目录 stock_forecast.db，可用环境变量 STOCK_DB 覆盖。
线程安全：每次操作独立连接（SQLite 连接不能跨线程共享）。
"""
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = os.environ.get("STOCK_DB") or str(Path(__file__).resolve().parent / "stock_forecast.db")

DEFAULT_USER = "default"


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """建表（幂等）。启动时调用一次。"""
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id  TEXT NOT NULL DEFAULT 'default',
                secid    TEXT NOT NULL,
                name     TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (user_id, secid)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                user_id TEXT NOT NULL DEFAULT 'default',
                secid   TEXT NOT NULL,
                period  TEXT NOT NULL,
                text    TEXT NOT NULL,
                ts      TEXT NOT NULL,
                PRIMARY KEY (user_id, secid, period)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                secid   TEXT NOT NULL,
                name    TEXT NOT NULL,
                ts      TEXT NOT NULL
            )""")
        conn.commit()
    finally:
        conn.close()


# ---------------- watchlist ----------------

def watchlist_get(user_id=DEFAULT_USER):
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT secid, name, added_at FROM watchlist WHERE user_id=? ORDER BY added_at",
            (user_id,)).fetchall()
        return [{"secid": r[0], "name": r[1], "added_at": r[2]} for r in rows]
    finally:
        conn.close()


def watchlist_add(user_id, secid, name):
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (user_id, secid, name, added_at) VALUES (?,?,?,?)",
            (user_id, secid, name, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def watchlist_remove(user_id, secid):
    conn = _conn()
    try:
        conn.execute("DELETE FROM watchlist WHERE user_id=? AND secid=?", (user_id, secid))
        conn.commit()
    finally:
        conn.close()


# ---------------- ai_cache ----------------

def ai_cache_get(user_id, secid, period):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT text, ts FROM ai_cache WHERE user_id=? AND secid=? AND period=?",
            (user_id, secid, period)).fetchone()
        if not row:
            return None
        return {"text": row[0], "ts": row[1]}
    finally:
        conn.close()


def ai_cache_set(user_id, secid, period, text):
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_cache (user_id, secid, period, text, ts) VALUES (?,?,?,?,?)",
            (user_id, secid, period, text, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def ai_cache_delete(user_id, secid, period):
    conn = _conn()
    try:
        conn.execute("DELETE FROM ai_cache WHERE user_id=? AND secid=? AND period=?",
                     (user_id, secid, period))
        conn.commit()
    finally:
        conn.close()


# ---------------- history ----------------

def history_add(user_id, secid, name):
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO history (user_id, secid, name, ts) VALUES (?,?,?,?)",
            (user_id, secid, name, time.strftime("%Y-%m-%d %H:%M:%S")))
        # 只保留最近 200 条，防止无限增长
        conn.execute(
            "DELETE FROM history WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 200)",
            (user_id, user_id))
        conn.commit()
    finally:
        conn.close()


def history_get(user_id=DEFAULT_USER, limit=50):
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT secid, name, ts FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)).fetchall()
        return [{"secid": r[0], "name": r[1], "ts": r[2]} for r in rows]
    finally:
        conn.close()


# 便于调试：打印数据库路径
def db_info():
    return {"db_path": DB_PATH, "tables": ["watchlist", "ai_cache", "history"]}


if __name__ == "__main__":
    init_db()
    print(json.dumps(db_info(), ensure_ascii=False))
    print("watchlist:", watchlist_get())
    print("history:", history_get())
