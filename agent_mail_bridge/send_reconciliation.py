"""Sent 回流的确定性匹配、歧义记录与外部发件事实登记。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import get_connection
from agent_mail_bridge.mail_common import normalize_message_id, parse_mailboxes
from agent_mail_bridge.mail_threading import record_sent_mapping
from agent_mail_bridge.send_requests import get_send_request, record_outbound_fact


EVIDENCE_PRIORITY = {
    "exact_outbound_id": 0,
    "exact_provider_id": 1,
    "exact_message_id": 2,
    "exact_raw_hash": 3,
    "exact_content_attachment_fingerprint": 4,
}


def find_sent_candidate(
    db_path: Path | str,
    *,
    account_id: str,
    provider_message_id: str,
    message_id: str,
    outbound_id: str,
    raw_sha256: str,
    content_fingerprint: str = "",
    from_header: str = "",
    to_header: str = "",
    cc_header: str = "",
    subject: str = "",
) -> dict[str, Any]:
    """只返回唯一的最强证据候选；歧义时绝不自动选择。"""
    connection = get_connection(db_path)
    matches: dict[str, set[str]] = {}
    if outbound_id:
        rows = connection.execute(
            """
            SELECT package_id FROM mail_packages
            WHERE account_id=? AND outbound_id=? AND direction='outbound'
            """,
            (account_id, outbound_id),
        ).fetchall()
        matches["exact_outbound_id"] = {str(row[0]) for row in rows}
    if provider_message_id:
        rows = connection.execute(
            """
            SELECT package_id FROM sent_server_mappings
            WHERE account_id=? AND provider_message_id=?
            """,
            (account_id, provider_message_id),
        ).fetchall()
        matches["exact_provider_id"] = {str(row[0]) for row in rows}
    normalized_id = normalize_message_id(message_id)
    if normalized_id:
        rows = connection.execute(
            """
            SELECT package_id FROM mail_packages
            WHERE account_id=? AND message_id=? COLLATE NOCASE
              AND direction='outbound'
            """,
            (account_id, normalized_id),
        ).fetchall()
        matches["exact_message_id"] = {str(row[0]) for row in rows}
    if raw_sha256:
        rows = connection.execute(
            """
            SELECT package_id FROM mail_packages
            WHERE account_id=? AND raw_eml_sha256=? AND direction='outbound'
            """,
            (account_id, raw_sha256),
        ).fetchall()
        matches["exact_raw_hash"] = {str(row[0]) for row in rows}
    if content_fingerprint:
        rows = connection.execute(
            """
            SELECT package_id FROM mail_packages
            WHERE account_id=? AND content_fingerprint=?
              AND direction='outbound'
            """,
            (account_id, content_fingerprint),
        ).fetchall()
        matches["exact_content_attachment_fingerprint"] = {
            str(row[0]) for row in rows
        }
    nonempty = [
        (evidence, candidates)
        for evidence, candidates in matches.items()
        if candidates
    ]
    if not nonempty:
        result = _find_recoverable_send_request(
            connection,
            account_id=account_id,
            message_id=normalized_id,
            raw_sha256=raw_sha256,
            from_header=from_header,
            to_header=to_header,
            cc_header=cc_header,
            subject=subject,
        )
    else:
        evidence, candidates = min(
            nonempty, key=lambda item: EVIDENCE_PRIORITY[item[0]]
        )
        if len(candidates) == 1:
            selected = next(iter(candidates))
            conflicting_unique = {
                next(iter(other_candidates))
                for _other_evidence, other_candidates in nonempty
                if len(other_candidates) == 1 and selected not in other_candidates
            }
            if conflicting_unique:
                result = {
                    "status": "ambiguous",
                    "evidence_type": "conflicting_exact_evidence",
                    "confidence": "manual_review",
                    "candidate_count": len({selected, *conflicting_unique}),
                    "package_id": None,
                }
            else:
                result = {
                    "status": "matched",
                    "evidence_type": evidence,
                    "confidence": "exact",
                    "candidate_count": 1,
                    "package_id": selected,
                }
        else:
            result = {
                "status": "ambiguous",
                "evidence_type": evidence,
                "confidence": "manual_review",
                "candidate_count": len(candidates),
                "package_id": None,
            }
    _record_sent_reconciliation(
        db_path,
        account_id=account_id,
        entity_id=provider_message_id or raw_sha256[:24] or normalized_id,
        result=result,
    )
    return result


def prepare_request_sent_observation(
    cfg: AppConfig,
    *,
    send_request_id: str,
    observed_at: str,
) -> str:
    """在归档 Sent 副本前建立可幂等复用的 Agent outbound 关联。"""
    request = get_send_request(cfg.db_path, send_request_id)
    if request is None:
        raise ValueError("待对账发件请求不存在")
    connection = get_connection(cfg.db_path)
    account = connection.execute(
        "SELECT provider, email_address FROM mail_accounts WHERE account_id=?",
        (str(request["sender_account_id"]),),
    ).fetchone()
    if account is None:
        raise ValueError("待对账发件账号不存在")
    sender_ref = str(account["email_address"] or "").strip()
    provider = str(account["provider"] or "unknown").strip().casefold()
    grouped: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
    for recipient in request.get("recipients") or []:
        kind = str(recipient.get("recipient_type") or "")
        if kind in grouped:
            grouped[kind].append(str(recipient.get("email_address") or ""))
    outbound_id = _outbound_id(send_request_id)
    record_outbound_fact(
        cfg.db_path,
        outbound_id=outbound_id,
        request=request,
        sender_ref=sender_ref,
        account_ref=f"{provider}:{sender_ref.casefold()}",
        to_emails=grouped["to"],
        cc_emails=grouped["cc"],
        bcc_emails=grouped["bcc"],
        raw_eml_sha256=str(request.get("raw_eml_sha256") or ""),
        package_id=None,
        status="sent",
        sent_at=str(observed_at or "") or None,
    )
    return outbound_id


def reconcile_sent_observation(
    db_path: Path | str,
    *,
    account_id: str,
    package_id: str,
    mailbox_id: str,
    provider_message_id: str | None,
    uidvalidity: int | None,
    provider_uid: str | None,
    message_id: str,
    evidence_type: str,
    confidence: str = "exact",
    send_request_id: str | None = None,
) -> dict[str, Any]:
    """建立 Sent 映射，并把关联发件请求推进到已对账。"""
    mapping_id = record_sent_mapping(
        db_path,
        account_id=account_id,
        package_id=package_id,
        mailbox_id=mailbox_id,
        provider_message_id=provider_message_id,
        uidvalidity=uidvalidity,
        provider_uid=provider_uid,
        message_id=message_id,
        matched_by=evidence_type,
        confidence=confidence,
        reconciliation_status="matched",
        details={"source": "sent_sync"},
    )
    connection = get_connection(db_path)
    now = _now(connection)
    connection.execute(
        """
        UPDATE outbound_messages
        SET package_id=COALESCE(package_id, ?), status='sent',
            reconciliation_status='matched', updated_at=?
        WHERE package_id=? OR request_id=?
        """,
        (package_id, now, package_id, str(send_request_id or "")),
    )
    outbound = connection.execute(
        "SELECT outbound_id FROM outbound_messages "
        "WHERE package_id=? OR request_id=? ORDER BY id LIMIT 1",
        (package_id, str(send_request_id or "")),
    ).fetchone()
    outbound_id = str(outbound[0]) if outbound else ""
    if outbound_id:
        connection.execute(
            "UPDATE mail_packages SET outbound_origin='outbound', "
            "outbound_id=?, local_outbound=1, direction='outbound', updated_at=? "
            "WHERE package_id=?",
            (outbound_id, now, package_id),
        )
    connection.execute(
        """
        UPDATE send_requests
            SET status=CASE
                WHEN status IN ('sent', 'sent_waiting_reconciliation',
                                'delivery_unknown', 'recovery_required',
                                'smtp_accepted', 'sent_archive_pending',
                                'sent_archive_failed')
                    THEN 'sent_reconciled'
                ELSE status END,
            delivery_status=CASE
                WHEN status IN ('sent', 'sent_waiting_reconciliation',
                                'delivery_unknown', 'recovery_required',
                                'smtp_accepted', 'sent_archive_pending',
                                'sent_archive_failed')
                    THEN 'sent'
                ELSE delivery_status END,
            current_stage=CASE
                WHEN status IN ('sent', 'sent_waiting_reconciliation',
                                'delivery_unknown', 'recovery_required',
                                'smtp_accepted', 'sent_archive_pending',
                                'sent_archive_failed')
                    THEN 'sent_reconciled'
                ELSE current_stage END,
            recovery_required=CASE
                WHEN status IN ('sent', 'sent_waiting_reconciliation',
                                'delivery_unknown', 'recovery_required',
                                'smtp_accepted', 'sent_archive_pending',
                                'sent_archive_failed')
                    THEN 0 ELSE recovery_required END,
            sent_reconciliation_status='matched',
            last_reconciled_at=?,
            completed_at=COALESCE(completed_at, ?),
            outbound_id=COALESCE(NULLIF(outbound_id, ''), NULLIF(?, '')),
            package_id=COALESCE(package_id, ?),
            updated_at=?
        WHERE sender_account_id=?
          AND (send_request_id=? OR package_id=? OR outbound_id IN (
                SELECT outbound_id FROM outbound_messages
                WHERE package_id=?
              ))
        """,
        (
            now,
            now,
            outbound_id,
            package_id,
            now,
            account_id,
            str(send_request_id or ""),
            package_id,
            package_id,
        ),
    )
    connection.commit()
    _record_sent_reconciliation(
        db_path,
        account_id=account_id,
        entity_id=str(provider_message_id or provider_uid or package_id),
        result={
            "status": "matched",
            "evidence_type": evidence_type,
            "confidence": confidence,
            "candidate_count": 1,
            "package_id": package_id,
            "send_request_id": str(send_request_id or "") or None,
        },
    )
    return {"mapping_id": mapping_id, "status": "matched"}


def record_external_outbound(
    db_path: Path | str,
    *,
    account_id: str,
    package_id: str,
    provider_message_id: str,
) -> None:
    _record_sent_reconciliation(
        db_path,
        account_id=account_id,
        entity_id=provider_message_id or package_id,
        result={
            "status": "external_fact_created",
            "evidence_type": "provider_sent_observation",
            "confidence": "high",
            "candidate_count": 0,
            "package_id": package_id,
        },
    )


def _record_sent_reconciliation(
    db_path: Path | str,
    *,
    account_id: str,
    entity_id: str,
    result: dict[str, Any],
) -> None:
    safe_entity = str(entity_id or "unknown")
    evidence = str(result.get("evidence_type") or "unknown")
    material = f"sent_observation\n{account_id}\n{safe_entity}\n{evidence}"
    reconciliation_id = "recon_" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]
    connection = get_connection(db_path)
    now = _now(connection)
    status = str(result.get("status") or "unmatched")
    package_id = str(result.get("package_id") or "") or None
    send_request_id = str(result.get("send_request_id") or "") or None
    connection.execute(
        """
        INSERT INTO reconciliation_records
            (reconciliation_id, entity_type, entity_id, account_id,
             package_id, send_request_id, status, evidence_type, confidence,
             candidate_count, details_json, first_seen_at, last_seen_at,
             resolved_at)
        VALUES (?, 'sent_observation', ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
        ON CONFLICT(reconciliation_id) DO UPDATE SET
            package_id=COALESCE(excluded.package_id,
                                reconciliation_records.package_id),
            send_request_id=COALESCE(excluded.send_request_id,
                                     reconciliation_records.send_request_id),
            status=excluded.status,
            confidence=excluded.confidence,
            candidate_count=excluded.candidate_count,
            last_seen_at=excluded.last_seen_at,
            resolved_at=excluded.resolved_at
        """,
        (
            reconciliation_id,
            safe_entity[:200],
            account_id,
            package_id,
            send_request_id,
            status,
            evidence,
            str(result.get("confidence") or "unknown"),
            int(result.get("candidate_count") or 0),
            now,
            now,
            now if status in {"matched", "external_fact_created"} else None,
        ),
    )
    connection.commit()


def _find_recoverable_send_request(
    connection: Any,
    *,
    account_id: str,
    message_id: str,
    raw_sha256: str,
    from_header: str,
    to_header: str,
    cc_header: str,
    subject: str,
) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT r.send_request_id, r.message_id, r.raw_eml_sha256, r.subject,
               a.email_address
        FROM send_requests r
        LEFT JOIN mail_accounts a ON a.account_id=r.sender_account_id
        WHERE r.sender_account_id=?
          AND r.status IN ('smtp_accepted', 'sent_archive_pending',
                           'sent_archive_failed', 'sent_waiting_reconciliation',
                           'delivery_unknown', 'recovery_required')
          AND (r.package_id IS NULL OR r.package_id='')
        LIMIT 500
        """,
        (account_id,),
    ).fetchall()
    raw_matches: set[str] = set()
    composite_matches: set[str] = set()
    observed_senders = set(parse_mailboxes(from_header))
    observed_to = set(parse_mailboxes(to_header))
    observed_cc = set(parse_mailboxes(cc_header))
    for row in rows:
        request_id = str(row["send_request_id"])
        request_hash = str(row["raw_eml_sha256"] or "")
        if raw_sha256 and request_hash and raw_sha256.casefold() == request_hash.casefold():
            raw_matches.add(request_id)
        if not message_id or normalize_message_id(str(row["message_id"] or "")) != message_id:
            continue
        sender = str(row["email_address"] or "").strip().casefold()
        if not sender or sender not in observed_senders:
            continue
        if subject and str(row["subject"] or "") != subject:
            continue
        recipients = connection.execute(
            "SELECT recipient_type, email_address FROM send_request_recipients "
            "WHERE send_request_id=?",
            (request_id,),
        ).fetchall()
        expected_to = {
            str(item["email_address"] or "").casefold()
            for item in recipients
            if str(item["recipient_type"] or "") == "to"
        }
        expected_cc = {
            str(item["email_address"] or "").casefold()
            for item in recipients
            if str(item["recipient_type"] or "") == "cc"
        }
        if expected_to != observed_to or expected_cc != observed_cc:
            continue
        composite_matches.add(request_id)

    matches = [
        ("exact_send_request_raw_hash", raw_matches),
        ("deterministic_send_request_composite", composite_matches),
    ]
    nonempty = [(evidence, candidates) for evidence, candidates in matches if candidates]
    if not nonempty:
        return {
            "status": "unmatched",
            "evidence_type": "unmatched",
            "confidence": "none",
            "candidate_count": 0,
            "package_id": None,
        }
    evidence, candidates = nonempty[0]
    if len(candidates) == 1:
        selected = next(iter(candidates))
        conflicts = {
            next(iter(other))
            for _other_evidence, other in nonempty[1:]
            if len(other) == 1 and selected not in other
        }
        if not conflicts:
            return {
                "status": "matched",
                "evidence_type": evidence,
                "confidence": "exact" if evidence.startswith("exact_") else "high",
                "candidate_count": 1,
                "package_id": None,
                "send_request_id": selected,
            }
        candidates = {selected, *conflicts}
    return {
        "status": "ambiguous",
        "evidence_type": "conflicting_exact_evidence"
        if len(nonempty) > 1
        else evidence,
        "confidence": "manual_review",
        "candidate_count": len(candidates),
        "package_id": None,
    }


def _outbound_id(send_request_id: str) -> str:
    return "out_" + hashlib.sha256(
        str(send_request_id).encode("utf-8")
    ).hexdigest()[:32]


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
