"""确定性线程关系与服务器 Sent 回流映射。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agent_mail_bridge.database import get_connection
from agent_mail_bridge.mail_common import normalize_message_id


def content_fingerprint(
    *,
    subject: str,
    body_text: str,
    recipients: list[str],
    attachments: list[dict[str, Any]],
) -> str:
    attachment_facts = sorted(
        f"{str(item.get('display_name') or '').casefold()}:"
        f"{int(item.get('size_bytes') or 0)}:"
        f"{str(item.get('sha256') or '').casefold()}"
        for item in attachments
    )
    material = "\n".join(
        (
            subject.strip(),
            body_text.replace("\r\n", "\n").strip(),
            ",".join(sorted(value.casefold() for value in recipients)),
            "|".join(attachment_facts),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def find_package_by_message_id(
    db_path: Path | str, *, account_id: str, message_id: str
) -> dict[str, Any] | None:
    normalized = normalize_message_id(message_id)
    if not normalized:
        return None
    rows = get_connection(db_path).execute(
        """
        SELECT * FROM mail_packages
        WHERE account_id = ? AND message_id = ? COLLATE NOCASE
        ORDER BY local_outbound DESC, id ASC
        LIMIT 2
        """,
        (account_id, normalized),
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def record_sent_mapping(
    db_path: Path | str,
    *,
    account_id: str,
    package_id: str,
    mailbox_id: str,
    provider_message_id: str | None,
    uidvalidity: int | None,
    provider_uid: str | None,
    message_id: str,
    matched_by: str,
    confidence: str = "high",
    reconciliation_status: str = "matched",
    details: dict[str, Any] | None = None,
) -> str:
    material = (
        f"{account_id}\n{mailbox_id}\n{provider_message_id or ''}\n"
        f"{provider_uid or ''}\n{package_id}"
    )
    mapping_id = "sentmap_" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]
    connection = get_connection(db_path)
    now = _now(connection)
    connection.execute(
        """
        INSERT INTO sent_server_mappings
            (mapping_id, account_id, package_id, mailbox_id,
             provider_message_id, uidvalidity, provider_uid, message_id,
             matched_by, confidence, reconciliation_status, matched_at,
             details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mapping_id) DO UPDATE SET
            package_id=excluded.package_id,
            provider_message_id=COALESCE(excluded.provider_message_id,
                                         sent_server_mappings.provider_message_id),
            uidvalidity=COALESCE(excluded.uidvalidity,
                                 sent_server_mappings.uidvalidity),
            provider_uid=COALESCE(excluded.provider_uid,
                                  sent_server_mappings.provider_uid),
            matched_by=excluded.matched_by,
            confidence=excluded.confidence,
            reconciliation_status=excluded.reconciliation_status,
            matched_at=excluded.matched_at,
            details_json=excluded.details_json
        """,
        (
            mapping_id,
            account_id,
            package_id,
            mailbox_id,
            provider_message_id,
            uidvalidity,
            provider_uid,
            normalize_message_id(message_id),
            matched_by,
            confidence,
            reconciliation_status,
            now,
            json.dumps(
                details or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    connection.commit()
    return mapping_id


def record_thread_relations(
    db_path: Path | str,
    *,
    account_id: str,
    package_id: str,
    in_reply_to_raw: str = "",
    references_raw: str = "",
    reply_to_package_id: str | None = None,
    forward_from_package_id: str | None = None,
) -> list[dict[str, str]]:
    candidates: list[tuple[str, str, str]] = []
    if reply_to_package_id:
        candidates.append((reply_to_package_id, "reply", "explicit_package"))
    if forward_from_package_id:
        candidates.append(
            (forward_from_package_id, "forward", "explicit_package")
        )
    if not reply_to_package_id:
        referenced = re.findall(
            r"<[^<>]+>", in_reply_to_raw or references_raw or ""
        )
        if referenced:
            source = find_package_by_message_id(
                db_path,
                account_id=account_id,
                message_id=referenced[-1],
            )
            if source:
                candidates.append(
                    (
                        str(source["package_id"]),
                        "reply",
                        "rfc_in_reply_to",
                    )
                )
    connection = get_connection(db_path)
    now = _now(connection)
    result: list[dict[str, str]] = []
    for related_package_id, relation_type, source in candidates:
        if related_package_id == package_id:
            continue
        exists = connection.execute(
            "SELECT 1 FROM mail_packages WHERE package_id = ? AND account_id = ?",
            (related_package_id, account_id),
        ).fetchone()
        if not exists:
            continue
        connection.execute(
            """
            INSERT INTO mail_thread_relations
                (account_id, package_id, related_package_id, relation_type,
                 source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id, related_package_id, relation_type)
            DO UPDATE SET source=excluded.source
            """,
            (
                account_id,
                package_id,
                related_package_id,
                relation_type,
                source,
                now,
            ),
        )
        result.append(
            {
                "package_id": package_id,
                "related_package_id": related_package_id,
                "relation_type": relation_type,
                "source": source,
            }
        )
    connection.commit()
    return result


def query_sent_mappings(
    db_path: Path | str, *, package_id: str | None = None
) -> list[dict[str, Any]]:
    connection = get_connection(db_path)
    if package_id:
        rows = connection.execute(
            "SELECT * FROM sent_server_mappings WHERE package_id = ? "
            "ORDER BY matched_at DESC",
            (package_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM sent_server_mappings ORDER BY matched_at DESC"
        ).fetchall()
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        try:
            row["details"] = json.loads(str(row.get("details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            row["details"] = {}
        result.append(row)
    return result


def _now(connection) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
