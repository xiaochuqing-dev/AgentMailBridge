from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.consistency_scan import scan_mail_consistency
from agent_mail_bridge.database import (
    V171_CONSISTENCY_MIGRATION_KEY,
    close_connection,
    get_connection,
    save_auto_receive_state,
    upsert_mailboxes,
    v171_consistency_migration_needed,
    set_mailbox_sync_enabled,
    upsert_history_import_run,
)
from agent_mail_bridge.imap_sync import _membership_snapshot_due, receive_imap_account
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_fact_membership import (
    record_direction_evidence,
    record_membership,
    reconcile_mailbox_uid_snapshot,
    reconcile_package_memberships,
)
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mail_resource_access import workspace_id_for_path
from agent_mail_bridge.mail_send import SmtpStageError, smtp_send_bytes_with_stage
from agent_mail_bridge.mail_threading import (
    record_sent_mapping,
    record_thread_relations,
)
from agent_mail_bridge.mailbox_checkpoint import (
    finish_mailbox_attempt,
    get_mailbox_checkpoint_state,
)
from agent_mail_bridge.maintenance import scan_consistency
from agent_mail_bridge.retention_cleanup import cleanup_send_snapshots
from agent_mail_bridge.send_reconciliation import (
    find_sent_candidate,
    reconcile_sent_observation,
)
from agent_mail_bridge.send_recovery import (
    reconcile_send_request_locally,
    recover_incomplete_send_requests,
)
from agent_mail_bridge.send_requests import (
    claim_for_send,
    complete_send_request,
    create_send_request,
    get_send_request,
    record_send_mime,
    record_send_stage,
    SendRequestError,
)
from agent_mail_bridge.ui.health_page import format_mail_health


def _account_context(tmp_cfg, *, provider: str = "gmail"):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    assert service.synchronize_mail_accounts().ok
    account = next(
        row
        for row in service.list_mail_accounts().details["accounts"]
        if row["provider"] == provider
    )
    account_id = str(account["account_id"])
    mailboxes = upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [
            {
                "external_ref": "INBOX",
                "raw_name": "INBOX",
                "display_name": "Inbox",
                "mailbox_role": "inbox",
                "role_source": "special_use",
            },
            {
                "external_ref": "Sent",
                "raw_name": "Sent",
                "display_name": "Sent",
                "mailbox_role": "sent",
                "role_source": "special_use",
            },
            {
                "external_ref": "Archive",
                "raw_name": "Archive",
                "display_name": "Archive",
                "mailbox_role": "archive",
                "role_source": "special_use",
            },
        ],
    )
    return service, account, {str(row["external_ref"]): row for row in mailboxes}


def _send_context(tmp_cfg):
    service, account, mailboxes = _account_context(tmp_cfg, provider="qq")
    account_id = str(account["account_id"])
    workspace_id = workspace_id_for_path(tmp_cfg.data_root_path)
    created = service.create_agent_client(
        client_type="codex",
        display_name="v1.7.1 recovery test",
        capabilities=[
            "mail.accounts.list",
            "mailboxes.list",
            "mail.send",
            "send.status",
        ],
        account_ids=[account_id],
        mailbox_ids=[str(mailboxes["INBOX"]["mailbox_id"])],
        send_account_ids=[account_id],
        attachment_workspace_ids=[workspace_id],
        send_mode="autonomous",
    )
    assert created.ok, created.message
    client_id = str(created.details["client"]["client_id"])
    token = str(created.details["scoped_token"])
    assert service.set_agent_client_state(client_id, "active", enabled=True).ok
    identity = service.resolve_agent_identity(client_id, token)
    return service, identity, account_id, client_id, mailboxes


def _create_ready_request(tmp_cfg, *, suffix: str):
    service, identity, account_id, client_id, mailboxes = _send_context(tmp_cfg)
    request_id = f"send-v171-{suffix}"
    request, created = create_send_request(
        tmp_cfg.db_path,
        send_request_id=request_id,
        client_id=client_id,
        idempotency_key=f"idem-v171-{suffix}",
        operation="new",
        sender_account_id=account_id,
        source_package_id=None,
        reply_to_package_id=None,
        forward_from_package_id=None,
        send_mode="autonomous",
        subject=f"recovery {suffix}",
        body_text="deterministic body",
        body_html="",
        status="ready_to_send",
        expires_at=None,
        message_id=f"<v171-{suffix}@agentmailbridge.local>",
        recipients=[
            {
                "recipient_type": "to",
                "email_address": "receiver@example.com",
            }
        ],
        attachments=[],
    )
    assert created
    return service, identity, account_id, request, mailboxes


def _insert_package(
    tmp_cfg,
    *,
    package_id: str,
    account_id: str,
    account_ref: str,
    mailbox_id: str,
    message_id: str,
    raw_sha256: str,
    direction: str = "outbound",
) -> None:
    now = "2026-07-30 12:00:00"
    get_connection(tmp_cfg.db_path).execute(
        """
        INSERT INTO mail_packages
            (package_id, account_ref, account_id, mailbox_ref, mailbox_id,
             backend, message_id, direction, package_root, raw_eml_sha256,
             raw_eml_status, archive_status, parse_status, created_at,
             updated_at)
        VALUES (?, ?, ?, 'Sent', ?, 'smtp', ?, ?, ?, ?, 'ready', 'ready',
                'ready', ?, ?)
        """,
        (
            package_id,
            account_ref,
            account_id,
            mailbox_id,
            message_id,
            direction,
            str(tmp_cfg.sent_dir / "mail" / package_id),
            raw_sha256,
            now,
            now,
        ),
    )
    get_connection(tmp_cfg.db_path).commit()


def test_v171_schema_migration_is_detected_backed_up_and_idempotent(tmp_cfg):
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "DELETE FROM migration_metadata WHERE migration_key=?",
        (V171_CONSISTENCY_MIGRATION_KEY,),
    )
    connection.commit()
    assert v171_consistency_migration_needed(tmp_cfg.db_path)

    initialized = ApplicationService(tmp_cfg).initialize()

    assert initialized.ok
    assert not v171_consistency_migration_needed(tmp_cfg.db_path)
    backups = list(
        (tmp_cfg.data_root_path / "backups").glob(
            "*before_v1_7_1_consistency*.db"
        )
    )
    assert len(backups) == 1
    outbound_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(outbound_messages)"
        ).fetchall()
    }
    resource_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(mail_resources)").fetchall()
    }
    assert "reconciliation_status" in outbound_columns
    assert "reconciliation_status" not in resource_columns
    assert connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='consistency_repair_runs'"
    ).fetchone()


