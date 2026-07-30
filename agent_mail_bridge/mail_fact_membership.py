"""永久邮件事实的目录归属、服务器存在性与方向证据。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.database import get_connection
from agent_mail_bridge.mail_common import parse_mailboxes


def record_membership(
    db_path: Path | str,
    *,
    package_id: str,
    account_id: str,
    mailbox_id: str,
    provider_message_id: str | None = None,
    uidvalidity: int | None = None,
    provider_uid: str | None = None,
    source: str = "sync_observed",
    reconciliation_status: str = "observed",
) -> None:
    """幂等记录当前归属；再次观察会恢复 currently_present。"""
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _assert_ownership(
            connection,
            package_id=package_id,
            account_id=account_id,
            mailbox_id=mailbox_id,
        )
        _upsert_membership(
            connection,
            package_id=package_id,
            account_id=account_id,
            mailbox_id=mailbox_id,
            provider_message_id=provider_message_id,
            uidvalidity=uidvalidity,
            provider_uid=provider_uid,
            source=source,
            reconciliation_status=reconciliation_status,
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def reconcile_package_memberships(
    db_path: Path | str,
    *,
    package_id: str,
    account_id: str,
    observed_mailbox_ids: Iterable[str],
    scope_mailbox_ids: Iterable[str],
    provider_message_id: str | None,
    source: str,
    complete_snapshot: bool,
) -> dict[str, int]:
    """用 Provider 的完整 Label 快照更新一封邮件的多目录归属。"""
    observed = {str(value) for value in observed_mailbox_ids if str(value)}
    scope = {str(value) for value in scope_mailbox_ids if str(value)}
    if not observed.issubset(scope):
        raise ValueError("观察到的目录不在本次快照范围内")
    connection = get_connection(db_path)
    now = _now(connection)
    added = 0
    removed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        package = connection.execute(
            "SELECT account_id FROM mail_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()
        if package is None or str(package[0] or "") != account_id:
            raise ValueError("邮件事实 ownership 不匹配")
        for mailbox_id in sorted(observed):
            _assert_ownership(
                connection,
                package_id=package_id,
                account_id=account_id,
                mailbox_id=mailbox_id,
            )
            existed = connection.execute(
                "SELECT currently_present FROM mail_package_mailboxes "
                "WHERE package_id=? AND mailbox_id=?",
                (package_id, mailbox_id),
            ).fetchone()
            _upsert_membership(
                connection,
                package_id=package_id,
                account_id=account_id,
                mailbox_id=mailbox_id,
                provider_message_id=provider_message_id,
                uidvalidity=None,
                provider_uid=None,
                source=source,
                reconciliation_status="observed",
                now=now,
            )
            if existed is None or not bool(existed[0]):
                added += 1
        if complete_snapshot and scope:
            placeholders = ",".join("?" for _ in scope)
            parameters: list[Any] = [
                now,
                source,
                package_id,
                account_id,
                *sorted(scope),
            ]
            excluded = ""
            if observed:
                excluded = (
                    " AND mailbox_id NOT IN ("
                    + ",".join("?" for _ in observed)
                    + ")"
                )
                parameters.extend(sorted(observed))
            cursor = connection.execute(
                f"""
                UPDATE mail_package_mailboxes
                SET currently_present=0,
                    removed_at=COALESCE(removed_at, ?),
                    source=?,
                    reconciliation_status='server_absent',
                    last_seen_at=last_seen_at
                WHERE package_id=? AND account_id=?
                  AND mailbox_id IN ({placeholders})
                  AND currently_present=1{excluded}
                """,
                tuple(parameters),
            )
            removed = max(0, int(cursor.rowcount))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"present": len(observed), "added": added, "removed": removed}


def reconcile_mailbox_snapshot(
    db_path: Path | str,
    *,
    account_id: str,
    mailbox_id: str,
    observed_package_ids: Iterable[str],
    source: str = "full_mailbox_snapshot",
) -> dict[str, int]:
    """完成一次全目录扫描后标记服务器已不再呈现的 membership。"""
    observed = {str(value) for value in observed_package_ids if str(value)}
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        mailbox = connection.execute(
            "SELECT account_id FROM mailboxes WHERE mailbox_id=?",
            (mailbox_id,),
        ).fetchone()
        if mailbox is None or str(mailbox[0]) != account_id:
            raise ValueError("邮箱目录 ownership 不匹配")
        parameters: list[Any] = [now, source, mailbox_id, account_id]
        excluded = ""
        if observed:
            excluded = (
                " AND package_id NOT IN ("
                + ",".join("?" for _ in observed)
                + ")"
            )
            parameters.extend(sorted(observed))
        cursor = connection.execute(
            """
            UPDATE mail_package_mailboxes
            SET currently_present=0,
                removed_at=COALESCE(removed_at, ?),
                source=?, reconciliation_status='server_absent'
            WHERE mailbox_id=? AND account_id=? AND currently_present=1
            """
            + excluded,
            tuple(parameters),
        )
        connection.commit()
        return {"observed": len(observed), "removed": max(0, cursor.rowcount)}
    except Exception:
        connection.rollback()
        raise


def reconcile_mailbox_uid_snapshot(
    db_path: Path | str,
    *,
    account_id: str,
    mailbox_id: str,
    uidvalidity: int,
    observed_provider_uids: Iterable[int | str],
    source: str = "imap_uid_snapshot",
) -> dict[str, int]:
    """用完整 IMAP UID 集合更新服务器存在性，不改动永久事实。"""
    observed = {
        str(value).strip()
        for value in observed_provider_uids
        if str(value).strip()
    }
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        mailbox = connection.execute(
            "SELECT account_id FROM mailboxes WHERE mailbox_id=?",
            (mailbox_id,),
        ).fetchone()
        if mailbox is None or str(mailbox[0] or "") != account_id:
            raise ValueError("邮箱目录 ownership 不匹配")
        rows = connection.execute(
            """
            SELECT id, provider_uid, uidvalidity
            FROM mail_package_mailboxes
            WHERE mailbox_id=? AND account_id=? AND currently_present=1
              AND provider_uid IS NOT NULL AND provider_uid!=''
            """,
            (mailbox_id, account_id),
        ).fetchall()
        removed_ids = [
            int(row["id"])
            for row in rows
            if (
                uidvalidity > 0
                and int(row["uidvalidity"] or 0) not in {0, uidvalidity}
            )
            or str(row["provider_uid"] or "") not in observed
        ]
        if removed_ids:
            connection.executemany(
                """
                UPDATE mail_package_mailboxes
                SET currently_present=0,
                    removed_at=COALESCE(removed_at, ?),
                    source=?, reconciliation_status='server_absent'
                WHERE id=? AND currently_present=1
                """,
                [
                    (now, str(source or "imap_uid_snapshot")[:80], row_id)
                    for row_id in removed_ids
                ],
            )
        connection.commit()
        return {"observed": len(observed), "removed": len(removed_ids)}
    except Exception:
        connection.rollback()
        raise


def infer_direction(
    db_path: Path | str,
    *,
    account_id: str,
    mailbox_id: str | None,
    backend: str,
    from_header: str,
    outbound_origin: str,
    provider_direction: str = "",
) -> tuple[str, list[dict[str, str]]]:
    """按确定性证据推导方向；目录角色只是证据之一。"""
    connection = get_connection(db_path)
    account = connection.execute(
        "SELECT email_address FROM mail_accounts WHERE account_id=?",
        (account_id,),
    ).fetchone()
    mailbox = (
        connection.execute(
            "SELECT mailbox_role, role_source FROM mailboxes WHERE mailbox_id=?",
            (mailbox_id,),
        ).fetchone()
        if mailbox_id
        else None
    )
    evidence: list[dict[str, str]] = []
    if outbound_origin.casefold() == "outbound" or backend == "smtp":
        evidence.append(
            {
                "type": "local_outbound_marker",
                "direction": "outbound",
                "confidence": "exact",
            }
        )
        return "outbound", evidence
    role = str(mailbox[0] or "") if mailbox else ""
    decisive_outbound = role == "sent" or provider_direction == "outbound"
    if decisive_outbound:
        evidence.append(
            {
                "type": (
                    "provider_sent_membership"
                    if role == "sent"
                    else "provider_direction_metadata"
                ),
                "direction": "outbound",
                "confidence": "high",
            }
        )
    own = str(account[0] or "").casefold() if account else ""
    senders = {value.casefold() for value in parse_mailboxes(from_header)}
    if own and own in senders:
        evidence.append(
            {
                "type": "configured_account_is_sender",
                "direction": "outbound",
                "confidence": "high" if role == "sent" else "medium",
            }
        )
    if decisive_outbound:
        return "outbound", evidence
    evidence.append(
        {
            "type": "provider_receive_observation",
            "direction": "inbound",
            "confidence": "medium",
        }
    )
    return "inbound", evidence


def record_direction_evidence(
    db_path: Path | str,
    *,
    package_id: str,
    proposed_direction: str,
    evidence: Iterable[dict[str, str]],
) -> dict[str, Any]:
    """追加去重后的证据；方向冲突只登记，不静默覆盖。"""
    if proposed_direction not in {"inbound", "outbound"}:
        raise ValueError("邮件方向无效")
    connection = get_connection(db_path)
    now = _now(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT account_id, direction, direction_evidence_json "
            "FROM mail_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()
        if row is None:
            raise ValueError("邮件事实不存在")
        try:
            saved = json.loads(str(row[2] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = []
        if not isinstance(saved, list):
            saved = []
        for item in evidence:
            normalized = {
                "type": str(item.get("type") or "unknown")[:80],
                "direction": str(item.get("direction") or proposed_direction),
                "confidence": str(item.get("confidence") or "unknown")[:20],
            }
            if normalized not in saved:
                saved.append(normalized)
        saved = saved[-32:]
        current = str(row[1] or "inbound")
        conflict = current != proposed_direction
        connection.execute(
            "UPDATE mail_packages SET direction_evidence_json=?, "
            "direction_conflict=CASE WHEN ? THEN 1 ELSE direction_conflict END, "
            "last_verified_at=?, updated_at=? WHERE package_id=?",
            (
                json.dumps(saved, ensure_ascii=False, separators=(",", ":")),
                1 if conflict else 0,
                now,
                now,
                package_id,
            ),
        )
        if conflict:
            _record_direction_conflict(
                connection,
                account_id=str(row[0] or ""),
                package_id=package_id,
                current=current,
                proposed=proposed_direction,
                now=now,
            )
        connection.commit()
        return {"direction": current, "conflict": conflict, "evidence": saved}
    except Exception:
        connection.rollback()
        raise


def _upsert_membership(
    connection: Any,
    *,
    package_id: str,
    account_id: str,
    mailbox_id: str,
    provider_message_id: str | None,
    uidvalidity: int | None,
    provider_uid: str | None,
    source: str,
    reconciliation_status: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO mail_package_mailboxes
            (package_id, mailbox_id, account_id, provider_message_id,
             uidvalidity, provider_uid, first_seen_at, last_seen_at,
             currently_present, removed_at, source, reconciliation_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
        ON CONFLICT(package_id, mailbox_id) DO UPDATE SET
            provider_message_id=COALESCE(excluded.provider_message_id,
                                         mail_package_mailboxes.provider_message_id),
            uidvalidity=COALESCE(excluded.uidvalidity,
                                 mail_package_mailboxes.uidvalidity),
            provider_uid=COALESCE(excluded.provider_uid,
                                  mail_package_mailboxes.provider_uid),
            last_seen_at=excluded.last_seen_at,
            currently_present=1,
            removed_at=NULL,
            source=excluded.source,
            reconciliation_status=excluded.reconciliation_status
        """,
        (
            package_id,
            mailbox_id,
            account_id,
            provider_message_id,
            uidvalidity,
            provider_uid,
            now,
            now,
            str(source or "sync_observed")[:80],
            str(reconciliation_status or "observed")[:40],
        ),
    )


