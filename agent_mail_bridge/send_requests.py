"""Durable Agent 发件请求、幂等状态机和发送事实写入。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.database import get_connection


TERMINAL_SEND_STATUSES = {
    "sent",
    "sent_reconciled",
    "sent_archive_failed",
    "failed",
    "delivery_unknown",
    "definitely_not_sent",
    "cancelled",
    "expired",
}

RECOVERY_SEND_STATUSES = {
    "sending",
    "smtp_accepted",
    "sent_archive_pending",
    "sent_waiting_reconciliation",
    "sent_archive_failed",
    "delivery_unknown",
    "recovery_required",
}

DEFAULT_SEND_LEASE_SECONDS = 120


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
                 body_html, status, current_stage, expires_at, message_id, created_at,
                 in_reply_to_raw, references_raw, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "pending_confirmation" if status == "pending_confirmation" else "ready",
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
    lease = connection.execute(
        "SELECT * FROM send_execution_leases WHERE send_request_id=?",
        (send_request_id,),
    ).fetchone()
    result["lease"] = dict(lease) if lease else None
    return result


def send_lease_is_active(
    request: dict[str, Any], *, now: str | None = None
) -> bool:
    lease = request.get("lease") or {}
    owner = str(lease.get("lease_owner") or "")
    expires_at = str(lease.get("lease_expires_at") or "")
    current = str(now or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return bool(
        owner
        and not owner.startswith("recovery_")
        and expires_at
        and expires_at >= current
    )


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


def expire_send_request_if_due(
    db_path: Path | str, send_request_id: str
) -> dict[str, Any]:
    """原子结束一个确已过期的待确认请求，不影响其他请求。"""
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
            WHERE send_request_id=?
              AND status='pending_confirmation'
              AND expires_at IS NOT NULL
              AND expires_at < ?
            """,
            (now, now, send_request_id, now),
        )
        exists = connection.execute(
            "SELECT 1 FROM send_requests WHERE send_request_id=?",
            (send_request_id,),
        ).fetchone()
        if exists is None:
            raise SendRequestError(
                "send_request_not_found", "发件请求不存在"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "changed": cursor.rowcount == 1,
        "request": get_send_request(db_path, send_request_id),
    }


