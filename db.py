# -*- coding: utf-8 -*-
"""
db.py
SQLite 存储层（标准库 sqlite3）。

项目目前无数据库，此前用文件存储（users/community/weekly_reports.json）。
本模块新增 SQLite（data/app.db）承载：
- 系统公告（announcement_table）
- 用户公告已读（user_announcement_read）
- AI 周报（report_table，并把旧 data/weekly_reports.json 迁移进来）

并发：开启 WAL；模块级 threading.Lock 保护写操作；SQLite 文件锁保证跨进程写序列化。
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime

logger = logging.getLogger("db")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_FILE = os.path.join(DATA_DIR, "app.db")
LEGACY_WEEKLY_REPORT_FILE = os.path.join(DATA_DIR, "weekly_reports.json")

_lock = threading.Lock()


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建表（幂等），并把旧周报 JSON 迁移进 report_table。"""
    _ensure_data_dir()
    with _lock:
        conn = _get_conn()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS announcement_table (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    publish_time TEXT NOT NULL,
                    expire_time TEXT NOT NULL,
                    create_admin_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_announcement_read (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    announcement_id INTEGER NOT NULL,
                    read_time TEXT NOT NULL,
                    UNIQUE(user_id, announcement_id)
                );
                CREATE TABLE IF NOT EXISTS report_table (
                    week_key TEXT PRIMARY KEY,
                    week_label TEXT DEFAULT '',
                    start TEXT DEFAULT '',
                    end TEXT DEFAULT '',
                    generated_at TEXT DEFAULT '',
                    dept TEXT DEFAULT '',
                    data TEXT,
                    created_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ann_read_user ON user_announcement_read(user_id);
                """
            )
            _migrate_weekly_reports(conn)
            conn.commit()
        finally:
            conn.close()


def _migrate_weekly_reports(conn: sqlite3.Connection) -> None:
    """把旧 data/weekly_reports.json 中尚未导入 report_table 的记录迁入（仅当表为空）。"""
    if not os.path.exists(LEGACY_WEEKLY_REPORT_FILE):
        return
    try:
        with open(LEGACY_WEEKLY_REPORT_FILE, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    except Exception:
        return
    if not isinstance(legacy, dict):
        return
    cur = conn.execute("SELECT COUNT(*) AS c FROM report_table")
    if cur.fetchone()["c"] > 0:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for week_key, rep in legacy.items():
        if not isinstance(rep, dict):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO report_table (week_key, week_label, start, end, generated_at, dept, data, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                week_key,
                rep.get("week_label", ""),
                rep.get("start", week_key),
                rep.get("end", ""),
                rep.get("generated_at", ""),
                rep.get("dept", ""),
                json.dumps(rep, ensure_ascii=False),
                now,
            ),
        )
    logger.info("周报已从 JSON 迁移到 SQLite（%d 条）", len(legacy))


# ------------------------------------------------------------------ #
# 公告
# ------------------------------------------------------------------ #
def create_announcement(title: str, content: str, publish_time: str, expire_time: str, admin_id: str) -> dict:
    """新增公告，返回公告 dict。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO announcement_table (title, content, publish_time, expire_time, create_admin_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (title, content, publish_time, expire_time, admin_id, now),
            )
            conn.commit()
            return _row_to_announcement(conn.execute("SELECT * FROM announcement_table WHERE id=?", (cur.lastrowid,)).fetchone())
        finally:
            conn.close()


def update_announcement(announcement_id: int, title: str, content: str, publish_time: str, expire_time: str) -> dict | None:
    """更新公告；不存在返回 None。"""
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "UPDATE announcement_table SET title=?, content=?, publish_time=?, expire_time=? WHERE id=?",
                (title, content, publish_time, expire_time, announcement_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            return _row_to_announcement(conn.execute("SELECT * FROM announcement_table WHERE id=?", (announcement_id,)).fetchone())
        finally:
            conn.close()


def delete_announcement(announcement_id: int) -> bool:
    """删除公告；返回是否删除。已读记录保留。"""
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute("DELETE FROM announcement_table WHERE id=?", (announcement_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def list_announcements() -> list[dict]:
    """返回全部公告（按发布时间倒序）。"""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM announcement_table ORDER BY publish_time DESC, id DESC").fetchall()
        return [_row_to_announcement(r) for r in rows]
    finally:
        conn.close()


def _row_to_announcement(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "publish_time": row["publish_time"],
        "expire_time": row["expire_time"],
        "create_admin_id": row["create_admin_id"],
        "created_at": row["created_at"],
    }


def list_unread_announcements(user_id: str) -> list[dict]:
    """返回该用户的「已生效、未过期、未读」公告，按 publish_time 升序。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT a.* FROM announcement_table a "
            "WHERE a.publish_time <= ? AND a.expire_time > ? "
            "AND NOT EXISTS (SELECT 1 FROM user_announcement_read r WHERE r.user_id=? AND r.announcement_id=a.id) "
            "ORDER BY a.publish_time ASC, a.id ASC",
            (now, now, user_id),
        ).fetchall()
        return [_row_to_announcement(r) for r in rows]
    finally:
        conn.close()


def mark_announcement_read(user_id: str, announcement_id: int) -> bool:
    """标记某用户已读某公告（幂等）。返回是否首次写入。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO user_announcement_read (user_id, announcement_id, read_time) VALUES (?,?,?)",
                (user_id, announcement_id, now),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ------------------------------------------------------------------ #
# AI 周报
# ------------------------------------------------------------------ #
def save_report(week_key: str, report: dict) -> None:
    """保存/覆盖某周周报。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO report_table (week_key, week_label, start, end, generated_at, dept, data, created_at) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(week_key) DO UPDATE SET week_label=excluded.week_label, start=excluded.start, "
                "end=excluded.end, generated_at=excluded.generated_at, dept=excluded.dept, data=excluded.data",
                (
                    week_key,
                    report.get("week_label", week_key),
                    report.get("start", week_key),
                    report.get("end", ""),
                    report.get("generated_at", now),
                    report.get("dept", ""),
                    json.dumps(report, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def load_report(week_key: str) -> dict | None:
    """读取某周周报；不存在返回 None。"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT data FROM report_table WHERE week_key=?", (week_key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["data"])
        except Exception:
            return None
    finally:
        conn.close()


def load_report_weeks() -> list[dict]:
    """返回已存档周列表（按 week_key 倒序）。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT week_key, week_label, generated_at FROM report_table ORDER BY week_key DESC"
        ).fetchall()
        return [
            {"week_key": r["week_key"], "week_label": r["week_label"] or r["week_key"], "generated_at": r["generated_at"] or ""}
            for r in rows
        ]
    finally:
        conn.close()
