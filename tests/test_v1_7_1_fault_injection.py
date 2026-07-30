from __future__ import annotations

import errno
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import get_connection
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.outbound_mail import (
    _resolve_recipients,
    execute_agent_send,
    prepare_agent_send,
    recover_agent_send_archive,
)
from agent_mail_bridge.retention_cleanup import cleanup_send_snapshots
from agent_mail_bridge.send_recovery import recover_incomplete_send_requests
from agent_mail_bridge.send_requests import (
    claim_for_send,
    create_send_request,
    get_send_request,
)
from agent_mail_bridge.storage import atomic_write_bytes, replace_atomically


def _ready_send_request(tmp_cfg, suffix: str) -> dict:
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    assert service.synchronize_mail_accounts().ok
    account = next(
        row
        for row in service.list_mail_accounts().details["accounts"]
        if row["provider"] == "qq"
    )
    account_id = str(account["account_id"])
    created = service.create_agent_client(
        client_type="codex",
        display_name=f"fault injection {suffix}",
        capabilities=["mail.send", "send.status"],
        send_account_ids=[account_id],
        send_mode="autonomous",
    )
    assert created.ok, created.message
    client_id = str(created.details["client"]["client_id"])
    assert service.set_agent_client_state(client_id, "active", enabled=True).ok
    request, is_new = create_send_request(
        tmp_cfg.db_path,
        send_request_id=f"fault-{suffix}",
        client_id=client_id,
        idempotency_key=f"fault-idem-{suffix}",
        operation="new",
        sender_account_id=account_id,
        source_package_id=None,
        reply_to_package_id=None,
        forward_from_package_id=None,
        send_mode="autonomous",
        subject=f"fault {suffix}",
        body_text="fault injection body",
        body_html="",
        status="ready_to_send",
        expires_at=None,
        message_id=f"<fault-{suffix}@agentmailbridge.local>",
        recipients=[
            {
                "recipient_type": "to",
                "email_address": "receiver@example.com",
            }
        ],
        attachments=[],
    )
    assert is_new
    return request


def test_atomic_write_detects_disk_full_before_staging(
    tmp_path, monkeypatch
):
    target = tmp_path / "raw.eml"
    monkeypatch.setattr(
        "agent_mail_bridge.storage.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=10, used=10, free=0),
    )

    with pytest.raises(OSError) as captured:
        atomic_write_bytes(target, b"exact mime bytes")

    assert captured.value.errno == errno.ENOSPC
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_replace_retries_windows_file_lock_32(monkeypatch):
    attempts: list[int] = []

    def locked_then_available(_source, _target):
        attempts.append(1)
        if len(attempts) < 3:
            error = PermissionError("file is in use")
            error.winerror = 32
            raise error

    monkeypatch.setattr(
        "agent_mail_bridge.storage.os.replace", locked_then_available
    )
    monkeypatch.setattr(
        "agent_mail_bridge.storage.time.sleep", lambda _seconds: None
    )
    monkeypatch.setattr("agent_mail_bridge.storage.sys.platform", "win32")

    replace_atomically(Path("source"), Path("target"))

    assert len(attempts) == 3


def test_sqlite_busy_leaves_send_request_and_lease_unchanged(tmp_cfg):
    request = _ready_send_request(tmp_cfg, "sqlite-busy")
    request_id = str(request["send_request_id"])
    connection = get_connection(tmp_cfg.db_path)
    connection.execute("PRAGMA busy_timeout=20")
    locker = sqlite3.connect(tmp_cfg.db_path, timeout=0, isolation_level=None)
    locker.execute("PRAGMA journal_mode=WAL")
    locker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            claim_for_send(
                tmp_cfg.db_path,
                request_id,
                lease_owner="busy-contender",
            )
    finally:
        locker.rollback()
        locker.close()
        connection.execute("PRAGMA busy_timeout=5000")

    assert get_send_request(tmp_cfg.db_path, request_id)["status"] == "ready_to_send"
    assert connection.execute(
        "SELECT COUNT(*) FROM send_execution_leases WHERE send_request_id=?",
        (request_id,),
    ).fetchone()[0] == 0
    claimed, acquired = claim_for_send(
        tmp_cfg.db_path,
        request_id,
        lease_owner="after-busy",
    )
    assert acquired is True
    assert claimed["status"] == "sending"


