"""SQLite 数据库模块。

职责：
1. 初始化 5 张表：received_messages / received_files / sent_files / mcp_calls / app_events。
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
from typing import Any, Callable, Iterator

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
    request_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1,
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
    original_filename TEXT,
    size_bytes INTEGER,
    source_origin TEXT NOT NULL DEFAULT 'controlled',
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

CREATE TABLE IF NOT EXISTS mcp_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    message TEXT,
    created_at TEXT,
    updated_at TEXT
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
CREATE INDEX IF NOT EXISTS idx_mcp_calls_created
    ON mcp_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_request_id
    ON mcp_calls(request_id);
"""


# received_messages 表迁移所需的新增列。
# 旧数据库可能缺少这些列，init_db 会检测并安全补列（不删旧数据）。
_RECEIVED_MESSAGES_NEW_COLUMNS = {
    "source": "TEXT",
    "gmail_message_id": "TEXT",
    "gmail_thread_id": "TEXT",
    "backend": "TEXT",
}

_SENT_FILES_NEW_COLUMNS = {
    "request_id": "TEXT",
    "attempt_count": "INTEGER NOT NULL DEFAULT 1",
    "original_filename": "TEXT",
    "size_bytes": "INTEGER",
    "source_origin": "TEXT NOT NULL DEFAULT 'controlled'",
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
        _migrate_sent_files(conn)
        _ensure_unique_indexes(conn)
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


def _migrate_sent_files(conn: sqlite3.Connection) -> None:
    """为旧数据库补充发送幂等字段。"""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(sent_files)").fetchall()
    }
    for col, col_type in _SENT_FILES_NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE sent_files ADD COLUMN {col} {col_type}")


