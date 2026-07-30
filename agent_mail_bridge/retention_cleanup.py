"""AgentMailBridge 管理的发件快照保留与显式安全清理。"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import get_connection
from agent_mail_bridge.security import assert_within_root
from agent_mail_bridge.storage import replace_atomically


SHORT_RETENTION_DAYS = 7
RECONCILED_RETENTION_DAYS = 30
PROTECTED_STATUSES = {
    "pending_confirmation",
    "sending",
    "smtp_accepted",
    "sent_archive_pending",
    "sent_archive_failed",
    "delivery_unknown",
    "recovery_required",
}


def recover_snapshot_cleanup_transactions(cfg: AppConfig) -> dict[str, int]:
    """恢复跨崩溃的目录重命名；不触碰任何永久邮件事实。"""
    quarantine_root = _quarantine_root(cfg)
    result = {"restored": 0, "purged": 0, "unresolved": 0}
    if not quarantine_root.exists():
        return result
    connection = get_connection(cfg.db_path)
    for quarantine in quarantine_root.iterdir():
        if (
            not quarantine.is_dir()
            or quarantine.is_symlink()
            or (hasattr(quarantine, "is_junction") and quarantine.is_junction())
        ):
            result["unresolved"] += 1
            continue
        request_id = quarantine.name.rsplit(".", 1)[0]
        row = connection.execute(
            "SELECT snapshot_cleaned_at FROM send_requests "
            "WHERE send_request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            result["unresolved"] += 1
            continue
        root = _request_root(cfg, request_id)
        try:
            if row[0]:
                shutil.rmtree(quarantine)
                result["purged"] += 1
            elif not root.exists():
                root.parent.mkdir(parents=True, exist_ok=True)
                replace_atomically(quarantine, root)
                result["restored"] += 1
            else:
                result["unresolved"] += 1
        except OSError:
            result["unresolved"] += 1
    return result


def preview_send_snapshot_cleanup(
    cfg: AppConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return cleanup_send_snapshots(cfg, dry_run=True, now=now)


def get_safe_cleanup_summary(
    cfg: AppConfig,
    *,
    request_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """只计算可清理项和空间，不删除文件，也不写入清理审计。"""
    current = now or datetime.now()
    selected = {
        str(value) for value in (request_ids or ()) if str(value).strip()
    }
    candidates = _eligible_requests(cfg, current=current)
    if selected:
        candidates = [
            item
            for item in candidates
            if str(item["send_request_id"]) in selected
        ]
    rows: list[dict[str, Any]] = []
    estimated = 0
    for request in candidates:
        request_id = str(request["send_request_id"])
        size = _tree_size(_request_root(cfg, request_id))
        estimated += size
        rows.append(
            {
                "send_request_id": request_id,
                "status": str(request.get("status") or ""),
                "size_bytes": size,
                "eligible_reason": str(request.get("eligible_reason") or ""),
            }
        )
    return {
        "eligible_count": len(rows),
        "estimated_bytes": estimated,
        "items": rows,
    }


def cleanup_send_snapshots(
    cfg: AppConfig,
    *,
    dry_run: bool = True,
    request_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """默认只预览；执行时只删产品管理的 request 快照目录。"""
    current = now or datetime.now()
    recovery = {"restored": 0, "purged": 0, "unresolved": 0}
    if not dry_run:
        recovery = recover_snapshot_cleanup_transactions(cfg)
    preview = get_safe_cleanup_summary(
        cfg, request_ids=request_ids, now=current
    )
    rows = list(preview["items"])
    estimated = int(preview["estimated_bytes"])

    cleanup_id = f"cleanup_{uuid.uuid4().hex}"
    cleaned = 0
    released = 0
    failures = 0
    eligibility_changed = 0
    pending_delete = 0
    if not dry_run:
        for item in rows:
            request_id = str(item["send_request_id"])
            try:
                outcome = _delete_snapshot_if_still_eligible(
                    cfg, request_id=request_id, current=current
                )
                if outcome["cleaned"]:
                    cleaned += 1
                    released += int(outcome["released_bytes"])
                    pending_delete += int(outcome["pending_delete"])
                else:
                    eligibility_changed += 1
            except Exception:  # noqa: BLE001 - audit counts, never exposes paths
                failures += 1
    status = (
        "preview"
        if dry_run
        else "partial"
        if failures or eligibility_changed or pending_delete or recovery["unresolved"]
        else "completed"
    )
    details = {
        "cleanup_id": cleanup_id,
        "dry_run": bool(dry_run),
        "status": status,
        "eligible_count": len(rows),
        "cleaned_count": cleaned,
        "failed_count": failures,
        "eligibility_changed_count": eligibility_changed,
        "pending_delete_count": pending_delete,
        "estimated_bytes": estimated,
        "released_bytes": released,
        "items": rows,
        "startup_recovery": recovery,
    }
    _record_cleanup(cfg, details)
    return details


def _eligible_requests(
    cfg: AppConfig, *, current: datetime
) -> list[dict[str, Any]]:
    connection = get_connection(cfg.db_path)
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT r.*,
                   EXISTS(SELECT 1 FROM mail_packages p
                          WHERE p.package_id=r.package_id
                            AND p.archive_status IN ('ready', 'legacy'))
                       AS package_ready,
                   EXISTS(SELECT 1 FROM sent_server_mappings sm
                          WHERE sm.package_id=r.package_id
                            AND sm.reconciliation_status='matched')
                       AS sent_matched
            FROM send_requests r
            WHERE snapshot_cleaned_at IS NULL
            """
        ).fetchall()
    ]
    result: list[dict[str, Any]] = []
    short_cutoff = current - timedelta(days=SHORT_RETENTION_DAYS)
    reconciled_cutoff = current - timedelta(days=RECONCILED_RETENTION_DAYS)
    for row in rows:
        status = str(row.get("status") or "")
        if status in PROTECTED_STATUSES:
            continue
        updated = _parse_time(str(row.get("updated_at") or ""))
        eligible_reason = ""
        if status in {"cancelled", "expired", "definitely_not_sent"}:
            if updated and updated <= short_cutoff:
                eligible_reason = "terminal_short_retention_elapsed"
        elif status == "failed" and not bool(row.get("recovery_required")):
            if updated and updated <= short_cutoff:
                eligible_reason = "failed_without_recovery_material_need"
        elif status in {"sent", "sent_reconciled"}:
            if (
                updated
                and updated <= reconciled_cutoff
                and bool(row.get("package_ready"))
                and bool(row.get("sent_matched"))
            ):
                eligible_reason = "sent_reconciled_retention_elapsed"
        if eligible_reason:
            row["eligible_reason"] = eligible_reason
            result.append(row)
    return result