def test_two_real_processes_cannot_claim_the_same_send_lease(tmp_cfg):
    request = _ready_send_request(tmp_cfg, "two-processes")
    request_id = str(request["send_request_id"])
    code = (
        "import json,sys; "
        "from agent_mail_bridge.send_requests import claim_for_send; "
        "row,claimed=claim_for_send(sys.argv[1],sys.argv[2],"
        "lease_owner=sys.argv[3]); "
        "print(json.dumps({'claimed':claimed,'status':row['status']}))"
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(tmp_cfg.db_path),
                request_id,
                f"process-{index}",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))

    assert sum(bool(item["claimed"]) for item in results) == 1
    assert get_connection(tmp_cfg.db_path).execute(
        "SELECT COUNT(*) FROM send_execution_leases WHERE send_request_id=?",
        (request_id,),
    ).fetchone()[0] == 1


def test_cleanup_revalidates_when_request_becomes_recovery_required(
    tmp_cfg, monkeypatch
):
    request = _ready_send_request(tmp_cfg, "cleanup-race")
    request_id = str(request["send_request_id"])
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='cancelled', updated_at=? "
        "WHERE send_request_id=?",
        (old, request_id),
    )
    connection.commit()
    root = tmp_cfg.send_dir / "agent_requests" / request_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "raw.eml").write_bytes(b"must survive the race")

    import agent_mail_bridge.retention_cleanup as cleanup_module

    original = cleanup_module._eligible_requests
    calls = 0

    def change_after_planning(cfg, *, current):
        nonlocal calls
        calls += 1
        rows = original(cfg, current=current)
        if calls == 1 and rows:
            connection.execute(
                "UPDATE send_requests SET status='delivery_unknown', "
                "recovery_required=1 WHERE send_request_id=?",
                (request_id,),
            )
            connection.commit()
        return rows

    monkeypatch.setattr(
        cleanup_module, "_eligible_requests", change_after_planning
    )

    result = cleanup_send_snapshots(
        tmp_cfg, dry_run=False, now=datetime.now()
    )

    assert result["cleaned_count"] == 0
    assert result["eligibility_changed_count"] == 1
    assert result["status"] == "partial"
    assert (root / "raw.eml").read_bytes() == b"must survive the race"
    assert get_send_request(tmp_cfg.db_path, request_id)["snapshot_cleaned_at"] is None