def _ensure_unique_indexes(conn: sqlite3.Connection) -> None:
    """数据库层阻止跨后端重复邮件和重复发送请求。"""
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_received_message_id_nocase "
            "ON received_messages(message_id COLLATE NOCASE)"
        )
    except sqlite3.IntegrityError:
        # 旧库若已有仅大小写不同的历史记录，不破坏启动；新写入仍使用归一化键。
        pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_sent_request_id "
        "ON sent_files(request_id) WHERE request_id IS NOT NULL"
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
    # 5 秒 busy timeout：覆盖桌面端短时并发写入，不隐藏长期锁故障。
    new_conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    new_conn.row_factory = sqlite3.Row
    # WAL 允许读取与短写事务并行；busy_timeout 单位为毫秒。
    new_conn.execute("PRAGMA foreign_keys = ON;")
    new_conn.execute("PRAGMA busy_timeout = 5000;")
    new_conn.execute("PRAGMA journal_mode = WAL;")
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
        if message_id.startswith("gmail_api:"):
            gmail_message_id = message_id.split(":", 1)[1]
            row = conn.execute(
                "SELECT 1 FROM received_messages WHERE gmail_message_id = ? LIMIT 1",
                (gmail_message_id,),
            ).fetchone()
            return row is not None
        row = conn.execute(
            "SELECT 1 FROM received_messages WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None


def store_received_message_atomically(
    db_path: Path | str,
    message: dict[str, Any],
    write_files: Callable[[], list[dict[str, Any]]],
) -> tuple[bool, list[dict[str, Any]]]:
    """在短写事务中完成去重、文件写入和数据库登记。

    返回 False 表示数据库唯一约束判定为重复。写入失败会回滚数据库，
    确定性文件名使下一次重试复用同一路径，不产生第二套文件。
    """
    now = _now()
    with _get_conn(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO received_messages
                    (message_id, gmail_uid, subject, from_email, to_email,
                     received_at, saved_date, body_file_path, body_sha256,
                     has_attachments, status, created_at, updated_at,
                     source, gmail_message_id, gmail_thread_id, backend)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'processing',
                        ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    message["message_id"], message.get("gmail_uid"),
                    message.get("subject", ""), message.get("from_email", ""),
                    message.get("to_email", ""), message.get("received_at"),
                    message["saved_date"], 1 if message.get("has_attachments") else 0,
                    now, now, message.get("source"),
                    message.get("gmail_message_id"), message.get("gmail_thread_id"),
                    message.get("backend"),
                ),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False, []

            files = write_files()
            for item in files:
                conn.execute(
                    """
                    INSERT INTO received_files
                        (message_id, file_type, original_filename, saved_filename,
                         saved_path, sha256, size_bytes, mime_type, saved_date,
                         status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message["message_id"], item["file_type"],
                        item["original_filename"], item["saved_filename"],
                        item["saved_path"], item.get("sha256"), item["size_bytes"],
                        item.get("mime_type"), message["saved_date"],
                        item.get("status", "normal"), now, now,
                    ),
                )

            body = next((item for item in files if item["file_type"] == "body"), None)
            conn.execute(
                """
                UPDATE received_messages
                SET body_file_path = ?, body_sha256 = ?, status = 'saved', updated_at = ?
                WHERE message_id = ?
                """,
                (
                    body["saved_path"] if body else None,
                    body.get("sha256") if body else None,
                    now,
                    message["message_id"],
                ),
            )
            conn.commit()
            return True, files
        except Exception:
            conn.rollback()
            raise


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


def query_recent_received_messages(
    db_path: Path | str, limit: int = 100
) -> list[dict[str, Any]]:
    """查询最近收件记录，供应用服务历史页使用。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM received_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


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
            SELECT files.*, messages.subject AS subject
            FROM received_files AS files
            LEFT JOIN received_messages AS messages
                ON messages.message_id = files.message_id
            WHERE files.saved_date = ?
            ORDER BY files.id ASC
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


def query_all_received_files_with_messages(
    db_path: Path | str,
) -> list[dict[str, Any]]:
    """文件管理使用的真实收件文件查询，并关联业务主题和后端。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT files.*, messages.subject AS subject,
                   messages.source AS message_source,
                   messages.backend AS message_backend
            FROM received_files AS files
            LEFT JOIN received_messages AS messages
                ON messages.message_id = files.message_id
            ORDER BY files.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


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
    request_id: str | None = None,
    attempt_count: int = 1,
) -> int:
    """插入一条发送记录（成功或失败均可记录）。"""
    now = _now()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO sent_files
                (request_id, attempt_count, source_path, send_copy_path, sent_copy_path, sha256,
                 subject, from_email, to_email, sent_at, status,
                 error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, attempt_count, source_path, send_copy_path, sent_copy_path, sha256,
                subject, from_email, to_email, sent_at, status,
                error_message, now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_send_by_request_id(
    db_path: Path | str, request_id: str
) -> dict[str, Any] | None:
    """按幂等请求标识查询发送记录。"""
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sent_files WHERE request_id = ? LIMIT 1", (request_id,)
        ).fetchone()
        return dict(row) if row else None


def create_or_retry_send_attempt(
    db_path: Path | str,
    *,
    request_id: str,
    source_path: str,
    sha256: str,
    subject: str,
    from_email: str,
    to_email: str,
    original_filename: str | None = None,
    size_bytes: int | None = None,
    source_origin: str = "controlled",
) -> tuple[str, dict[str, Any]]:
    """创建发送尝试；失败记录可重试，已发送记录按重复返回。"""
    now = _now()
    with _get_conn(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sent_files WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                current = dict(row)
                if current["status"] in {"sent", "sent_archive_failed", "attempt_created"}:
                    conn.rollback()
                    return "duplicate", current
                conn.execute(
                    """
                    UPDATE sent_files
                    SET status = 'attempt_created', error_message = NULL,
                        attempt_count = attempt_count + 1, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (now, request_id),
                )
                conn.commit()
                return "retry", get_send_by_request_id(db_path, request_id) or current

            conn.execute(
                """
                INSERT INTO sent_files
                    (request_id, attempt_count, source_path, sha256, subject,
                     from_email, to_email, status, original_filename, size_bytes,
                     source_origin, created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, ?, 'attempt_created', ?, ?, ?, ?, ?)
                """,
                (
                    request_id, source_path, sha256, subject, from_email, to_email,
                    original_filename, size_bytes, source_origin, now, now,
                ),
            )
            conn.commit()
            return "created", get_send_by_request_id(db_path, request_id) or {}
        except Exception:
            conn.rollback()
            raise


def update_send_attempt(
    db_path: Path | str,
    request_id: str,
    *,
    status: str,
    send_copy_path: str | None = None,
    sent_copy_path: str | None = None,
    sent_at: str | None = None,
    error_message: str | None = None,
) -> None:
    """更新一次发送尝试的准确最终状态。"""
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE sent_files
            SET status = ?, send_copy_path = COALESCE(?, send_copy_path),
                sent_copy_path = COALESCE(?, sent_copy_path),
                sent_at = COALESCE(?, sent_at), error_message = ?, updated_at = ?
            WHERE request_id = ?
            """,
            (
                status, send_copy_path, sent_copy_path, sent_at,
                error_message, _now(), request_id,
            ),
        )
        conn.commit()


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


def query_recent_sent_files(
    db_path: Path | str, limit: int = 100
) -> list[dict[str, Any]]:
    """查询最近发送记录。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sent_files ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


# ============================================================
# mcp_calls
# ============================================================

def insert_mcp_call(
    db_path: Path | str,
    *,
    request_id: str,
    file_path: str,
    title: str | None,
    status: str = "attempt_created",
) -> int:
    """登记一次 MCP 调用，所有成功和失败都可审计。"""
    now = _now()
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO mcp_calls
                (request_id, file_path, title, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (request_id, file_path, title, status, now, now),
        )
        conn.commit()
        return cursor.lastrowid


def update_mcp_call(
    db_path: Path | str,
    call_id: int,
    *,
    status: str,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    """写入 MCP 调用最终状态。"""
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE mcp_calls
            SET status = ?, error_code = ?, message = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error_code, message, _now(), call_id),
        )
        conn.commit()


def query_recent_mcp_calls(
    db_path: Path | str, limit: int = 100
) -> list[dict[str, Any]]:
    """查询最近 MCP 调用，按新到旧返回。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_calls ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [dict(row) for row in rows]


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
        # 保持最新事件在前，界面打开即可看到最新日志。
        return [dict(r) for r in rows]
