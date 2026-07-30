"""普通用户可理解的邮件运行健康摘要。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import get_connection
from agent_mail_bridge.retention_cleanup import get_safe_cleanup_summary


def get_mail_health(cfg: AppConfig) -> dict[str, Any]:
    connection = get_connection(cfg.db_path)
    mailboxes = [
        {
            "account_id": str(row["account_id"]),
            "account_name": str(row["account_name"] or row["account_id"]),
            "mailbox_id": str(row["mailbox_id"]),
            "mailbox_name": str(row["display_name"] or row["raw_name"]),
            "mailbox_role": str(row["mailbox_role"] or "other"),
            "last_success_at": row["last_success_at"],
            "last_attempt_at": row["last_attempt_at"],
            "last_result": str(row["last_result"] or "not_started"),
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "reconciliation_required": bool(row["reconciliation_required"]),
            "uidvalidity_changed_at": row["uidvalidity_changed_at"],
            "full_rescan_cursor": row["full_rescan_cursor"],
            "current_attempt": _json_object(row["current_attempt_json"]),
        }
        for row in connection.execute(
            """
            SELECT m.account_id, m.mailbox_id, m.display_name, m.raw_name,
                   m.mailbox_role, s.last_success_at, s.last_attempt_at,
                   s.last_result, s.consecutive_failures,
                   s.reconciliation_required, s.uidvalidity_changed_at,
                   s.full_rescan_cursor, s.current_attempt_json,
                   COALESCE(NULLIF(a.display_name, ''), a.email_address,
                            m.account_id) AS account_name
            FROM mailboxes m
            LEFT JOIN mail_accounts a ON a.account_id=m.account_id
            LEFT JOIN mailbox_sync_states s ON s.mailbox_id=m.mailbox_id
            WHERE m.enabled=1
            ORDER BY m.account_id, m.mailbox_role, m.display_name
            LIMIT 500
            """
        ).fetchall()
    ]
    send_counts = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM send_requests GROUP BY status"
        ).fetchall()
    }
    fact_row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM reconciliation_records
           WHERE status IN ('unmatched', 'unresolved')
             AND resolved_at IS NULL) AS unmatched_sent,
          (SELECT COUNT(*) FROM reconciliation_records
           WHERE status='ambiguous' AND resolved_at IS NULL) AS ambiguous,
          (SELECT COUNT(*) FROM health_issues
           WHERE issue_code='duplicate_fact_candidate' AND state='open')
              AS duplicate_candidates,
          (SELECT COUNT(*) FROM (
             SELECT package_id FROM mail_package_mailboxes
             WHERE currently_present=1 GROUP BY package_id HAVING COUNT(*)>1
           )) AS multi_membership,
          (SELECT COUNT(*) FROM mail_package_mailboxes
           WHERE currently_present=0) AS server_absent,
          (SELECT COUNT(*) FROM mailbox_sync_states
           WHERE reconciliation_required=1) AS rescan_required
        """
    ).fetchone()
    history_imports = [
        {
            "run_id": str(row["run_id"]),
            "account_id": str(row["account_id"]),
            "account_name": str(row["account_name"] or row["account_id"]),
            "status": str(row["status"]),
            "scanned": int(row["scanned"] or 0),
            "saved": int(row["saved"] or 0),
            "failed": int(row["failed"] or 0),
            "segment_index": int(row["segment_index"] or 0),
            "total_segments": int(row["total_segments"] or 0),
            "next_segment_index": int(row["next_segment_index"] or 0),
            "updated_at": row["updated_at"],
        }
        for row in connection.execute(
            """
            SELECT h.run_id, h.account_id, h.status, h.scanned, h.saved,
                   h.failed, h.segment_index, h.total_segments,
                   h.next_segment_index, h.updated_at,
                   COALESCE(NULLIF(a.display_name, ''), a.email_address,
                            h.account_id) AS account_name
            FROM history_import_runs h
            LEFT JOIN mail_accounts a ON a.account_id=h.account_id
            WHERE h.status IN ('running', 'partial', 'failed', 'cancelled')
            ORDER BY h.updated_at DESC, h.run_id DESC
            LIMIT 50
            """
        ).fetchall()
    ]
    open_issues = [
        {
            "issue_code": str(row["issue_code"]),
            "severity": str(row["severity"]),
            "entity_type": str(row["entity_type"]),
            "entity_id": str(row["entity_id"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
        }
        for row in connection.execute(
            """
            SELECT issue_code, severity, entity_type, entity_id,
                   first_seen_at, last_seen_at
            FROM health_issues WHERE state='open'
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                         WHEN 'warning' THEN 2 ELSE 3 END,
                     last_seen_at DESC
            LIMIT 100
            """
        ).fetchall()
    ]
    open_issue_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM health_issues WHERE state='open'"
        ).fetchone()[0]
    )
    cleanup = get_safe_cleanup_summary(cfg)
    return {
        "mailboxes": mailboxes,
        "history_imports": history_imports,
        "send": {
            "pending_confirmation": send_counts.get("pending_confirmation", 0),
            "sending": sum(
                send_counts.get(name, 0)
                for name in ("sending", "smtp_accepted", "sent_archive_pending")
            ),
            "waiting_sent": int(
                connection.execute(
                    "SELECT COUNT(*) FROM send_requests WHERE status='sent' "
                    "AND sent_reconciliation_status='waiting'"
                ).fetchone()[0]
            ),
            "delivery_unknown": send_counts.get("delivery_unknown", 0),
            "archive_recovery": send_counts.get("sent_archive_failed", 0),
            "recovery_required": send_counts.get("recovery_required", 0),
            "completed": send_counts.get("sent", 0)
            + send_counts.get("sent_reconciled", 0),
            "cancelled_or_expired": send_counts.get("cancelled", 0)
            + send_counts.get("expired", 0),
        },
        "facts": {
            "unmatched_sent": int(fact_row["unmatched_sent"] or 0),
            "ambiguous_candidates": int(fact_row["ambiguous"] or 0),
            "duplicate_candidates": int(fact_row["duplicate_candidates"] or 0),
            "multi_membership": int(fact_row["multi_membership"] or 0),
            "server_absent": int(fact_row["server_absent"] or 0),
            "rescan_required": int(fact_row["rescan_required"] or 0),
        },
        "storage": {
            "permanent_resources_bytes": _folder_size(
                [cfg.received_dir / "mail", cfg.sent_dir / "mail"],
                excluded_dir_names={".staging"},
            ),
            "send_snapshots_bytes": _folder_size(
                cfg.send_dir / "agent_requests"
            ),
            "work_copies_bytes": _folder_size(
                [
                    Path(root) / ".agentmailbridge" / "mail"
                    for root in getattr(cfg, "allowed_send_roots", [])
                ]
            ),
            "backups_bytes": _folder_size(cfg.data_root_path / "backups"),
            "safe_cleanup_bytes": int(cleanup["estimated_bytes"] or 0),
            "safe_cleanup_count": int(cleanup["eligible_count"] or 0),
        },
        "issues": open_issues,
        "issue_count": open_issue_count,
    }


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _folder_size(
    paths: Any,
    *,
    excluded_dir_names: set[str] | None = None,
) -> int:
    if isinstance(paths, (str, Path)):
        roots = [Path(paths)]
    else:
        roots = [Path(value) for value in (paths or ())]
    total = 0
    excluded = excluded_dir_names or set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and not excluded.intersection(
                    path.relative_to(root).parts
                ):
                    total += path.stat().st_size
            except OSError:
                continue
    return total