def test_v170_message_identity_constraints_migrate_without_fact_loss(tmp_cfg):
    _service, account, _mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = str(account["email_address"])
    message["Subject"] = "migration identity"
    message["Message-ID"] = "<migration-identity-v171@example.com>"
    message.set_content("preserve this raw fact")
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="41",
        uidvalidity=700,
        received_at="2026-07-30 12:00:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    archived = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )
    package_id = str(archived["package_id"])
    connection = get_connection(tmp_cfg.db_path)
    before_package = dict(connection.execute(
        "SELECT id, package_id, raw_eml_path, raw_eml_sha256, package_root "
        "FROM mail_packages WHERE package_id=?",
        (package_id,),
    ).fetchone())
    before_received = dict(connection.execute(
        "SELECT id, package_id FROM received_messages WHERE package_id=?",
        (package_id,),
    ).fetchone())
    before_resources = [
        tuple(row)
        for row in connection.execute(
            "SELECT resource_id, sha256 FROM mail_resources "
            "WHERE package_id=? ORDER BY resource_id",
            (package_id,),
        ).fetchall()
    ]
    before_memberships = int(connection.execute(
        "SELECT COUNT(*) FROM mail_package_mailboxes WHERE package_id=?",
        (package_id,),
    ).fetchone()[0])
    connection.execute(
        "CREATE UNIQUE INDEX ux_mail_packages_account_message "
        "ON mail_packages(account_id, message_id COLLATE NOCASE)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX ux_received_account_message_id "
        "ON received_messages(account_id, message_id COLLATE NOCASE)"
    )
    connection.execute(
        "UPDATE migration_metadata SET schema_version=1 "
        "WHERE migration_key=?",
        (V171_CONSISTENCY_MIGRATION_KEY,),
    )
    connection.commit()
    assert v171_consistency_migration_needed(tmp_cfg.db_path)

    close_connection()
    initialized = ApplicationService(tmp_cfg).initialize()

    assert initialized.ok, initialized.message
    connection = get_connection(tmp_cfg.db_path)
    assert dict(connection.execute(
        "SELECT id, package_id, raw_eml_path, raw_eml_sha256, package_root "
        "FROM mail_packages WHERE package_id=?",
        (package_id,),
    ).fetchone()) == before_package
    assert dict(connection.execute(
        "SELECT id, package_id FROM received_messages WHERE package_id=?",
        (package_id,),
    ).fetchone()) == before_received
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT resource_id, sha256 FROM mail_resources "
            "WHERE package_id=? ORDER BY resource_id",
            (package_id,),
        ).fetchall()
    ] == before_resources
    assert connection.execute(
        "SELECT COUNT(*) FROM mail_package_mailboxes WHERE package_id=?",
        (package_id,),
    ).fetchone()[0] == before_memberships
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    package_unique_sets = {
        tuple(
            str(column[2])
            for column in connection.execute(
                f"PRAGMA index_info('{index[1]}')"
            ).fetchall()
        )
        for index in connection.execute("PRAGMA index_list(mail_packages)")
        if int(index[2])
    }
    assert ("account_id", "message_id") not in package_unique_sets
    _insert_package(
        tmp_cfg,
        package_id="pkg-migration-duplicate-id",
        account_id=account_id,
        account_ref=str(connection.execute(
            "SELECT account_ref FROM mail_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()[0]),
        mailbox_id=str(connection.execute(
            "SELECT mailbox_id FROM mail_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()[0]),
        message_id="<migration-identity-v171@example.com>",
        raw_sha256="different-raw-hash",
    )
    assert not v171_consistency_migration_needed(tmp_cfg.db_path)


def test_distinct_mail_with_duplicate_message_id_remain_distinct_facts(tmp_cfg):
    _service, account, _mailboxes = _account_context(tmp_cfg)
    raw_messages: list[tuple[bytes, str]] = []
    for uid, body in (("501", "first physical message"),
                      ("502", "second physical message")):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = str(account["email_address"])
        message["Subject"] = "duplicate Message-ID"
        message["Message-ID"] = "<duplicate-fact-v171@example.com>"
        message.set_content(body)
        raw_messages.append((message.as_bytes(), uid))

    normalized_messages = [
        normalized_mail_from_raw(
            raw,
            backend="imap",
            backend_message_id="",
            thread_id="",
            uid=uid,
            uidvalidity=900,
            received_at="2026-07-30 12:30:00",
            saved_date="2026-07-30",
            max_attachment_bytes=tmp_cfg.max_attachment_bytes,
            mailbox_ref="INBOX",
        )
        for raw, uid in raw_messages
    ]
    first = process_normalized_mail(
        tmp_cfg, normalized_messages[0], apply_receive_rule=False
    )
    second = process_normalized_mail(
        tmp_cfg, normalized_messages[1], apply_receive_rule=False
    )

    assert first["status"] == "saved"
    assert second["status"] == "saved"
    assert first["package_id"] != second["package_id"]
    connection = get_connection(tmp_cfg.db_path)
    assert connection.execute(
        "SELECT COUNT(*) FROM mail_packages "
        "WHERE account_id=? AND message_id=? COLLATE NOCASE",
        (
            str(account["account_id"]),
            "<duplicate-fact-v171@example.com>",
        ),
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM received_messages "
        "WHERE account_id=? AND message_id=? COLLATE NOCASE",
        (
            str(account["account_id"]),
            "<duplicate-fact-v171@example.com>",
        ),
    ).fetchone()[0] == 2
    repeated = [
        process_normalized_mail(tmp_cfg, item, apply_receive_rule=False)
        for item in normalized_messages
    ]
    assert [item["status"] for item in repeated] == ["duplicate", "duplicate"]
    assert [item["package_id"] for item in repeated] == [
        first["package_id"], second["package_id"]
    ]


def test_legacy_message_id_without_fingerprint_is_not_merged_by_guess(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["INBOX"]["mailbox_id"])
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        """
        INSERT INTO mail_packages
            (package_id, account_ref, account_id, mailbox_ref, mailbox_id,
             backend, message_id, direction, package_root, raw_eml_status,
             archive_status, parse_status, legacy, created_at, updated_at)
        VALUES ('pkg-legacy-weak-identity', ?, ?, 'INBOX', ?, 'legacy',
                '<legacy-reused-id@example.com>', 'inbound', ?, 'unavailable',
                'legacy', 'legacy', 1, ?, ?)
        """,
        (
            f"gmail:{account['email_address']}",
            account_id,
            mailbox_id,
            str(tmp_cfg.received_dir / "mail" / "pkg-legacy-weak-identity"),
            "2026-07-30 12:00:00",
            "2026-07-30 12:00:00",
        ),
    )
    connection.commit()
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = str(account["email_address"])
    message["Subject"] = "new mail with a reused legacy id"
    message["Message-ID"] = "<legacy-reused-id@example.com>"
    message.set_content("deterministic new content")
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="503",
        uidvalidity=900,
        received_at="2026-07-30 12:31:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )

    result = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )

    assert result["status"] == "saved"
    assert result["package_id"] != "pkg-legacy-weak-identity"
    assert connection.execute(
        "SELECT COUNT(*) FROM mail_packages WHERE account_id=? "
        "AND message_id='<legacy-reused-id@example.com>' COLLATE NOCASE",
        (account_id,),
    ).fetchone()[0] == 2


def test_one_fact_has_multiple_memberships_and_direction_conflict_is_audited(
    tmp_cfg,
):
    _service, account, mailboxes = _account_context(tmp_cfg)
    message = EmailMessage()
    message["From"] = str(account["email_address"])
    message["To"] = str(account["email_address"])
    message["Subject"] = "self copy in inbox"
    message["Message-ID"] = "<self-copy-v171@example.com>"
    message.set_content("inbound observation wins outside Sent")
    raw = message.as_bytes()
    normalized = normalized_mail_from_raw(
        raw,
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="10",
        uidvalidity=100,
        received_at="2026-07-30 12:00:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
        direction="inbound",
    )

    result = process_normalized_mail(tmp_cfg, normalized, apply_receive_rule=False)
    package_id = str(result["package_id"])
    package = get_connection(tmp_cfg.db_path).execute(
        "SELECT direction, direction_evidence_json FROM mail_packages "
        "WHERE package_id=?",
        (package_id,),
    ).fetchone()
    assert package["direction"] == "inbound"
    evidence_types = {
        item["type"] for item in json.loads(package["direction_evidence_json"])
    }
    assert evidence_types == {
        "configured_account_is_sender",
        "provider_receive_observation",
    }

    account_id = str(account["account_id"])
    record_membership(
        tmp_cfg.db_path,
        package_id=package_id,
        account_id=account_id,
        mailbox_id=str(mailboxes["Archive"]["mailbox_id"]),
        provider_uid="10",
    )
    reconciled = reconcile_package_memberships(
        tmp_cfg.db_path,
        package_id=package_id,
        account_id=account_id,
        observed_mailbox_ids=[str(mailboxes["Archive"]["mailbox_id"])],
        scope_mailbox_ids=[
            str(mailboxes["INBOX"]["mailbox_id"]),
            str(mailboxes["Archive"]["mailbox_id"]),
        ],
        provider_message_id="provider-10",
        source="test_snapshot",
        complete_snapshot=True,
    )
    assert reconciled == {"present": 1, "added": 0, "removed": 1}
    rows = get_connection(tmp_cfg.db_path).execute(
        "SELECT mailbox_id, currently_present FROM mail_package_mailboxes "
        "WHERE package_id=? ORDER BY mailbox_id",
        (package_id,),
    ).fetchall()
    assert len(rows) == 2
    assert sum(int(row["currently_present"]) for row in rows) == 1
    assert (
        get_connection(tmp_cfg.db_path)
        .execute("SELECT COUNT(*) FROM mail_packages WHERE package_id=?", (package_id,))
        .fetchone()[0]
        == 1
    )

    conflict = record_direction_evidence(
        tmp_cfg.db_path,
        package_id=package_id,
        proposed_direction="outbound",
        evidence=[
            {
                "type": "provider_sent_membership",
                "direction": "outbound",
                "confidence": "high",
            }
        ],
    )
    assert conflict["conflict"] is True
    canonical = get_connection(tmp_cfg.db_path).execute(
        "SELECT direction, direction_conflict FROM mail_packages WHERE package_id=?",
        (package_id,),
    ).fetchone()
    assert (canonical["direction"], canonical["direction_conflict"]) == (
        "inbound",
        1,
    )


def test_checkpoint_failure_preserves_success_and_reconciliation_can_complete(
    tmp_cfg,
):
    _service, account, mailboxes = _account_context(tmp_cfg)
    mailbox_id = str(mailboxes["INBOX"]["mailbox_id"])
    account_id = str(account["account_id"])
    finish_mailbox_attempt(
        tmp_cfg.db_path,
        mailbox_id=mailbox_id,
        account_id=account_id,
        uidvalidity=100,
        uidnext=11,
        highestmodseq=5,
        last_uid=10,
        checkpoint={"last_uid": 10},
        result="success",
    )
    first = get_mailbox_checkpoint_state(tmp_cfg.db_path, mailbox_id)
    assert first is not None
    save_auto_receive_state(
        tmp_cfg.db_path,
        account_id=account_id,
        checkpoint=json.dumps(
            {"mailboxes": {"INBOX": {"last_uid": 1}}},
            separators=(",", ":"),
        ),
    )
    from agent_mail_bridge.imap_sync import (
        _load_authoritative_mailbox_checkpoint,
    )

    authoritative = _load_authoritative_mailbox_checkpoint(
        tmp_cfg,
        account_id=account_id,
        mailbox="INBOX",
        mailbox_id=mailbox_id,
        checkpoint_state=first,
    )
    assert authoritative == {"last_uid": 10}

    finish_mailbox_attempt(
        tmp_cfg.db_path,
        mailbox_id=mailbox_id,
        account_id=account_id,
        uidvalidity=0,
        uidnext=0,
        highestmodseq=0,
        last_uid=0,
        checkpoint={},
        result="failed",
        error="network_timeout",
        current_attempt={"stage": "fetching"},
    )
    failed = get_mailbox_checkpoint_state(tmp_cfg.db_path, mailbox_id)
    assert failed is not None
    assert failed["uidvalidity"] == 100
    assert failed["last_uid"] == 10
    assert failed["checkpoint"] == {"last_uid": 10}
    assert failed["last_success_at"] == first["last_success_at"]
    assert failed["consecutive_failures"] == 1
    assert failed["last_error_at"]

    finish_mailbox_attempt(
        tmp_cfg.db_path,
        mailbox_id=mailbox_id,
        account_id=account_id,
        uidvalidity=200,
        uidnext=21,
        highestmodseq=8,
        last_uid=20,
        checkpoint={"last_uid": 20},
        result="partial",
        reconciliation_required=True,
        uidvalidity_changed=True,
        full_rescan_cursor="20",
    )
    pending = get_mailbox_checkpoint_state(tmp_cfg.db_path, mailbox_id)
    assert pending is not None
    assert pending["reconciliation_required"] == 1
    assert pending["full_rescan_cursor"] == "20"

    finish_mailbox_attempt(
        tmp_cfg.db_path,
        mailbox_id=mailbox_id,
        account_id=account_id,
        uidvalidity=200,
        uidnext=21,
        highestmodseq=8,
        last_uid=20,
        checkpoint={"last_uid": 20},
        result="no_changes",
        reconciliation_completed=True,
    )
    completed = get_mailbox_checkpoint_state(tmp_cfg.db_path, mailbox_id)
    assert completed is not None
    assert completed["reconciliation_required"] == 0
    assert completed["full_rescan_cursor"] is None
    assert completed["consecutive_failures"] == 0


def test_imap_uid_snapshot_marks_server_absent_without_deleting_facts(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["INBOX"]["mailbox_id"])
    for suffix, uid, uidvalidity in (
        ("present", "20", 200),
        ("deleted", "21", 200),
        ("old-generation", "22", 100),
    ):
        package_id = f"pkg-uid-snapshot-{suffix}"
        _insert_package(
            tmp_cfg,
            package_id=package_id,
            account_id=account_id,
            account_ref=f"gmail:{suffix}@example.com",
            mailbox_id=mailbox_id,
            message_id=f"<{suffix}@uid-snapshot.example.com>",
            raw_sha256=hashlib.sha256(suffix.encode("ascii")).hexdigest(),
            direction="inbound",
        )
        record_membership(
            tmp_cfg.db_path,
            package_id=package_id,
            account_id=account_id,
            mailbox_id=mailbox_id,
            uidvalidity=uidvalidity,
            provider_uid=uid,
        )

    result = reconcile_mailbox_uid_snapshot(
        tmp_cfg.db_path,
        account_id=account_id,
        mailbox_id=mailbox_id,
        uidvalidity=200,
        observed_provider_uids=[20],
    )

    assert result == {"observed": 1, "removed": 2}
    rows = get_connection(tmp_cfg.db_path).execute(
        "SELECT package_id, currently_present, removed_at "
        "FROM mail_package_mailboxes WHERE mailbox_id=? ORDER BY package_id",
        (mailbox_id,),
    ).fetchall()
    presence = {
        str(row["package_id"]): bool(row["currently_present"]) for row in rows
    }
    assert presence == {
        "pkg-uid-snapshot-deleted": False,
        "pkg-uid-snapshot-old-generation": False,
        "pkg-uid-snapshot-present": True,
    }
    assert (
        get_connection(tmp_cfg.db_path)
        .execute(
            "SELECT COUNT(*) FROM mail_packages "
            "WHERE package_id LIKE 'pkg-uid-snapshot-%'"
        )
        .fetchone()[0]
        == 3
    )


def test_imap_sync_detects_server_deletion_via_periodic_uid_snapshot(
    tmp_cfg, monkeypatch
):
    service, account, mailboxes = _account_context(tmp_cfg, provider="qq")
    account_id = str(account["account_id"])
    set_mailbox_sync_enabled(
        tmp_cfg.db_path, str(mailboxes["Sent"]["mailbox_id"]), False
    )
    runtime = service._account_router.context(
        account_id, capability="receive"
    ).config
    monkeypatch.setattr(
        "agent_mail_bridge.imap_sync.MEMBERSHIP_SNAPSHOT_INTERVAL",
        timedelta(0),
    )

    def raw(uid: int) -> bytes:
        return (
            "From: sender@example.com\r\n"
            f"To: {account['email_address']}\r\n"
            f"Subject: uid-{uid}\r\n"
            f"Message-ID: <uid-{uid}@snapshot.example.com>\r\n"
            "Date: Thu, 30 Jul 2026 10:00:00 +0800\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"body-{uid}"
        ).encode("utf-8")

    class SnapshotClient:
        def __init__(self, uids):
            self.uids = list(uids)

        def login(self, _username, _secret):
            return None

        def id_(self, _parameters):
            return None

        def list_folders(self):
            return [((b"\\Inbox",), b"/", b"INBOX")]

        def select_folder(self, _mailbox, readonly=True):
            return {
                b"UIDVALIDITY": 500,
                b"UIDNEXT": max(self.uids, default=0) + 1,
                b"HIGHESTMODSEQ": 0,
            }

        def search(self, _criteria):
            return list(self.uids)

        def fetch(self, uids, _parts):
            return {int(uid): {b"BODY[]": raw(int(uid))} for uid in uids}

        def logout(self):
            return None

    first = receive_imap_account(
        runtime,
        client_factory=lambda *_args, **_kwargs: SnapshotClient([1, 2]),
    )
    assert first["saved"] == 2

    second = receive_imap_account(
        runtime,
        client_factory=lambda *_args, **_kwargs: SnapshotClient([2]),
    )

    assert second["failed"] == 0
    rows = get_connection(tmp_cfg.db_path).execute(
        """
        SELECT mm.provider_uid, mm.currently_present, mm.removed_at
        FROM mail_package_mailboxes mm
        WHERE mm.mailbox_id=? ORDER BY CAST(mm.provider_uid AS INTEGER)
        """,
        (mailboxes["INBOX"]["mailbox_id"],),
    ).fetchall()
    assert [(str(row["provider_uid"]), bool(row["currently_present"])) for row in rows] == [
        ("1", False),
        ("2", True),
    ]
    assert rows[0]["removed_at"]
    assert get_connection(tmp_cfg.db_path).execute(
        "SELECT COUNT(*) FROM mail_packages"
    ).fetchone()[0] == 2


def test_future_membership_snapshot_timestamp_is_due_after_clock_rollback():
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")

    assert _membership_snapshot_due(
        {"full_membership_scanned_at": future}
    ) is True


def test_dual_executor_can_acquire_only_one_durable_send_lease(tmp_cfg):
    _service, _identity, _account_id, request, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="dual"
    )
    request_id = str(request["send_request_id"])
    barrier = threading.Barrier(2)

    def acquire(owner: str):
        try:
            barrier.wait(timeout=5)
            return claim_for_send(
                tmp_cfg.db_path,
                request_id,
                lease_owner=owner,
                process_id=owner,
            )[1]
        finally:
            close_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(acquire, ("process-a", "process-b")))

    assert sorted(claimed) == [False, True]
    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert persisted is not None
    assert persisted["status"] == "sending"
    assert persisted["lease"]["lease_owner"] in {"process-a", "process-b"}
    assert (
        get_connection(tmp_cfg.db_path)
        .execute(
            "SELECT COUNT(*) FROM send_attempt_events "
            "WHERE send_request_id=? AND stage='lease_acquired'",
            (request_id,),
        )
        .fetchone()[0]
        == 1
    )


