"""SQLite 数据库模块。

职责：
1. 初始化业务表、MCP 审计表、自动收件状态表与有限重试表。
2. 提供线程安全的连接管理（每线程一个连接）。
3. 提供增 / 改 / 查函数，供收件 / 发件 / 文件扫描 / GUI 调用。

所有时间字段统一使用 ISO-like 字符串：YYYY-MM-DD HH:MM:SS。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from agent_mail_bridge.utils import fmt_datetime, now_local

# 每线程连接缓存
_local = threading.local()

DEFAULT_NORMAL_EVENT_RETENTION_DAYS = 30
DEFAULT_ERROR_EVENT_RETENTION_DAYS = 90
DEFAULT_APP_EVENT_MAX_COUNT = 10_000
APP_EVENT_TARGET_RATIO = 0.8
_event_retention_limits: dict[str, int] = {}
_event_limit_lock = threading.Lock()

_DAILY_CHECK_SQL = """
(
    lower(event_type) IN ('receive', 'receive_auto', 'auto_receive')
    AND (
        message LIKE '%开始收取邮件%'
        OR message LIKE '%开始通过 Gmail API 收取邮件%'
        OR message LIKE '%暂无新邮件%'
        OR message LIKE '%暂时没有新邮件%'
        OR message LIKE '%新存 0 封%'
    )
)
"""

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
    backend TEXT,
    package_id TEXT
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
    updated_at TEXT,
    package_id TEXT,
    resource_id TEXT
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
    source_sha256 TEXT,
    staged_sha256 TEXT,
    attachment_sha256 TEXT,
    sent_archive_sha256 TEXT,
    outbound_id TEXT,
    outbound_resource_id TEXT,
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
    staged_path TEXT,
    source_size_bytes INTEGER,
    staged_size_bytes INTEGER,
    source_sha256 TEXT,
    staged_sha256 TEXT,
    attachment_sha256 TEXT,
    sent_archive_sha256 TEXT,
    staging_at TEXT,
    staging_status TEXT,
    staging_failure_reason TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS mcp_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL UNIQUE,
    called_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    client_name TEXT,
    session_id TEXT,
    request_id TEXT,
    query_summary TEXT,
    mail_id TEXT,
    resource_id TEXT,
    result_count INTEGER,
    target_summary TEXT,
    source_path TEXT,
    prepared_path TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    bytes_returned INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    ensure_fresh INTEGER NOT NULL DEFAULT 0,
    sync_triggered INTEGER NOT NULL DEFAULT 0,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS mail_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL UNIQUE,
    account_ref TEXT NOT NULL,
    mailbox_ref TEXT NOT NULL,
    backend TEXT NOT NULL,
    message_id TEXT NOT NULL,
    provider_message_id TEXT,
    thread_ref TEXT,
    subject TEXT,
    from_email TEXT,
    to_emails TEXT,
    cc_emails TEXT,
    bcc_emails TEXT,
    sent_at TEXT,
    received_at TEXT,
    saved_at TEXT,
    package_root TEXT NOT NULL,
    raw_eml_path TEXT,
    raw_eml_sha256 TEXT,
    raw_eml_status TEXT NOT NULL,
    body_plain_path TEXT,
    body_html_path TEXT,
    body_readable_path TEXT,
    body_text_sha256 TEXT,
    search_text TEXT,
    resource_count INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    inline_image_count INTEGER NOT NULL DEFAULT 0,
    link_count INTEGER NOT NULL DEFAULT 0,
    downloaded_count INTEGER NOT NULL DEFAULT 0,
    archive_status TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    last_error TEXT,
    legacy INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_ref, message_id COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS mail_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    original_name TEXT,
    mime_type TEXT,
    local_path TEXT,
    original_url TEXT,
    content_id TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL,
    error TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(package_id) REFERENCES mail_packages(package_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbound_id TEXT NOT NULL UNIQUE,
    sender_account_ref TEXT NOT NULL,
    sender_ref TEXT NOT NULL,
    source_origin TEXT NOT NULL,
    request_id TEXT,
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT '',
    to_emails TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    link_count INTEGER NOT NULL DEFAULT 0,
    legacy_limited INTEGER NOT NULL DEFAULT 0,
    legacy_sent_file_id INTEGER UNIQUE,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL UNIQUE,
    outbound_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    mime_type TEXT,
    source_path TEXT,
    staged_path TEXT,
    sent_archive_path TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    staged_sha256 TEXT,
    sent_archive_sha256 TEXT,
    status TEXT NOT NULL,
    error TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(outbound_id) REFERENCES outbound_messages(outbound_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbound_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbound_id TEXT NOT NULL,
    url TEXT NOT NULL,
    display_text TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(outbound_id) REFERENCES outbound_messages(outbound_id) ON DELETE CASCADE,
    UNIQUE(outbound_id, url)
);

CREATE TABLE IF NOT EXISTS trusted_domains (
    domain TEXT PRIMARY KEY,
    include_subdomains INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_metadata (
    migration_key TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_receive_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_seconds INTEGER NOT NULL DEFAULT 60,
    last_check_at TEXT,
    last_success_at TEXT,
    last_result TEXT,
    last_error TEXT,
    consecutive_global_failures INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT,
    checkpoint TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS receive_retries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    message_id TEXT,
    attachment_id TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at TEXT,
    next_retry_at TEXT,
    terminal_status TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE (backend, resource_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_received_messages_saved_date
    ON received_messages(saved_date);
CREATE INDEX IF NOT EXISTS idx_received_files_saved_date
    ON received_files(saved_date);
CREATE INDEX IF NOT EXISTS idx_received_files_message_id
    ON received_files(message_id);
CREATE INDEX IF NOT EXISTS idx_mail_packages_received
    ON mail_packages(received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_mail_packages_thread
    ON mail_packages(account_ref, thread_ref, received_at);
CREATE INDEX IF NOT EXISTS idx_mail_packages_mailbox
    ON mail_packages(account_ref, mailbox_ref, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_mail_resources_package
    ON mail_resources(package_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_mail_resources_name
    ON mail_resources(display_name);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_sent
    ON outbound_messages(COALESCE(sent_at, created_at) DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_outbound_resources_message
    ON outbound_resources(outbound_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_outbound_links_message
    ON outbound_links(outbound_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_sent_files_sent_date
    ON sent_files(sent_at);
CREATE INDEX IF NOT EXISTS idx_app_events_created
    ON app_events(created_at);
CREATE INDEX IF NOT EXISTS idx_app_events_level_created
    ON app_events(level, created_at);
CREATE INDEX IF NOT EXISTS idx_app_events_type_created
    ON app_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_created
    ON mcp_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_request_id
    ON mcp_calls(request_id);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_called
    ON mcp_audit_events(called_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_tool
    ON mcp_audit_events(tool_name, called_at DESC);
CREATE INDEX IF NOT EXISTS idx_receive_retries_next_retry
    ON receive_retries(next_retry_at);
"""