def _verify_managed_snapshot_paths(
    cfg: AppConfig, request_id: str, root: Path
) -> None:
    assert_within_root(root, cfg.send_dir)
    candidate = cfg.send_dir / "agent_requests" / request_id
    if candidate.exists() and (
        candidate.is_symlink()
        or (hasattr(candidate, "is_junction") and candidate.is_junction())
    ):
        raise OSError("发件快照目录不能是链接或联接点")
    expected = candidate.resolve()
    if root != expected:
        raise OSError("发件快照目录身份不匹配")
    connection = get_connection(cfg.db_path)
    for row in connection.execute(
        "SELECT snapshot_path FROM send_request_attachments "
        "WHERE send_request_id=?",
        (request_id,),
    ).fetchall():
        path = Path(str(row[0] or "")).resolve()
        assert_within_root(path, root)


def _delete_snapshot_if_still_eligible(
    cfg: AppConfig, *, request_id: str, current: datetime
) -> dict[str, int | bool]:
    """事务内复核并原子隔离；提交失败时把目录放回原位。"""
    connection = get_connection(cfg.db_path)
    root = _request_root(cfg, request_id)
    quarantine: Path | None = None
    moved = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        eligible = {
            str(row["send_request_id"]): row
            for row in _eligible_requests(cfg, current=current)
        }
        if request_id not in eligible:
            connection.rollback()
            return {"cleaned": False, "released_bytes": 0, "pending_delete": 0}
        _verify_managed_snapshot_paths(cfg, request_id, root)
        if root.exists():
            quarantine_root = _quarantine_root(cfg)
            quarantine_root.mkdir(parents=True, exist_ok=True)
            quarantine = quarantine_root / f"{request_id}.{uuid.uuid4().hex}"
            replace_atomically(root, quarantine)
            moved = True
        now = _now(connection)
        _mark_snapshot_cleaned(connection, request_id=request_id, now=now)
        connection.commit()
        released = 0
        pending_delete = 0
        if quarantine is not None and quarantine.exists():
            quarantined_size = _tree_size(quarantine)
            try:
                shutil.rmtree(quarantine)
                released = quarantined_size
            except OSError:
                pending_delete = 1
        return {
            "cleaned": True,
            "released_bytes": released,
            "pending_delete": pending_delete,
        }
    except Exception:
        connection.rollback()
        if moved and quarantine is not None and quarantine.exists() and not root.exists():
            try:
                replace_atomically(quarantine, root)
            except OSError:
                pass
        raise