def test_stale_lease_before_data_is_not_sent_but_after_data_is_unknown(tmp_cfg):
    for suffix, stage, expected in (
        ("pre-data", "mime_built", "definitely_not_sent"),
        ("post-data", "smtp_data_started", "delivery_unknown"),
    ):
        _service, _identity, _account_id, request, _mailboxes = (
            _create_ready_request(tmp_cfg, suffix=suffix)
        )
        request_id = str(request["send_request_id"])
        claimed, acquired = claim_for_send(
            tmp_cfg.db_path, request_id, lease_owner=f"owner-{suffix}"
        )
        assert acquired
        connection = get_connection(tmp_cfg.db_path)
        connection.execute(
            "UPDATE send_execution_leases SET lease_expires_at=?, current_stage=? "
            "WHERE send_request_id=?",
            ("2000-01-01 00:00:00", stage, request_id),
        )
        connection.execute(
            "UPDATE send_requests SET current_stage=? WHERE send_request_id=?",
            (stage, request_id),
        )
        connection.commit()
        _unchanged, reacquired = claim_for_send(
            tmp_cfg.db_path, request_id, lease_owner=f"second-{suffix}"
        )
        assert reacquired is False
        with pytest.raises(SendRequestError, match="租约已失效"):
            record_send_stage(
                tmp_cfg.db_path,
                request_id,
                lease_owner=f"owner-{suffix}",
                stage="smtp_data_started",
            )
        with pytest.raises(SendRequestError, match="租约已失效"):
            record_send_mime(
                tmp_cfg.db_path,
                request_id,
                raw_eml_path="stale-owner.eml",
                raw_eml_sha256="f" * 64,
                lease_owner=f"owner-{suffix}",
            )
        with pytest.raises(SendRequestError, match="租约已失效"):
            complete_send_request(
                tmp_cfg.db_path,
                request_id,
                status="failed",
                lease_owner=f"owner-{suffix}",
            )
        protected = get_send_request(tmp_cfg.db_path, request_id)
        assert protected["status"] == "recovery_required"
        assert protected["current_stage"] == "stale_lease"
        assert protected["raw_eml_path"] is None
        assert str(protected["lease"]["lease_owner"]).startswith("recovery_")

        recovery = recover_incomplete_send_requests(tmp_cfg.db_path)

        assert get_send_request(tmp_cfg.db_path, request_id)["status"] == expected
        assert recovery[expected] >= 1