def _assert_ownership(
    connection: Any,
    *,
    package_id: str,
    account_id: str,
    mailbox_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT p.account_id AS package_account, m.account_id AS mailbox_account
        FROM mail_packages p JOIN mailboxes m ON m.mailbox_id=?
        WHERE p.package_id=?
        """,
        (mailbox_id, package_id),
    ).fetchone()
    if row is None or str(row[0] or "") != account_id or str(row[1]) != account_id:
        raise ValueError("邮件事实与目录 ownership 不匹配")


def _record_direction_conflict(
    connection: Any,
    *,
    account_id: str,
    package_id: str,
    current: str,
    proposed: str,
    now: str,
) -> None:
    key = f"direction\n{package_id}\n{current}\n{proposed}"
    reconciliation_id = "recon_" + hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()[:24]
    details = json.dumps(
        {"current": current, "proposed": proposed},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO reconciliation_records
            (reconciliation_id, entity_type, entity_id, account_id,
             package_id, status, evidence_type, confidence,
             candidate_count, details_json, first_seen_at, last_seen_at)
        VALUES (?, 'mail_direction', ?, ?, ?, 'conflict',
                'conflicting_direction_evidence', 'manual_review',
                2, ?, ?, ?)
        ON CONFLICT(reconciliation_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            details_json=excluded.details_json,
            resolved_at=NULL
        """,
        (
            reconciliation_id,
            package_id,
            account_id,
            package_id,
            details,
            now,
            now,
        ),
    )


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
