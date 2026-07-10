"""SQLite 数据库模块。

职责：
1. 初始化 4 张表：received_messages / received_files / sent_files / app_events。
2. 提供线程安全的连接管理（每线程一个连接）。
3. 提供增 / 改 / 查函数，供收件 / 发件 / 文件扫描 / GUI 调用。

所有时间字段统一使用 ISO-like 字符串：YYYY-MM-DD HH:MM:SS。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from agent_mail_bridge.utils import fmt_datetime, now_local

# 每线程连接缓存
_local = threading.local()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS received_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    gmail_uid TEXT,
    subject TEXT,
    from_email TEXT,
    to_email TEXT,
    received_at TEXT,
    saved_date TEXT,
    body_file_path TEXT,
    body_sha256 TEXT,
    has_attachments INTEGER,
    status TEXT,
    created_at TEXT,
    updated_at TEXT,
    source TEXT,
    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    backend TEXT
);

CREATE TABLE IF NOT EXISTS received_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    file_type TEXT,
    original_filename TEXT,
    saved_filename TEXT,
    saved_path TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    mime_type TEXT,
    saved_date TEXT,
    status TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sent_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT,
    send_copy_path TEXT,
    sent_copy_path TEXT,
    sha256 TEXT,
    subject TEXT,
    from_email TEXT,
    to_email TEXT,
    sent_at TEXT,
    status TEXT,
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS app_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT,
    event_type TEXT,
    message TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_received_messages_saved_date
    ON received_messages(saved_date);
CREATE INDEX IF NOT EXISTS idx_received_files_saved_date
    ON received_files(saved_date);
CREATE INDEX IF NOT EXISTS idx_received_files_message_id
    ON received_files(message_id);
CREATE INDEX IF NOT EXISTS idx_sent_files_sent_date
    ON sent_files(sent_at);
CREATE INDEX IF NOT EXISTS idx_app_events_created
    ON app_events(created_at);
"""


# received_messages 表迁移所需的新增列。
# 旧数据库可能缺少这些列，init_db 会检测并安全补列（不删旧数据）。
_RECEIVED_MESSAGES_NEW_COLUMNS = {
    "source": "TEXT",
    "gmail_message_id": "TEXT",
    "gmail_thread_id": "TEXT",
    "backend": "TEXT",
}


def init_db(db_path: Path | str) -> None:
    """初始化数据库文件及所有表。目录不存在会自动创建。

    向后兼容：若 received_messages 表已存在但缺少 Gmail API 新增列，
    会自动 ALTER TABLE 补列，不删除旧数据。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_received_messages(conn)
        conn.commit()


def _migrate_received_messages(conn: sqlite3.Connection) -> None:
    """检测 received_messages 表缺失列并安全补列。

    SQLite 不支持 IF NOT EXISTS 加列，故先查 PRAGMA table_info。
    """
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(received_messages)").fetchall()
    }
    for col, col_type in _RECEIVED_MESSAGES_NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(
                f"ALTER TABLE received_messages ADD COLUMN {col} {col_type}"
            )


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """获取当前线程的数据库连接（缓存复用）。

    sqlite3 默认 check_same_thread=True，这里为每线程单独建连接。
    """
    key = str(Path(db_path).resolve())
    conn = getattr(_local, "conn", None)
    cached_key = getattr(_local, "key", None)
    if conn is not None and cached_key == key:
        return conn  # type: ignore[return-value]

    # 关闭旧连接（路径变化时）
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    new_conn = sqlite3.connect(str(db_path))
    new_conn.row_factory = sqlite3.Row
    # 开启外键约束
    new_conn.execute("PRAGMA foreign_keys = ON;")
    _local.conn = new_conn
    _local.key = key
    return new_conn


def close_connection() -> None:
    """关闭当前线程的连接（主要用于测试 / 进程退出）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None
            _local.key = None


@contextmanager
def _get_conn(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """上下文管理器，复用线程连接。"""
    conn = get_connection(db_path)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


def _now() -> str:
    return fmt_datetime(now_local())


# ============================================================
# received_messages
# ============================================================

def insert_received_message(
    db_path: Path | str,
    *,
    message_id: str,
    gmail_uid: str | None,
    subject: str,
    from_email: str,
    to_email: str,
    received_at: str | None,
    saved_date: str,
    body_file_path: str | None,
    body_sha256: str | None,
    has_attachments: bool,
    status: str = "saved",
    source: str | None = None,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    backend: str | None = None,
) -> int | None:
    """插入一条收件记录。若 message_id 已存在则忽略并返回 None。

    source/gmail_message_id/gmail_thread_id/backend 为 Gmail API 后端
    新增的可选字段，旧调用方可不传（兼容 IMAP 既有逻辑）。
    """
    now = _now()
    with _get_conn(db_path) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO received_messages
                    (message_id, gmail_uid, subject, from_email, to_email,
                     received_at, saved_date, body_file_path, body_sha256,
                     has_attachments, status, created_at, updated_at,
                     source, gmail_message_id, gmail_thread_id, backend)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, gmail_uid, subject, from_email, to_email,
                    received_at, saved_date, body_file_path, body_sha256,
                    1 if has_attachments else 0, status, now, now,
                    source, gmail_message_id, gmail_thread_id, backend,
                ),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # message_id 已存在，忽略
            return None