def claim_for_send(
    db_path: Path | str,
    send_request_id: str,
    *,
    confirmed_by: str | None = None,
    lease_owner: str | None = None,
    process_id: str | None = None,
    lease_seconds: int = DEFAULT_SEND_LEASE_SECONDS,
) -> tuple[dict[str, Any], bool]:
    """用 BEGIN IMMEDIATE 原子取得唯一 SMTP 执行租约。"""
    connection = get_connection(db_path)
    now = _now(connection)
    owner = str(lease_owner or f"lease_{uuid.uuid4().hex}")
    pid = str(process_id or os.getpid())
    lease_expires_at = _future_time(lease_seconds)
    attempt_no = 0
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
        lease = connection.execute(
            "SELECT * FROM send_execution_leases WHERE send_request_id=?",
            (send_request_id,),
        ).fetchone()
        if status == "sending" and lease is not None:
            lease_row = dict(lease)
            if str(lease_row.get("lease_expires_at") or "") >= now:
                connection.commit()
                result = get_send_request(db_path, send_request_id)
                if result is None:
                    raise SendRequestError(
                        "send_request_missing", "发件请求状态不可用"
                    )
                return result, False
            attempt_no = int(lease_row.get("attempt_no") or 0)
            connection.execute(
                """
                UPDATE send_requests
                SET status='recovery_required', recovery_required=1,
                    current_stage='stale_lease',
                    error_code='stale_send_lease',
                    error_message='上一次发送执行意外中断，需要先恢复或对账',
                    updated_at=?
                WHERE send_request_id=? AND status='sending'
                """,
                (now, send_request_id),
            )
            connection.execute(
                """
                UPDATE send_execution_leases
                SET lease_owner=?, process_id=?, heartbeat_at=?,
                    lease_expires_at=?, updated_at=?
                WHERE send_request_id=?
                """,
                (
                    f"recovery_{owner}",
                    pid,
                    now,
                    now,
                    now,
                    send_request_id,
                ),
            )
            _insert_send_event(
                connection,
                send_request_id=send_request_id,
                attempt_no=attempt_no,
                stage="stale_lease",
                outcome="recovery_required",
                now=now,
            )
            connection.commit()
            result = get_send_request(db_path, send_request_id)
            if result is None:
                raise SendRequestError(
                    "send_request_missing", "发件请求状态不可用"
                )
            return result, False
        if status in TERMINAL_SEND_STATUSES or status in RECOVERY_SEND_STATUSES:
            connection.commit()
            result = get_send_request(db_path, send_request_id)
            if result is None:
                raise SendRequestError(
                    "send_request_missing", "发件请求状态不可用"
                )
            return result, False
        request_expires_at = str(current.get("expires_at") or "")
        if request_expires_at and request_expires_at < now:
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
        attempt_no = int(
            connection.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 "
                "FROM send_attempt_events WHERE send_request_id=?",
                (send_request_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE send_requests
            SET status='sending',
                confirmed_at=CASE WHEN ? = 'gui' THEN ? ELSE confirmed_at END,
                confirmed_by=CASE WHEN ? = 'gui' THEN 'gui' ELSE confirmed_by END,
                current_stage='lease_acquired',
                recovery_required=0,
                error_code=NULL,
                error_message=NULL,
                updated_at=?
            WHERE send_request_id=?
            """,
            (confirmed_by, now, confirmed_by, now, send_request_id),
        )
        connection.execute(
            """
            INSERT INTO send_execution_leases
                (send_request_id, lease_owner, process_id, acquired_at,
                 heartbeat_at, lease_expires_at, attempt_no, current_stage,
                 fixed_message_id, fixed_mime_sha256, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'lease_acquired', ?, ?, ?)
            ON CONFLICT(send_request_id) DO UPDATE SET
                lease_owner=excluded.lease_owner,
                process_id=excluded.process_id,
                acquired_at=excluded.acquired_at,
                heartbeat_at=excluded.heartbeat_at,
                lease_expires_at=excluded.lease_expires_at,
                attempt_no=excluded.attempt_no,
                current_stage=excluded.current_stage,
                fixed_message_id=excluded.fixed_message_id,
                fixed_mime_sha256=excluded.fixed_mime_sha256,
                updated_at=excluded.updated_at
            """,
            (
                send_request_id,
                owner,
                pid,
                now,
                now,
                lease_expires_at,
                attempt_no,
                str(current.get("message_id") or ""),
                current.get("raw_eml_sha256"),
                now,
            ),
        )
        _insert_send_event(
            connection,
            send_request_id=send_request_id,
            attempt_no=attempt_no,
            stage="lease_acquired",
            outcome="success",
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    result = get_send_request(db_path, send_request_id)
    if result is None:
        raise SendRequestError("send_request_missing", "发件请求状态不可用")
    result["_lease_owner"] = owner
    result["_attempt_no"] = attempt_no
    return result, True


def record_send_stage(
    db_path: Path | str,
    send_request_id: str,
    *,
    lease_owner: str,
    stage: str,
    outcome: str = "success",
    lease_seconds: int = DEFAULT_SEND_LEASE_SECONDS,
) -> None:
    """更新轻量心跳与确定性阶段；租约 ownership 不匹配时拒绝。"""
    connection = get_connection(db_path)
    now = _now(connection)
    expires_at = _future_time(lease_seconds)
    try:
        connection.execute("BEGIN IMMEDIATE")
        lease = connection.execute(
            "SELECT * FROM send_execution_leases WHERE send_request_id=?",
            (send_request_id,),
        ).fetchone()
        if (
            lease is None
            or str(lease["lease_owner"]) != lease_owner
            or str(lease["lease_expires_at"] or "") < now
        ):
            raise SendRequestError(
                "send_lease_lost", "发件执行租约已失效，已停止继续发送"
            )
        previous_stage = str(lease["current_stage"] or "")
        attempt_no = int(lease["attempt_no"] or 0)
        smtp_attempt = stage == "smtp_data_started" and previous_stage not in {
            "smtp_data_started",
            "smtp_accepted",
            "archive_pending",
            "archive_complete",
        }
        accepted = stage == "smtp_accepted"
        connection.execute(
            """
            UPDATE send_execution_leases
            SET heartbeat_at=?, lease_expires_at=?, current_stage=?,
                fixed_mime_sha256=COALESCE(
                    (SELECT raw_eml_sha256 FROM send_requests
                     WHERE send_request_id=?), fixed_mime_sha256),
                updated_at=?
            WHERE send_request_id=? AND lease_owner=?
            """,
            (
                now,
                expires_at,
                stage,
                send_request_id,
                now,
                send_request_id,
                lease_owner,
            ),
        )
        connection.execute(
            """
            UPDATE send_requests
            SET current_stage=?,
                smtp_attempt_count=smtp_attempt_count + ?,
                status=CASE WHEN ? THEN 'smtp_accepted' ELSE status END,
                smtp_accepted_at=CASE WHEN ? THEN COALESCE(smtp_accepted_at, ?)
                                      ELSE smtp_accepted_at END,
                delivery_status=CASE WHEN ? THEN 'smtp_accepted'
                                     ELSE delivery_status END,
                recovery_required=CASE WHEN ? THEN 1 ELSE recovery_required END,
                sent_reconciliation_status=CASE WHEN ? THEN 'waiting'
                    ELSE sent_reconciliation_status END,
                updated_at=?
            WHERE send_request_id=?
            """,
            (
                stage,
                1 if smtp_attempt else 0,
                1 if accepted else 0,
                1 if accepted else 0,
                now,
                1 if accepted else 0,
                1 if accepted else 0,
                1 if accepted else 0,
                now,
                send_request_id,
            ),
        )
        _insert_send_event(
            connection,
            send_request_id=send_request_id,
            attempt_no=attempt_no,
            stage=stage,
            outcome=outcome,
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def release_send_lease(
    db_path: Path | str,
    send_request_id: str,
    *,
    lease_owner: str | None = None,
    outcome: str = "completed",
) -> None:
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        lease = connection.execute(
            "SELECT lease_owner, attempt_no, current_stage "
            "FROM send_execution_leases WHERE send_request_id=?",
            (send_request_id,),
        ).fetchone()
        if lease is not None and (
            lease_owner is None or str(lease["lease_owner"]) == lease_owner
        ):
            _insert_send_event(
                connection,
                send_request_id=send_request_id,
                attempt_no=int(lease["attempt_no"] or 0),
                stage=str(lease["current_stage"] or "lease_released"),
                outcome=outcome,
                now=now,
            )
            connection.execute(
                "DELETE FROM send_execution_leases WHERE send_request_id=?",
                (send_request_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


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
    lease_owner: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_SEND_STATUSES:
        raise ValueError("发件请求终态无效")
    connection = get_connection(db_path)
    now = _now(connection)
    recovery_required = status in {"delivery_unknown", "sent_archive_failed"}
    reconciliation_status = (
        "matched"
        if status == "sent_reconciled"
        else "waiting"
        if status in {"sent", "sent_archive_failed"}
        else "unknown"
        if status == "delivery_unknown"
        else "resolved_not_sent"
        if status == "definitely_not_sent"
        else "not_started"
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        lease = connection.execute(
            "SELECT lease_owner, attempt_no FROM send_execution_leases "
            "WHERE send_request_id=?",
            (send_request_id,),
        ).fetchone()
        if lease_owner is not None and (
            lease is None or str(lease["lease_owner"]) != lease_owner
        ):
            raise SendRequestError(
                "send_lease_lost", "发件执行租约已失效，拒绝写入终态"
            )
        connection.execute(
            """
            UPDATE send_requests
            SET status=?, current_stage=?, recovery_required=?,
                sent_reconciliation_status=?, delivery_status=?,
                provider_result=?, error_code=?, error_message=?,
                outbound_id=COALESCE(?, outbound_id),
                package_id=COALESCE(?, package_id), completed_at=?, updated_at=?
            WHERE send_request_id=?
            """,
            (
                status,
                status,
                1 if recovery_required else 0,
                reconciliation_status,
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
        if lease is not None:
            _insert_send_event(
                connection,
                send_request_id=send_request_id,
                attempt_no=int(lease["attempt_no"] or 0),
                stage=status,
                outcome="completed" if not recovery_required else "needs_recovery",
                now=now,
            )
            connection.execute(
                "DELETE FROM send_execution_leases WHERE send_request_id=?",
                (send_request_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
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
    lease_owner: str | None = None,
) -> None:
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if lease_owner:
            lease = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM send_execution_leases "
                "WHERE send_request_id=?",
                (send_request_id,),
            ).fetchone()
            if (
                lease is None
                or str(lease["lease_owner"]) != lease_owner
                or str(lease["lease_expires_at"] or "") < now
            ):
                raise SendRequestError(
                    "send_lease_lost", "MIME 固化时发件租约已失效"
                )
        connection.execute(
            """
            UPDATE send_requests
            SET raw_eml_path=?, raw_eml_sha256=?, mime_built_at=?,
                current_stage='mime_built', updated_at=?
            WHERE send_request_id=?
            """,
            (raw_eml_path, raw_eml_sha256, now, now, send_request_id),
        )
        if lease_owner:
            cursor = connection.execute(
                """
                UPDATE send_execution_leases
                SET fixed_mime_sha256=?, current_stage='mime_built',
                    heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE send_request_id=? AND lease_owner=?
                """,
                (
                    raw_eml_sha256,
                    now,
                    _future_time(DEFAULT_SEND_LEASE_SECONDS),
                    now,
                    send_request_id,
                    lease_owner,
                ),
            )
            if cursor.rowcount != 1:
                raise SendRequestError(
                    "send_lease_lost", "MIME 固化时发件租约已失效"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


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
            "current_stage",
            "recovery_required",
            "smtp_accepted_at",
            "sent_reconciliation_status",
            "last_reconciled_at",
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


def _future_time(seconds: int) -> str:
    value = datetime.now() + timedelta(
        seconds=max(30, min(int(seconds), 3600))
    )
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _insert_send_event(
    connection: Any,
    *,
    send_request_id: str,
    attempt_no: int,
    stage: str,
    outcome: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO send_attempt_events
            (event_id, send_request_id, attempt_no, stage, outcome,
             details_json, created_at)
        VALUES (?, ?, ?, ?, ?, '{}', ?)
        """,
        (
            f"sevt_{uuid.uuid4().hex}",
            send_request_id,
            max(0, int(attempt_no)),
            str(stage or "unknown")[:80],
            str(outcome or "unknown")[:40],
            now,
        ),
    )
