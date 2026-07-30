"""SQLite 备份恢复、文件一致性扫描与脱敏维护报告。"""

from __future__ import annotations

import json
import sqlite3
import hashlib
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection
from agent_mail_bridge.consistency_scan import scan_mail_consistency
from agent_mail_bridge.security import SecurityError, assert_within_root
from agent_mail_bridge.storage import atomic_write_text, replace_atomically
from agent_mail_bridge.utils import sha256_of_file


@dataclass
class ScanSummary:
    missing: int = 0
    orphan: int = 0
    hash_mismatch: int = 0
    unsafe_path: int = 0
    staging_residual: int = 0
    inaccessible: int = 0
    package_missing: int = 0
    manifest_missing: int = 0
    package_orphan: int = 0
    manifest_identity_mismatch: int = 0
    manifest_raw_mismatch: int = 0
    manifest_resource_mismatch: int = 0
    manifest_resource_ownership_mismatch: int = 0


def backup_dir(cfg: AppConfig) -> Path:
    path = cfg.data_root_path / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_database_backup(cfg: AppConfig, *, label: str = "manual") -> dict[str, Any]:
    """使用 SQLite 在线备份能力创建并校验备份。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir(cfg) / f"agent_mail_bridge_{timestamp}_{label}.db"
    temporary = destination.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        source = sqlite3.connect(str(cfg.db_path), timeout=5.0)
        target = sqlite3.connect(str(temporary), timeout=5.0)
        try:
            source.backup(target)
            target.commit()
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if str(check).lower() != "ok":
                raise sqlite3.DatabaseError(f"备份完整性校验失败：{check}")
        finally:
            target.close()
            source.close()
        replace_atomically(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database_file": destination.name,
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_of_file(destination),
        "integrity_check": "ok",
    }
    atomic_write_text(
        destination.with_suffix(".json"),
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return {**manifest, "path": str(destination)}


def verify_database_backup(cfg: AppConfig, path: str | Path) -> dict[str, Any]:
    """验证备份位于本地备份目录且 SQLite 完整。"""
    candidate = Path(path).resolve()
    assert_within_root(candidate, backup_dir(cfg))
    if not candidate.is_file() or candidate.suffix.lower() != ".db":
        raise ValueError("备份文件不存在或类型不正确")
    connection = sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        check = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if str(check).lower() != "ok":
        raise sqlite3.DatabaseError(f"备份已损坏：{check}")
    manifest_path = candidate.with_suffix(".json")
    expected_hash = ""
    if manifest_path.exists():
        expected_hash = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("sha256", ""))
    actual_hash = sha256_of_file(candidate)
    if expected_hash and expected_hash != actual_hash:
        raise sqlite3.DatabaseError("备份 Hash 与清单不一致")
    return {
        "path": str(candidate),
        "name": candidate.name,
        "size_bytes": candidate.stat().st_size,
        "sha256": actual_hash,
        "integrity_check": "ok",
    }


def list_database_backups(cfg: AppConfig) -> list[dict[str, Any]]:
    """列出备份，不自动执行保留期删除。"""
    rows = []
    for path in sorted(backup_dir(cfg).glob("*.db"), reverse=True):
        try:
            item = verify_database_backup(cfg, path)
            item["status"] = "valid"
        except Exception as exc:  # noqa: BLE001
            item = {
                "path": str(path), "name": path.name,
                "size_bytes": path.stat().st_size, "status": "invalid",
                "error": str(exc),
            }
        rows.append(item)
    return rows


def restore_database_backup(cfg: AppConfig, path: str | Path) -> dict[str, Any]:
    """校验后恢复；恢复前自动备份，失败时从该备份回滚。"""
    verified = verify_database_backup(cfg, path)
    safety = create_database_backup(cfg, label="before_restore")
    close_connection()
    try:
        source = sqlite3.connect(str(verified["path"]), timeout=5.0)
        target = sqlite3.connect(str(cfg.db_path), timeout=5.0)
        try:
            source.backup(target)
            target.commit()
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if str(check).lower() != "ok":
                raise sqlite3.DatabaseError(f"恢复后校验失败：{check}")
        finally:
            target.close()
            source.close()
    except Exception:
        rollback_source = sqlite3.connect(str(safety["path"]), timeout=5.0)
        rollback_target = sqlite3.connect(str(cfg.db_path), timeout=5.0)
        try:
            rollback_source.backup(rollback_target)
            rollback_target.commit()
        finally:
            rollback_target.close()
            rollback_source.close()
        raise
    return {"restored": verified, "safety_backup": safety}


def _database_references(cfg: AppConfig) -> list[dict[str, str]]:
    connection = sqlite3.connect(str(cfg.db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        references: list[dict[str, str]] = []
        specs = (
            ("received_messages", "body_file_path", "body_sha256"),
            ("received_files", "saved_path", "sha256"),
            ("sent_files", "source_path", "sha256"),
            ("sent_files", "send_copy_path", "sha256"),
            ("sent_files", "sent_copy_path", "sha256"),
            ("outbound_resources", "staged_path", "staged_sha256"),
            ("outbound_resources", "sent_archive_path", "sent_archive_sha256"),
            ("send_requests", "raw_eml_path", "raw_eml_sha256"),
            ("send_request_attachments", "snapshot_path", "sha256"),
        )
        for table, path_column, hash_column in specs:
            rows = connection.execute(
                f"SELECT rowid AS reference_id, {path_column} AS path, "
                f"{hash_column} AS sha256 FROM {table} "
                f"WHERE {path_column} IS NOT NULL AND {path_column} != ''"
            ).fetchall()
            references.extend(
                {
                    **dict(row),
                    "entity_type": table,
                    "entity_id": str(row["reference_id"]),
                }
                for row in rows
            )
        packages = connection.execute(
            "SELECT package_id, package_root, raw_eml_path, raw_eml_sha256, raw_eml_status "
            "FROM mail_packages"
        ).fetchall()
        for package in packages:
            root = Path(str(package["package_root"]))
            references.append({
                "path": str(root / "manifest.json"), "sha256": "",
                "scope_root": str(root), "package_id": str(package["package_id"]),
                "entity_type": "mail_package_manifest",
                "entity_id": str(package["package_id"]),
            })
            if package["raw_eml_status"] == "available" and package["raw_eml_path"]:
                references.append({
                    "path": str(root / str(package["raw_eml_path"])),
                    "sha256": str(package["raw_eml_sha256"] or ""),
                    "scope_root": str(root), "package_id": str(package["package_id"]),
                    "entity_type": "raw_eml",
                    "entity_id": str(package["package_id"]),
                })
        resources = connection.execute(
            """
            SELECT r.package_id, r.resource_id, p.package_root,
                   r.local_path, r.sha256
            FROM mail_resources r JOIN mail_packages p ON p.package_id = r.package_id
            WHERE r.local_path IS NOT NULL AND r.local_path != ''
            """
        ).fetchall()
        for resource in resources:
            root = Path(str(resource["package_root"]))
            references.append({
                "path": str(root / str(resource["local_path"])),
                "sha256": str(resource["sha256"] or ""),
                "scope_root": str(root), "package_id": str(resource["package_id"]),
                "entity_type": "mail_resource",
                "entity_id": str(resource["resource_id"]),
            })
        return references
    finally:
        connection.close()


def scan_consistency(cfg: AppConfig) -> dict[str, Any]:
    """默认只报告，不删除、移动或重建任何用户数据。"""
    summary = ScanSummary()
    issues: list[dict[str, str]] = []
    referenced: set[Path] = set()
    for row in _database_references(cfg):
        raw = str(row.get("path") or "")
        try:
            path = Path(raw).resolve()
            assert_within_root(path, cfg.data_root_path)
            if row.get("scope_root"):
                assert_within_root(path, Path(row["scope_root"]).resolve())
        except SecurityError:
            summary.unsafe_path += 1
            issues.append({
                "type": "unsafe_path",
                "entity_type": str(row.get("entity_type") or "file"),
                "name": str(row.get("entity_id") or Path(raw).name),
                "severity": "error",
            })
            continue
        referenced.add(path)
        try:
            if not path.is_file():
                summary.missing += 1
                issues.append({
                    "type": "missing",
                    "entity_type": str(row.get("entity_type") or "file"),
                    "name": str(row.get("entity_id") or path.name),
                    "severity": "error",
                })
                continue
            expected = str(row.get("sha256") or "")
            if expected and sha256_of_file(path) != expected:
                summary.hash_mismatch += 1
                issues.append({
                    "type": "hash_mismatch",
                    "entity_type": str(row.get("entity_type") or "file"),
                    "name": str(row.get("entity_id") or path.name),
                    "severity": "error",
                })
        except OSError:
            summary.inaccessible += 1
            issues.append({
                "type": "inaccessible",
                "entity_type": str(row.get("entity_type") or "file"),
                "name": str(row.get("entity_id") or path.name),
                "severity": "error",
            })

    excluded = {cfg.db_path.resolve()}
    roots = (cfg.received_dir, cfg.send_dir, cfg.sent_dir)
    staging_roots = (
        cfg.send_dir / "staging",
        cfg.received_dir / "mail" / ".staging",
        cfg.sent_dir / "mail" / ".staging",
    )
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.resolve() in excluded:
                continue
            resolved = path.resolve()
            if any(
                staging_root.exists()
                and resolved.is_relative_to(staging_root.resolve())
                for staging_root in staging_roots
            ):
                continue
            if resolved not in referenced:
                summary.orphan += 1
                issues.append({"type": "orphan", "name": path.name})

    connection = sqlite3.connect(str(cfg.db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        packages = connection.execute(
            "SELECT package_id, package_root, account_id, raw_eml_status, "
            "raw_eml_path, raw_eml_sha256 FROM mail_packages"
        ).fetchall()
        known_package_ids = {str(row["package_id"]) for row in packages}
        resource_rows = connection.execute(
            """
            SELECT package_id, resource_id, resource_type, local_path,
                   size_bytes, sha256, status
            FROM mail_resources
            """
        ).fetchall()
    finally:
        connection.close()
    resources_by_package: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for resource in resource_rows:
        resources_by_package[str(resource["package_id"])][
            str(resource["resource_id"])
        ] = dict(resource)
    for package in packages:
        root = Path(str(package["package_root"]))
        try:
            assert_within_root(root, cfg.data_root_path)
        except SecurityError:
            continue
        if not root.is_dir():
            summary.package_missing += 1
            issues.append({"type": "package_missing", "name": str(package["package_id"])})
            continue
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            summary.manifest_missing += 1
            issues.append({"type": "manifest_missing", "name": str(package["package_id"])})
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            identity_mismatch = (
                str(manifest.get("package_id") or "")
                != str(package["package_id"])
            )
            manifest_account = str(manifest.get("account_id") or "")
            package_account = str(package["account_id"] or "")
            if manifest_account and package_account and manifest_account != package_account:
                identity_mismatch = True
            if identity_mismatch:
                summary.manifest_identity_mismatch += 1
                issues.append({
                    "type": "manifest_identity_mismatch",
                    "entity_type": "mail_package",
                    "name": str(package["package_id"]),
                    "severity": "error",
                })
            raw_fact = manifest.get("raw_eml")
            expected_raw = {
                "status": str(package["raw_eml_status"] or ""),
                "path": str(package["raw_eml_path"] or ""),
                "sha256": str(package["raw_eml_sha256"] or ""),
            }
            actual_raw = {
                "status": str((raw_fact or {}).get("status") or ""),
                "path": str((raw_fact or {}).get("path") or ""),
                "sha256": str((raw_fact or {}).get("sha256") or ""),
            } if isinstance(raw_fact, dict) else {}
            if actual_raw != expected_raw:
                summary.manifest_raw_mismatch += 1
                issues.append({
                    "type": "manifest_raw_mismatch",
                    "entity_type": "mail_package",
                    "name": str(package["package_id"]),
                    "severity": "error",
                })
            manifest_resources = manifest.get("resources")
            if not isinstance(manifest_resources, list):
                raise ValueError("manifest resources must be a list")
            manifest_by_id: dict[str, dict[str, Any]] = {}
            duplicate_resource_ids: set[str] = set()
            for raw_resource in manifest_resources:
                if not isinstance(raw_resource, dict):
                    raise ValueError("manifest resource must be an object")
                resource_id = str(raw_resource.get("resource_id") or "")
                if not resource_id or resource_id in manifest_by_id:
                    duplicate_resource_ids.add(resource_id or "missing")
                    continue
                manifest_by_id[resource_id] = raw_resource
            database_resources = resources_by_package.get(
                str(package["package_id"]), {}
            )
            if duplicate_resource_ids or set(manifest_by_id) != set(database_resources):
                summary.manifest_resource_ownership_mismatch += 1
                issues.append({
                    "type": "manifest_resource_ownership_mismatch",
                    "entity_type": "mail_package",
                    "name": str(package["package_id"]),
                    "severity": "error",
                })
            resource_mismatch = False
            for resource_id in set(manifest_by_id).intersection(database_resources):
                manifest_resource = manifest_by_id[resource_id]
                database_resource = database_resources[resource_id]
                manifest_size = manifest_resource.get("size_bytes")
                database_size = database_resource.get("size_bytes")
                if (
                    str(manifest_resource.get("internal_type") or "")
                    != str(database_resource.get("resource_type") or "")
                    or str(manifest_resource.get("path") or "")
                    != str(database_resource.get("local_path") or "")
                    or str(manifest_resource.get("sha256") or "")
                    != str(database_resource.get("sha256") or "")
                    or (None if manifest_size is None else int(manifest_size))
                    != (None if database_size is None else int(database_size))
                    or str(manifest_resource.get("status") or "")
                    != str(database_resource.get("status") or "")
                ):
                    resource_mismatch = True
                    break
            if resource_mismatch:
                summary.manifest_resource_mismatch += 1
                issues.append({
                    "type": "manifest_resource_mismatch",
                    "entity_type": "mail_package",
                    "name": str(package["package_id"]),
                    "severity": "error",
                })
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            summary.inaccessible += 1
            issues.append({
                "type": "manifest_invalid",
                "entity_type": "mail_package",
                "name": str(package["package_id"]),
                "severity": "error",
            })

    for package_mail_root in (
        cfg.received_dir / "mail",
        cfg.sent_dir / "mail",
    ):
        if package_mail_root.exists():
            for manifest_path in package_mail_root.rglob("manifest.json"):
                if ".staging" in manifest_path.parts:
                    continue
                try:
                    package_id = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("package_id") or "")
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                if package_id and package_id not in known_package_ids:
                    summary.package_orphan += 1
                    issues.append({"type": "package_orphan", "name": package_id})

    cutoff = datetime.now() - timedelta(hours=24)  # 暂存残留阈值：24 小时
    for staging_root in staging_roots:
        if not staging_root.exists():
            continue
        for path in staging_root.rglob("*"):
            try:
                if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    summary.staging_residual += 1
                    relative_key = str(path.relative_to(staging_root)).casefold()
                    issues.append({
                        "type": "staging_residual",
                        "entity_type": "staging_file",
                        "name": "staging_" + hashlib.sha256(
                            relative_key.encode("utf-8")
                        ).hexdigest()[:16],
                        "severity": "warning",
                    })
            except OSError:
                summary.inaccessible += 1
                issues.append({
                    "type": "inaccessible",
                    "entity_type": "staging_file",
                    "name": path.name,
                    "severity": "warning",
                })
    mail_scan = scan_mail_consistency(cfg, additional_issues=issues)
    combined_summary = asdict(ScanSummary())
    combined_summary.update(mail_scan["summary"])
    return {
        "scan_id": mail_scan["scan_id"],
        "summary": combined_summary,
        "issues": mail_scan["issues"],
    }


def data_statistics(cfg: AppConfig) -> dict[str, Any]:
    """返回维护页所需的非敏感容量与计数。"""
    def folder_state(path: Path) -> dict[str, int]:
        files = [item for item in path.rglob("*") if item.is_file()] if path.exists() else []
        total = 0
        for item in files:
            try:
                total += item.stat().st_size
            except OSError:
                pass
        return {"files": len(files), "size_bytes": total}

    connection = sqlite3.connect(str(cfg.db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "received_messages", "received_files", "mail_packages",
                "mail_resources", "outbound_messages", "outbound_resources",
                "outbound_links", "sent_files", "mcp_calls", "app_events",
            )
        }
        archive_counts = connection.execute(
            """
            SELECT
                COUNT(*) AS package_count,
                SUM(CASE WHEN archive_status = 'partial' THEN 1 ELSE 0 END) AS partial_count,
                SUM(CASE WHEN archive_status = 'needs_attention' THEN 1 ELSE 0 END) AS needs_attention_count
            FROM mail_packages
            """
        ).fetchone()
        resource_sizes = {
            str(row["resource_type"]): int(row["size_bytes"] or 0)
            for row in connection.execute(
                """
                SELECT resource_type, SUM(COALESCE(size_bytes, 0)) AS size_bytes
                FROM mail_resources GROUP BY resource_type
                """
            ).fetchall()
        }
    finally:
        connection.close()
    backups = list_database_backups(cfg)
    package_state = folder_state(cfg.received_dir / "mail")
    raw_size = 0
    connection = sqlite3.connect(str(cfg.db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        for row in connection.execute(
            "SELECT package_root, raw_eml_path FROM mail_packages WHERE raw_eml_status = 'available'"
        ).fetchall():
            try:
                raw_path = Path(str(row["package_root"])) / str(row["raw_eml_path"])
                assert_within_root(raw_path, cfg.data_root_path)
                if raw_path.is_file():
                    raw_size += raw_path.stat().st_size
            except (OSError, SecurityError):
                pass
    finally:
        connection.close()
    return {
        "database_size_bytes": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
        "integrity_check": integrity,
        "counts": counts,
        "received": folder_state(cfg.received_dir),
        "send": folder_state(cfg.send_dir),
        "sent": folder_state(cfg.sent_dir),
        "logs": folder_state(cfg.logs_dir),
        "backups": backups,
        "backups_size_bytes": sum(
            int(item.get("size_bytes") or 0) for item in backups
        ),
        "mail_archive": {
            "package_count": int(archive_counts["package_count"] or 0),
            "package_size_bytes": package_state["size_bytes"],
            "raw_eml_size_bytes": raw_size,
            "body_size_bytes": sum(
                resource_sizes.get(name, 0)
                for name in ("body_plain", "body_html", "body_readable")
            ),
            "attachment_size_bytes": resource_sizes.get("attachment", 0),
            "inline_image_size_bytes": resource_sizes.get("inline_image", 0),
            "downloads_size_bytes": resource_sizes.get("downloaded_file", 0),
            "partial_count": int(archive_counts["partial_count"] or 0),
            "needs_attention_count": int(archive_counts["needs_attention_count"] or 0),
        },
    }


def export_maintenance_report(cfg: AppConfig, destination: str | Path) -> Path:
    """导出不含正文、内容、凭据和完整私人路径的维护报告。"""
    target = Path(destination)
    if target.exists():
        raise FileExistsError("目标报告已存在")
    stats = data_statistics(cfg)
    scan = scan_consistency(cfg)
    summary = scan["summary"]
    lines = [
        "# AgentMailBridge 脱敏维护报告", "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"数据库完整性：{stats['integrity_check']}",
        f"数据库大小：{stats['database_size_bytes']} 字节", "",
        "## 记录数量", "",
    ]
    lines.extend(f"- {name}：{count}" for name, count in stats["counts"].items())
    lines.extend([
        "", "## 一致性结果", "",
        f"- 缺失文件：{summary['missing']}",
        f"- 孤立文件：{summary['orphan']}",
        f"- Hash 异常：{summary['hash_mismatch']}",
        f"- 越界路径：{summary['unsafe_path']}",
        f"- 暂存残留：{summary['staging_residual']}",
        f"- 无法访问：{summary['inaccessible']}",
        f"- 可用备份：{sum(item['status'] == 'valid' for item in stats['backups'])}",
        "", "建议：先验证备份，再处理异常清单；本工具不会自动删除用户数据。", "",
        "隐私：本报告不包含邮件正文、附件内容、密码、token 或完整私人路径。", "",
    ])
    atomic_write_text(target, "\n".join(lines))
    return target
