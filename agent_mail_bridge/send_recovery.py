"""发件启动恢复、确定性本地对账与人工结果登记。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_mail_bridge.database import get_connection
from agent_mail_bridge.send_requests import (
    RECOVERY_SEND_STATUSES,
    complete_send_request,
    get_send_request,
    list_send_requests,
    send_lease_is_active,
)


UNCERTAIN_STAGES = {
    "legacy_sending",
    "smtp_data_started",
    "smtp_accepted",
    "archive_pending",
}


def recover_incomplete_send_requests(
    db_path: Path | str,
) -> dict[str, Any]:
    """启动时轻量分类；不访问 SMTP，也不自动重发任何请求。"""
    rows = list_send_requests(
        db_path, statuses=RECOVERY_SEND_STATUSES, limit=500
    )
    archive_requests: list[str] = []
    summary = {
        "scanned": len(rows),
        "active": 0,
        "archive_pending": 0,
        "reconciled": 0,
        "delivery_unknown": 0,
        "definitely_not_sent": 0,
        "unchanged": 0,
    }
    for request in rows:
        result = _recover_incomplete_send_request(db_path, request)
        outcome = str(result["outcome"])
        summary[outcome] += 1
        if result.get("archive_required"):
            archive_requests.append(str(request["send_request_id"]))
    return {**summary, "archive_requests": archive_requests}


def recover_incomplete_send_request(
    db_path: Path | str, send_request_id: str
) -> dict[str, Any]:
    """只恢复一个请求，供用户选择式修复复用。"""
    request = get_send_request(db_path, send_request_id)
    if request is None:
        raise ValueError("发件请求不存在")
    return _recover_incomplete_send_request(db_path, request)


def _recover_incomplete_send_request(
    db_path: Path | str, request: dict[str, Any]
) -> dict[str, Any]:
    request_id = str(request["send_request_id"])
    status = str(request.get("status") or "")
    if send_lease_is_active(request):
        return {"outcome": "active", "changed": False, "status": status}
    if status in {"sent_archive_failed", "smtp_accepted", "sent_archive_pending"}:
        return {
            "outcome": "archive_pending",
            "changed": False,
            "status": status,
            "archive_required": True,
        }
    reconciled = reconcile_send_request_locally(db_path, request_id)
    if reconciled.get("changed"):
        return {
            "outcome": "reconciled",
            "changed": True,
            "status": str(reconciled.get("status") or status),
        }
    if status in {"delivery_unknown", "sent_waiting_reconciliation"}:
        return {"outcome": "unchanged", "changed": False, "status": status}
    lease = request.get("lease") or {}
    lease_stage = str((lease or {}).get("current_stage") or "")
    stage = str(request.get("current_stage") or "")
    if stage == "stale_lease" and lease_stage:
        stage = lease_stage
    if request.get("smtp_accepted_at"):
        complete_send_request(
            db_path,
            request_id,
            status="sent_archive_failed",
            delivery_status="sent",
            provider_result=str(request.get("provider_result") or "accepted"),
            error_code="startup_archive_recovery",
            error_message="SMTP 已接受，正在恢复本地发件归档",
            outbound_id=str(request.get("outbound_id") or "") or None,
            package_id=str(request.get("package_id") or "") or None,
        )
        return {
            "outcome": "archive_pending",
            "changed": status != "sent_archive_failed",
            "status": "sent_archive_failed",
            "archive_required": True,
        }
    if stage in UNCERTAIN_STAGES:
        complete_send_request(
            db_path,
            request_id,
            status="delivery_unknown",
            delivery_status="delivery_unknown",
            error_code="startup_delivery_unknown",
            error_message="上一次执行在发送边界中断，绝不会自动重发",
        )
        _record_resolution(
            db_path,
            request_id=request_id,
            account_id=str(request.get("sender_account_id") or ""),
            status="unresolved",
            evidence_type="stale_lease_uncertain_stage",
            confidence="manual_review",
        )
        return {
            "outcome": "delivery_unknown",
            "changed": status != "delivery_unknown",
            "status": "delivery_unknown",
        }
    complete_send_request(
        db_path,
        request_id,
        status="definitely_not_sent",
        delivery_status="not_sent",
        error_code="startup_not_sent",
        error_message="执行在 SMTP DATA 前中断，可由用户创建新请求",
    )
    _record_resolution(
        db_path,
        request_id=request_id,
        account_id=str(request.get("sender_account_id") or ""),
        status="resolved_not_sent",
        evidence_type="pre_data_interruption",
        confidence="high",
    )
    return {
        "outcome": "definitely_not_sent",
        "changed": status != "definitely_not_sent",
        "status": "definitely_not_sent",
    }


def reconcile_send_request_locally(
    db_path: Path | str, send_request_id: str
) -> dict[str, Any]:
    """仅用正式本地事实和 Sent mapping 对账，不接触网络。"""
    request = get_send_request(db_path, send_request_id)
    if request is None:
        raise ValueError("发件请求不存在")
    connection = get_connection(db_path)
    rows = connection.execute(
        """
        SELECT p.package_id, p.archive_status,
               EXISTS(SELECT 1 FROM sent_server_mappings sm
                      WHERE sm.package_id=p.package_id
                        AND sm.reconciliation_status='matched') AS sent_matched,
               CASE
                 WHEN p.package_id=? THEN 0
                 WHEN EXISTS(SELECT 1 FROM outbound_messages own
                             WHERE own.package_id=p.package_id
                               AND own.request_id=?) THEN 1
                 ELSE 2
               END AS evidence_rank
        FROM mail_packages p
        WHERE p.account_id=? AND p.direction='outbound'
          AND (p.package_id=? OR EXISTS(
                 SELECT 1 FROM outbound_messages o
                 WHERE o.package_id=p.package_id AND o.request_id=?
               )
               OR (p.message_id=? COLLATE NOCASE AND p.message_id!=''))
        ORDER BY evidence_rank, sent_matched DESC, p.local_outbound DESC, p.id ASC
        LIMIT 3
        """,
        (
            str(request.get("package_id") or ""),
            send_request_id,
            str(request.get("sender_account_id") or ""),
            str(request.get("package_id") or ""),
            send_request_id,
            str(request.get("message_id") or ""),
        ),
    ).fetchall()
    if not rows:
        return {"changed": False, "status": str(request.get("status") or "")}
    best_rank = min(int(item["evidence_rank"]) for item in rows)
    candidates = {
        str(item["package_id"]): item
        for item in rows
        if int(item["evidence_rank"]) == best_rank
        and str(item["archive_status"] or "") in {"ready", "legacy"}
    }
    if len(candidates) != 1:
        if len(candidates) > 1:
            _record_resolution(
                db_path,
                request_id=send_request_id,
                account_id=str(request.get("sender_account_id") or ""),
                status="unresolved",
                evidence_type="ambiguous_local_outbound_candidates",
                confidence="manual_review",
            )
        return {
            "changed": False,
            "status": str(request.get("status") or ""),
            "ambiguous": len(candidates) > 1,
            "candidate_count": len(candidates),
        }
    row = next(iter(candidates.values()))
    matched = bool(row["sent_matched"])
    status = "sent_reconciled" if matched else "sent"
    completed = complete_send_request(
        db_path,
        send_request_id,
        status=status,
        delivery_status="sent",
        provider_result=str(request.get("provider_result") or "recovered_local_fact"),
        outbound_id=str(request.get("outbound_id") or "") or None,
        package_id=str(row["package_id"]),
    )
    _record_resolution(
        db_path,
        request_id=send_request_id,
        account_id=str(request.get("sender_account_id") or ""),
        package_id=str(row["package_id"]),
        status="matched" if matched else "local_fact_confirmed",
        evidence_type="exact_sent_mapping" if matched else "local_outbound_fact",
        confidence="exact" if matched else "high",
    )
    return {
        "changed": str(request.get("status") or "") != status,
        "status": status,
        "request": completed,
    }


def mark_send_request_resolution(
    db_path: Path | str,
    send_request_id: str,
    *,
    resolution: str,
) -> dict[str, Any]:
    """记录 GUI 用户的明确结论；不触发 SMTP。"""
    request = get_send_request(db_path, send_request_id)
    if request is None:
        raise ValueError("发件请求不存在")
    if str(request.get("status") or "") not in {
        "delivery_unknown",
        "recovery_required",
        "sent_archive_failed",
    }:
        raise ValueError("当前发件状态不允许人工确认")
    if resolution == "not_sent" and (
        request.get("smtp_accepted_at")
        or str(request.get("delivery_status") or "")
        in {"sent", "smtp_accepted"}
    ):
        raise ValueError("SMTP 已接受的邮件不能标记为未发送")
    if resolution == "sent":
        status = (
            "sent_reconciled"
            if request.get("package_id")
            else "sent_archive_failed"
        )
        delivery_status = "sent"
        evidence = "manual_confirmed_sent"
    elif resolution == "not_sent":
        status = "definitely_not_sent"
        delivery_status = "not_sent"
        evidence = "manual_confirmed_not_sent"
    else:
        raise ValueError("人工结论无效")
    completed = complete_send_request(
        db_path,
        send_request_id,
        status=status,
        delivery_status=delivery_status,
        provider_result="manual_resolution",
        outbound_id=str(request.get("outbound_id") or "") or None,
        package_id=str(request.get("package_id") or "") or None,
    )
    _record_resolution(
        db_path,
        request_id=send_request_id,
        account_id=str(request.get("sender_account_id") or ""),
        package_id=str(request.get("package_id") or "") or None,
        status="matched" if resolution == "sent" else "resolved_not_sent",
        evidence_type=evidence,
        confidence="user_confirmed",
    )
    return completed


def list_recovery_requests(
    db_path: Path | str, *, limit: int = 100
) -> list[dict[str, Any]]:
    return list_send_requests(
        db_path,
        statuses={
            "delivery_unknown",
            "sent_archive_failed",
            "recovery_required",
            "smtp_accepted",
            "sent_archive_pending",
        },
        limit=limit,
    )


def _record_resolution(
    db_path: Path | str,
    *,
    request_id: str,
    account_id: str,
    status: str,
    evidence_type: str,
    confidence: str,
    package_id: str | None = None,
) -> None:
    connection = get_connection(db_path)
    now = _now(connection)
    material = f"send_request\n{request_id}\n{evidence_type}"
    reconciliation_id = "recon_" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]
    connection.execute(
        """
        INSERT INTO reconciliation_records
            (reconciliation_id, entity_type, entity_id, account_id,
             package_id, send_request_id, status, evidence_type, confidence,
             candidate_count, details_json, first_seen_at, last_seen_at,
             resolved_at)
        VALUES (?, 'send_request', ?, ?, ?, ?, ?, ?, ?, 1, '{}', ?, ?, ?)
        ON CONFLICT(reconciliation_id) DO UPDATE SET
            package_id=COALESCE(excluded.package_id,
                                reconciliation_records.package_id),
            status=excluded.status,
            confidence=excluded.confidence,
            last_seen_at=excluded.last_seen_at,
            resolved_at=excluded.resolved_at
        """,
        (
            reconciliation_id,
            request_id,
            account_id or None,
            package_id,
            request_id,
            status,
            evidence_type,
            confidence,
            now,
            now,
            now if status != "unresolved" else None,
        ),
    )
    connection.commit()


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
