"""一致性问题分类、单项修复授权与脱敏审计。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import get_connection


class ConsistencyRepairError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_REPAIR_RULES: dict[str, dict[str, str]] = {
    "sent_archive_failed": {
        "action": "recover_sent_archive",
        "category": "发件恢复",
        "title": "恢复已接受邮件的本地归档",
        "reason": "仅使用已固定并校验的 MIME 恢复归档，不会再次发送",
    },
    "delivery_unknown": {
        "action": "reconcile_send_locally",
        "category": "发件恢复",
        "title": "重新核对结果不确定的发件",
        "reason": "只检查本地正式事实和 Sent 证据，没有确定证据时保持不变",
    },
    "send_recovery_required": {
        "action": "recover_send_state",
        "category": "发件恢复",
        "title": "恢复中断的发件状态",
        "reason": "按 SMTP 边界证据分类或恢复归档，不会自动重发",
    },
    "stale_send_lease": {
        "action": "recover_stale_send_lease",
        "category": "发件恢复",
        "title": "处理过期发件租约",
        "reason": "仅恢复所选请求；SMTP DATA 后的中断会保持结果不确定",
    },
    "expired_pending_request": {
        "action": "expire_pending_request",
        "category": "发件恢复",
        "title": "结束已过期的待确认请求",
        "reason": "只把已超过有效期的所选请求标记为过期",
    },
}


def list_consistency_repair_candidates(
    cfg: AppConfig, *, scan_id: str
) -> dict[str, Any]:
    """返回最近一次扫描的分类预览，不执行任何修复。"""
    connection = get_connection(cfg.db_path)
    _require_current_scan(connection, scan_id)
    rows = connection.execute(
        """
        SELECT issue_id, issue_code, severity, entity_type, entity_id
        FROM health_issues
        WHERE category='mail_consistency' AND state='open'
        ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                     WHEN 'warning' THEN 2 ELSE 3 END,
                 issue_code, entity_id
        LIMIT 2000
        """
    ).fetchall()
    issues = [_candidate(dict(row)) for row in rows]
    return {
        "scan_id": scan_id,
        "issues": issues,
        "repairable_count": sum(bool(row["repairable"]) for row in issues),
        "manual_count": sum(not bool(row["repairable"]) for row in issues),
    }


def begin_consistency_repair(
    cfg: AppConfig, *, scan_id: str, issue_id: str
) -> dict[str, str]:
    """锁定最近扫描中的一个白名单问题，并创建修复审计记录。"""
    connection = get_connection(cfg.db_path)
    repair_id = f"repair_{uuid.uuid4().hex}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_current_scan(connection, scan_id)
        row = connection.execute(
            """
            SELECT issue_id, issue_code, severity, entity_type, entity_id
            FROM health_issues
            WHERE issue_id=? AND category='mail_consistency' AND state='open'
            """,
            (issue_id,),
        ).fetchone()
        if row is None:
            raise ConsistencyRepairError(
                "consistency_issue_changed",
                "所选问题已变化，请重新执行一致性扫描",
            )
        candidate = _candidate(dict(row))
        if not candidate["repairable"]:
            raise ConsistencyRepairError(
                "consistency_issue_manual_only",
                "所选问题只能人工处理，不能自动修复",
            )
        now = _now(connection)
        connection.execute(
            """
            INSERT INTO consistency_repair_runs
                (repair_id, scan_id, issue_id, issue_code, entity_type,
                 entity_id, action, status, backup_name, details_json,
                 created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_backup', NULL, '{}', ?, NULL)
            """,
            (
                repair_id,
                scan_id,
                issue_id,
                candidate["issue_code"],
                candidate["entity_type"],
                candidate["entity_id"],
                candidate["action"],
                now,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "repair_id": repair_id,
        "scan_id": scan_id,
        "issue_id": issue_id,
        "issue_code": str(candidate["issue_code"]),
        "entity_type": str(candidate["entity_type"]),
        "entity_id": str(candidate["entity_id"]),
        "action": str(candidate["action"]),
    }


def mark_consistency_repair_backup(
    cfg: AppConfig, *, repair_id: str, backup_name: str
) -> None:
    connection = get_connection(cfg.db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE consistency_repair_runs
            SET backup_name=?, status='executing'
            WHERE repair_id=? AND status='awaiting_backup'
            """,
            (Path(backup_name).name, repair_id),
        )
        if cursor.rowcount != 1:
            raise ConsistencyRepairError(
                "consistency_repair_state_changed", "修复记录状态已变化"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def finish_consistency_repair(
    cfg: AppConfig,
    *,
    repair_id: str,
    status: str,
    changed: bool,
    result_code: str = "",
) -> None:
    """只记录状态和错误代码，不记录正文、路径或异常文本。"""
    if status not in {"completed", "no_change", "failed"}:
        raise ValueError("一致性修复审计状态无效")
    details = json.dumps(
        {"changed": bool(changed), "result_code": str(result_code or "")[:80]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection = get_connection(cfg.db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE consistency_repair_runs
            SET status=?, details_json=?, completed_at=?
            WHERE repair_id=? AND status IN ('awaiting_backup', 'executing')
            """,
            (status, details, _now(connection), repair_id),
        )
        if cursor.rowcount != 1:
            raise ConsistencyRepairError(
                "consistency_repair_state_changed", "修复记录状态已变化"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    issue_code = str(row.get("issue_code") or "")
    rule = _REPAIR_RULES.get(issue_code)
    if rule:
        classification = dict(rule)
        repairable = True
    else:
        classification = {
            "action": "manual_review",
            "category": _manual_category(issue_code),
            "title": "保留证据并人工处理",
            "reason": _manual_reason(issue_code),
        }
        repairable = False
    return {
        "issue_id": str(row.get("issue_id") or ""),
        "issue_code": issue_code,
        "severity": str(row.get("severity") or "warning"),
        "entity_type": str(row.get("entity_type") or "unknown"),
        "entity_id": str(row.get("entity_id") or ""),
        "repairable": repairable,
        **classification,
    }


def _manual_category(issue_code: str) -> str:
    if "secret" in issue_code:
        return "安全"
    if "checkpoint" in issue_code or "mailbox" in issue_code:
        return "同步进度"
    if any(
        token in issue_code
        for token in ("snapshot", "staging", "work_copy", "missing", "hash")
    ):
        return "存储"
    if any(
        token in issue_code
        for token in ("membership", "direction", "fact", "resource", "manifest")
    ):
        return "邮件事实"
    if "reconciliation" in issue_code or "sent_mapping" in issue_code:
        return "Sent 对账"
    return "一致性"


def _manual_reason(issue_code: str) -> str:
    if issue_code in {"duplicate_fact_candidate", "reconciliation_unresolved"}:
        return "候选证据可能不唯一，禁止静默合并"
    if "secret" in issue_code:
        return "需要先隔离敏感数据并人工核查"
    if any(token in issue_code for token in ("missing", "hash", "snapshot")):
        return "可能涉及唯一恢复资料，禁止自动删除或覆盖"
    return "当前没有足够确定的无损修复规则"


def _require_current_scan(connection: Any, scan_id: str) -> None:
    row = connection.execute(
        "SELECT scan_id FROM consistency_scan_runs "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None or str(row["scan_id"]) != str(scan_id):
        raise ConsistencyRepairError(
            "consistency_preview_stale", "修复预览已过期，请重新扫描"
        )


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