def test_cleanup_database_failure_restores_snapshot_and_rolls_back(
    tmp_cfg, monkeypatch
):
    request = _ready_send_request(tmp_cfg, "cleanup-rollback")
    request_id = str(request["send_request_id"])
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET status='cancelled', updated_at=? "
        "WHERE send_request_id=?",
        (old, request_id),
    )
    connection.commit()
    root = tmp_cfg.send_dir / "agent_requests" / request_id
    root.mkdir(parents=True, exist_ok=True)
    payload = b"must return after database rollback"
    (root / "raw.eml").write_bytes(payload)

    def fail_database_update(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated transaction failure")

    monkeypatch.setattr(
        "agent_mail_bridge.retention_cleanup._mark_snapshot_cleaned",
        fail_database_update,
    )

    result = cleanup_send_snapshots(
        tmp_cfg, dry_run=False, now=datetime.now()
    )

    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert result["cleaned_count"] == 0
    assert result["failed_count"] == 1
    assert result["status"] == "partial"
    assert (root / "raw.eml").read_bytes() == payload
    assert persisted["status"] == "cancelled"
    assert persisted["snapshot_cleaned_at"] is None
    quarantine = tmp_cfg.send_dir / "staging" / "snapshot_cleanup"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []


def test_startup_recovery_does_not_take_an_active_send_lease(tmp_cfg):
    request = _ready_send_request(tmp_cfg, "active-lease")
    request_id = str(request["send_request_id"])
    claimed, acquired = claim_for_send(
        tmp_cfg.db_path,
        request_id,
        lease_owner="still-running",
        process_id="active-process",
    )
    assert acquired is True

    result = recover_incomplete_send_requests(tmp_cfg.db_path)

    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert result["active"] == 1
    assert request_id not in result["archive_requests"]
    assert persisted["status"] == "sending"
    assert persisted["lease"]["lease_owner"] == "still-running"
    assert persisted["lease"]["attempt_no"] == claimed["lease"]["attempt_no"]


def test_unicode_sender_local_part_is_rejected_before_smtp(tmp_cfg, monkeypatch):
    request = _ready_send_request(tmp_cfg, "unicode-sender")
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda *_args, **_kwargs: smtp_calls.append(True),
    )

    result = execute_agent_send(
        tmp_cfg,
        runtime_cfg=tmp_cfg,
        send_request_id=str(request["send_request_id"]),
        sender_address="用户@example.com",
        sender_display_name="Sender",
        authorize=lambda _request: None,
    )

    persisted = get_send_request(tmp_cfg.db_path, str(request["send_request_id"]))
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_sender"
    assert persisted["smtp_attempt_count"] == 0
    assert persisted["raw_eml_path"] is None
    assert smtp_calls == []


@pytest.mark.parametrize(
    "crash_stage,expected_stage",
    [
        ("after_lease", "lease_acquired"),
        ("after_mime", "mime_built"),
        ("before_smtp", "smtp_starting"),
    ],
)
def test_pre_smtp_crash_matrix_recovers_without_smtp(
    tmp_cfg, monkeypatch, crash_stage, expected_stage
):
    request = _ready_send_request(tmp_cfg, f"crash-{crash_stage}")
    request_id = str(request["send_request_id"])
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda *_args, **_kwargs: smtp_calls.append(True),
    )

    def crash(stage: str):
        if stage == crash_stage:
            raise SystemExit(f"simulated crash at {stage}")

    with pytest.raises(SystemExit, match=crash_stage):
        execute_agent_send(
            tmp_cfg,
            runtime_cfg=tmp_cfg,
            send_request_id=request_id,
            sender_address=tmp_cfg.qq_email,
            sender_display_name="Sender",
            authorize=lambda _request: None,
            fault_hook=crash,
        )

    interrupted = get_send_request(tmp_cfg.db_path, request_id)
    assert interrupted["status"] == "sending"
    assert interrupted["current_stage"] == expected_stage
    assert interrupted["smtp_attempt_count"] == 0
    assert smtp_calls == []
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_execution_leases SET lease_expires_at='2000-01-01 00:00:00' "
        "WHERE send_request_id=?",
        (request_id,),
    )
    connection.commit()

    recovery = recover_incomplete_send_requests(tmp_cfg.db_path)

    recovered = get_send_request(tmp_cfg.db_path, request_id)
    assert recovery["definitely_not_sent"] >= 1
    assert recovered["status"] == "definitely_not_sent"
    assert recovered["raw_eml_path"] is None or Path(
        str(recovered["raw_eml_path"])
    ).is_file()
    assert connection.execute(
        "SELECT COUNT(*) FROM send_attempt_events "
        "WHERE send_request_id=? AND stage=?",
        (request_id, expected_stage),
    ).fetchone()[0] >= 1


def test_before_lease_crash_leaves_request_ready_and_unclaimed(tmp_cfg):
    request = _ready_send_request(tmp_cfg, "crash-before-lease")
    request_id = str(request["send_request_id"])

    def crash_before_lease(stage: str):
        if stage == "before_lease":
            raise SystemExit("before_lease")

    with pytest.raises(SystemExit, match="before_lease"):
        execute_agent_send(
            tmp_cfg,
            runtime_cfg=tmp_cfg,
            send_request_id=request_id,
            sender_address=tmp_cfg.qq_email,
            sender_display_name="Sender",
            authorize=lambda _request: None,
            fault_hook=crash_before_lease,
        )

    persisted = get_send_request(tmp_cfg.db_path, request_id)
    assert persisted["status"] == "ready_to_send"
    assert persisted["lease"] is None
    assert persisted["smtp_attempt_count"] == 0


