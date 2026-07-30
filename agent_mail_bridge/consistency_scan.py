"""跨数据库事实、发件恢复、membership 与 checkpoint 的只读扫描。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import get_connection
from agent_mail_bridge.security import SecurityError, assert_within_root
from agent_mail_bridge.utils import sha256_of_file


def scan_mail_consistency(
    cfg: AppConfig,
    *,
    additional_issues: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """默认只报告并记录脱敏结果；不修复、不合并、不发送。"""
    connection = get_connection(cfg.db_path)
    scan_id = f"scan_{uuid.uuid4().hex}"
    started_at = _now(connection)
    issues: list[dict[str, str]] = [
        {
            "type": str(issue.get("type") or "unknown_consistency_issue"),
            "entity_type": str(issue.get("entity_type") or "file"),
            "name": str(issue.get("name") or "unknown"),
            "severity": str(issue.get("severity") or "warning"),
        }
        for issue in additional_issues
    ]

    def add(issue_type: str, entity_type: str, entity_id: str, severity: str) -> None:
        issues.append(
            {
                "type": issue_type,
                "entity_type": entity_type,
                "name": str(entity_id),
                "severity": severity,
            }
        )

    for row in connection.execute(
        """
        SELECT mm.id, mm.package_id, mm.mailbox_id, mm.account_id,
               p.account_id AS package_account,
               m.account_id AS mailbox_account
        FROM mail_package_mailboxes mm
        LEFT JOIN mail_packages p ON p.package_id=mm.package_id
        LEFT JOIN mailboxes m ON m.mailbox_id=mm.mailbox_id
        WHERE p.package_id IS NULL OR m.mailbox_id IS NULL
           OR mm.account_id<>p.account_id OR mm.account_id<>m.account_id
        LIMIT 500
        """
    ).fetchall():
        add("membership_ownership_invalid", "membership", str(row["id"]), "error")
    for row in connection.execute(
        """
        SELECT id FROM mail_package_mailboxes
        WHERE (currently_present=1 AND removed_at IS NOT NULL)
           OR (currently_present=0 AND removed_at IS NULL)
        LIMIT 500
        """
    ).fetchall():
        add("membership_presence_invalid", "membership", str(row[0]), "warning")
    for row in connection.execute(
        "SELECT package_id FROM mail_packages WHERE direction_conflict=1 LIMIT 500"
    ).fetchall():
        add("direction_conflict", "mail_package", str(row[0]), "warning")
    for row in connection.execute(
        """
        SELECT r.send_request_id
        FROM send_requests r
        LEFT JOIN mail_packages p ON p.package_id=r.package_id
        WHERE (
            r.status IN ('sent', 'sent_reconciled') AND r.package_id IS NULL
        ) OR (
            r.package_id IS NOT NULL AND (
                p.package_id IS NULL
                OR (p.account_id IS NOT NULL
                    AND p.account_id<>r.sender_account_id)
            )
        )
        LIMIT 500
        """
    ).fetchall():
        add("send_request_package_invalid", "send_request", str(row[0]), "error")
    for row in connection.execute(
        """
        SELECT r.send_request_id
        FROM send_requests r
        LEFT JOIN outbound_messages o ON o.outbound_id=r.outbound_id
        WHERE r.outbound_id IS NOT NULL AND r.outbound_id!=''
          AND (o.outbound_id IS NULL OR o.request_id IS NULL
               OR o.request_id<>r.send_request_id)
        LIMIT 500
        """
    ).fetchall():
        add("send_request_outbound_invalid", "send_request", str(row[0]), "error")
    for row in connection.execute(
        """
        SELECT o.outbound_id
        FROM outbound_messages o
        LEFT JOIN mail_packages p ON p.package_id=o.package_id
        LEFT JOIN send_requests r ON r.send_request_id=o.request_id
        WHERE (o.package_id IS NOT NULL AND o.package_id!='' AND (
                   p.package_id IS NULL
                   OR (o.from_account_id IS NOT NULL
                       AND p.account_id IS NOT NULL
                       AND o.from_account_id<>p.account_id)
               ))
           OR (o.request_id IS NOT NULL AND o.request_id!=''
               AND r.send_request_id IS NULL)
        LIMIT 500
        """
    ).fetchall():
        add("outbound_fact_link_invalid", "outbound_message", str(row[0]), "error")
    for row in connection.execute(
        """
        SELECT r.resource_id
        FROM mail_resources r
        LEFT JOIN mail_packages p ON p.package_id=r.package_id
        WHERE p.package_id IS NULL
        LIMIT 500
        """
    ).fetchall():
        add("resource_ownership_invalid", "mail_resource", str(row[0]), "error")
    for row in connection.execute(
        """
        SELECT r.resource_id
        FROM outbound_resources r
        LEFT JOIN outbound_messages o ON o.outbound_id=r.outbound_id
        WHERE o.outbound_id IS NULL
        LIMIT 500
        """
    ).fetchall():
        add("resource_ownership_invalid", "outbound_resource", str(row[0]), "error")
    for row in connection.execute(
        """
        SELECT rm.id
        FROM received_messages rm
        LEFT JOIN mail_packages p ON p.package_id=rm.package_id
        WHERE rm.package_id IS NOT NULL AND rm.package_id!=''
          AND (p.package_id IS NULL
               OR (rm.account_id IS NOT NULL AND p.account_id IS NOT NULL
                   AND rm.account_id<>p.account_id))
        LIMIT 500
        """
    ).fetchall():
        add("received_fact_link_invalid", "received_message", str(row[0]), "error")
    for status, issue_type, severity in (
        ("delivery_unknown", "delivery_unknown", "error"),
        ("sent_archive_failed", "sent_archive_failed", "error"),
        ("recovery_required", "send_recovery_required", "warning"),
    ):
        for row in connection.execute(
            "SELECT send_request_id FROM send_requests WHERE status=? LIMIT 500",
            (status,),
        ).fetchall():
            add(issue_type, "send_request", str(row[0]), severity)
    for row in connection.execute(
        """
        SELECT sm.mapping_id
        FROM sent_server_mappings sm
        LEFT JOIN mail_packages p ON p.package_id=sm.package_id
        LEFT JOIN mailboxes m ON m.mailbox_id=sm.mailbox_id
        WHERE p.package_id IS NULL OR m.mailbox_id IS NULL
           OR sm.account_id<>p.account_id OR sm.account_id<>m.account_id
        LIMIT 500
        """
    ).fetchall():
        add("sent_mapping_invalid", "sent_mapping", str(row[0]), "error")
    for row in connection.execute(
        """
        SELECT reconciliation_id FROM reconciliation_records
        WHERE status IN ('ambiguous', 'conflict', 'unmatched', 'unresolved')
          AND resolved_at IS NULL
        LIMIT 500
        """
    ).fetchall():
        add("reconciliation_unresolved", "reconciliation", str(row[0]), "warning")
    for row in connection.execute(
        """
        SELECT account_id || ':' || raw_eml_sha256 AS candidate
        FROM mail_packages
        WHERE raw_eml_sha256 IS NOT NULL AND raw_eml_sha256!=''
        GROUP BY account_id, raw_eml_sha256 HAVING COUNT(*)>1
        LIMIT 500
        """
    ).fetchall():
        digest = hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest()[:16]
        add("duplicate_fact_candidate", "mail_package_group", digest, "warning")
    now = _now(connection)
    for row in connection.execute(
        "SELECT send_request_id FROM send_execution_leases "
        "WHERE lease_expires_at<? LIMIT 500",
        (now,),
    ).fetchall():
        add("stale_send_lease", "send_request", str(row[0]), "error")
    for row in connection.execute(
        "SELECT send_request_id FROM send_requests "
        "WHERE status='pending_confirmation' AND expires_at<? LIMIT 500",
        (now,),
    ).fetchall():
        add("expired_pending_request", "send_request", str(row[0]), "warning")
    for row in connection.execute(
        """
        SELECT mailbox_id FROM mailbox_sync_states
        WHERE last_uid<0 OR reconciliation_required=1
           OR (last_result='failed' AND last_error_at IS NULL)
        LIMIT 500
        """
    ).fetchall():
        add("checkpoint_requires_attention", "mailbox", str(row[0]), "warning")
    for row in connection.execute(
        """
        SELECT tr.id FROM mail_thread_relations tr
        LEFT JOIN mail_packages p ON p.package_id=tr.package_id
        LEFT JOIN mail_packages rp ON rp.package_id=tr.related_package_id
        WHERE p.package_id IS NULL OR rp.package_id IS NULL
           OR tr.account_id<>p.account_id OR tr.account_id<>rp.account_id
        LIMIT 500
        """
    ).fetchall():
        add("thread_relation_invalid", "thread_relation", str(row[0]), "warning")

    known_request_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT send_request_id FROM send_requests"
        ).fetchall()
    }
    request_root = cfg.send_dir / "agent_requests"
    if request_root.exists():
        for path in request_root.iterdir():
            if path.is_dir() and path.name not in known_request_ids:
                add("orphan_send_snapshot", "snapshot", path.name, "warning")
    for row in connection.execute(
        """
        SELECT attachment_id, send_request_id, snapshot_path, size_bytes, sha256
        FROM send_request_attachments WHERE status!='snapshot_cleaned'
        LIMIT 2000
        """
    ).fetchall():
        try:
            path = Path(str(row["snapshot_path"])).resolve()
            root = (request_root / str(row["send_request_id"])).resolve()
            assert_within_root(path, root)
            if not path.is_file():
                add("snapshot_missing", "attachment", str(row["attachment_id"]), "error")
            elif (
                path.stat().st_size != int(row["size_bytes"])
                or sha256_of_file(path) != str(row["sha256"])
            ):
                add("snapshot_hash_mismatch", "attachment", str(row["attachment_id"]), "error")
        except (OSError, SecurityError):
            add("snapshot_inaccessible", "attachment", str(row["attachment_id"]), "error")

    known_package_ids = {
        str(row[0])
        for row in connection.execute("SELECT package_id FROM mail_packages").fetchall()
    }
    for workspace in getattr(cfg, "allowed_send_roots", ()):
        workspace_path = Path(workspace)
        workspace_entity = "workspace_" + hashlib.sha256(
            str(workspace_path).casefold().encode("utf-8")
        ).hexdigest()[:12]
        try:
            resolved_workspace = workspace_path.resolve(strict=True)
            work_copy_root = resolved_workspace / ".agentmailbridge" / "mail"
            if not work_copy_root.exists():
                continue
            if work_copy_root.is_symlink() or not work_copy_root.is_dir():
                add("work_copy_root_unsafe", "workspace", workspace_entity, "error")
                continue
            assert_within_root(work_copy_root.resolve(strict=True), resolved_workspace)
            for package_root in work_copy_root.iterdir():
                if package_root.is_symlink() or not package_root.is_dir():
                    continue
                if package_root.name not in known_package_ids:
                    add(
                        "orphan_work_copy",
                        "work_copy",
                        package_root.name,
                        "warning",
                    )
        except (OSError, SecurityError):
            add("work_copy_root_inaccessible", "workspace", workspace_entity, "warning")

    canary = os.getenv("AGENT_MAIL_BRIDGE_SECRET_CANARY", "")
    if canary:
        leaked = 0
        for table, columns in (
            ("send_requests", ("error_message", "provider_result")),
            ("send_attempt_events", ("details_json",)),
            ("reconciliation_records", ("details_json",)),
            ("health_issues", ("details_json",)),
            ("consistency_repair_runs", ("details_json",)),
        ):
            expression = " OR ".join(
                f"instr(COALESCE({column}, ''), ?) > 0" for column in columns
            )
            leaked += int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {expression}",
                    tuple(canary for _ in columns),
                ).fetchone()[0]
            )
        if leaked:
            add("secret_canary_detected", "security", "redacted", "critical")

    summary: dict[str, int] = {}
    for issue in issues:
        summary[issue["type"]] = summary.get(issue["type"], 0) + 1
    connection.execute(
        """
        INSERT INTO consistency_scan_runs
            (scan_id, status, issue_count, summary_json, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scan_id,
            "issues_found" if issues else "clean",
            len(issues),
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            started_at,
            _now(connection),
        ),
    )
    _sync_health_issues(connection, issues)
    connection.commit()
    return {"scan_id": scan_id, "summary": summary, "issues": issues}