# received_messages 表迁移所需的新增列。
# 旧数据库可能缺少这些列，init_db 会检测并安全补列（不删旧数据）。
_RECEIVED_MESSAGES_NEW_COLUMNS = {
    "source": "TEXT",
    "gmail_message_id": "TEXT",
    "gmail_thread_id": "TEXT",
    "backend": "TEXT",
    "package_id": "TEXT",
}

_RECEIVED_FILES_NEW_COLUMNS = {
    "package_id": "TEXT",
    "resource_id": "TEXT",
}

_SENT_FILES_NEW_COLUMNS = {
    "request_id": "TEXT",
    "attempt_count": "INTEGER NOT NULL DEFAULT 1",
    "original_filename": "TEXT",
    "size_bytes": "INTEGER",
    "source_origin": "TEXT NOT NULL DEFAULT 'controlled'",
    "source_sha256": "TEXT",
    "staged_sha256": "TEXT",
    "attachment_sha256": "TEXT",
    "sent_archive_sha256": "TEXT",
    "outbound_id": "TEXT",
    "outbound_resource_id": "TEXT",
}

_MCP_CALLS_NEW_COLUMNS = {
    "staged_path": "TEXT",
    "source_size_bytes": "INTEGER",
    "staged_size_bytes": "INTEGER",
    "source_sha256": "TEXT",
    "staged_sha256": "TEXT",
    "attachment_sha256": "TEXT",
    "sent_archive_sha256": "TEXT",
    "staging_at": "TEXT",
    "staging_status": "TEXT",
    "staging_failure_reason": "TEXT",
}

RECEIVE_RETRY_DELAYS_SECONDS = (60, 300, 1800, 7200)
RECEIVE_RETRY_TERMINAL_COUNT = 5


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
        _migrate_received_files(conn)
        _migrate_sent_files(conn)
        _migrate_mcp_calls(conn)
        _ensure_unique_indexes(conn)
        _backfill_legacy_outbound_messages(conn)
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


def _migrate_received_files(conn: sqlite3.Connection) -> None:
    """为旧文件记录补充邮件归档关联，不改写旧路径或内容。"""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(received_files)").fetchall()
    }
    for col, col_type in _RECEIVED_FILES_NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE received_files ADD COLUMN {col} {col_type}")


def _migrate_sent_files(conn: sqlite3.Connection) -> None:
    """为旧数据库补充发送幂等字段。"""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(sent_files)").fetchall()
    }
    for col, col_type in _SENT_FILES_NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE sent_files ADD COLUMN {col} {col_type}")


def _migrate_mcp_calls(conn: sqlite3.Connection) -> None:
    """为旧数据库补充 MCP staging 与完整性审计字段。"""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(mcp_calls)").fetchall()
    }
    for col, col_type in _MCP_CALLS_NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE mcp_calls ADD COLUMN {col} {col_type}")


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
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_outbound_request_id "
        "ON outbound_messages(request_id) WHERE request_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_received_messages_package "
        "ON received_messages(package_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_received_files_package "
        "ON received_files(package_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_received_files_resource "
        "ON received_files(resource_id) WHERE resource_id IS NOT NULL"
    )


def _backfill_legacy_outbound_messages(conn: sqlite3.Connection) -> None:
    """把旧 sent_files 幂等映射成邮件级发送事实，不伪造正文。"""
    now = _now()
    rows = conn.execute(
        """
        SELECT * FROM sent_files
        WHERE outbound_id IS NULL OR outbound_id = ''
        ORDER BY id ASC
        """
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        sent_file_id = int(row["id"])
        outbound_id = f"legacy_out_{sent_file_id}"
        resource_id = f"legacy_out_res_{sent_file_id}"
        raw_origin = str(row.get("source_origin") or "legacy")
        if raw_origin == "manual_gui":
            source_origin = "manual_gui"
        elif row.get("request_id"):
            source_origin = "agent_mcp"
        else:
            source_origin = "legacy"
        raw_status = str(row.get("status") or "failed")
        status = (
            "sent" if raw_status == "sent"
            else "partial" if raw_status == "sent_archive_failed"
            else "failed"
        )
        created_at = str(row.get("created_at") or row.get("sent_at") or now)
        display_name = str(
            row.get("original_filename")
            or Path(str(row.get("sent_copy_path") or row.get("send_copy_path") or row.get("source_path") or "")).name
            or "旧版本附件"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_messages
                (outbound_id, sender_account_ref, sender_ref, source_origin,
                 request_id, subject, body_text, to_emails, status, error,
                 attachment_count, link_count, legacy_limited,
                 legacy_sent_file_id, created_at, sent_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, 1, 0, 1, ?, ?, ?, ?)
            """,
            (
                outbound_id,
                f"qq:{str(row.get('from_email') or '').strip().casefold() or 'legacy'}",
                str(row.get("from_email") or ""),
                source_origin,
                row.get("request_id"),
                str(row.get("subject") or display_name),
                json.dumps([str(row.get("to_email") or "")], ensure_ascii=False),
                status,
                row.get("error_message"),
                sent_file_id,
                created_at,
                row.get("sent_at"),
                now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_resources
                (resource_id, outbound_id, resource_type, display_name,
                 source_path, staged_path, sent_archive_path, size_bytes,
                 sha256, staged_sha256, sent_archive_sha256, status, error,
                 sort_order, created_at, updated_at)
            VALUES (?, ?, 'attachment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                resource_id,
                outbound_id,
                display_name,
                row.get("source_path"),
                row.get("send_copy_path"),
                row.get("sent_copy_path"),
                row.get("size_bytes"),
                row.get("source_sha256") or row.get("sha256"),
                row.get("staged_sha256") or row.get("sha256"),
                row.get("sent_archive_sha256"),
                status,
                row.get("error_message"),
                created_at,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE sent_files
            SET outbound_id = ?, outbound_resource_id = ?, updated_at = COALESCE(updated_at, ?)
            WHERE id = ?
            """,
            (outbound_id, resource_id, now, sent_file_id),
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
# unified mail archive / migration / trusted domains
# ============================================================

def legacy_archive_backfill_needed(db_path: Path | str) -> bool:
    """旧业务邮件尚未全部进入权威邮件归档模型时返回 True。"""
    path = Path(db_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "received_messages" not in tables:
            return False
        legacy_count = int(
            connection.execute("SELECT COUNT(*) FROM received_messages").fetchone()[0]
        )
        if not legacy_count or "mail_packages" not in tables:
            return bool(legacy_count)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(received_messages)")
        }
        if "package_id" not in columns:
            return bool(legacy_count)
        package_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM received_messages "
                "WHERE package_id IS NOT NULL AND package_id != ''"
            ).fetchone()[0]
        )
        return package_count < legacy_count
    finally:
        connection.close()


