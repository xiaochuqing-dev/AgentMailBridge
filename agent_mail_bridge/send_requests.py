"""Durable Agent 发件请求、幂等状态机和发送事实写入。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.database import get_connection


TERMINAL_SEND_STATUSES = {
    "sent",
    "sent_archive_failed",
    "failed",
    "delivery_unknown",
    "cancelled",
    "expired",
}


class SendRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def new_send_request_id() -> str:
    return f"send_{uuid.uuid4().hex}"


def get_by_idempotency(
    db_path: Path | str, client_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = get_connection(db_path).execute(
        "SELECT send_request_id FROM send_requests "
        "WHERE client_id = ? AND idempotency_key = ?",
        (client_id, idempotency_key),
    ).fetchone()
    return get_send_request(db_path, str(row[0])) if row else None


def create_send_request(
    db_path: Path | str,
    *,
    send_request_id: str,
    client_id: str,
    idempotency_key: str,
    operation: str,
    sender_account_id: str,
    source_package_id: str | None,
    reply_to_package_id: str | None,
    forward_from_package_id: str | None,
    send_mode: str,
    subject: str,
    body_text: str,
    body_html: str,
    status: str,
    expires_at: str | None,
    message_id: str,
    in_reply_to_raw: str = "",
    references_raw: str = "",
    recipients: Iterable[dict[str, Any]],
    attachments: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """创建请求；同一 Client + key 已存在时返回原请求。"""
    connection = get_connection(db_path)
    now = _now(connection)
    recipient_rows = [dict(item) for item in recipients]
    attachment_rows = [dict(item) for item in attachments]
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT send_request_id FROM send_requests "
            "WHERE client_id = ? AND idempotency_key = ?",
            (client_id, idempotency_key),
        ).fetchone()
        if existing:
            connection.commit()
            result = get_send_request(db_path, str(existing[0]))
            if result is None:
                raise SendRequestError(
                    "send_request_missing", "幂等请求记录不可用"
                )
            return result, False
        connection.execute(
            """
            INSERT INTO send_requests
                (send_request_id, client_id, idempotency_key, operation,
                 sender_account_id, source_package_id, reply_to_package_id,
                 forward_from_package_id, send_mode, subject, body_text,
                 body_html, status, expires_at, message_id, created_at,
                 in_reply_to_raw, references_raw, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                send_request_id,
                client_id,
                idempotency_key,
                operation,
                sender_account_id,
                source_package_id,
                reply_to_package_id,
                forward_from_package_id,
                send_mode,
                subject,
                body_text,
                body_html,
                status,
                expires_at,
                message_id,
                now,
                in_reply_to_raw or None,
                references_raw or None,
                now,
            ),
        )
        for order, item in enumerate(recipient_rows, 1):
            connection.execute(
                """
                INSERT INTO send_request_recipients
                    (send_request_id, recipient_type, display_name,
                     email_address, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    send_request_id,
                    str(item["recipient_type"]),
                    str(item.get("display_name") or ""),
                    str(item["email_address"]),
                    int(item.get("sort_order") or order),
                    now,
                ),
            )
        for order, item in enumerate(attachment_rows, 1):
            connection.execute(
                """
                INSERT INTO send_request_attachments
                    (attachment_id, send_request_id, source_kind,
                     source_package_id, source_resource_id, source_path,
                     snapshot_path, display_name, mime_type, size_bytes,
                     sha256, status, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item["attachment_id"]),
                    send_request_id,
                    str(item["source_kind"]),
                    item.get("source_package_id"),
                    item.get("source_resource_id"),
                    item.get("source_path"),
                    str(item["snapshot_path"]),
                    str(item["display_name"]),
                    str(item.get("mime_type") or "application/octet-stream"),
                    int(item["size_bytes"]),
                    str(item["sha256"]),
                    str(item.get("status") or "ready"),
                    int(item.get("sort_order") or order),
                    now,
                    now,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    result = get_send_request(db_path, send_request_id)
    if result is None:
        raise SendRequestError("send_request_missing", "发件请求创建后不可用")
    return result, True


def get_send_request(
    db_path: Path | str, send_request_id: str
) -> dict[str, Any] | None:
    connection = get_connection(db_path)
    row = connection.execute(
        "SELECT * FROM send_requests WHERE send_request_id = ?",
        (send_request_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["recipients"] = [
        dict(item)
        for item in connection.execute(
            "SELECT recipient_type, display_name, email_address, sort_order "
            "FROM send_request_recipients WHERE send_request_id = ? "
            "ORDER BY sort_order, id",
            (send_request_id,),
        ).fetchall()
    ]
    result["attachments"] = [
        dict(item)
        for item in connection.execute(
            "SELECT * FROM send_request_attachments WHERE send_request_id = ? "
            "ORDER BY sort_order, id",
            (send_request_id,),
        ).fetchall()
    ]
    return result


def list_send_requests(
    db_path: Path | str,
    *,
    client_id: str | None = None,
    statuses: Iterable[str] = (),
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    normalized_statuses = sorted(
        {str(value) for value in statuses if str(value)}
    )
    if normalized_statuses:
        where.append(
            "status IN (" + ",".join("?" for _ in normalized_statuses) + ")"
        )
        params.extend(normalized_statuses)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = get_connection(db_path).execute(
        f"SELECT send_request_id FROM send_requests{clause} "
        "ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit), 500))),
    ).fetchall()
    return [
        item
        for item in (
            get_send_request(db_path, str(row[0])) for row in rows
        )
        if item is not None
    ]


def expire_due_send_requests(db_path: Path | str) -> int:
    """Persist terminal expiry before pending requests are displayed or queried."""
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE send_requests
            SET status='expired',
                error_code='request_expired',
                error_message='待确认发件请求已过期',
                completed_at=?,
                updated_at=?
            WHERE status='pending_confirmation'
              AND expires_at IS NOT NULL
              AND expires_at < ?
            """,
            (now, now, now),
        )
        connection.commit()
        return max(0, int(cursor.rowcount))
    except Exception:
        connection.rollback()
        raise


def claim_for_send(
    db_path: Path | str,
    send_request_id: str,
    *,
    confirmed_by: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """原子取得唯一 SMTP 执行权；重复确认或重试不会再次发送。"""
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM send_requests WHERE send_request_id = ?",
            (send_request_id,),
        ).fetchone()
        if row is None:
            raise SendRequestError("send_request_not_found", "发件请求不存在")
        current = dict(row)
        status = str(current["status"])
        if status in TERMINAL_SEND_STATUSES or status == "sending":
            connection.commit()
            result = get_send_request(db_path, send_request_id)
            if result is None:
                raise SendRequestError(
                    "send_request_missing", "发件请求状态不可用"
                )
            return result, False
        expires_at = str(current.get("expires_at") or "")
        if expires_at and expires_at < now:
            connection.execute(
                "UPDATE send_requests SET status='expired', error_code='request_expired', "
                "error_message='待确认发件请求已过期', completed_at=?, updated_at=? "
                "WHERE send_request_id=?",
                (now, now, send_request_id),
            )
            connection.commit()
            result = get_send_request(db_path, send_request_id)
            if result is None:
                raise SendRequestError(
                    "send_request_missing", "过期发件请求不可用"
                )
            return result, False
        mode = str(current.get("send_mode") or "")
        expected = "pending_confirmation" if mode == "confirm" else "ready_to_send"
        if status != expected:
            raise SendRequestError(
                "invalid_send_state", f"当前状态不允许发送：{status}"
            )
        if mode == "confirm" and confirmed_by != "gui":
            raise SendRequestError(
                "gui_confirmation_required", "只能在 AgentMailBridge GUI 中确认发送"
            )
        connection.execute(
            """
            UPDATE send_requests
            SET status='sending',
                confirmed_at=CASE WHEN ? = 'gui' THEN ? ELSE confirmed_at END,
                confirmed_by=CASE WHEN ? = 'gui' THEN 'gui' ELSE confirmed_by END,
                smtp_attempt_count=smtp_attempt_count + 1,
                updated_at=?
            WHERE send_request_id=?
            """,
            (confirmed_by, now, confirmed_by, now, send_request_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    result = get_send_request(db_path, send_request_id)
    if result is None:
        raise SendRequestError("send_request_missing", "发件请求状态不可用")
    return result, True


def cancel_send_request(
    db_path: Path | str, send_request_id: str
) -> dict[str, Any]:
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM send_requests WHERE send_request_id = ?",
            (send_request_id,),
        ).fetchone()
        if row is None:
            raise SendRequestError("send_request_not_found", "发件请求不存在")
        status = str(row[0])
        if status == "cancelled":
            connection.commit()
        elif status != "pending_confirmation":
            raise SendRequestError(
                "invalid_send_state", "只有待确认请求可以取消"
            )
        else:
            connection.execute(
                "UPDATE send_requests SET status='cancelled', cancelled_at=?, "
                "completed_at=?, updated_at=? WHERE send_request_id=?",
                (now, now, now, send_request_id),
            )
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    result = get_send_request(db_path, send_request_id)
    if result is None:
        raise SendRequestError("send_request_missing", "发件请求状态不可用")
    return result


def complete_send_request(
    db_path: Path | str,
    send_request_id: str,
    *,
    status: str,
    delivery_status: str | None = None,
    provider_result: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    outbound_id: str | None = None,
    package_id: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_SEND_STATUSES:
        raise ValueError("发件请求终态无效")
    connection = get_connection(db_path)
    now = _now(connection)
    connection.execute(
        """
        UPDATE send_requests
        SET status=?, delivery_status=?, provider_result=?, error_code=?,
            error_message=?, outbound_id=COALESCE(?, outbound_id),
            package_id=COALESCE(?, package_id), completed_at=?, updated_at=?
        WHERE send_request_id=?
        """,
        (
            status,
            delivery_status,
            provider_result,
            error_code,
            error_message,
            outbound_id,
            package_id,
            now,
            now,
            send_request_id,
        ),
    )
    connection.commit()
    result = get_send_request(db_path, send_request_id)
    if result is None:
        raise SendRequestError("send_request_not_found", "发件请求不存在")
    return result


def record_send_mime(
    db_path: Path | str,
    send_request_id: str,
    *,
    raw_eml_path: str,
    raw_eml_sha256: str,
) -> None:
    connection = get_connection(db_path)
    now = _now(connection)
    connection.execute(
        """
        UPDATE send_requests
        SET raw_eml_path=?, raw_eml_sha256=?, mime_built_at=?, updated_at=?
        WHERE send_request_id=?
        """,
        (raw_eml_path, raw_eml_sha256, now, now, send_request_id),
    )
    connection.commit()


def record_outbound_fact(
    db_path: Path | str,
    *,
    outbound_id: str,
    request: dict[str, Any],
    sender_ref: str,
    account_ref: str,
    to_emails: Iterable[str],
    cc_emails: Iterable[str],
    bcc_emails: Iterable[str],
    raw_eml_sha256: str,
    package_id: str | None,
    status: str,
    sent_at: str | None,
    error: str | None = None,
) -> None:
    now = _now(get_connection(db_path))
    connection = get_connection(db_path)
    recipients_to = ", ".join(to_emails)
    recipients_cc = ", ".join(cc_emails)
    recipients_bcc = ", ".join(bcc_emails)
    connection.execute(
        """
        INSERT INTO outbound_messages
            (outbound_id, sender_account_ref, from_account_id, sender_ref,
             source_origin, request_id, client_id, send_mode, operation,
             message_id, package_id, subject, body_text, body_html, to_emails,
             cc_emails, bcc_emails, status, error, attachment_count,
             link_count, legacy_limited, raw_eml_sha256, confirmation_at,
             reply_to_package_id, forward_from_package_id, created_at,
             sent_at, updated_at)
        VALUES (?, ?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(outbound_id) DO UPDATE SET
            package_id=COALESCE(excluded.package_id, outbound_messages.package_id),
            status=excluded.status,
            error=excluded.error,
            raw_eml_sha256=excluded.raw_eml_sha256,
            sent_at=COALESCE(excluded.sent_at, outbound_messages.sent_at),
            updated_at=excluded.updated_at
        """,
        (
            outbound_id,
            account_ref,
            str(request["sender_account_id"]),
            sender_ref,
            str(request["send_request_id"]),
            str(request["client_id"]),
            str(request["send_mode"]),
            str(request["operation"]),
            str(request.get("message_id") or ""),
            package_id,
            str(request.get("subject") or ""),
            str(request.get("body_text") or ""),
            str(request.get("body_html") or ""),
            recipients_to,
            recipients_cc,
            recipients_bcc,
            status,
            error,
            len(request.get("attachments") or []),
            raw_eml_sha256,
            request.get("confirmed_at"),
            request.get("reply_to_package_id"),
            request.get("forward_from_package_id"),
            str(request.get("created_at") or now),
            sent_at,
            now,
        ),
    )
    connection.commit()


def public_send_request(row: dict[str, Any]) -> dict[str, Any]:
    """MCP/审计安全 DTO：保留事实，不返回本机原始路径。"""
    recipients: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
    for item in row.get("recipients") or []:
        kind = str(item.get("recipient_type") or "")
        if kind in recipients:
            recipients[kind].append(str(item.get("email_address") or ""))
    return {
        key: row.get(key)
        for key in (
            "send_request_id",
            "idempotency_key",
            "operation",
            "sender_account_id",
            "source_package_id",
            "reply_to_package_id",
            "forward_from_package_id",
            "send_mode",
            "subject",
            "body_text",
            "body_html",
            "status",
            "expires_at",
            "confirmed_at",
            "cancelled_at",
            "smtp_attempt_count",
            "delivery_status",
            "error_code",
            "error_message",
            "message_id",
            "in_reply_to_raw",
            "references_raw",
            "raw_eml_sha256",
            "mime_built_at",
            "outbound_id",
            "package_id",
            "created_at",
            "updated_at",
            "completed_at",
        )
    } | {
        "recipients": recipients,
        "attachments": [
            {
                "attachment_id": item.get("attachment_id"),
                "source_kind": item.get("source_kind"),
                "source_package_id": item.get("source_package_id"),
                "source_resource_id": item.get("source_resource_id"),
                "display_name": item.get("display_name"),
                "mime_type": item.get("mime_type"),
                "size_bytes": item.get("size_bytes"),
                "sha256": item.get("sha256"),
                "status": item.get("status"),
            }
            for item in row.get("attachments") or []
        ],
    }


def _now(connection) -> str:
    row = connection.execute(
        "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
    ).fetchone()
    return str(row[0])
