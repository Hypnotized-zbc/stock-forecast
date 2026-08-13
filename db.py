#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库模块（SQLite，标准库，零依赖）
=====================================
四张表：
- users     : 用户账号（user_id, username, password_hash, salt, created_at）
- watchlist : 自选股列表（user_id, secid, name, added_at）
- ai_cache  : AI 解读缓存（user_id, secid, period, text, ts）
- history   : 查询历史（user_id, secid, name, ts）

用户数据按 user_id 隔离（多用户）。密码存储为加盐哈希，不存明文。
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
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                email         TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            )""")
        # 老库迁移：users 表可能缺 email 列（v0.20.2 新增密保邮箱）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "email" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            print("[db] users 表已迁移：新增 email 列")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email <> ''")
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
        # 会话持久化（v0.21.0）：token 落盘，服务重启后仍保持登录
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token     TEXT PRIMARY KEY,
                user_id   INTEGER NOT NULL,
                expire_ts REAL NOT NULL,
                created_at TEXT NOT NULL
            )""")
        conn.commit()
    finally:
        conn.close()


# ---------------- users ----------------

def user_create(username, password_hash, salt, email=""):
    """创建用户。username 或 email 冲突时抛 sqlite3.IntegrityError。"""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, email, created_at) VALUES (?,?,?,?,?)",
            (username, password_hash, salt, email, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return conn.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()[0]
    finally:
        conn.close()


def user_get_by_email(email):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id, username, password_hash, salt, email FROM users WHERE email=?",
            (email,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "username": row[1], "password_hash": row[2], "salt": row[3], "email": row[4]}
    finally:
        conn.close()


def user_get_by_username(username):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id, username, password_hash, salt, email FROM users WHERE username=?",
            (username,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "username": row[1], "password_hash": row[2], "salt": row[3], "email": row[4]}
    finally:
        conn.close()


def user_get(user_id):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id, username, created_at FROM users WHERE user_id=?",
            (user_id,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "username": row[1], "created_at": row[2]}
    finally:
        conn.close()


def user_update_password(user_id, password_hash, salt):
    """更新用户密码哈希与盐。"""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE user_id=?",
            (password_hash, salt, user_id))
        conn.commit()
    finally:
        conn.close()


def user_delete(user_id):
    """注销账号：删除用户及其所有数据（自选股/AI缓存/历史/会话）。"""
    conn = _conn()
    try:
        for table in ("sessions", "watchlist", "ai_cache", "history"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------- sessions（会话持久化） ----------------

def session_set(token, user_id, expire_ts):
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (token, user_id, expire_ts, created_at) VALUES (?,?,?,?)",
            (token, user_id, expire_ts, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def session_get(token):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id, expire_ts FROM sessions WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "expire_ts": row[1]}
    finally:
        conn.close()


def session_delete(token):
    conn = _conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


def session_delete_user(user_id, keep_token=None):
    """删除某用户全部会话（改密/注销用）；keep_token 保留的除外。"""
    conn = _conn()
    try:
        if keep_token:
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user_id, keep_token))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def session_cleanup():
    """删除全部过期会话，防 sessions 表无限增长。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM sessions WHERE expire_ts < ?", (time.time(),))
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
    return {"db_path": DB_PATH, "tables": ["users", "watchlist", "ai_cache", "history"]}


if __name__ == "__main__":
    init_db()
    print(json.dumps(db_info(), ensure_ascii=False))
    print("watchlist:", watchlist_get())
    print("history:", history_get())
