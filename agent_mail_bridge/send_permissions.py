"""Client 邮箱目录、发件账号和附件目录权限。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.database import (
    get_connection,
    get_mail_account,
    get_mailbox,
    query_mail_accounts,
    query_mailboxes,
)
from agent_mail_bridge.mail_resource_access import workspace_id_for_path


SCOPE_MODES = {"all", "selected"}
SEND_MODES = {"confirm", "autonomous"}


def replace_extended_scopes(
    db_path: Path | str,
    client_id: str,
    *,
    mailbox_ids: Iterable[str] = (),
    denied_mailbox_ids: Iterable[str] = (),
    send_account_ids: Iterable[str] = (),
    denied_send_account_ids: Iterable[str] = (),
    attachment_workspace_ids: Iterable[str] = (),
    denied_attachment_workspace_ids: Iterable[str] = (),
) -> None:
    """原子替换三个独立 scope；不改变通用 capability 或读账号 scope。"""
    now = _now(db_path)
    groups = (
        (
            "agent_client_mailbox_scopes",
            "mailbox_id",
            _normalized(mailbox_ids),
            _normalized(denied_mailbox_ids),
        ),
        (
            "agent_client_send_account_scopes",
            "account_id",
            _normalized(send_account_ids),
            _normalized(denied_send_account_ids),
        ),
        (
            "agent_client_attachment_scopes",
            "workspace_id",
            _normalized(attachment_workspace_ids),
            _normalized(denied_attachment_workspace_ids),
        ),
    )
    connection = get_connection(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table, column, allowed, denied in groups:
            connection.execute(
                f"DELETE FROM {table} WHERE client_id = ?", (client_id,)
            )
            for effect, values in (("allow", allowed), ("deny", denied)):
                connection.executemany(
                    f"""
                    INSERT INTO {table}
                        (client_id, {column}, effect, enabled,
                         created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (
                        (client_id, value, effect, now, now)
                        for value in sorted(values)
                    ),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def query_extended_scopes(
    db_path: Path | str, client_id: str
) -> dict[str, set[str]]:
    connection = get_connection(db_path)
    result: dict[str, set[str]] = {
        "mailbox_ids": set(),
        "denied_mailbox_ids": set(),
        "send_account_ids": set(),
        "denied_send_account_ids": set(),
        "attachment_workspace_ids": set(),
        "denied_attachment_workspace_ids": set(),
    }
    specs = (
        (
            "agent_client_mailbox_scopes",
            "mailbox_id",
            "mailbox_ids",
            "denied_mailbox_ids",
        ),
        (
            "agent_client_send_account_scopes",
            "account_id",
            "send_account_ids",
            "denied_send_account_ids",
        ),
        (
            "agent_client_attachment_scopes",
            "workspace_id",
            "attachment_workspace_ids",
            "denied_attachment_workspace_ids",
        ),
    )
    for table, column, allow_key, deny_key in specs:
        rows = connection.execute(
            f"SELECT {column}, effect FROM {table} "
            "WHERE client_id = ? AND enabled = 1",
            (client_id,),
        ).fetchall()
        for row in rows:
            value = str(row[column] or "")
            if value:
                result[deny_key if row["effect"] == "deny" else allow_key].add(
                    value
                )
    return result


def effective_mailbox_ids(
    db_path: Path | str,
    *,
    account_ids: Iterable[str],
    mode: str,
    selected: Iterable[str],
    denied: Iterable[str],
) -> frozenset[str]:
    allowed_accounts = {str(value) for value in account_ids}
    denied_set = {str(value) for value in denied}
    if str(mode).casefold() == "all":
        values = {
            str(row["mailbox_id"])
            for row in query_mailboxes(db_path, enabled_only=True)
            if str(row["account_id"]) in allowed_accounts
        }
    else:
        values = {
            str(value)
            for value in selected
            if str(value)
            and (get_mailbox(db_path, str(value)) or {}).get("enabled")
            and str((get_mailbox(db_path, str(value)) or {}).get("account_id") or "")
            in allowed_accounts
        }
    return frozenset(values - denied_set)


def effective_send_account_ids(
    db_path: Path | str,
    *,
    mode: str,
    selected: Iterable[str],
    denied: Iterable[str],
) -> frozenset[str]:
    denied_set = {str(value) for value in denied}
    send_capable = {
        str(row["account_id"])
        for row in query_mail_accounts(db_path, enabled_only=True)
        if row.get("send_enabled") and "send" in set(row.get("capabilities") or ())
    }
    values = (
        send_capable
        if str(mode).casefold() == "all"
        else {str(value) for value in selected} & send_capable
    )
    return frozenset(values - denied_set)


def effective_attachment_workspace_ids(
    configured_roots: Iterable[Path | str],
    *,
    mode: str,
    selected: Iterable[str],
    denied: Iterable[str],
) -> frozenset[str]:
    current = {
        workspace_id_for_path(path)
        for path in configured_roots
    }
    values = (
        current
        if str(mode).casefold() == "all"
        else {str(value) for value in selected} & current
    )
    return frozenset(values - {str(value) for value in denied})


def validate_extended_scope_values(
    db_path: Path | str,
    *,
    mailbox_ids: Iterable[str],
    send_account_ids: Iterable[str],
    attachment_workspace_ids: Iterable[str],
    configured_roots: Iterable[Path | str],
) -> None:
    known_mailboxes = {
        str(row["mailbox_id"])
        for row in query_mailboxes(db_path, enabled_only=True)
    }
    if not _normalized(mailbox_ids).issubset(known_mailboxes):
        raise ValueError("包含不存在或已停用的邮箱目录")
    known_send_accounts = {
        str(row["account_id"])
        for row in query_mail_accounts(db_path, enabled_only=True)
        if row.get("send_enabled") and "send" in set(row.get("capabilities") or ())
    }
    if not _normalized(send_account_ids).issubset(known_send_accounts):
        raise ValueError("包含不存在、已停用或不可发件的邮箱账号")
    known_workspaces = {
        workspace_id_for_path(path)
        for path in configured_roots
    }
    if not _normalized(attachment_workspace_ids).issubset(known_workspaces):
        raise ValueError("包含不存在的 Agent 附件资料目录")


def account_is_send_capable(db_path: Path | str, account_id: str) -> bool:
    row = get_mail_account(db_path, account_id)
    return bool(
        row
        and row.get("enabled")
        and row.get("send_enabled")
        and "send" in set(row.get("capabilities") or ())
    )


def _normalized(values: Iterable[str]) -> set[str]:
    return {
        str(value).strip()
        for value in values
        if str(value).strip()
    }


def _now(db_path: Path | str) -> str:
    row = get_connection(db_path).execute(
        "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
    ).fetchone()
    return str(row[0])