def test_expired_lease_owner_cannot_renew_without_recovery_claim(tmp_cfg):
    _service, _identity, _account_id, request, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="expired-owner"
    )
    request_id = str(request["send_request_id"])
    _claimed, acquired = claim_for_send(
        tmp_cfg.db_path, request_id, lease_owner="expired-owner"
    )
    assert acquired is True
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_execution_leases SET lease_expires_at='2000-01-01 00:00:00' "
        "WHERE send_request_id=?",
        (request_id,),
    )
    connection.commit()

    with pytest.raises(SendRequestError, match="租约已失效"):
        record_send_stage(
            tmp_cfg.db_path,
            request_id,
            lease_owner="expired-owner",
            stage="smtp_starting",
        )
    with pytest.raises(SendRequestError, match="租约已失效"):
        record_send_mime(
            tmp_cfg.db_path,
            request_id,
            raw_eml_path="expired-owner.eml",
            raw_eml_sha256="e" * 64,
            lease_owner="expired-owner",
        )

    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert persisted["status"] == "sending"
    assert persisted["current_stage"] == "lease_acquired"
    assert persisted["raw_eml_path"] is None
    recovery = recover_incomplete_send_requests(tmp_cfg.db_path)
    assert recovery["definitely_not_sent"] >= 1
    assert get_send_request(
        tmp_cfg.db_path, request_id
    )["status"] == "definitely_not_sent"


def test_smtp_acceptance_callback_failure_is_delivery_unknown(
    tmp_cfg, monkeypatch
):
    sendmail_calls = []

    class FakeSmtp:
        def __init__(self, *_args, **_kwargs):
            pass

        def ehlo(self):
            return None

        def login(self, _username, _secret):
            return None

        def sendmail(self, from_addr, recipients, raw_bytes):
            sendmail_calls.append((from_addr, tuple(recipients), raw_bytes))
            return {}

        def quit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        "agent_mail_bridge.mail_send.smtplib.SMTP_SSL", FakeSmtp
    )

    def persist_stage(stage: str):
        if stage == "smtp_accepted":
            raise RuntimeError("simulated SQLite busy after SMTP acceptance")

    with pytest.raises(SmtpStageError) as captured:
        smtp_send_bytes_with_stage(
            tmp_cfg,
            b"From: test@qq.com\r\nTo: receiver@example.com\r\n\r\nbody",
            from_addr="test@qq.com",
            to_addrs=["receiver@example.com"],
            stage_callback=persist_stage,
        )

    assert captured.value.delivery_unknown is True
    assert captured.value.stage == "send"
    assert len(sendmail_calls) == 1


def test_smtp_acceptance_persistence_failure_never_retries_smtp(
    tmp_cfg, monkeypatch
):
    service, identity, account_id, _client_id, _mailboxes = _send_context(tmp_cfg)
    smtp_calls = []

    def accepted_then_interrupt(
        _cfg, _raw_bytes, *, from_addr, to_addrs, stage_callback=None
    ):
        smtp_calls.append((from_addr, tuple(to_addrs)))
        assert stage_callback is not None
        stage_callback("smtp_data_started")
        stage_callback("smtp_accepted")
        raise RuntimeError("simulated persistence interruption after acceptance")

    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        accepted_then_interrupt,
    )
    first = service.send_agent_mail(
        identity,
        request_id="smtp-accepted-v171",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        subject="accepted boundary",
        body_text="must archive without resending",
    )
    assert first.details["send_status"] == "sent_archive_failed"
    assert first.details["send_request"]["smtp_attempt_count"] == 1

    duplicate = service.send_agent_mail(
        identity,
        request_id="smtp-accepted-v171",
        operation="new",
        sender_account_id=account_id,
        to=["different@example.com"],
    )
    assert duplicate.details["send_status"] == "sent_archive_failed"
    assert len(smtp_calls) == 1
    rejected = service.mark_agent_send_resolution(
        str(first.details["send_request_id"]), resolution="not_sent"
    )
    assert not rejected.ok
    assert "不能标记为未发送" in rejected.message
    assert len(smtp_calls) == 1

    recovered = service.reconcile_agent_send_request(
        str(first.details["send_request_id"])
    )
    assert recovered.ok
    assert recovered.details["send_status"] == "sent"
    assert recovered.details["send_request"]["package_id"]
    assert len(smtp_calls) == 1


