"""Mailbox 级权威 checkpoint 与失败隔离。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_mail_bridge.database import get_connection


SUCCESS_RESULTS = {"success", "no_changes", "partial"}


def begin_mailbox_attempt(
    db_path: Path | str,
    *,
    mailbox_id: str,
    account_id: str,
    attempt: dict[str, Any] | None = None,
) -> None:
    """轻量登记一次尝试，不触碰最后成功 checkpoint。"""
    connection = get_connection(db_path)
    now = _now(connection)
    payload = _json(attempt or {"stage": "starting"})
    connection.execute(
        """
        INSERT INTO mailbox_sync_states
            (mailbox_id, account_id, last_uid, checkpoint_json,
             current_attempt_json, last_check_at, last_attempt_at,
             last_result, updated_at)
        VALUES (?, ?, 0, '{}', ?, ?, ?, 'running', ?)
        ON CONFLICT(mailbox_id) DO UPDATE SET
            current_attempt_json=excluded.current_attempt_json,
            last_check_at=excluded.last_check_at,
            last_attempt_at=excluded.last_attempt_at,
            last_result='running',
            updated_at=excluded.updated_at
        """,
        (mailbox_id, account_id, payload, now, now, now),
    )
    connection.commit()


def finish_mailbox_attempt(
    db_path: Path | str,
    *,
    mailbox_id: str,
    account_id: str,
    uidvalidity: int,
    uidnext: int,
    highestmodseq: int,
    last_uid: int,
    checkpoint: dict[str, Any],
    result: str,
    error: str | None = None,
    current_attempt: dict[str, Any] | None = None,
    server_snapshot: dict[str, Any] | None = None,
    reconciliation_required: bool = False,
    reconciliation_completed: bool = False,
    uidvalidity_changed: bool = False,
    full_rescan_cursor: str | None = None,
) -> None:
    """成功才推进游标；失败仅记录错误和连续失败次数。"""
    connection = get_connection(db_path)
    now = _now(connection)
    succeeded = result in SUCCESS_RESULTS
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO mailbox_sync_states
                (mailbox_id, account_id, uidvalidity, uidnext, highestmodseq,
                 last_uid, checkpoint_json, current_attempt_json,
                 last_check_at, last_attempt_at, last_success_at,
                 last_error_at, consecutive_failures,
                 reconciliation_required, full_rescan_cursor,
                 server_snapshot_json, uidvalidity_changed_at,
                 last_result, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?)
            ON CONFLICT(mailbox_id) DO UPDATE SET
                uidvalidity=CASE WHEN ? THEN excluded.uidvalidity
                                 ELSE mailbox_sync_states.uidvalidity END,
                uidnext=CASE WHEN ? THEN excluded.uidnext
                             ELSE mailbox_sync_states.uidnext END,
                highestmodseq=CASE WHEN ? THEN excluded.highestmodseq
                                   ELSE mailbox_sync_states.highestmodseq END,
                last_uid=CASE WHEN ? THEN excluded.last_uid
                              ELSE mailbox_sync_states.last_uid END,
                checkpoint_json=CASE WHEN ? THEN excluded.checkpoint_json
                                     ELSE mailbox_sync_states.checkpoint_json END,
                current_attempt_json=excluded.current_attempt_json,
                last_check_at=excluded.last_check_at,
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=CASE WHEN ? THEN excluded.last_success_at
                                     ELSE mailbox_sync_states.last_success_at END,
                last_error_at=CASE WHEN ? THEN mailbox_sync_states.last_error_at
                                   ELSE excluded.last_error_at END,
                consecutive_failures=CASE WHEN ? THEN 0
                    ELSE mailbox_sync_states.consecutive_failures + 1 END,
                reconciliation_required=CASE
                    WHEN ? THEN 0
                    WHEN excluded.reconciliation_required=1 THEN 1
                    ELSE mailbox_sync_states.reconciliation_required END,
                full_rescan_cursor=CASE WHEN ? THEN NULL ELSE COALESCE(
                    excluded.full_rescan_cursor,
                    mailbox_sync_states.full_rescan_cursor) END,
                server_snapshot_json=CASE
                    WHEN ? THEN excluded.server_snapshot_json
                    ELSE mailbox_sync_states.server_snapshot_json END,
                uidvalidity_changed_at=COALESCE(
                    excluded.uidvalidity_changed_at,
                    mailbox_sync_states.uidvalidity_changed_at),
                last_result=excluded.last_result,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                mailbox_id,
                account_id,
                uidvalidity or None,
                uidnext or None,
                highestmodseq or None,
                max(0, int(last_uid)),
                _json(checkpoint),
                "{}" if succeeded else _json(current_attempt or {"stage": "failed"}),
                now,
                now,
                now if succeeded else None,
                None if succeeded else now,
                0 if succeeded else 1,
                1 if reconciliation_required else 0,
                full_rescan_cursor,
                _json(server_snapshot or {}),
                now if uidvalidity_changed else None,
                result,
                error,
                now,
                *([1 if succeeded else 0] * 8),
                1 if succeeded and reconciliation_completed else 0,
                1 if succeeded and reconciliation_completed else 0,
                1 if succeeded else 0,
            ),
        )
        if succeeded:
            connection.execute(
                """
                UPDATE mailboxes
                SET uidvalidity=?, uidnext=?, highestmodseq=?, updated_at=?
                WHERE mailbox_id=? AND account_id=?
                """,
                (
                    uidvalidity or None,
                    uidnext or None,
                    highestmodseq or None,
                    now,
                    mailbox_id,
                    account_id,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def get_mailbox_checkpoint_state(
    db_path: Path | str, mailbox_id: str
) -> dict[str, Any] | None:
    row = get_connection(db_path).execute(
        "SELECT * FROM mailbox_sync_states WHERE mailbox_id=?",
        (mailbox_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for column in (
        "checkpoint_json",
        "current_attempt_json",
        "server_snapshot_json",
    ):
        try:
            result[column.removesuffix("_json")] = json.loads(
                str(result.get(column) or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            result[column.removesuffix("_json")] = {}
    return result


def _json(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