def outbound_mail_migration_needed(db_path: Path | str) -> bool:
    """旧数据库尚无邮件级发件表或 sent_files 关联列时返回 True。"""
    path = Path(db_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sent_files" not in tables:
            return False
        required_tables = {
            "outbound_messages", "outbound_resources", "outbound_links"
        }
        if not required_tables.issubset(tables):
            return True
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sent_files)")
        }
        if not {"outbound_id", "outbound_resource_id"}.issubset(columns):
            return True
        unlinked = int(
            connection.execute(
                "SELECT COUNT(*) FROM sent_files "
                "WHERE outbound_id IS NULL OR outbound_id = ''"
            ).fetchone()[0]
        )
        return unlinked > 0
    finally:
        connection.close()


def query_legacy_messages_for_backfill(db_path: Path | str) -> list[dict[str, Any]]:
    """返回尚未关联 package_id 的旧邮件，不读取正文或附件内容。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM received_messages
            WHERE package_id IS NULL OR package_id = ''
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_mail_package_by_identity(
    db_path: Path | str, account_ref: str, message_id: str
) -> dict[str, Any] | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM mail_packages
            WHERE account_ref = ? AND message_id = ? COLLATE NOCASE
            LIMIT 1
            """,
            (account_ref, message_id),
        ).fetchone()
        return dict(row) if row else None


def get_mail_package(db_path: Path | str, package_id: str) -> dict[str, Any] | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM mail_packages WHERE package_id = ? LIMIT 1",
            (package_id,),
        ).fetchone()
        return dict(row) if row else None


def query_mail_resources(
    db_path: Path | str, package_id: str
) -> list[dict[str, Any]]:
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM mail_resources
            WHERE package_id = ? ORDER BY sort_order ASC, id ASC
            """,
            (package_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def query_mail_local_resources(db_path: Path | str) -> list[dict[str, Any]]:
    """列出所有有本地文件的邮件资源，并携带所属邮件事实。"""
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.*, p.subject AS mail_subject, p.from_email AS mail_from,
                   p.backend AS mail_backend, p.received_at AS mail_received_at,
                   p.saved_at AS mail_saved_at, p.package_root,
                   p.archive_status AS mail_archive_status, p.legacy AS mail_legacy
            FROM mail_resources r
            JOIN mail_packages p ON p.package_id = r.package_id
            WHERE r.local_path IS NOT NULL AND r.local_path != ''
            ORDER BY COALESCE(p.received_at, p.saved_at) DESC,
                     r.sort_order ASC, r.id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def store_mail_archive_atomically(
    db_path: Path | str,
    package: dict[str, Any],
    resources: list[dict[str, Any]],
    compatibility_files: list[dict[str, Any]],
) -> None:
    """一次事务写入权威归档事实与旧 UI 兼容行。"""
    now = _now()
    created_at = package.get("created_at") or now
    with _get_conn(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO mail_packages
                    (package_id, account_ref, mailbox_ref, backend, message_id,
                     provider_message_id, thread_ref, subject, from_email,
                     to_emails, cc_emails, bcc_emails, sent_at, received_at,
                     saved_at, package_root, raw_eml_path, raw_eml_sha256,
                     raw_eml_status, body_plain_path, body_html_path,
                     body_readable_path, body_text_sha256, search_text,
                     resource_count, attachment_count, inline_image_count,
                     link_count, downloaded_count, archive_status, parse_status,
                     last_error, legacy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_id) DO UPDATE SET
                    mailbox_ref=excluded.mailbox_ref,
                    backend=excluded.backend,
                    provider_message_id=COALESCE(excluded.provider_message_id, mail_packages.provider_message_id),
                    thread_ref=COALESCE(excluded.thread_ref, mail_packages.thread_ref),
                    subject=excluded.subject,
                    from_email=excluded.from_email,
                    to_emails=excluded.to_emails,
                    cc_emails=excluded.cc_emails,
                    bcc_emails=excluded.bcc_emails,
                    sent_at=excluded.sent_at,
                    received_at=excluded.received_at,
                    saved_at=excluded.saved_at,
                    package_root=excluded.package_root,
                    raw_eml_path=excluded.raw_eml_path,
                    raw_eml_sha256=excluded.raw_eml_sha256,
                    raw_eml_status=excluded.raw_eml_status,
                    body_plain_path=excluded.body_plain_path,
                    body_html_path=excluded.body_html_path,
                    body_readable_path=excluded.body_readable_path,
                    body_text_sha256=excluded.body_text_sha256,
                    search_text=excluded.search_text,
                    resource_count=excluded.resource_count,
                    attachment_count=excluded.attachment_count,
                    inline_image_count=excluded.inline_image_count,
                    link_count=excluded.link_count,
                    downloaded_count=excluded.downloaded_count,
                    archive_status=excluded.archive_status,
                    parse_status=excluded.parse_status,
                    last_error=excluded.last_error,
                    legacy=excluded.legacy,
                    updated_at=excluded.updated_at
                """,
                (
                    package["package_id"], package["account_ref"],
                    package["mailbox_ref"], package["backend"],
                    package["message_id"], package.get("provider_message_id"),
                    package.get("thread_ref"), package.get("subject", ""),
                    package.get("from_email", ""), package.get("to_emails", ""),
                    package.get("cc_emails", ""), package.get("bcc_emails", ""),
                    package.get("sent_at"), package.get("received_at"),
                    package.get("saved_at") or now, package["package_root"],
                    package.get("raw_eml_path"), package.get("raw_eml_sha256"),
                    package["raw_eml_status"], package.get("body_plain_path"),
                    package.get("body_html_path"), package.get("body_readable_path"),
                    package.get("body_text_sha256"), package.get("search_text", ""),
                    int(package.get("resource_count") or 0),
                    int(package.get("attachment_count") or 0),
                    int(package.get("inline_image_count") or 0),
                    int(package.get("link_count") or 0),
                    int(package.get("downloaded_count") or 0),
                    package["archive_status"], package["parse_status"],
                    package.get("last_error"), 1 if package.get("legacy") else 0,
                    created_at, now,
                ),
            )

            for resource in resources:
                conn.execute(
                    """
                    INSERT INTO mail_resources
                        (resource_id, package_id, resource_type, source_type,
                         display_name, original_name, mime_type, local_path,
                         original_url, content_id, size_bytes, sha256, status,
                         error, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(resource_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        original_name=excluded.original_name,
                        mime_type=excluded.mime_type,
                        local_path=excluded.local_path,
                        original_url=excluded.original_url,
                        content_id=excluded.content_id,
                        size_bytes=excluded.size_bytes,
                        sha256=excluded.sha256,
                        status=excluded.status,
                        error=excluded.error,
                        sort_order=excluded.sort_order,
                        updated_at=excluded.updated_at
                    """,
                    (
                        resource["resource_id"], package["package_id"],
                        resource["resource_type"], resource["source_type"],
                        resource["display_name"], resource.get("original_name"),
                        resource.get("mime_type"), resource.get("local_path"),
                        resource.get("original_url"), resource.get("content_id"),
                        resource.get("size_bytes"), resource.get("sha256"),
                        resource["status"], resource.get("error"),
                        int(resource.get("sort_order") or 0),
                        resource.get("created_at") or now, now,
                    ),
                )

            compatibility_status = (
                "saved" if package["archive_status"] in {"ready", "legacy"}
                else package["archive_status"]
            )
            body_resource = next(
                (item for item in compatibility_files if item["file_type"] == "body"),
                None,
            )
            conn.execute(
                """
                INSERT INTO received_messages
                    (message_id, gmail_uid, subject, from_email, to_email,
                     received_at, saved_date, body_file_path, body_sha256,
                     has_attachments, status, created_at, updated_at, source,
                     gmail_message_id, gmail_thread_id, backend, package_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    package_id=excluded.package_id,
                    body_file_path=COALESCE(received_messages.body_file_path, excluded.body_file_path),
                    body_sha256=COALESCE(received_messages.body_sha256, excluded.body_sha256),
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    package["message_id"], package.get("gmail_uid"),
                    package.get("subject", ""), package.get("from_email", ""),
                    ", ".join(filter(None, [package.get("to_emails", ""), package.get("cc_emails", "")])),
                    package.get("received_at"), package.get("saved_date", ""),
                    body_resource.get("saved_path") if body_resource else None,
                    body_resource.get("sha256") if body_resource else None,
                    1 if int(package.get("attachment_count") or 0) else 0,
                    compatibility_status, created_at, now, package.get("backend"),
                    package.get("provider_message_id"), package.get("gmail_thread_id"),
                    package.get("backend"), package["package_id"],
                ),
            )

            for item in compatibility_files:
                legacy_file_id = item.get("legacy_file_id")
                if legacy_file_id:
                    conn.execute(
                        "UPDATE received_files SET package_id = ?, resource_id = ?, updated_at = ? WHERE id = ?",
                        (package["package_id"], item["resource_id"], now, legacy_file_id),
                    )
                    continue
                existing = conn.execute(
                    "SELECT id FROM received_files WHERE resource_id = ? LIMIT 1",
                    (item["resource_id"],),
                ).fetchone()
                values = (
                    package["message_id"], item["file_type"], item["original_filename"],
                    item["saved_filename"], item["saved_path"], item.get("sha256"),
                    item.get("size_bytes"), item.get("mime_type"),
                    package.get("saved_date", ""), item.get("status", "normal"),
                    package["package_id"], item["resource_id"], now,
                )
                if existing:
                    conn.execute(
                        """
                        UPDATE received_files SET
                            message_id=?, file_type=?, original_filename=?, saved_filename=?,
                            saved_path=?, sha256=?, size_bytes=?, mime_type=?, saved_date=?,
                            status=?, package_id=?, resource_id=?, updated_at=? WHERE id=?
                        """,
                        (*values, existing["id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO received_files
                            (message_id, file_type, original_filename, saved_filename,
                             saved_path, sha256, size_bytes, mime_type, saved_date,
                             status, package_id, resource_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (*values[:-1], now, now),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def save_migration_metadata(
    db_path: Path | str,
    migration_key: str,
    *,
    schema_version: int,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    now = _now()
    completed_at = now if status in {"completed", "partial", "failed"} else None
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO migration_metadata
                (migration_key, schema_version, status, details_json,
                 started_at, completed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(migration_key) DO UPDATE SET
                schema_version=excluded.schema_version,
                status=excluded.status,
                details_json=excluded.details_json,
                completed_at=excluded.completed_at,
                updated_at=excluded.updated_at
            """,
            (
                migration_key, schema_version, status,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now, completed_at, now,
            ),
        )
        conn.commit()