def test_manual_unknown_resolution_archives_sent_or_clones_not_sent(
    tmp_cfg, monkeypatch
):
    service, identity, account_id, _client_id, _mailboxes = _send_context(tmp_cfg)
    smtp_calls = []

    def uncertain(_cfg, _raw_bytes, *, stage_callback=None, **_kwargs):
        smtp_calls.append(True)
        if stage_callback is not None:
            stage_callback("smtp_data_started")
        raise SmtpStageError(
            "send", "connection ended after DATA", delivery_unknown=True
        )

    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage", uncertain
    )
    first = service.send_agent_mail(
        identity,
        request_id="manual-sent-v171",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        subject="manual sent",
        body_text="fixed mime can be archived",
    )
    assert first.details["send_status"] == "delivery_unknown"

    marked_sent = service.mark_agent_send_resolution(
        str(first.details["send_request_id"]), resolution="sent"
    )

    assert marked_sent.ok
    sent_request = marked_sent.details["send_request"]
    assert sent_request["status"] == "sent"
    assert sent_request["package_id"]
    assert len(smtp_calls) == 1

    second = service.send_agent_mail(
        identity,
        request_id="manual-not-sent-v171",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        subject="manual not sent",
        body_text="clone requires a new key",
    )
    assert second.details["send_status"] == "delivery_unknown"
    marked_not_sent = service.mark_agent_send_resolution(
        str(second.details["send_request_id"]), resolution="not_sent"
    )
    assert marked_not_sent.ok
    assert (
        marked_not_sent.details["send_request"]["status"]
        == "definitely_not_sent"
    )

    cloned = service.clone_agent_send_request(
        str(second.details["send_request_id"])
    )

    assert cloned.ok
    cloned_request = cloned.details["send_request"]
    assert cloned_request["status"] == "pending_confirmation"
    assert cloned_request["send_request_id"] != second.details["send_request_id"]
    assert cloned_request["idempotency_key"] != second.details["send_request"][
        "idempotency_key"
    ]
    assert len(smtp_calls) == 2

def test_sent_matching_refuses_ambiguous_message_id_and_prefers_provider_id(
    tmp_cfg,
):
    _service, account, mailboxes = _account_context(tmp_cfg, provider="qq")
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    first_message_id = "<ambiguous-a-v171@example.com>"
    second_message_id = "<ambiguous-b-v171@example.com>"
    ambiguous_raw_hash = "c" * 64
    _insert_package(
        tmp_cfg,
        package_id="pkg-v171-a",
        account_id=account_id,
        account_ref="qq:first@example.com",
        mailbox_id=mailbox_id,
        message_id=first_message_id,
        raw_sha256=ambiguous_raw_hash,
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v171-b",
        account_id=account_id,
        account_ref="qq:second@example.com",
        mailbox_id=mailbox_id,
        message_id=second_message_id,
        raw_sha256=ambiguous_raw_hash,
    )

    ambiguous = find_sent_candidate(
        tmp_cfg.db_path,
        account_id=account_id,
        provider_message_id="",
        message_id="",
        outbound_id="",
        raw_sha256=ambiguous_raw_hash,
    )
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidate_count"] == 2
    assert ambiguous["package_id"] is None

    record_sent_mapping(
        tmp_cfg.db_path,
        account_id=account_id,
        package_id="pkg-v171-a",
        mailbox_id=mailbox_id,
        provider_message_id="provider-sent-1",
        uidvalidity=123,
        provider_uid="88",
        message_id=first_message_id,
        matched_by="exact_message_id",
    )
    exact = find_sent_candidate(
        tmp_cfg.db_path,
        account_id=account_id,
        provider_message_id="provider-sent-1",
        message_id="",
        outbound_id="",
        raw_sha256=ambiguous_raw_hash,
    )
    assert exact == {
        "status": "matched",
        "evidence_type": "exact_provider_id",
        "confidence": "exact",
        "candidate_count": 1,
        "package_id": "pkg-v171-a",
    }
    conflict = find_sent_candidate(
        tmp_cfg.db_path,
        account_id=account_id,
        provider_message_id="provider-sent-1",
        message_id=second_message_id,
        outbound_id="",
        raw_sha256="",
    )
    assert conflict == {
        "status": "matched",
        "evidence_type": "exact_provider_id",
        "confidence": "exact",
        "candidate_count": 1,
        "package_id": "pkg-v171-a",
        "decision_reason": "strong_evidence_overrode_message_id",
    }


def test_sent_observation_advances_request_to_reconciled_terminal_state(tmp_cfg):
    _service, _identity, account_id, request, mailboxes = _create_ready_request(
        tmp_cfg, suffix="sent-reconciled"
    )
    request_id = str(request["send_request_id"])
    package_id = "pkg-v171-sent-reconciled"
    message_id = str(request["message_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    _insert_package(
        tmp_cfg,
        package_id=package_id,
        account_id=account_id,
        account_ref=f"qq:{account_id}",
        mailbox_id=mailbox_id,
        message_id=message_id,
        raw_sha256="9" * 64,
    )
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='sent', delivery_status='sent', "
        "package_id=?, sent_reconciliation_status='waiting' "
        "WHERE send_request_id=?",
        (package_id, request_id),
    )
    connection.commit()

    result = reconcile_sent_observation(
        tmp_cfg.db_path,
        account_id=account_id,
        package_id=package_id,
        mailbox_id=mailbox_id,
        provider_message_id="provider-v171-reconciled",
        uidvalidity=171,
        provider_uid="171",
        message_id=message_id,
        evidence_type="exact_message_id",
    )

    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert result["status"] == "matched"
    assert persisted["status"] == "sent_reconciled"
    assert persisted["current_stage"] == "sent_reconciled"
    assert persisted["sent_reconciliation_status"] == "matched"
    assert persisted["recovery_required"] == 0