def _sync_health_issues(connection: Any, issues: list[dict[str, str]]) -> None:
    now = _now(connection)
    active_ids: set[str] = set()
    for issue in issues:
        material = (
            f"{issue['type']}\n{issue['entity_type']}\n{issue['name']}"
        )
        issue_id = "health_" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:24]
        active_ids.add(issue_id)
        connection.execute(
            """
            INSERT INTO health_issues
                (issue_id, category, issue_code, severity, entity_type,
                 entity_id, state, details_json, first_seen_at, last_seen_at)
            VALUES (?, 'mail_consistency', ?, ?, ?, ?, 'open', '{}', ?, ?)
            ON CONFLICT(issue_code, entity_type, entity_id) DO UPDATE SET
                severity=excluded.severity,
                state='open',
                last_seen_at=excluded.last_seen_at,
                resolved_at=NULL
            """,
            (
                issue_id,
                issue["type"],
                issue["severity"],
                issue["entity_type"],
                issue["name"],
                now,
                now,
            ),
        )
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        connection.execute(
            f"""
            UPDATE health_issues SET state='resolved', resolved_at=?, last_seen_at=?
            WHERE category='mail_consistency' AND state='open'
              AND issue_id NOT IN ({placeholders})
            """,
            (now, now, *sorted(active_ids)),
        )
    else:
        connection.execute(
            "UPDATE health_issues SET state='resolved', resolved_at=?, last_seen_at=? "
            "WHERE category='mail_consistency' AND state='open'",
            (now, now),
        )


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