def query_trusted_domains(db_path: Path | str) -> list[dict[str, Any]]:
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM trusted_domains ORDER BY domain ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_trusted_domain(
    db_path: Path | str,
    domain: str,
    *,
    include_subdomains: bool = False,
    enabled: bool = True,
) -> None:
    now = _now()
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trusted_domains
                (domain, include_subdomains, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                include_subdomains=excluded.include_subdomains,
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (domain, 1 if include_subdomains else 0, 1 if enabled else 0, now, now),
        )
        conn.commit()


def delete_trusted_domain(db_path: Path | str, domain: str) -> None:
    with _get_conn(db_path) as conn:
        conn.execute("DELETE FROM trusted_domains WHERE domain = ?", (domain,))
        conn.commit()


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
    original_filename: str | None = None,
    size_bytes: int | None = None,
    source_origin: str = "controlled",
    source_sha256: str | None = None,
    staged_sha256: str | None = None,
    attachment_sha256: str | None = None,
    sent_archive_sha256: str | None = None,
    outbound_id: str | None = None,
    outbound_resource_id: str | None = None,
) -> int:
    """插入一条发送记录（成功或失败均可记录）。"""
    now = _now()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO sent_files
                (request_id, attempt_count, source_path, send_copy_path, sent_copy_path, sha256,
                 subject, from_email, to_email, sent_at, status,
                 error_message, original_filename, size_bytes, source_origin,
                 source_sha256, staged_sha256, attachment_sha256,
                 sent_archive_sha256, outbound_id, outbound_resource_id,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, attempt_count, source_path, send_copy_path, sent_copy_path, sha256,
                subject, from_email, to_email, sent_at, status,
                error_message, original_filename, size_bytes, source_origin,
                source_sha256, staged_sha256, attachment_sha256,
                sent_archive_sha256, outbound_id, outbound_resource_id,
                now, now,
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
    source_sha256: str | None = None,
    staged_sha256: str | None = None,
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
                        attempt_count = attempt_count + 1,
                        source_sha256 = COALESCE(?, source_sha256),
                        staged_sha256 = COALESCE(?, staged_sha256), updated_at = ?
                    WHERE request_id = ?
                    """,
                    (source_sha256, staged_sha256, now, request_id),
                )
                conn.commit()
                return "retry", get_send_by_request_id(db_path, request_id) or current

            conn.execute(
                """
                INSERT INTO sent_files
                    (request_id, attempt_count, source_path, sha256, subject,
                     from_email, to_email, status, original_filename, size_bytes,
                     source_origin, source_sha256, staged_sha256, created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, ?, 'attempt_created', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id, source_path, sha256, subject, from_email, to_email,
                    original_filename, size_bytes, source_origin,
                    source_sha256, staged_sha256, now, now,
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
    attachment_sha256: str | None = None,
    sent_archive_sha256: str | None = None,
) -> None:
    """更新一次发送尝试的准确最终状态。"""
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE sent_files
            SET status = ?, send_copy_path = COALESCE(?, send_copy_path),
                sent_copy_path = COALESCE(?, sent_copy_path),
                sent_at = COALESCE(?, sent_at), error_message = ?,
                attachment_sha256 = COALESCE(?, attachment_sha256),
                sent_archive_sha256 = COALESCE(?, sent_archive_sha256),
                updated_at = ?
            WHERE request_id = ?
            """,
            (
                status, send_copy_path, sent_copy_path, sent_at,
                error_message, attachment_sha256, sent_archive_sha256,
                _now(), request_id,
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


def update_mcp_staging(
    db_path: Path | str,
    call_id: int,
    *,
    staging_status: str,
    staged_path: str | None = None,
    source_size_bytes: int | None = None,
    staged_size_bytes: int | None = None,
    source_sha256: str | None = None,
    staged_sha256: str | None = None,
    attachment_sha256: str | None = None,
    sent_archive_sha256: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """持久化 MCP 受控 staging 与端到端本地 Hash 事实。"""
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE mcp_calls
            SET staged_path = COALESCE(?, staged_path),
                source_size_bytes = COALESCE(?, source_size_bytes),
                staged_size_bytes = COALESCE(?, staged_size_bytes),
                source_sha256 = COALESCE(?, source_sha256),
                staged_sha256 = COALESCE(?, staged_sha256),
                attachment_sha256 = COALESCE(?, attachment_sha256),
                sent_archive_sha256 = COALESCE(?, sent_archive_sha256),
                staging_at = COALESCE(staging_at, ?), staging_status = ?,
                staging_failure_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                staged_path, source_size_bytes, staged_size_bytes,
                source_sha256, staged_sha256, attachment_sha256,
                sent_archive_sha256, _now(), staging_status,
                failure_reason, _now(), call_id,
            ),
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


def insert_mcp_audit_event(
    db_path: Path | str,
    *,
    call_id: str,
    called_at: str,
    completed_at: str,
    tool_name: str,
    operation_type: str,
    status: str,
    client_name: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    query_summary: str | None = None,
    mail_id: str | None = None,
    resource_id: str | None = None,
    result_count: int | None = None,
    target_summary: str | None = None,
    source_path: str | None = None,
    prepared_path: str | None = None,
    error_code: str | None = None,
    duration_ms: int = 0,
    bytes_returned: int = 0,
    cached: bool = False,
    ensure_fresh: bool = False,
    sync_triggered: bool = False,
    details: dict[str, Any] | None = None,
) -> int:
    """记录统一 MCP 审计；查询和详情均不保存正文或资源内容。"""
    safe_query = " ".join(str(query_summary or "").split())[:500] or None
    safe_target = " ".join(str(target_summary or "").split())[:500] or None
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO mcp_audit_events
                (call_id, called_at, completed_at, tool_name, operation_type,
                 client_name, session_id, request_id, query_summary, mail_id,
                 resource_id, result_count, target_summary, source_path,
                 prepared_path, status, error_code, duration_ms, bytes_returned,
                 cached, ensure_fresh, sync_triggered, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id, called_at, completed_at, tool_name, operation_type,
                client_name, session_id, request_id, safe_query, mail_id,
                resource_id, result_count, safe_target, source_path,
                prepared_path, status, error_code, max(0, int(duration_ms)),
                max(0, int(bytes_returned)), 1 if cached else 0,
                1 if ensure_fresh else 0, 1 if sync_triggered else 0,
                details_json,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def query_recent_mcp_audit_events(
    db_path: Path | str, limit: int = 100
) -> list[dict[str, Any]]:
    """返回新统一审计，并无损补入尚未迁移的旧 submit_result 记录。"""
    safe_limit = max(1, min(int(limit), 500))
    with _get_conn(db_path) as conn:
        audit_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM mcp_audit_events ORDER BY called_at DESC, id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        ]
        legacy_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT c.* FROM mcp_calls c
                WHERE NOT EXISTS (
                    SELECT 1 FROM mcp_audit_events a
                    WHERE a.tool_name = 'submit_result' AND a.request_id = c.request_id
                )
                ORDER BY c.id DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        ]
    unified = list(audit_rows)
    for row in legacy_rows:
        unified.append(
            {
                **row,
                "call_id": f"legacy-{row.get('id')}",
                "called_at": row.get("created_at"),
                "completed_at": row.get("updated_at"),
                "tool_name": "submit_result",
                "operation_type": "send",
                "target_summary": row.get("title") or Path(str(row.get("file_path") or "")).name,
                "source_path": row.get("file_path"),
                "prepared_path": row.get("staged_path"),
                "duration_ms": 0,
                "bytes_returned": 0,
                "cached": 0,
                "ensure_fresh": 0,
                "sync_triggered": 0,
                "details_json": "{}",
            }
        )
    unified.sort(key=lambda row: str(row.get("called_at") or ""), reverse=True)
    return unified[:safe_limit]


def link_sent_file_to_outbound(
    db_path: Path | str,
    *,
    outbound_id: str,
    resource_id: str,
    request_id: str | None = None,
    sent_file_id: int | None = None,
) -> None:
    """把兼容文件事实关联到邮件级发送对象。"""
    if request_id is None and sent_file_id is None:
        raise ValueError("request_id 与 sent_file_id 至少提供一个")
    with _get_conn(db_path) as conn:
        if request_id is not None:
            conn.execute(
                """
                UPDATE sent_files
                SET outbound_id = ?, outbound_resource_id = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (outbound_id, resource_id, _now(), request_id),
            )
        else:
            conn.execute(
                """
                UPDATE sent_files
                SET outbound_id = ?, outbound_resource_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (outbound_id, resource_id, _now(), sent_file_id),
            )
        conn.commit()


# ============================================================
# outbound mail facts
# ============================================================

def create_outbound_message(
    db_path: Path | str,
    *,
    outbound_id: str,
    sender_account_ref: str,
    sender_ref: str,
    source_origin: str,
    request_id: str | None,
    subject: str,
    body_text: str,
    to_emails: list[str],
    attachment_count: int,
    link_count: int,
    status: str = "sending",
    error: str | None = None,
) -> dict[str, Any]:
    """创建一封发送邮件；相同 outbound_id 不会产生第二封。"""
    now = _now()
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_messages
                (outbound_id, sender_account_ref, sender_ref, source_origin,
                 request_id, subject, body_text, to_emails, status, error,
                 attachment_count, link_count, legacy_limited,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                outbound_id, sender_account_ref, sender_ref, source_origin,
                request_id, subject, body_text,
                json.dumps(to_emails, ensure_ascii=False), status, error,
                int(attachment_count), int(link_count), now, now,
            ),
        )
        conn.commit()
    return get_outbound_message(db_path, outbound_id) or {}


def update_outbound_message(
    db_path: Path | str,
    outbound_id: str,
    *,
    status: str,
    sent_at: str | None = None,
    error: str | None = None,
) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = ?, sent_at = COALESCE(?, sent_at), error = ?, updated_at = ?
            WHERE outbound_id = ?
            """,
            (status, sent_at, error, _now(), outbound_id),
        )
        conn.commit()