def test_sent_observation_recovers_delivery_unknown_without_duplicate_fact(tmp_cfg):
    service, _identity, account_id, request, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="unknown-sent-observation"
    )
    request_id = str(request["send_request_id"])
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='delivery_unknown', "
        "delivery_status='delivery_unknown', current_stage='smtp_data_started', "
        "recovery_required=1 WHERE send_request_id=?",
        (request_id,),
    )
    connection.commit()
    runtime_cfg = service._account_router.context(
        account_id, capability="receive"
    ).config
    message = EmailMessage()
    message["From"] = tmp_cfg.qq_email
    message["To"] = "receiver@example.com"
    message["Subject"] = str(request["subject"])
    message["Message-ID"] = str(request["message_id"])
    message.set_content(str(request["body_text"] or "deterministic body"))
    observed_at = str(request["created_at"])
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="provider-unknown-recovered",
        thread_id="",
        uid="172",
        uidvalidity=171,
        received_at=observed_at,
        saved_date=observed_at[:10],
        max_attachment_bytes=runtime_cfg.max_attachment_bytes,
        mailbox_ref="Sent",
        direction="outbound",
    )

    first = process_normalized_mail(
        runtime_cfg, normalized, apply_receive_rule=False
    )
    second = process_normalized_mail(
        runtime_cfg, normalized, apply_receive_rule=False
    )

    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert second["package_id"] == first["package_id"]
    assert persisted["status"] == "sent_reconciled"
    assert persisted["package_id"] == first["package_id"]
    assert persisted["outbound_id"]
    package = connection.execute(
        "SELECT package_root, direction, local_outbound, outbound_id "
        "FROM mail_packages WHERE package_id=?",
        (first["package_id"],),
    ).fetchone()
    assert (
        package["direction"],
        package["local_outbound"],
        package["outbound_id"],
    ) == ("outbound", 1, persisted["outbound_id"])
    manifest = json.loads(
        (Path(str(package["package_root"])) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["metadata"]["local_outbound"] is True
    assert manifest["metadata"]["outbound_id"] == persisted["outbound_id"]
    outbound = connection.execute(
        "SELECT request_id, package_id, reconciliation_status "
        "FROM outbound_messages WHERE outbound_id=?",
        (persisted["outbound_id"],),
    ).fetchone()
    assert tuple(outbound) == (request_id, first["package_id"], "matched")
    assert connection.execute(
        "SELECT COUNT(*) FROM reconciliation_records "
        "WHERE package_id=? AND status='external_fact_created'",
        (first["package_id"],),
    ).fetchone()[0] == 0


def test_sent_observation_keeps_duplicate_request_candidates_ambiguous(tmp_cfg):
    service, _identity, account_id, first, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="ambiguous-request-a"
    )
    _service2, _identity2, _account2, second, _mailboxes2 = _create_ready_request(
        tmp_cfg, suffix="ambiguous-request-b"
    )
    shared_message_id = "<ambiguous-active-request-v171@example.com>"
    shared_subject = "ambiguous active request"
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='delivery_unknown', "
        "delivery_status='delivery_unknown', recovery_required=1, "
        "message_id=?, subject=? WHERE send_request_id IN (?, ?)",
        (
            shared_message_id,
            shared_subject,
            first["send_request_id"],
            second["send_request_id"],
        ),
    )
    connection.commit()
    runtime_cfg = service._account_router.context(
        account_id, capability="receive"
    ).config
    message = EmailMessage()
    message["From"] = tmp_cfg.qq_email
    message["To"] = "receiver@example.com"
    message["Subject"] = shared_subject
    message["Message-ID"] = shared_message_id
    message.set_content("ambiguous body")
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="provider-ambiguous-request",
        thread_id="",
        uid="173",
        uidvalidity=171,
        received_at="2026-07-30 17:13:00",
        saved_date="2026-07-30",
        max_attachment_bytes=runtime_cfg.max_attachment_bytes,
        mailbox_ref="Sent",
        direction="outbound",
    )

    result = process_normalized_mail(
        runtime_cfg, normalized, apply_receive_rule=False
    )

    assert result["status"] == "saved"
    assert get_send_request(
        tmp_cfg.db_path, str(first["send_request_id"])
    )["status"] == "delivery_unknown"
    assert get_send_request(
        tmp_cfg.db_path, str(second["send_request_id"])
    )["status"] == "delivery_unknown"
    assert connection.execute(
        "SELECT COUNT(*) FROM outbound_messages WHERE package_id=?",
        (result["package_id"],),
    ).fetchone()[0] == 0
    mapping = connection.execute(
        "SELECT reconciliation_status, confidence FROM sent_server_mappings "
        "WHERE package_id=?",
        (result["package_id"],),
    ).fetchone()
    assert tuple(mapping) == ("ambiguous", "manual_review")


def test_local_recovery_and_threading_refuse_duplicate_message_id(tmp_cfg):
    _service, _identity, account_id, request, mailboxes = _create_ready_request(
        tmp_cfg, suffix="duplicate-message-id"
    )
    request_id = str(request["send_request_id"])
    message_id = "<duplicate-recovery-v171@example.com>"
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='delivery_unknown', message_id=?, "
        "recovery_required=1 WHERE send_request_id=?",
        (message_id, request_id),
    )
    connection.commit()
    for suffix in ("one", "two"):
        _insert_package(
            tmp_cfg,
            package_id=f"pkg-duplicate-{suffix}",
            account_id=account_id,
            account_ref=f"qq:duplicate-{suffix}@example.com",
            mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
            message_id=message_id,
            raw_sha256=suffix[0] * 64,
        )
    _insert_package(
        tmp_cfg,
        package_id="pkg-thread-target",
        account_id=account_id,
        account_ref="qq:thread-target@example.com",
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id="<thread-target-v171@example.com>",
        raw_sha256="t" * 64,
    )

    result = reconcile_send_request_locally(tmp_cfg.db_path, request_id)
    relations = record_thread_relations(
        tmp_cfg.db_path,
        account_id=account_id,
        package_id="pkg-thread-target",
        in_reply_to_raw=message_id,
    )

    assert result["changed"] is False
    assert result["ambiguous"] is True
    assert result["candidate_count"] == 2
    assert get_send_request(tmp_cfg.db_path, request_id)["status"] == "delivery_unknown"
    assert relations == []
    assert connection.execute(
        "SELECT COUNT(*) FROM reconciliation_records "
        "WHERE send_request_id=? AND status='unresolved'",
        (request_id,),
    ).fetchone()[0] == 1


def test_external_sent_observation_creates_one_outbound_fact(tmp_cfg):
    _service, account, _mailboxes = _account_context(tmp_cfg, provider="gmail")
    message = EmailMessage()
    message["From"] = str(account["email_address"])
    message["To"] = "receiver@example.com"
    message["Subject"] = "external client sent"
    message["Message-ID"] = "<external-v171@example.com>"
    message.set_content("sent outside AgentMailBridge")
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="external-provider-1",
        thread_id="",
        uid="99",
        uidvalidity=456,
        received_at="2026-07-30 13:00:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="Sent",
        direction="outbound",
    )

    first = process_normalized_mail(tmp_cfg, normalized, apply_receive_rule=False)
    second = process_normalized_mail(tmp_cfg, normalized, apply_receive_rule=False)

    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert first["package_id"] == second["package_id"]
    package = get_connection(tmp_cfg.db_path).execute(
        "SELECT direction, local_outbound FROM mail_packages WHERE package_id=?",
        (first["package_id"],),
    ).fetchone()
    assert (package["direction"], package["local_outbound"]) == ("outbound", 0)
    records = get_connection(tmp_cfg.db_path).execute(
        "SELECT status FROM reconciliation_records "
        "WHERE package_id=? AND entity_type='sent_observation'",
        (first["package_id"],),
    ).fetchall()
    assert {str(row["status"]) for row in records} >= {
        "external_fact_created",
        "matched",
    }


def test_snapshot_cleanup_is_dry_run_first_and_protects_unknown_delivery(tmp_cfg):
    _service, _identity, _account_id, cancelled, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="cleanup-cancelled"
    )
    _service2, _identity2, _account_id2, unknown, _mailboxes2 = (
        _create_ready_request(tmp_cfg, suffix="cleanup-unknown")
    )
    connection = get_connection(tmp_cfg.db_path)
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        "UPDATE send_requests SET status='cancelled', updated_at=? "
        "WHERE send_request_id=?",
        (old, cancelled["send_request_id"]),
    )
    connection.execute(
        "UPDATE send_requests SET status='delivery_unknown', recovery_required=1, "
        "updated_at=? WHERE send_request_id=?",
        (old, unknown["send_request_id"]),
    )
    connection.commit()
    cancelled_root = (
        tmp_cfg.send_dir / "agent_requests" / str(cancelled["send_request_id"])
    )
    unknown_root = tmp_cfg.send_dir / "agent_requests" / str(unknown["send_request_id"])
    cancelled_root.mkdir(parents=True, exist_ok=True)
    unknown_root.mkdir(parents=True, exist_ok=True)
    (cancelled_root / "snapshot.bin").write_bytes(b"safe to clean")
    (unknown_root / "raw.eml").write_bytes(b"only recovery material")

    preview = cleanup_send_snapshots(
        tmp_cfg, dry_run=True, now=datetime.now()
    )

    assert preview["eligible_count"] == 1
    assert preview["items"][0]["send_request_id"] == cancelled["send_request_id"]
    assert cancelled_root.is_dir()
    assert unknown_root.is_dir()

    executed = cleanup_send_snapshots(
        tmp_cfg, dry_run=False, now=datetime.now()
    )
    assert executed["cleaned_count"] == 1
    assert not cancelled_root.exists()
    assert unknown_root.is_dir()
    assert get_send_request(
        tmp_cfg.db_path, str(cancelled["send_request_id"])
    )["snapshot_cleaned_at"]


