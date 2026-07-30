"""Provider-neutral 邮箱目录事实与同步选择。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.database import (
    get_mailbox,
    query_mailboxes,
    upsert_mailboxes,
)
from agent_mail_bridge.mailbox_checkpoint import finish_mailbox_attempt


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
        discovered.append(
            gmail_mailbox_fact(
                label_id,
                name=str(label.get("name") or label_id),
                label_type=str(label.get("type") or "user"),
            )
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


def ensure_gmail_message_mailboxes(
    db_path: Path | str,
    account_id: str,
    label_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """补记 labels.list 与 messages.get 之间新出现的 Label，不启用其同步。"""
    normalized = {
        str(value).strip() for value in label_ids if str(value).strip()
    }
    if not normalized:
        return query_mailboxes(db_path, account_id=account_id)
    existing = query_mailboxes(db_path, account_id=account_id)
    known = {str(row.get("raw_name") or "") for row in existing}
    missing = sorted(normalized - known)
    if missing:
        upsert_mailboxes(
            db_path,
            account_id,
            [
                gmail_mailbox_fact(
                    label_id,
                    name=label_id,
                    label_type="message_snapshot",
                )
                for label_id in missing
            ],
            replace_discovery=False,
        )
    return query_mailboxes(db_path, account_id=account_id)


def gmail_mailbox_fact(
    label_id: str,
    *,
    name: str,
    label_type: str,
) -> dict[str, Any]:
    clean_id = str(label_id).strip()
    role = _GMAIL_ROLES.get(clean_id.upper(), "other")
    return {
        "external_ref": f"gmail:{clean_id}",
        "raw_name": clean_id,
        "display_name": str(name or clean_id),
        "delimiter": "/",
        "flags": [f"gmail:{str(label_type or 'user')}"],
        "mailbox_role": role,
        "role_source": (
            "gmail_system_label" if role != "other" else "gmail_user_label"
        ),
        "role_confidence": "high",
        "sync_enabled": role in {"inbox", "sent"},
    }


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
    current_attempt: dict[str, Any] | None = None,
    server_snapshot: dict[str, Any] | None = None,
    reconciliation_required: bool = False,
    reconciliation_completed: bool = False,
    uidvalidity_changed: bool = False,
    full_rescan_cursor: str | None = None,
) -> None:
    mailbox = get_mailbox(db_path, mailbox_id)
    if mailbox is None:
        raise ValueError("邮箱目录不存在")
    finish_mailbox_attempt(
        db_path,
        mailbox_id=mailbox_id,
        account_id=str(mailbox["account_id"]),
        uidvalidity=uidvalidity,
        uidnext=uidnext,
        highestmodseq=highestmodseq,
        last_uid=last_uid,
        checkpoint=checkpoint,
        result=result,
        error=error,
        current_attempt=current_attempt,
        server_snapshot=server_snapshot,
        reconciliation_required=reconciliation_required,
        reconciliation_completed=reconciliation_completed,
        uidvalidity_changed=uidvalidity_changed,
        full_rescan_cursor=full_rescan_cursor,
    )


def mailbox_direction(mailbox: dict[str, Any]) -> str:
    return (
        "outbound"
        if str(mailbox.get("mailbox_role") or "") == "sent"
        else "inbound"
    )