def upsert_outbound_resource(
    db_path: Path | str,
    *,
    resource_id: str,
    outbound_id: str,
    display_name: str,
    mime_type: str | None,
    source_path: str | None,
    staged_path: str | None,
    sent_archive_path: str | None,
    size_bytes: int | None,
    sha256: str | None,
    staged_sha256: str | None,
    sent_archive_sha256: str | None,
    status: str,
    error: str | None,
    sort_order: int,
) -> None:
    now = _now()
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO outbound_resources
                (resource_id, outbound_id, resource_type, display_name,
                 mime_type, source_path, staged_path, sent_archive_path,
                 size_bytes, sha256, staged_sha256, sent_archive_sha256,
                 status, error, sort_order, created_at, updated_at)
            VALUES (?, ?, 'attachment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_id) DO UPDATE SET
                display_name=excluded.display_name,
                mime_type=COALESCE(excluded.mime_type, outbound_resources.mime_type),
                source_path=COALESCE(excluded.source_path, outbound_resources.source_path),
                staged_path=COALESCE(excluded.staged_path, outbound_resources.staged_path),
                sent_archive_path=COALESCE(excluded.sent_archive_path, outbound_resources.sent_archive_path),
                size_bytes=COALESCE(excluded.size_bytes, outbound_resources.size_bytes),
                sha256=COALESCE(excluded.sha256, outbound_resources.sha256),
                staged_sha256=COALESCE(excluded.staged_sha256, outbound_resources.staged_sha256),
                sent_archive_sha256=COALESCE(excluded.sent_archive_sha256, outbound_resources.sent_archive_sha256),
                status=excluded.status, error=excluded.error,
                sort_order=excluded.sort_order, updated_at=excluded.updated_at
            """,
            (
                resource_id, outbound_id, display_name, mime_type,
                source_path, staged_path, sent_archive_path, size_bytes,
                sha256, staged_sha256, sent_archive_sha256, status, error,
                int(sort_order), now, now,
            ),
        )
        conn.commit()


def replace_outbound_links(
    db_path: Path | str,
    outbound_id: str,
    links: list[dict[str, Any]],
) -> None:
    """保存当前新邮件的显式链接清单。"""
    with _get_conn(db_path) as conn:
        conn.execute("DELETE FROM outbound_links WHERE outbound_id = ?", (outbound_id,))
        for index, link in enumerate(links, 1):
            conn.execute(
                """
                INSERT INTO outbound_links
                    (outbound_id, url, display_text, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    outbound_id,
                    str(link.get("url") or ""),
                    str(link.get("display_text") or ""),
                    int(link.get("sort_order") or index),
                    _now(),
                ),
            )
        conn.commit()