def test_consistency_scan_finds_and_resolves_fact_and_snapshot_issues(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg, provider="qq")
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    duplicate_hash = hashlib.sha256(b"same raw").hexdigest()
    _insert_package(
        tmp_cfg,
        package_id="pkg-scan-a",
        account_id=account_id,
        account_ref="qq:scan-a@example.com",
        mailbox_id=mailbox_id,
        message_id="<scan-a@example.com>",
        raw_sha256=duplicate_hash,
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-scan-b",
        account_id=account_id,
        account_ref="qq:scan-b@example.com",
        mailbox_id=mailbox_id,
        message_id="<scan-b@example.com>",
        raw_sha256=duplicate_hash,
    )
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE mail_packages SET direction_conflict=1 WHERE package_id='pkg-scan-a'"
    )
    connection.commit()
    orphan = tmp_cfg.send_dir / "agent_requests" / "orphan-snapshot-v171"
    orphan.mkdir(parents=True)

    first = scan_mail_consistency(tmp_cfg)

    assert first["summary"]["duplicate_fact_candidate"] == 1
    assert first["summary"]["direction_conflict"] == 1
    assert first["summary"]["orphan_send_snapshot"] == 1
    connection.execute("DELETE FROM mail_packages WHERE package_id='pkg-scan-b'")
    connection.execute(
        "UPDATE mail_packages SET direction_conflict=0 WHERE package_id='pkg-scan-a'"
    )
    connection.commit()
    orphan.rmdir()

    second = scan_mail_consistency(tmp_cfg)

    assert "duplicate_fact_candidate" not in second["summary"]
    assert "direction_conflict" not in second["summary"]
    assert "orphan_send_snapshot" not in second["summary"]
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM health_issues WHERE state='open'"
        ).fetchone()[0]
        == 0
    )


def test_consistency_repair_is_single_item_backed_up_and_audited(tmp_cfg):
    service, _identity, _account_id, first, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="repair-first"
    )
    _other, _identity, _account_id, second, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="repair-second"
    )
    for index, request in enumerate((first, second), start=1):
        claim_for_send(
            tmp_cfg.db_path,
            str(request["send_request_id"]),
            lease_owner=f"repair-owner-{index}",
            process_id=f"repair-process-{index}",
        )
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_execution_leases "
        "SET lease_expires_at='2000-01-01 00:00:00', "
        "current_stage='mime_built'"
    )
    connection.commit()

    scanned = service.scan_consistency()

    assert scanned.ok
    preview = scanned.details["repair_preview"]
    stale = [
        row
        for row in preview["issues"]
        if row["issue_code"] == "stale_send_lease"
    ]
    assert len(stale) == 2
    selected = next(
        row
        for row in stale
        if row["entity_id"] == str(first["send_request_id"])
    )
    unconfirmed = service.apply_consistency_repair(
        scan_id=str(scanned.details["scan_id"]),
        issue_id=str(selected["issue_id"]),
    )
    assert not unconfirmed.ok
    assert unconfirmed.error_code == "consistency_repair_confirmation_required"

    repaired = service.apply_consistency_repair(
        scan_id=str(scanned.details["scan_id"]),
        issue_id=str(selected["issue_id"]),
        confirmed=True,
    )

    assert repaired.ok, repaired.message
    assert repaired.details["changed"] is True
    assert get_send_request(
        tmp_cfg.db_path, str(first["send_request_id"])
    )["status"] == "definitely_not_sent"
    assert get_send_request(
        tmp_cfg.db_path, str(second["send_request_id"])
    )["status"] == "sending"
    audit = connection.execute(
        "SELECT * FROM consistency_repair_runs WHERE repair_id=?",
        (repaired.details["repair_id"],),
    ).fetchone()
    assert audit["status"] == "completed"
    assert audit["issue_id"] == selected["issue_id"]
    assert audit["backup_name"] == repaired.details["backup_name"]
    assert json.loads(audit["details_json"]) == {
        "changed": True,
        "result_code": "definitely_not_sent",
    }
    backup = tmp_cfg.data_root_path / "backups" / str(audit["backup_name"])
    assert backup.is_file()
    assert backup.with_suffix(".json").is_file()
    remaining = repaired.details["scan"]["repair_preview"]["issues"]
    assert any(
        row["issue_code"] == "stale_send_lease"
        and row["entity_id"] == str(second["send_request_id"])
        for row in remaining
    )


def test_consistency_repair_keeps_ambiguous_fact_issues_manual(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg, provider="qq")
    _insert_package(
        tmp_cfg,
        package_id="pkg-manual-repair",
        account_id=str(account["account_id"]),
        account_ref="qq:manual-repair@example.com",
        mailbox_id=str(mailboxes["INBOX"]["mailbox_id"]),
        message_id="<manual-repair@example.com>",
        raw_sha256=hashlib.sha256(b"manual repair fact").hexdigest(),
    )
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE mail_packages SET direction_conflict=1 "
        "WHERE package_id='pkg-manual-repair'"
    )
    connection.commit()
    service = ApplicationService(tmp_cfg)

    scanned = service.scan_consistency()

    issue = next(
        row
        for row in scanned.details["repair_preview"]["issues"]
        if row["issue_code"] == "direction_conflict"
    )
    assert issue["repairable"] is False
    assert issue["action"] == "manual_review"
    attempted = service.apply_consistency_repair(
        scan_id=str(scanned.details["scan_id"]),
        issue_id=str(issue["issue_id"]),
        confirmed=True,
    )
    assert not attempted.ok
    assert attempted.error_code == "consistency_issue_manual_only"
    assert connection.execute(
        "SELECT COUNT(*) FROM consistency_repair_runs"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT direction_conflict FROM mail_packages "
        "WHERE package_id='pkg-manual-repair'"
    ).fetchone()[0] == 1


def test_consistency_scan_rejects_outbound_link_without_request_owner(tmp_cfg):
    _service, _identity, account_id, request, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="null-outbound-owner"
    )
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "INSERT INTO outbound_messages "
        "(outbound_id, sender_account_ref, from_account_id, sender_ref, "
        "source_origin, request_id, subject, to_emails, status, created_at, "
        "updated_at) VALUES ('out-null-owner', ?, ?, ?, 'agent', NULL, "
        "'subject', 'receiver@example.com', 'prepared', ?, ?)",
        (
            f"qq:{account_id}",
            account_id,
            account_id,
            "2026-07-30 12:00:00",
            "2026-07-30 12:00:00",
        ),
    )
    connection.execute(
        "UPDATE send_requests SET outbound_id='out-null-owner' "
        "WHERE send_request_id=?",
        (request["send_request_id"],),
    )
    connection.commit()

    result = scan_mail_consistency(tmp_cfg)

    assert result["summary"]["send_request_outbound_invalid"] == 1


def test_secret_canary_detection_is_redacted_from_scan_result(
    tmp_cfg, monkeypatch
):
    _service, _identity, _account_id, request, _mailboxes = _create_ready_request(
        tmp_cfg, suffix="secret-canary"
    )
    canary = "V171_SECRET_CANARY_DO_NOT_EXPOSE"
    monkeypatch.setenv("AGENT_MAIL_BRIDGE_SECRET_CANARY", canary)
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET error_message=? WHERE send_request_id=?",
        (canary, request["send_request_id"]),
    )
    connection.commit()

    result = scan_mail_consistency(tmp_cfg)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["summary"]["secret_canary_detected"] == 1
    assert canary not in serialized
    issue = next(
        row for row in result["issues"] if row["type"] == "secret_canary_detected"
    )
    assert issue["name"] == "redacted"