@pytest.mark.parametrize(
    "crash_stage,expected_terminal",
    [
        ("smtp_data_started", "delivery_unknown"),
        ("smtp_accepted", "sent"),
        ("after_smtp_accepted", "sent"),
        ("after_archive_before_completion", "sent"),
    ],
)
def test_smtp_boundary_crash_matrix_never_retries(
    tmp_cfg, monkeypatch, crash_stage, expected_terminal
):
    request = _ready_send_request(tmp_cfg, f"crash-{crash_stage}")
    request_id = str(request["send_request_id"])
    smtp_calls = []

    def accepted_smtp(
        _cfg, _raw_bytes, *, from_addr, to_addrs, stage_callback=None
    ):
        smtp_calls.append((from_addr, tuple(to_addrs)))
        assert stage_callback is not None
        stage_callback("smtp_connected")
        stage_callback("smtp_data_started")
        stage_callback("smtp_accepted")

    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        accepted_smtp,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail._append_provider_sent_copy",
        lambda *_args, **_kwargs: {"status": "skipped"},
    )

    def crash(stage: str):
        if stage == crash_stage:
            raise SystemExit(f"simulated crash at {stage}")

    with pytest.raises(SystemExit, match=crash_stage):
        execute_agent_send(
            tmp_cfg,
            runtime_cfg=tmp_cfg,
            send_request_id=request_id,
            sender_address=tmp_cfg.qq_email,
            sender_display_name="Sender",
            authorize=lambda _request: None,
            fault_hook=crash,
        )

    interrupted = get_send_request(tmp_cfg.db_path, request_id)
    assert len(smtp_calls) == 1
    assert interrupted["smtp_attempt_count"] == 1
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_execution_leases SET lease_expires_at='2000-01-01 00:00:00' "
        "WHERE send_request_id=?",
        (request_id,),
    )
    connection.commit()
    recovery = recover_incomplete_send_requests(tmp_cfg.db_path)
    if expected_terminal == "delivery_unknown":
        assert recovery["delivery_unknown"] >= 1
    else:
        assert request_id in recovery["archive_requests"]
        recover_agent_send_archive(
            tmp_cfg,
            runtime_cfg=tmp_cfg,
            send_request_id=request_id,
        )

    recovered = get_send_request(tmp_cfg.db_path, request_id)
    assert recovered["status"] == expected_terminal
    replay = execute_agent_send(
        tmp_cfg,
        runtime_cfg=tmp_cfg,
        send_request_id=request_id,
        sender_address=tmp_cfg.qq_email,
        sender_display_name="Sender",
        authorize=lambda _request: None,
    )
    assert replay["status"] == expected_terminal
    assert len(smtp_calls) == 1


