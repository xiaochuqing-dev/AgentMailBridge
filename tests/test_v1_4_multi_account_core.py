"""v1.4 第一阶段 Multi-Account Core 专项回归。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    close_connection,
    create_outbound_message,
    get_auto_receive_state,
    get_connection,
    init_db,
    multi_account_migration_needed,
    query_mail_accounts,
    query_mailboxes,
    save_auto_receive_state,
    sync_mail_accounts,
)
from agent_mail_bridge.mail_accounts import (
    MailAccount,
    current_receive_account_id,
    current_send_account_id,
    legacy_accounts_from_config,
    stable_account_id,
)
from agent_mail_bridge.mail_archive import archive_normalized_mail
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_facts import list_mail_messages
from agent_mail_bridge.provider_adapters import get_provider_adapter
from agent_mail_bridge.utils import sha256_of_file


def _downgrade_schema_to_v13(db_path: Path) -> None:
    close_connection()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE IF EXISTS account_sync_states")
        connection.execute("DROP TABLE IF EXISTS mailboxes")
        connection.execute("DROP TABLE IF EXISTS mail_accounts")
        connection.execute(
            "DELETE FROM migration_metadata WHERE migration_key = 'multi_account_core_v1'"
        )
        connection.execute("DROP TABLE received_messages")
        connection.execute(
            """
            CREATE TABLE received_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE,
                gmail_uid TEXT,
                subject TEXT,
                from_email TEXT,
                to_email TEXT,
                received_at TEXT,
                saved_date TEXT,
                body_file_path TEXT,
                body_sha256 TEXT,
                has_attachments INTEGER,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                source TEXT,
                gmail_message_id TEXT,
                gmail_thread_id TEXT,
                backend TEXT,
                package_id TEXT
            )
            """
        )
        connection.execute("DROP TABLE receive_retries")
        connection.execute(
            """
            CREATE TABLE receive_retries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backend TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                message_id TEXT,
                attachment_id TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_attempt_at TEXT,
                next_retry_at TEXT,
                terminal_status TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (backend, resource_id, attachment_id)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def _seed_v13_facts(cfg, raw_path: Path) -> None:
    connection = sqlite3.connect(cfg.db_path)
    try:
        now = "2026-07-22 12:00:00"
        raw_hash = sha256_of_file(raw_path)
        connection.execute(
            """
            INSERT INTO mail_packages
                (package_id, account_ref, mailbox_ref, backend, message_id,
                 provider_message_id, subject, from_email, to_emails,
                 received_at, saved_at, package_root, raw_eml_path,
                 raw_eml_sha256, raw_eml_status, contacts_json,
                 resource_count, attachment_count, inline_image_count,
                 link_count, downloaded_count, archive_status, parse_status,
                 legacy, created_at, updated_at)
            VALUES ('pkg_old', ?, 'gmail:me/inbox', 'gmail_api', '<old@test>',
                    'gmail-old', '旧邮件', 'sender@example.com', ?, ?, ?, ?,
                    'raw.eml', ?, 'available', '{}', 0, 0, 0, 0, 0,
                    'ready', 'parsed', 0, ?, ?)
            """,
            (
                f"gmail:{cfg.gmail_address}", cfg.gmail_address, now, now,
                str(raw_path.parent), raw_hash, now, now,
            ),
        )
        connection.execute(
            """
            INSERT INTO received_messages
                (message_id, subject, from_email, to_email, received_at,
                 saved_date, has_attachments, status, created_at, updated_at,
                 source, gmail_message_id, backend, package_id)
            VALUES ('<old@test>', '旧邮件', 'sender@example.com', ?, ?,
                    '2026-07-22', 0, 'saved', ?, ?, 'gmail_api',
                    'gmail-old', 'gmail_api', 'pkg_old')
            """,
            (cfg.gmail_address, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO outbound_messages
                (outbound_id, sender_account_ref, sender_ref, source_origin,
                 subject, body_text, to_emails, status, attachment_count,
                 link_count, legacy_limited, created_at, updated_at)
            VALUES ('out_old', ?, ?, 'manual_gui', '旧发件', '', '[]',
                    'sent', 0, 0, 0, ?, ?)
            """,
            (f"qq:{cfg.qq_email}", cfg.qq_email, now, now),
        )
        connection.execute(
            """
            INSERT INTO sent_files
                (source_path, subject, from_email, to_email, status,
                 source_origin, outbound_id, created_at, updated_at)
            VALUES ('legacy.txt', '旧发件', ?, ?, 'sent', 'manual_gui',
                    'out_old', ?, ?)
            """,
            (cfg.qq_email, cfg.owner_gmail, now, now),
        )
        connection.execute(
            """
            INSERT INTO auto_receive_state
                (id, enabled, interval_seconds, last_check_at, last_success_at,
                 last_result, consecutive_global_failures, updated_at)
            VALUES (1, 1, 60, ?, ?, 'no_changes', 0, ?)
            """,
            (now, now, now),
        )
        connection.execute(
            """
            INSERT INTO receive_retries
                (backend, resource_id, message_id, attachment_id, retry_count,
                 last_error, last_attempt_at, next_retry_at, created_at, updated_at)
            VALUES ('gmail_api', 'retry-old', '<retry@test>', '', 1,
                    'temporary', ?, ?, ?, ?)
            """,
            (now, now, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def test_stable_ids_and_provider_adapter_boundaries(tmp_cfg):
    first = stable_account_id("gmail", "First.User@gmail.com")
    assert first == stable_account_id("GMAIL", "first.user@GMAIL.COM")
    assert first != stable_account_id("gmail", "second@gmail.com")
    assert get_provider_adapter("gmail").supports("receive")
    assert not get_provider_adapter("gmail").supports("send")
    assert (
        get_provider_adapter("generic_imap_smtp").status
        == "implementation_ready_e2e_required"
    )
    assert get_provider_adapter("generic_imap_smtp").supports("receive")
    assert get_provider_adapter("generic_imap_smtp").supports("send")
    assert get_provider_adapter("microsoft").implemented_capabilities == ()


def test_v13_facts_migrate_atomically_and_idempotently_without_touching_raw(
    tmp_cfg, tmp_path
):
    _downgrade_schema_to_v13(tmp_cfg.db_path)
    raw_path = tmp_path / "legacy-package" / "raw.eml"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"From: sender@example.com\r\nMessage-ID: <old@test>\r\n\r\nbody")
    before_hash = sha256_of_file(raw_path)
    _seed_v13_facts(tmp_cfg, raw_path)
    assert multi_account_migration_needed(tmp_cfg.db_path)

    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    gmail_id = current_receive_account_id(tmp_cfg)
    qq_id = current_send_account_id(tmp_cfg)
    connection = get_connection(tmp_cfg.db_path)
    package = dict(connection.execute(
        "SELECT * FROM mail_packages WHERE package_id = 'pkg_old'"
    ).fetchone())
    outbound = dict(connection.execute(
        "SELECT * FROM outbound_messages WHERE outbound_id = 'out_old'"
    ).fetchone())
    retry = dict(connection.execute(
        "SELECT * FROM receive_retries WHERE resource_id = 'retry-old'"
    ).fetchone())
    assert package["account_id"] == gmail_id
    assert package["mailbox_id"]
    assert outbound["from_account_id"] == qq_id
    assert retry["account_id"] == gmail_id
    assert get_auto_receive_state(tmp_cfg.db_path, account_id=gmail_id)["enabled"] == 1
    assert sha256_of_file(raw_path) == before_hash
    accounts_before = query_mail_accounts(tmp_cfg.db_path)
    mailboxes_before = query_mailboxes(tmp_cfg.db_path)

    init_db(tmp_cfg.db_path, legacy_accounts=legacy_accounts_from_config(tmp_cfg))
    assert query_mail_accounts(tmp_cfg.db_path) == accounts_before
    assert query_mailboxes(tmp_cfg.db_path) == mailboxes_before
    assert sha256_of_file(raw_path) == before_hash
    assert not multi_account_migration_needed(tmp_cfg.db_path)


def test_same_provider_accounts_and_sync_state_remain_isolated(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    second_id = stable_account_id("gmail", "second@gmail.com")
    second = MailAccount(
        account_id=second_id,
        provider="gmail",
        email_address="second@gmail.com",
        display_name="第二个 Gmail",
        auth_type="oauth2",
        receive_enabled=True,
        send_enabled=False,
        capabilities=("receive", "archive", "mail_facts", "gmail_api"),
    )
    sync_mail_accounts(
        tmp_cfg.db_path, (*legacy_accounts_from_config(tmp_cfg), second)
    )
    first_id = current_receive_account_id(tmp_cfg)
    save_auto_receive_state(
        tmp_cfg.db_path, account_id=first_id, checkpoint="first", interval_seconds=60
    )
    save_auto_receive_state(
        tmp_cfg.db_path, account_id=second_id, checkpoint="second", interval_seconds=180
    )
    assert get_auto_receive_state(
        tmp_cfg.db_path, account_id=first_id
    )["checkpoint"] == "first"
    assert get_auto_receive_state(
        tmp_cfg.db_path, account_id=second_id
    )["checkpoint"] == "second"
    ids = {item["account_id"] for item in query_mail_accounts(tmp_cfg.db_path)}
    assert {first_id, second_id, current_send_account_id(tmp_cfg)} <= ids


def test_same_provider_outbound_ownership_remains_isolated(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    first_id = current_send_account_id(tmp_cfg)
    second_address = "second@qq.com"
    second_id = stable_account_id("qq", second_address)
    second = MailAccount(
        account_id=second_id,
        provider="qq",
        email_address=second_address,
        display_name="第二个 QQ 邮箱",
        auth_type="app_password",
        receive_enabled=False,
        send_enabled=True,
        capabilities=("send", "smtp", "outbound_archive"),
    )
    sync_mail_accounts(
        tmp_cfg.db_path, (*legacy_accounts_from_config(tmp_cfg), second)
    )
    first = create_outbound_message(
        tmp_cfg.db_path,
        outbound_id="outbound-first",
        sender_account_ref=f"qq:{tmp_cfg.qq_email}",
        from_account_id=first_id,
        sender_ref=tmp_cfg.qq_email,
        source_origin="manual_gui",
        request_id=None,
        subject="first",
        body_text="",
        to_emails=[tmp_cfg.owner_gmail],
        attachment_count=0,
        link_count=0,
    )
    second_row = create_outbound_message(
        tmp_cfg.db_path,
        outbound_id="outbound-second",
        sender_account_ref=f"qq:{second_address}",
        from_account_id=second_id,
        sender_ref=second_address,
        source_origin="manual_gui",
        request_id=None,
        subject="second",
        body_text="",
        to_emails=[tmp_cfg.owner_gmail],
        attachment_count=0,
        link_count=0,
    )
    assert first["from_account_id"] == first_id
    assert second_row["from_account_id"] == second_id


def test_same_message_id_can_belong_to_two_gmail_accounts(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: shared message\r\n"
        b"Message-ID: <shared-across-accounts@test>\r\n\r\nbody"
    )

    def archive_for(address: str, provider_id: str):
        tmp_cfg.gmail_address = address
        assert service.synchronize_mail_accounts().ok
        normalized = normalized_mail_from_raw(
            raw,
            backend="gmail_api",
            backend_message_id=provider_id,
            thread_id=f"thread-{provider_id}",
            uid="",
            received_at="2026-07-22 12:00:00",
            saved_date="2026-07-22",
            max_attachment_bytes=1024 * 1024,
            mailbox_ref="gmail:me/inbox",
        )
        return archive_normalized_mail(
            tmp_cfg, normalized, "<shared-across-accounts@test>"
        )

    first = archive_for("first@gmail.com", "provider-first")
    second = archive_for("second@gmail.com", "provider-second")
    first_id = stable_account_id("gmail", "first@gmail.com")
    second_id = stable_account_id("gmail", "second@gmail.com")
    assert first.package_id != second.package_id
    assert len(list_mail_messages(tmp_cfg.db_path, account_id=first_id)) == 1
    assert len(list_mail_messages(tmp_cfg.db_path, account_id=second_id)) == 1
    connection = get_connection(tmp_cfg.db_path)
    assert connection.execute(
        "SELECT COUNT(*) FROM received_messages "
        "WHERE message_id = '<shared-across-accounts@test>'"
    ).fetchone()[0] == 2


def test_migration_failure_rolls_back_schema(tmp_cfg, monkeypatch):
    _downgrade_schema_to_v13(tmp_cfg.db_path)
    import agent_mail_bridge.database as database

    def fail_after_schema(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setattr(database, "_backfill_multi_account_ownership", fail_after_schema)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        init_db(
            tmp_cfg.db_path,
            legacy_accounts=legacy_accounts_from_config(tmp_cfg),
        )
    close_connection()
    connection = sqlite3.connect(tmp_cfg.db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(received_messages)")
        }
        assert "mail_accounts" not in tables
        assert "account_id" not in columns
    finally:
        connection.close()