def _mark_snapshot_cleaned(
    connection: Any, *, request_id: str, now: str
) -> None:
    connection.execute(
        "UPDATE send_request_attachments SET status='snapshot_cleaned', "
        "updated_at=? WHERE send_request_id=?",
        (now, request_id),
    )
    connection.execute(
        "UPDATE send_requests SET snapshot_cleaned_at=?, updated_at=? "
        "WHERE send_request_id=?",
        (now, now, request_id),
    )


def _record_cleanup(cfg: AppConfig, details: dict[str, Any]) -> None:
    connection = get_connection(cfg.db_path)
    now = _now(connection)
    audit_items = [
        {
            "send_request_id": str(item.get("send_request_id") or ""),
            "status": str(item.get("status") or ""),
            "size_bytes": int(item.get("size_bytes") or 0),
            "eligible_reason": str(item.get("eligible_reason") or ""),
        }
        for item in details.get("items") or []
    ]
    connection.execute(
        """
        INSERT INTO cleanup_records
            (cleanup_id, dry_run, status, eligible_count, cleaned_count,
             estimated_bytes, released_bytes, details_json, created_at,
             completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            details["cleanup_id"],
            1 if details.get("dry_run") else 0,
            details["status"],
            int(details.get("eligible_count") or 0),
            int(details.get("cleaned_count") or 0),
            int(details.get("estimated_bytes") or 0),
            int(details.get("released_bytes") or 0),
            json.dumps(
                {
                    "items": audit_items,
                    "failed_count": details.get("failed_count", 0),
                    "eligibility_changed_count": details.get(
                        "eligibility_changed_count", 0
                    ),
                    "pending_delete_count": details.get(
                        "pending_delete_count", 0
                    ),
                    "startup_recovery": details.get("startup_recovery", {}),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            now,
            None if details.get("dry_run") else now,
        ),
    )
    connection.commit()


def _request_root(cfg: AppConfig, request_id: str) -> Path:
    root = (cfg.send_dir / "agent_requests" / request_id).resolve()
    assert_within_root(root, cfg.data_root_path)
    return root


def _quarantine_root(cfg: AppConfig) -> Path:
    root = (cfg.send_dir / "staging" / "snapshot_cleanup").resolve()
    assert_within_root(root, cfg.data_root_path)
    return root


def _tree_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _now(connection: Any) -> str:
    return str(
        connection.execute(
            "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
        ).fetchone()[0]
    )