def test_large_attachment_set_and_oversized_part_are_isolated(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    assert service.synchronize_mail_accounts().ok
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = tmp_cfg.gmail_address
    message["Subject"] = "51 attachments"
    message["Message-ID"] = "<many-parts-v171@example.com>"
    message.set_content("large attachment set")
    for index in range(51):
        message.add_attachment(
            f"part-{index}".encode(),
            maintype="application",
            subtype="octet-stream",
            filename=f"part-{index:02d}.bin",
        )
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="710",
        uidvalidity=1000,
        received_at="2026-07-30 14:00:00",
        observed_at="2026-07-30 14:01:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    archived = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )
    assert archived["status"] == "saved"
    assert archived["attachments"] == 51

    oversized = EmailMessage()
    oversized["From"] = "sender@example.com"
    oversized["To"] = tmp_cfg.gmail_address
    oversized["Subject"] = "oversized part"
    oversized["Message-ID"] = "<oversized-part-v171@example.com>"
    oversized.set_content("keep the mail fact")
    oversized.add_attachment(
        b"0123456789",
        maintype="application",
        subtype="octet-stream",
        filename="too-large.bin",
    )
    oversized_normalized = normalized_mail_from_raw(
        oversized.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="711",
        uidvalidity=1000,
        received_at="2026-07-30 14:02:00",
        saved_date="2026-07-30",
        max_attachment_bytes=8,
        mailbox_ref="INBOX",
    )
    partial = process_normalized_mail(
        tmp_cfg, oversized_normalized, apply_receive_rule=False
    )
    assert partial["status"] == "partial"
    package = get_connection(tmp_cfg.db_path).execute(
        "SELECT archive_status, raw_eml_status FROM mail_packages "
        "WHERE package_id=?",
        (partial["package_id"],),
    ).fetchone()
    assert tuple(package) == ("partial", "available")
    failed_resource = get_connection(tmp_cfg.db_path).execute(
        "SELECT status, local_path FROM mail_resources "
        "WHERE package_id=? AND resource_type='attachment'",
        (partial["package_id"],),
    ).fetchone()
    assert tuple(failed_resource) == ("failed", None)


def test_corrupt_mime_and_abnormal_date_preserve_observation_time(tmp_cfg):
    assert ApplicationService(tmp_cfg).initialize().ok
    raw = (
        b"From: sender@example.com\r\n"
        + f"To: {tmp_cfg.gmail_address}\r\n".encode()
        + b"Subject: malformed multipart\r\n"
        + b"Message-ID: <malformed-v171@example.com>\r\n"
        + b"Date: definitely-not-a-date\r\n"
        + b"Content-Type: multipart/mixed; boundary=broken\r\n\r\n"
        + b"--broken\r\nContent-Disposition: attachment; filename=bad.bin\r\n"
        + b"\r\npayload without closing boundary\xff"
    )
    normalized = normalized_mail_from_raw(
        raw,
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="801",
        uidvalidity=1100,
        received_at="2026-07-30 15:00:00",
        observed_at="2026-07-30 15:05:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    result = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )
    assert result["status"] in {"saved", "partial"}
    package = get_connection(tmp_cfg.db_path).execute(
        "SELECT date_header_raw, declared_at, observed_at, raw_eml_sha256 "
        "FROM mail_packages WHERE package_id=?",
        (result["package_id"],),
    ).fetchone()
    assert package["date_header_raw"] == "definitely-not-a-date"
    assert package["declared_at"] is None
    assert package["observed_at"] == "2026-07-30 15:05:00"
    assert package["raw_eml_sha256"]


def test_long_references_chain_uses_only_unique_deterministic_target(tmp_cfg):
    assert ApplicationService(tmp_cfg).initialize().ok
    source = EmailMessage()
    source["From"] = "sender@example.com"
    source["To"] = tmp_cfg.gmail_address
    source["Subject"] = "thread root"
    source["Message-ID"] = "<thread-root-v171@example.com>"
    source.set_content("root")
    source_mail = normalized_mail_from_raw(
        source.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="901",
        uidvalidity=1200,
        received_at="2026-07-30 16:00:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    source_result = process_normalized_mail(
        tmp_cfg, source_mail, apply_receive_rule=False
    )
    reply = EmailMessage()
    reply["From"] = "reply@example.com"
    reply["To"] = tmp_cfg.gmail_address
    reply["Subject"] = "unrelated subject text"
    reply["Message-ID"] = "<thread-reply-v171@example.com>"
    reply["References"] = " ".join(
        [f"<unresolved-{index}@example.com>" for index in range(600)]
        + ["<thread-root-v171@example.com>"]
    )
    reply.set_content("reply")
    reply_mail = normalized_mail_from_raw(
        reply.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="902",
        uidvalidity=1200,
        received_at="2026-07-30 16:01:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    reply_result = process_normalized_mail(
        tmp_cfg, reply_mail, apply_receive_rule=False
    )
    relations = get_connection(tmp_cfg.db_path).execute(
        "SELECT related_package_id, source FROM mail_thread_relations "
        "WHERE package_id=?",
        (reply_result["package_id"],),
    ).fetchall()
    assert [tuple(row) for row in relations] == [
        (source_result["package_id"], "rfc_in_reply_to")
    ]


def test_timezone_and_idn_address_edges_are_normalized_deterministically(tmp_cfg):
    request = _ready_send_request(tmp_cfg, "time-idn")
    dated = EmailMessage()
    dated["From"] = "Sender <sender@example.com>"
    dated["To"] = tmp_cfg.gmail_address
    dated["Subject"] = "timezone declaration"
    dated["Message-ID"] = "<timezone-v171@example.com>"
    dated["Date"] = "Thu, 30 Jul 2026 08:30:00 -0400"
    dated.set_content("declared and observed are separate")
    normalized = normalized_mail_from_raw(
        dated.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid="990",
        uidvalidity=1300,
        received_at="2026-07-30 20:30:00",
        observed_at="2026-07-31 09:00:00",
        saved_date="2026-07-30",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
    )
    result = process_normalized_mail(
        tmp_cfg, normalized, apply_receive_rule=False
    )
    timeline = get_connection(tmp_cfg.db_path).execute(
        "SELECT date_header_raw, declared_at, observed_at FROM mail_packages "
        "WHERE package_id=?",
        (result["package_id"],),
    ).fetchone()
    assert timeline["date_header_raw"] == "Thu, 30 Jul 2026 08:30:00 -0400"
    assert timeline["declared_at"]
    assert timeline["declared_at"] != timeline["observed_at"]
    assert timeline["observed_at"] == "2026-07-31 09:00:00"

    prepared, created = prepare_agent_send(
        tmp_cfg,
        client_id=str(request["client_id"]),
        send_mode="confirm",
        sender_account_id=str(request["sender_account_id"]),
        sender_address=tmp_cfg.qq_email,
        sender_display_name="发送者",
        own_addresses=(tmp_cfg.qq_email, tmp_cfg.gmail_address),
        idempotency_key="idn-domain-v171",
        operation="new",
        to=("Recipient <person@例子.公司>",),
        body_text="IDN recipient",
    )
    assert created is True
    assert prepared["recipients"]["to"] == [
        "person@xn--fsqu00a.xn--55qx5d"
    ]


def test_recipient_filtering_applies_all_own_addresses_only_to_reply_all():
    sender = "sender@qq.com"
    other_account = "other@163.com"
    owner_account = "owner@gmail.com"
    own_addresses = (sender, other_account, owner_account)

    new_recipients = _resolve_recipients(
        "new",
        to=(other_account,),
        cc=(),
        bcc=(sender,),
        sender_address=sender,
        own_addresses=own_addresses,
        source_message=None,
    )
    assert [row["email_address"] for row in new_recipients["to"]] == [
        other_account
    ]
    assert [row["email_address"] for row in new_recipients["bcc"]] == [
        sender
    ]

    source_message = {
        "from_addresses": [other_account],
        "to_addresses": [sender, owner_account, "external@example.com"],
        "cc_addresses": [other_account, "team@example.com"],
    }
    reply_recipients = _resolve_recipients(
        "reply",
        to=(),
        cc=(),
        bcc=(),
        sender_address=sender,
        own_addresses=own_addresses,
        source_message=source_message,
    )
    assert [row["email_address"] for row in reply_recipients["to"]] == [
        other_account
    ]

    reply_all_recipients = _resolve_recipients(
        "reply_all",
        to=(),
        cc=(),
        bcc=(),
        sender_address=sender,
        own_addresses=own_addresses,
        source_message=source_message,
    )
    assert [row["email_address"] for row in reply_all_recipients["to"]] == [
        "external@example.com"
    ]
    assert [row["email_address"] for row in reply_all_recipients["cc"]] == [
        "team@example.com"
    ]