def get_outbound_message(
    db_path: Path | str, outbound_id: str
) -> dict[str, Any] | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE outbound_id = ? LIMIT 1",
            (outbound_id,),
        ).fetchone()
        if row is None:
            return None
        result = _outbound_message_dto(dict(row))
        resources = conn.execute(
            """
            SELECT * FROM outbound_resources
            WHERE outbound_id = ? ORDER BY sort_order ASC, id ASC
            """,
            (outbound_id,),
        ).fetchall()
        links = conn.execute(
            """
            SELECT * FROM outbound_links
            WHERE outbound_id = ? ORDER BY sort_order ASC, id ASC
            """,
            (outbound_id,),
        ).fetchall()
        result["resources"] = [dict(item) for item in resources]
        result["links"] = [dict(item) for item in links]
        return result


def get_outbound_by_request_id(
    db_path: Path | str, request_id: str
) -> dict[str, Any] | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT outbound_id FROM outbound_messages WHERE request_id = ? LIMIT 1",
            (request_id,),
        ).fetchone()
    return get_outbound_message(db_path, str(row["outbound_id"])) if row else None


def query_recent_outbound_messages(
    db_path: Path | str, limit: int = 100
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 500))
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM outbound_messages
            ORDER BY COALESCE(sent_at, created_at) DESC, id DESC LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return [_outbound_message_dto(dict(row)) for row in rows]


def _outbound_message_dto(row: dict[str, Any]) -> dict[str, Any]:
    try:
        recipients = json.loads(str(row.get("to_emails") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        recipients = [str(row.get("to_emails") or "")]
    row["to"] = [str(item) for item in recipients if str(item)]
    row["legacy_limited"] = bool(row.get("legacy_limited"))
    row["attachment_count"] = int(row.get("attachment_count") or 0)
    row["link_count"] = int(row.get("link_count") or 0)
    return row


def backfill_legacy_outbound_messages(db_path: Path | str) -> dict[str, int]:
    """公开的幂等迁移入口，主要用于升级验证。"""
    with _get_conn(db_path) as conn:
        before = int(conn.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0])
        _backfill_legacy_outbound_messages(conn)
        conn.commit()
        after = int(conn.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0])
    return {"migrated": after - before, "total": after}


# ============================================================
# auto_receive_state / receive_retries
# ============================================================

def get_auto_receive_state(db_path: Path | str) -> dict[str, Any]:
    """读取持久化调度状态；首次使用返回健康的默认状态。"""
    with _get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM auto_receive_state WHERE id = 1").fetchone()
        if row is not None:
            return dict(row)
    return {
        "id": 1,
        "enabled": 0,
        "interval_seconds": 60,
        "last_check_at": None,
        "last_success_at": None,
        "last_result": None,
        "last_error": None,
        "consecutive_global_failures": 0,
        "next_check_at": None,
        "checkpoint": None,
        "updated_at": None,
    }


