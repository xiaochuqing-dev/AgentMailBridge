"""SQLite 备份恢复、文件一致性扫描与脱敏维护报告。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection
from agent_mail_bridge.security import SecurityError, assert_within_root
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


def backup_dir(cfg: AppConfig) -> Path:
    path = cfg.data_root_path / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_database_backup(cfg: AppConfig, *, label: str = "manual") -> dict[str, Any]:
    """使用 SQLite 在线备份能力创建并校验备份。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir(cfg) / f"agent_mail_bridge_{timestamp}_{label}.db"
    source = sqlite3.connect(str(cfg.db_path), timeout=5.0)
    target = sqlite3.connect(str(destination), timeout=5.0)
    try:
        source.backup(target)
        target.commit()
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if str(check).lower() != "ok":
            raise sqlite3.DatabaseError(f"备份完整性校验失败：{check}")
    finally:
        target.close()
        source.close()
    manifest = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database_file": destination.name,
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_of_file(destination),
        "integrity_check": "ok",
    }
    destination.with_suffix(".json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
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
        )
        for table, path_column, hash_column in specs:
            rows = connection.execute(
                f"SELECT {path_column} AS path, {hash_column} AS sha256 FROM {table} "
                f"WHERE {path_column} IS NOT NULL AND {path_column} != ''"
            ).fetchall()
            references.extend(dict(row) for row in rows)
        packages = connection.execute(
            "SELECT package_id, package_root, raw_eml_path, raw_eml_sha256, raw_eml_status "
            "FROM mail_packages"
        ).fetchall()
        for package in packages:
            root = Path(str(package["package_root"]))
            references.append({
                "path": str(root / "manifest.json"), "sha256": "",
                "scope_root": str(root), "package_id": str(package["package_id"]),
            })
            if package["raw_eml_status"] == "available" and package["raw_eml_path"]:
                references.append({
                    "path": str(root / str(package["raw_eml_path"])),
                    "sha256": str(package["raw_eml_sha256"] or ""),
                    "scope_root": str(root), "package_id": str(package["package_id"]),
                })
        resources = connection.execute(
            """
            SELECT r.package_id, p.package_root, r.local_path, r.sha256
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
            issues.append({"type": "unsafe_path", "name": Path(raw).name})
            continue
        referenced.add(path)
        try:
            if not path.is_file():
                summary.missing += 1
                issues.append({"type": "missing", "name": path.name})
                continue
            expected = str(row.get("sha256") or "")
            if expected and sha256_of_file(path) != expected:
                summary.hash_mismatch += 1
                issues.append({"type": "hash_mismatch", "name": path.name})
        except OSError:
            summary.inaccessible += 1
            issues.append({"type": "inaccessible", "name": path.name})

    excluded = {cfg.db_path.resolve()}
    roots = (cfg.received_dir, cfg.send_dir, cfg.sent_dir)
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.resolve() in excluded:
                continue
            if path.resolve() not in referenced:
                summary.orphan += 1
                issues.append({"type": "orphan", "name": path.name})

    connection = sqlite3.connect(str(cfg.db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        packages = connection.execute(
            "SELECT package_id, package_root, raw_eml_status FROM mail_packages"
        ).fetchall()
        known_package_ids = {str(row["package_id"]) for row in packages}
    finally:
        connection.close()
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
            if str(manifest.get("package_id") or "") != str(package["package_id"]):
                summary.hash_mismatch += 1
                issues.append({"type": "manifest_identity_mismatch", "name": str(package["package_id"])})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            summary.inaccessible += 1
            issues.append({"type": "manifest_invalid", "name": str(package["package_id"])})

    package_mail_root = cfg.received_dir / "mail"
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
    staging = cfg.send_dir / "staging"
    staging_roots = (staging, cfg.received_dir / "mail" / ".staging")
    for staging_root in staging_roots:
        if not staging_root.exists():
            continue
        for path in staging_root.rglob("*"):
            try:
                if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    summary.staging_residual += 1
            except OSError:
                summary.inaccessible += 1
    return {"summary": asdict(summary), "issues": issues}


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
                "mail_resources", "sent_files", "mcp_calls", "app_events",
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