def message_id_exists(db_path: Path | str, message_id: str) -> bool:
    """判断某 message_id 是否已记录过（用于去重）。"""
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM received_messages WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None


def update_received_message_status(
    db_path: Path | str, message_id: str, status: str
) -> None:
    """更新收件记录状态。"""
    with _get_conn(db_path) as conn:
        conn.execute(
            "UPDATE received_messages SET status = ?, updated_at = ? WHERE message_id = ?",
            (status, _now(), message_id),
        )
        conn.commit()


def update_received_message_body(
    db_path: Path | str,
    message_id: str,
    *,
    body_file_path: str | None = None,
    body_sha256: str | None = None,
) -> None:
    """更新收件记录的正文路径与 hash（状态扫描后修正用）。"""
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE received_messages
            SET body_file_path = COALESCE(?, body_file_path),
                body_sha256 = COALESCE(?, body_sha256),
                updated_at = ?
            WHERE message_id = ?
            """,
            (body_file_path, body_sha256, _now(), message_id),
        )
        conn.commit()


def query_received_messages_by_date(
    db_path: Path | str, saved_date: str
) -> list[dict[str, Any]]:
    """查询某天收到的邮件记录。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM received_messages
            WHERE saved_date = ?
            ORDER BY received_at ASC
            """,
            (saved_date,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# received_files
# ============================================================

def insert_received_file(
    db_path: Path | str,
    *,
    message_id: str,
    file_type: str,  # body / attachment
    original_filename: str,
    saved_filename: str,
    saved_path: str,
    sha256: str | None,
    size_bytes: int,
    mime_type: str | None,
    saved_date: str,
    status: str = "normal",
) -> int:
    """插入一条收件文件记录（正文或附件）。"""
    now = _now()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO received_files
                (message_id, file_type, original_filename, saved_filename,
                 saved_path, sha256, size_bytes, mime_type, saved_date,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, file_type, original_filename, saved_filename,
                saved_path, sha256, size_bytes, mime_type, saved_date,
                status, now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid


def query_received_files_by_date(
    db_path: Path | str, saved_date: str
) -> list[dict[str, Any]]:
    """查询某天收到的所有文件记录（正文 + 附件）。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM received_files
            WHERE saved_date = ?
            ORDER BY id ASC
            """,
            (saved_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def query_received_files_by_message(
    db_path: Path | str, message_id: str
) -> list[dict[str, Any]]:
    """查询某封邮件下的所有文件记录。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM received_files WHERE message_id = ? ORDER BY id ASC",
            (message_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_received_file_status(
    db_path: Path | str, file_id: int, status: str, *, saved_path: str | None = None
) -> None:
    """更新收件文件状态（必要时更新路径，例如检测到改名）。"""
    now = _now()
    with _get_conn(db_path) as conn:
        if saved_path is not None:
            conn.execute(
                """
                UPDATE received_files
                SET status = ?, saved_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, saved_path, now, file_id),
            )
        else:
            conn.execute(
                "UPDATE received_files SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, file_id),
            )
        conn.commit()


def query_all_received_files(db_path: Path | str) -> list[dict[str, Any]]:
    """查询全部收件文件记录（状态扫描用）。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM received_files ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# sent_files
# ============================================================

def insert_sent_file(
    db_path: Path | str,
    *,
    source_path: str,
    send_copy_path: str | None,
    sent_copy_path: str | None,
    sha256: str | None,
    subject: str,
    from_email: str,
    to_email: str,
    sent_at: str | None,
    status: str = "sent",
    error_message: str | None = None,
) -> int:
    """插入一条发送记录（成功或失败均可记录）。"""
    now = _now()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO sent_files
                (source_path, send_copy_path, sent_copy_path, sha256,
                 subject, from_email, to_email, sent_at, status,
                 error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_path, send_copy_path, sent_copy_path, sha256,
                subject, from_email, to_email, sent_at, status,
                error_message, now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid


def query_sent_files_by_date(
    db_path: Path | str, date_str: str
) -> list[dict[str, Any]]:
    """查询某天发送的文件记录（按 sent_at 字段前缀匹配 YYYY-MM-DD）。"""
    prefix = f"{date_str}%"
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM sent_files
            WHERE sent_at LIKE ?
            ORDER BY sent_at ASC
            """,
            (prefix,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# app_events
# ============================================================

def log_event(
    db_path: Path | str,
    level: str,
    event_type: str,
    message: str,
) -> None:
    """记录一条应用事件，供后续 GUI 展示最近日志。

    level: INFO / SUCCESS / WARNING / ERROR
    event_type: receive / send / config / db / file
    """
    now = _now()
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_events (level, event_type, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (level, event_type, message, now),
        )
        conn.commit()


def query_recent_events(
    db_path: Path | str, limit: int = 50
) -> list[dict[str, Any]]:
    """查询最近 N 条事件。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM app_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        # 反转为时间正序，便于阅读
        return [dict(r) for r in reversed(rows)]