def save_auto_receive_state(
    db_path: Path | str,
    **changes: Any,
) -> dict[str, Any]:
    """以白名单字段更新单行调度状态，跨重启保留真实运行事实。"""
    allowed = {
        "enabled", "interval_seconds", "last_check_at", "last_success_at",
        "last_result", "last_error", "consecutive_global_failures",
        "next_check_at", "checkpoint",
    }
    values = {key: value for key, value in changes.items() if key in allowed}
    current = get_auto_receive_state(db_path)
    current.update(values)
    now = _now()
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO auto_receive_state
                (id, enabled, interval_seconds, last_check_at, last_success_at,
                 last_result, last_error, consecutive_global_failures,
                 next_check_at, checkpoint, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,
                interval_seconds=excluded.interval_seconds,
                last_check_at=excluded.last_check_at,
                last_success_at=excluded.last_success_at,
                last_result=excluded.last_result,
                last_error=excluded.last_error,
                consecutive_global_failures=excluded.consecutive_global_failures,
                next_check_at=excluded.next_check_at,
                checkpoint=excluded.checkpoint,
                updated_at=excluded.updated_at
            """,
            (
                1 if current.get("enabled") else 0,
                max(30, int(current.get("interval_seconds") or 60)),
                current.get("last_check_at"), current.get("last_success_at"),
                current.get("last_result"), current.get("last_error"),
                max(0, int(current.get("consecutive_global_failures") or 0)),
                current.get("next_check_at"), current.get("checkpoint"), now,
            ),
        )
        conn.commit()
    return get_auto_receive_state(db_path)


def get_receive_retry(
    db_path: Path | str,
    backend: str,
    resource_id: str,
    attachment_id: str = "",
) -> dict[str, Any] | None:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM receive_retries
            WHERE backend = ? AND resource_id = ? AND attachment_id = ?
            """,
            (backend, resource_id, attachment_id),
        ).fetchone()
        return dict(row) if row else None


def receive_retry_is_due(
    db_path: Path | str,
    backend: str,
    resource_id: str,
    *,
    attachment_id: str = "",
    now: datetime | None = None,
) -> bool:
    """终态或未到 next_retry_at 的坏资源不会污染每轮轮询。"""
    row = get_receive_retry(db_path, backend, resource_id, attachment_id)
    if row is None:
        return True
    if row.get("terminal_status"):
        return False
    next_retry = row.get("next_retry_at")
    if not next_retry:
        return True
    try:
        due_at = datetime.fromisoformat(str(next_retry))
    except ValueError:
        return True
    return (now or now_local()) >= due_at


