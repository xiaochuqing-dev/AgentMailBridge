"""阶段 C：数据库备份恢复与数据一致性维护测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import get_connection, log_event
from agent_mail_bridge.maintenance import backup_dir
from agent_mail_bridge.models import OperationStatus
from agent_mail_bridge.utils import sha256_of_file


def test_online_backup_is_created_listed_and_verified(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    log_event(tmp_cfg.db_path, "INFO", "test", "backup source")

    created = service.create_backup()
    listed = service.list_backups()
    verified = service.verify_backup(created.details["path"])

    assert created.ok
    assert Path(created.details["path"]).is_file()
    assert created.details["integrity_check"] == "ok"
    assert listed.details["backups"][0]["status"] == "valid"
    assert verified.ok


def test_corrupted_backup_is_rejected(tmp_cfg):
    corrupt = backup_dir(tmp_cfg) / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")

    result = ApplicationService(tmp_cfg).verify_backup(corrupt)

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "backup_invalid"


def test_restore_requires_confirmation_and_creates_safety_backup(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    log_event(tmp_cfg.db_path, "INFO", "test", "before backup")
    backup = service.create_backup().details["path"]
    log_event(tmp_cfg.db_path, "INFO", "test", "after backup")

    refused = service.restore_backup(backup)
    restored = service.restore_backup(backup, confirmed=True)
    count = get_connection(tmp_cfg.db_path).execute(
        "SELECT COUNT(*) FROM app_events WHERE event_type = 'test'"
    ).fetchone()[0]

    assert refused.error_code == "restore_confirmation_required"
    assert restored.ok
    assert count == 1
    assert "before_restore" in Path(restored.details["safety_backup"]["path"]).name


def test_consistency_scan_finds_missing_orphan_hash_and_unsafe(tmp_cfg, tmp_path):
    valid = tmp_cfg.received_dir / "valid.txt"
    valid.write_text("changed", encoding="utf-8")
    missing = tmp_cfg.received_dir / "missing.txt"
    orphan = tmp_cfg.sent_dir / "orphan.txt"
    orphan.write_text("orphan", encoding="utf-8")
    unsafe = tmp_path / "outside.txt"
    connection = get_connection(tmp_cfg.db_path)
    now = "2026-07-11 12:00:00"
    for message_id, path, digest in (
        ("missing", missing, "a" * 64),
        ("changed", valid, "b" * 64),
        ("unsafe", unsafe, "c" * 64),
    ):
        connection.execute(
            """
            INSERT INTO received_files
                (message_id, saved_path, sha256, status, created_at, updated_at)
            VALUES (?, ?, ?, 'saved', ?, ?)
            """,
            (message_id, str(path), digest, now, now),
        )
    connection.commit()

    result = ApplicationService(tmp_cfg).scan_consistency()
    summary = result.details["summary"]

    assert result.ok
    assert summary["missing"] == 1
    assert summary["orphan"] >= 1
    assert summary["hash_mismatch"] == 1
    assert summary["unsafe_path"] == 1
    assert orphan.is_file()


def test_maintenance_report_is_redacted(tmp_cfg, tmp_path):
    destination = tmp_path / "maintenance.md"
    result = ApplicationService(tmp_cfg).export_maintenance_report(destination)
    content = destination.read_text(encoding="utf-8")

    assert result.ok
    assert "数据库完整性" in content
    assert tmp_cfg.gmail_app_password not in content
    assert tmp_cfg.qq_auth_code not in content
    assert str(tmp_cfg.data_root_path) not in content
