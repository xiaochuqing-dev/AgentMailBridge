"""Provider-neutral 邮箱目录事实与同步选择。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.database import (
    get_connection,
    get_mailbox,
    query_mailboxes,
    upsert_mailboxes,
)


_GMAIL_ROLES = {
    "INBOX": "inbox",
    "SENT": "sent",
    "DRAFT": "drafts",
    "SPAM": "junk",
    "TRASH": "trash",
    "IMPORTANT": "important",
    "STARRED": "flagged",
    "ALL": "all",
}


def discover_gmail_mailboxes(
    *,
    service: Any,
    db_path: Path | str,
    account_id: str,
) -> list[dict[str, Any]]:
    """在 gmail.readonly 范围内发现系统 Label 和用户 Label。"""
    response = service.users().labels().list(userId="me").execute()
    discovered: list[dict[str, Any]] = []
    labels = response.get("labels", []) if isinstance(response, dict) else []
    for label in labels or []:
        label_id = str(label.get("id") or "").strip()
        if not label_id:
            continue
        name = str(label.get("name") or label_id)
        role = _GMAIL_ROLES.get(label_id.upper(), "other")
        discovered.append(
            {
                "external_ref": f"gmail:{label_id}",
                "raw_name": label_id,
                "display_name": name,
                "delimiter": "/",
                "flags": [f"gmail:{str(label.get('type') or 'user')}"],
                "mailbox_role": role,
                "role_source": (
                    "gmail_system_label"
                    if role != "other"
                    else "gmail_user_label"
                ),
                "role_confidence": "high",
                "sync_enabled": role in {"inbox", "sent"},
            }
        )
    if not discovered:
        discovered = [
            {
                "external_ref": "gmail:INBOX",
                "raw_name": "INBOX",
                "display_name": "Inbox",
                "delimiter": "/",
                "flags": ["gmail:system"],
                "mailbox_role": "inbox",
                "role_source": "gmail_fallback",
                "role_confidence": "fallback",
                "sync_enabled": True,
            }
        ]
    return upsert_mailboxes(
        db_path,
        account_id,
        discovered,
        replace_discovery=True,
    )


def persist_imap_discovery(
    db_path: Path | str,
    account_id: str,
    discovered: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return upsert_mailboxes(
        db_path,
        account_id,
        discovered,
        replace_discovery=True,
    )


def selected_sync_mailboxes(
    db_path: Path | str,
    account_id: str,
    *,
    mailbox_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """解析启用目录；显式列表仍不能绕过停用或 ownership。"""
    requested = {
        str(value).strip()
        for value in (mailbox_ids or ())
        if str(value).strip()
    }
    rows = query_mailboxes(
        db_path,
        account_id=account_id,
        enabled_only=True,
    )
    if requested:
        selected = [
            row for row in rows if str(row["mailbox_id"]) in requested
        ]
        if len(selected) != len(requested):
            raise ValueError("包含不存在、已停用或不属于该账号的邮箱目录")
        return selected
    selected = [row for row in rows if row.get("sync_enabled")]
    if selected:
        return selected
    return [
        row for row in rows if str(row.get("mailbox_role") or "") == "inbox"
    ]


def update_mailbox_checkpoint(
    db_path: Path | str,
    mailbox_id: str,
    *,
    uidvalidity: int,
    uidnext: int,
    highestmodseq: int,
    last_uid: int,
    checkpoint: dict[str, Any],
    result: str,
    error: str | None = None,
) -> None:
    mailbox = get_mailbox(db_path, mailbox_id)
    if mailbox is None:
        raise ValueError("邮箱目录不存在")
    connection = get_connection(db_path)
    now = _now(connection)
    connection.execute(
        """
        INSERT INTO mailbox_sync_states
            (mailbox_id, account_id, uidvalidity, uidnext, highestmodseq,
             last_uid, checkpoint_json, last_check_at, last_success_at,
             last_result, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mailbox_id) DO UPDATE SET
            uidvalidity=excluded.uidvalidity,
            uidnext=excluded.uidnext,
            highestmodseq=excluded.highestmodseq,
            last_uid=excluded.last_uid,
            checkpoint_json=excluded.checkpoint_json,
            last_check_at=excluded.last_check_at,
            last_success_at=CASE
                WHEN excluded.last_result IN ('success', 'no_changes', 'partial')
                THEN excluded.last_success_at
                ELSE mailbox_sync_states.last_success_at
            END,
            last_result=excluded.last_result,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            mailbox_id,
            str(mailbox["account_id"]),
            uidvalidity or None,
            uidnext or None,
            highestmodseq or None,
            max(0, int(last_uid)),
            _json(checkpoint),
            now,
            now,
            result,
            error,
            now,
        ),
    )
    connection.execute(
        """
        UPDATE mailboxes
        SET uidvalidity=?, uidnext=?, highestmodseq=?, updated_at=?
        WHERE mailbox_id=?
        """,
        (
            uidvalidity or None,
            uidnext or None,
            highestmodseq or None,
            now,
            mailbox_id,
        ),
    )
    connection.commit()


def mailbox_direction(mailbox: dict[str, Any]) -> str:
    return (
        "outbound"
        if str(mailbox.get("mailbox_role") or "") == "sent"
        else "inbound"
    )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _now(connection) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