def query_due_receive_retries(
    db_path: Path | str,
    backend: str,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return non-terminal retry resources even after they leave the overlap window."""
    now_text = fmt_datetime(now or now_local())
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM receive_retries
            WHERE backend = ? AND terminal_status IS NULL
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY COALESCE(next_retry_at, last_attempt_at) ASC, id ASC
            LIMIT ?
            """,
            (backend, now_text, max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def record_receive_failure(
    db_path: Path | str,
    *,
    backend: str,
    resource_id: str,
    error: str,
    message_id: str | None = None,
    attachment_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """记录单邮件或单附件有限重试，连接级失败不进入此表。"""
    attempted_at = now or now_local()
    previous = get_receive_retry(db_path, backend, resource_id, attachment_id)
    retry_count = int(previous.get("retry_count") or 0) + 1 if previous else 1
    terminal = "needs_attention" if retry_count >= RECEIVE_RETRY_TERMINAL_COUNT else None
    if terminal:
        next_retry_at = None
    else:
        delay_index = min(retry_count - 1, len(RECEIVE_RETRY_DELAYS_SECONDS) - 1)
        next_retry_at = fmt_datetime(
            attempted_at + timedelta(seconds=RECEIVE_RETRY_DELAYS_SECONDS[delay_index])
        )
    attempted_text = fmt_datetime(attempted_at)
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO receive_retries
                (backend, resource_id, message_id, attachment_id, retry_count,
                 last_error, last_attempt_at, next_retry_at, terminal_status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(backend, resource_id, attachment_id) DO UPDATE SET
                message_id=COALESCE(excluded.message_id, receive_retries.message_id),
                retry_count=excluded.retry_count,
                last_error=excluded.last_error,
                last_attempt_at=excluded.last_attempt_at,
                next_retry_at=excluded.next_retry_at,
                terminal_status=excluded.terminal_status,
                updated_at=excluded.updated_at
            """,
            (
                backend, resource_id, message_id, attachment_id, retry_count,
                error[:2000], attempted_text, next_retry_at, terminal,
                attempted_text, attempted_text,
            ),
        )
        if terminal and message_id:
            conn.execute(
                """
                UPDATE mail_packages
                SET archive_status = 'needs_attention', updated_at = ?
                WHERE message_id = ? COLLATE NOCASE OR provider_message_id = ?
                """,
                (attempted_text, message_id, resource_id),
            )
        conn.commit()
    return get_receive_retry(db_path, backend, resource_id, attachment_id) or {}


def clear_receive_retry(
    db_path: Path | str,
    backend: str,
    resource_id: str,
    attachment_id: str = "",
) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM receive_retries WHERE backend = ? AND resource_id = ? AND attachment_id = ?",
            (backend, resource_id, attachment_id),
        )
        conn.commit()


def count_receive_retries(db_path: Path | str) -> dict[str, int]:
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN terminal_status IS NULL THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN terminal_status = 'needs_attention' THEN 1 ELSE 0 END) AS needs_attention
            FROM receive_retries
            """
        ).fetchone()
    return {
        "pending": int(row["pending"] or 0),
        "needs_attention": int(row["needs_attention"] or 0),
    }


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
        cursor = conn.execute(
            """
            INSERT INTO app_events (level, event_type, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (level, event_type, message, now),
        )
        conn.commit()
        event_id = int(cursor.lastrowid or 0)
        if event_id:
            _schedule_app_event_count_prune(conn, db_path)


def query_recent_events(
    db_path: Path | str, limit: int = 50, *, include_daily_checks: bool = True
) -> list[dict[str, Any]]:
    """查询最近 N 条事件。"""
    with _get_conn(db_path) as conn:
        where = "" if include_daily_checks else f" WHERE NOT {_DAILY_CHECK_SQL}"
        rows = conn.execute(
            f"SELECT * FROM app_events{where} ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        # 保持最新事件在前，界面打开即可看到最新日志。
        return [dict(r) for r in rows]


def configure_app_event_retention(db_path: Path | str, *, max_count: int) -> None:
    """登记当前进程使用的硬上限，供事件插入边界检查使用。"""
    _event_retention_limits[str(Path(db_path).resolve())] = max(100, int(max_count))


def query_app_events(
    db_path: Path | str,
    *,
    levels: tuple[str, ...] = (),
    event_types: tuple[str, ...] = (),
    date_from: str | None = None,
    search: str = "",
    include_daily_checks: bool = False,
    limit: int = 150,
    offset: int = 0,
) -> dict[str, Any]:
    """组合筛选技术事件，查询始终分页且不加载全部 app_events。"""
    where: list[str] = []
    params: list[Any] = []
    normalized_levels = tuple(str(item).upper() for item in levels if str(item).strip())
    if normalized_levels:
        placeholders = ",".join("?" for _ in normalized_levels)
        where.append(f"upper(level) IN ({placeholders})")
        params.extend(normalized_levels)
    normalized_types = tuple(str(item).lower() for item in event_types if str(item).strip())
    if normalized_types:
        placeholders = ",".join("?" for _ in normalized_types)
        where.append(f"lower(event_type) IN ({placeholders})")
        params.extend(normalized_types)
    if date_from:
        where.append("created_at >= ?")
        params.append(str(date_from))
    keyword = " ".join(str(search or "").split())
    if keyword:
        for token in keyword.split(" "):
            pattern = f"%{token}%"
            where.append(
                "(message LIKE ? COLLATE NOCASE OR event_type LIKE ? COLLATE NOCASE "
                "OR level LIKE ? COLLATE NOCASE)"
            )
            params.extend((pattern, pattern, pattern))
    if not include_daily_checks:
        where.append(f"NOT {_DAILY_CHECK_SQL}")
    clause = " WHERE " + " AND ".join(where) if where else ""
    safe_limit = min(500, max(1, int(limit)))
    safe_offset = max(0, int(offset))
    with _get_conn(db_path) as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM app_events{clause}", params
        ).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM app_events{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, safe_limit, safe_offset),
        ).fetchall()
    return {"events": [dict(row) for row in rows], "total": total}


def app_event_overview(
    db_path: Path | str,
    *,
    normal_days: int = DEFAULT_NORMAL_EVENT_RETENTION_DAYS,
    error_days: int = DEFAULT_ERROR_EVENT_RETENTION_DAYS,
) -> dict[str, Any]:
    now = now_local()
    today = now.strftime("%Y-%m-%d")
    normal_cutoff = fmt_datetime(now - timedelta(days=max(1, int(normal_days))))
    error_cutoff = fmt_datetime(now - timedelta(days=max(1, int(error_days))))
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN created_at >= ? AND upper(level) IN ('WARNING', 'ERROR', 'FAILED') THEN 1 ELSE 0 END) AS today_errors,
                SUM(CASE WHEN """ + _DAILY_CHECK_SQL + """ THEN 1 ELSE 0 END) AS daily_checks,
                SUM(CASE
                    WHEN upper(level) IN ('WARNING', 'ERROR', 'FAILED') AND created_at < ? THEN 1
                    WHEN upper(level) NOT IN ('WARNING', 'ERROR', 'FAILED') AND created_at < ? THEN 1
                    ELSE 0 END) AS expired
            FROM app_events
            """,
            (today, error_cutoff, normal_cutoff),
        ).fetchone()
        last_cleanup = conn.execute(
            "SELECT created_at, message FROM app_events "
            "WHERE event_type = 'log_maintenance' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "today_errors": int(row["today_errors"] or 0),
        "daily_checks": int(row["daily_checks"] or 0),
        "expired": int(row["expired"] or 0),
        "last_cleanup_at": last_cleanup["created_at"] if last_cleanup else None,
    }


def prune_app_events(
    db_path: Path | str,
    *,
    normal_days: int = DEFAULT_NORMAL_EVENT_RETENTION_DAYS,
    error_days: int = DEFAULT_ERROR_EVENT_RETENTION_DAYS,
    max_count: int = DEFAULT_APP_EVENT_MAX_COUNT,
    now: datetime | None = None,
) -> dict[str, int]:
    """仅清理 app_events：先按时间，再按硬上限批量降到 80%。"""
    moment = now or now_local()
    normal_cutoff = fmt_datetime(moment - timedelta(days=max(1, int(normal_days))))
    error_cutoff = fmt_datetime(moment - timedelta(days=max(1, int(error_days))))
    safe_max = max(100, int(max_count))
    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = int(conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0])
        cursor = conn.execute(
            """
            DELETE FROM app_events
            WHERE (
                upper(level) IN ('WARNING', 'ERROR', 'FAILED') AND created_at < ?
            ) OR (
                upper(level) NOT IN ('WARNING', 'ERROR', 'FAILED') AND created_at < ?
            )
            """,
            (error_cutoff, normal_cutoff),
        )
        deleted_by_age = max(0, int(cursor.rowcount or 0))
        remaining = int(conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0])
        deleted_by_count = 0
        if remaining > safe_max:
            target = max(1, int(safe_max * APP_EVENT_TARGET_RATIO))
            deleted_by_count = remaining - target
            conn.execute(
                "DELETE FROM app_events WHERE id IN ("
                "SELECT id FROM app_events ORDER BY id ASC LIMIT ?)",
                (deleted_by_count,),
            )
        after = int(conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0])
        conn.commit()
    return {
        "before": before,
        "deleted_by_age": deleted_by_age,
        "deleted_by_count": deleted_by_count,
        "deleted": before - after,
        "after": after,
    }


def clear_daily_check_events(db_path: Path | str) -> int:
    with _get_conn(db_path) as conn:
        cursor = conn.execute(f"DELETE FROM app_events WHERE {_DAILY_CHECK_SQL}")
        conn.commit()
        return max(0, int(cursor.rowcount or 0))


def clear_all_app_events(db_path: Path | str) -> int:
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM app_events")
        conn.commit()
        return max(0, int(cursor.rowcount or 0))


def _schedule_app_event_count_prune(
    conn: sqlite3.Connection, db_path: Path | str
) -> None:
    """超限时只调度后台批量删除，日志写入线程不承担大事务。"""
    key = str(Path(db_path).resolve())
    safe_max = _event_retention_limits.get(key, DEFAULT_APP_EVENT_MAX_COUNT)
    count = int(conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0])
    if count <= safe_max:
        return
    if not _event_limit_lock.acquire(blocking=False):
        return

    def run() -> None:
        try:
            target = max(1, int(safe_max * APP_EVENT_TARGET_RATIO))
            with _get_conn(db_path) as background_conn:
                background_conn.execute("BEGIN IMMEDIATE")
                current = int(
                    background_conn.execute(
                        "SELECT COUNT(*) FROM app_events"
                    ).fetchone()[0]
                )
                if current > safe_max:
                    background_conn.execute(
                        "DELETE FROM app_events WHERE id IN ("
                        "SELECT id FROM app_events ORDER BY id ASC LIMIT ?)",
                        (current - target,),
                    )
                background_conn.commit()
        finally:
            _event_limit_lock.release()

    threading.Thread(
        target=run,
        name="AgentMailBridgeEventCap",
        daemon=True,
    ).start()