def test_full_consistency_scan_cross_checks_archive_and_work_copies(tmp_cfg):
    service, _account, _mailboxes = _account_context(tmp_cfg, provider="gmail")
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = tmp_cfg.gmail_address
    message["Subject"] = "consistency cross-check"
    message["Message-ID"] = "<consistency-cross-check@example.com>"
    message.set_content("body for consistency scan")
    message.add_attachment(
        b"resource bytes",
        maintype="application",
        subtype="octet-stream",
        filename="resource.bin",
    )
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="1710",
        uidvalidity=171,
        received_at="2026-07-30 17:10:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    archived = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )
    package = get_connection(tmp_cfg.db_path).execute(
        "SELECT package_root FROM mail_packages WHERE package_id=?",
        (archived["package_id"],),
    ).fetchone()
    root = Path(str(package["package_root"]))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attachment = next(
        item
        for item in manifest["resources"]
        if item.get("internal_type") == "attachment"
    )
    (root / str(attachment["path"])).unlink()
    (root / "raw.eml").write_bytes(b"corrupted raw bytes")
    manifest["raw_eml"]["sha256"] = "0" * 64
    manifest["resources"][0]["sha256"] = "f" * 64
    manifest["resources"].pop()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    orphan_package = (
        tmp_cfg.received_dir
        / "mail"
        / "2026"
        / "07"
        / "30"
        / "pkg-orphan-disk"
    )
    orphan_package.mkdir(parents=True)
    (orphan_package / "manifest.json").write_text(
        json.dumps({"package_id": "pkg-orphan-disk"}), encoding="utf-8"
    )
    workspace = tmp_cfg.data_root_path.parent / "consistency-workspace"
    work_copy = (
        workspace
        / ".agentmailbridge"
        / "mail"
        / "pkg-unknown-work-copy"
        / "完整邮件资料"
    )
    work_copy.mkdir(parents=True)
    (work_copy / "body.txt").write_text("copy", encoding="utf-8")
    tmp_cfg.allowed_send_roots = [workspace]

    result = scan_consistency(tmp_cfg)

    assert result["summary"]["missing"] >= 1
    assert result["summary"]["hash_mismatch"] >= 1
    assert result["summary"]["manifest_raw_mismatch"] == 1
    assert result["summary"]["manifest_resource_mismatch"] == 1
    assert result["summary"]["manifest_resource_ownership_mismatch"] == 1
    assert result["summary"]["package_orphan"] == 1
    assert result["summary"]["orphan_work_copy"] == 1
    issue_codes = {
        row["issue_code"]
        for row in service.get_mail_health_status().details["issues"]
    }
    assert {
        "missing",
        "hash_mismatch",
        "manifest_raw_mismatch",
        "manifest_resource_mismatch",
        "manifest_resource_ownership_mismatch",
        "orphan_work_copy",
    }.issubset(issue_codes)


def test_manifest_identity_conflicts_are_reported_once_per_package(tmp_cfg):
    _service, account, _mailboxes = _account_context(tmp_cfg, provider="gmail")
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = tmp_cfg.gmail_address
    message["Subject"] = "manifest identity"
    message["Message-ID"] = "<manifest-identity-v171@example.com>"
    message.set_content("manifest identity body")
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="1711",
        uidvalidity=171,
        received_at="2026-07-30 17:11:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    archived = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )
    package = get_connection(tmp_cfg.db_path).execute(
        "SELECT package_root FROM mail_packages WHERE package_id=?",
        (archived["package_id"],),
    ).fetchone()
    manifest_path = Path(str(package["package_root"])) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_id"] = "pkg-wrong"
    manifest["account_id"] = str(account["account_id"]) + "-wrong"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = scan_consistency(tmp_cfg)

    assert result["summary"]["manifest_identity_mismatch"] == 1
    assert sum(
        issue["type"] == "manifest_identity_mismatch"
        for issue in result["issues"]
    ) == 1


def test_active_staging_files_are_not_reported_as_orphans(tmp_cfg):
    assert ApplicationService(tmp_cfg).initialize().ok
    staging_roots = (
        tmp_cfg.received_dir / "mail" / ".staging" / "received-active",
        tmp_cfg.sent_dir / "mail" / ".staging" / "sent-active",
        tmp_cfg.send_dir / "staging" / "send-active",
    )
    for index, root in enumerate(staging_roots):
        root.mkdir(parents=True, exist_ok=True)
        (root / f"active-{index}.tmp").write_bytes(b"active staging")

    result = scan_consistency(tmp_cfg)

    assert result["summary"]["orphan"] == 0
    assert result["summary"]["package_orphan"] == 0
    assert result["summary"]["staging_residual"] == 0
    assert not {
        "orphan",
        "package_orphan",
        "staging_residual",
    }.intersection(issue["type"] for issue in result["issues"])


def test_health_summary_includes_sync_history_and_safe_cleanup(tmp_cfg):
    service, account, mailboxes = _account_context(tmp_cfg, provider="qq")
    account_id = str(account["account_id"])
    inbox_id = str(mailboxes["INBOX"]["mailbox_id"])
    finish_mailbox_attempt(
        tmp_cfg.db_path,
        mailbox_id=inbox_id,
        account_id=account_id,
        uidvalidity=77,
        uidnext=90,
        highestmodseq=0,
        last_uid=75,
        checkpoint={"last_uid": 75},
        result="failed",
        error="temporary network failure",
        current_attempt={"stage": "fetch"},
    )
    upsert_history_import_run(
        tmp_cfg.db_path,
        run_id="history-health-v171",
        account_id=account_id,
        preset="custom",
        date_from="2026-01-01 00:00:00",
        date_to="2026-07-30 23:59:59",
        mailbox_ids=[inbox_id],
        apply_receive_rule=True,
        status="partial",
        scanned=120,
        saved=90,
        failed=2,
        segment_index=2,
        total_segments=4,
        next_segment_index=2,
    )
    _send_service, _identity, _send_account, request, _send_mailboxes = (
        _create_ready_request(tmp_cfg, suffix="health-cleanup")
    )
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='cancelled', updated_at=? "
        "WHERE send_request_id=?",
        (old, request["send_request_id"]),
    )
    connection.commit()
    snapshot_root = (
        tmp_cfg.send_dir / "agent_requests" / str(request["send_request_id"])
    )
    snapshot_root.mkdir(parents=True, exist_ok=True)
    (snapshot_root / "snapshot.bin").write_bytes(b"safe cleanup bytes")
    (snapshot_root / "raw.eml").write_bytes(b"exact raw MIME")
    staging_root = tmp_cfg.received_dir / "mail" / ".staging" / "pending"
    staging_root.mkdir(parents=True)
    (staging_root / "raw.eml").write_bytes(b"not permanent")

    health = service.get_mail_health_status()

    assert health.ok
    assert any(
        row["mailbox_id"] == inbox_id
        and row["last_result"] == "failed"
        and row["current_attempt"]["stage"] == "fetch"
        for row in health.details["mailboxes"]
    )
    assert health.details["history_imports"][0]["status"] == "partial"
    assert health.details["storage"]["safe_cleanup_count"] == 1
    expected_snapshot_bytes = len(b"safe cleanup bytes") + len(b"exact raw MIME")
    assert health.details["storage"]["safe_cleanup_bytes"] == expected_snapshot_bytes
    assert health.details["storage"]["send_snapshots_bytes"] == expected_snapshot_bytes
    assert health.details["storage"]["permanent_resources_bytes"] == 0
    rendered = format_mail_health(
        health.details, lambda value: f"{int(value or 0)} B"
    )
    assert "连续失败 1 次" in rendered
    assert "部分完成，可继续" in rendered
    assert f"可安全清理 {expected_snapshot_bytes} B (1 项)" in rendered


def test_health_issue_total_is_exact_while_details_remain_bounded(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    connection = get_connection(tmp_cfg.db_path)
    now = "2026-07-30 18:00:00"
    connection.executemany(
        "INSERT INTO health_issues "
        "(issue_id, category, issue_code, severity, entity_type, entity_id, "
        "state, details_json, first_seen_at, last_seen_at) "
        "VALUES (?, 'test', 'bounded_issue', 'warning', 'test_entity', ?, "
        "'open', '{}', ?, ?)",
        [
            (f"health-bounded-{index}", f"entity-{index}", now, now)
            for index in range(105)
        ],
    )
    connection.commit()

    health = service.get_mail_health_status()

    assert health.ok
    assert health.details["issue_count"] == 105
    assert len(health.details["issues"]) == 100
