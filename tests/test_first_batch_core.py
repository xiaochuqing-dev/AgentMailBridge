"""第一批次核心收口与审计整改回归测试。"""

from __future__ import annotations

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    get_connection,
    get_send_by_request_id,
    insert_received_message,
    query_received_messages_by_date,
)
from agent_mail_bridge.gmail_api_receive import _extract_payload_unified
from agent_mail_bridge.gmail_api_auth import get_oauth_state
from agent_mail_bridge.gui import BridgeWindow
from agent_mail_bridge.mail_common import (
    AttachmentData,
    NormalizedMail,
    canonical_gmail_address,
    is_trusted_self_mail,
    normalize_message_id,
)
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mail_receive import _extract_message_content
from agent_mail_bridge.mail_send import SmtpStageError, _smtp_send_with_stage
from agent_mail_bridge.models import OperationStatus


def _mail(backend: str = "imap", message_id: str = "<Case@Test.COM>") -> NormalizedMail:
    return NormalizedMail(
        backend=backend,
        message_id=message_id,
        backend_message_id="api-1" if backend == "gmail_api" else "",
        thread_id="thread-1",
        uid="10",
        from_raw='"用户" <test+agent@gmail.com>',
        to_raw="other@example.com, test@gmail.com",
        cc_raw="",
        subject="中文测试",
        received_at="2026-07-10 12:00:00",
        saved_date="2026-07-10",
        body_text="正文",
        attachments=[AttachmentData("报告.txt", b"content", "text/plain", "allowed")],
    )


def test_address_and_message_id_rules_are_shared():
    assert canonical_gmail_address(" User.Name+tag@googlemail.com ") == "username@gmail.com"
    assert is_trusted_self_mail(
        "username@gmail.com",
        '"用户" <User.Name+agent@gmail.com>',
        "other@example.com",
        "USER.NAME@gmail.com",
    )
    assert normalize_message_id(" < Case@Test.COM > ") == "<case@test.com>"


def test_sqlite_uses_wal_and_five_second_busy_timeout(tmp_cfg):
    connection = get_connection(tmp_cfg.db_path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_cross_backend_message_is_saved_once(tmp_cfg):
    first = process_normalized_mail(tmp_cfg, _mail("imap"))
    second = process_normalized_mail(tmp_cfg, _mail("gmail_api"))
    rows = query_received_messages_by_date(tmp_cfg.db_path, "2026-07-10")
    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert len(rows) == 1


def test_missing_message_id_uses_cross_backend_fallback(tmp_cfg):
    first = process_normalized_mail(tmp_cfg, _mail("imap", ""))
    second = process_normalized_mail(tmp_cfg, _mail("gmail_api", ""))
    assert first["status"] == "saved"
    assert second["status"] == "duplicate"


def test_concurrent_receive_has_one_record_and_file_set(tmp_cfg):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: process_normalized_mail(tmp_cfg, _mail()), range(2)))
    assert sorted(item["status"] for item in results) == ["duplicate", "saved"]
    rows = query_received_messages_by_date(tmp_cfg.db_path, "2026-07-10")
    assert len(rows) == 1
    saved_files = [path for item in results for path in item.get("saved_files", [])]
    assert len(saved_files) == 2


def test_file_failure_rolls_back_completed_database_state(tmp_cfg, monkeypatch):
    original = Path.write_bytes

    def fail_attachment(path: Path, data: bytes):
        if path.parent.name == "attachments":
            raise OSError("模拟附件写入失败")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_attachment)
    with pytest.raises(OSError):
        process_normalized_mail(tmp_cfg, _mail())
    assert query_received_messages_by_date(tmp_cfg.db_path, "2026-07-10") == []


def test_imap_and_api_mime_selection_are_consistent(tmp_cfg):
    message = EmailMessage()
    message.set_content("中文正文")
    message.add_alternative("<p>HTML正文</p>", subtype="html")
    message.add_attachment(
        b"attachment", maintype="application", subtype="octet-stream", filename="报告.bin"
    )
    imap_body, imap_attachments = _extract_message_content(message, tmp_cfg)

    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = {
        "mimeType": "multipart/mixed",
        "body": {},
        "parts": [
            {
                "mimeType": "multipart/alternative", "body": {}, "parts": [
                    {"mimeType": "text/plain", "filename": "", "body": {"data": encode("中文正文\n".encode())}},
                    {"mimeType": "text/html", "filename": "", "body": {"data": encode("<p>HTML正文</p>".encode())}},
                ],
            },
            {
                "mimeType": "application/octet-stream", "filename": "报告.bin",
                "body": {"data": encode(b"attachment")},
            },
        ],
    }
    api_body, api_attachments = _extract_payload_unified(payload, tmp_cfg, Mock(), "m1")
    assert imap_body.strip() == api_body.strip() == "中文正文"
    assert [(item.filename, item.content, item.security_status) for item in imap_attachments] == [
        (item.filename, item.content, item.security_status) for item in api_attachments
    ]


def test_receive_lock_rejects_second_click(tmp_cfg, monkeypatch):
    service = ApplicationService(tmp_cfg)
    started = threading.Event()
    release = threading.Event()

    def slow_receive(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        return {"ok": True, "fetched": 0, "saved": 0, "skipped": 0, "attachments": 0, "errors": []}

    monkeypatch.setattr("agent_mail_bridge.application_service.receive_mails", slow_receive)
    tmp_cfg.gmail_receive_backend = "imap"
    worker = threading.Thread(target=service.receive)
    worker.start()
    assert started.wait(timeout=1)
    second = service.receive()
    release.set()
    worker.join(timeout=2)
    assert second.status == OperationStatus.CANCELLED
    assert second.error_code == "receive_busy"


def test_send_request_is_idempotent_after_archive_failure(tmp_cfg, monkeypatch):
    source = tmp_cfg.data_root_path / "result.txt"
    source.write_text("result", encoding="utf-8")
    smtp_calls = Mock()
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage", lambda cfg, message: smtp_calls()
    )
    original_copy = __import__("agent_mail_bridge.mail_send", fromlist=["copy_file"]).copy_file

    def fail_sent_archive(src, dst):
        if Path(dst).is_relative_to(tmp_cfg.sent_dir):
            raise OSError("模拟归档失败")
        return original_copy(src, dst)

    monkeypatch.setattr("agent_mail_bridge.mail_send.copy_file", fail_sent_archive)
    service = ApplicationService(tmp_cfg)
    first = service.send_file(source, request_id="request-1")
    second = service.send_file(source, request_id="request-1")
    row = get_send_by_request_id(tmp_cfg.db_path, "request-1")
    assert first.send_status == "sent_archive_failed"
    assert second.status == OperationStatus.DUPLICATE
    assert smtp_calls.call_count == 1
    assert row and row["status"] == "sent_archive_failed"


def test_smtp_failure_can_retry_same_request(tmp_cfg, monkeypatch):
    source = tmp_cfg.data_root_path / "retry.txt"
    source.write_text("retry", encoding="utf-8")
    calls = {"count": 0}

    def fail_then_succeed(cfg, message):
        calls["count"] += 1
        if calls["count"] == 1:
            raise SmtpStageError("send", "模拟发送失败")

    monkeypatch.setattr("agent_mail_bridge.mail_send._smtp_send_with_stage", fail_then_succeed)
    service = ApplicationService(tmp_cfg)
    first = service.send_file(source, request_id="request-retry")
    second = service.send_file(source, request_id="request-retry")
    assert first.status == OperationStatus.FAILED
    assert second.status == OperationStatus.SUCCESS
    assert calls["count"] == 2


def test_send_rejects_path_outside_allowed_roots(tmp_cfg, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    result = ApplicationService(tmp_cfg).send_file(outside, request_id="outside")
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "file_validation_failed"


def test_history_does_not_return_legacy_path_outside_data_root(tmp_cfg, tmp_path):
    outside = tmp_path / "legacy.md"
    insert_received_message(
        tmp_cfg.db_path,
        message_id="<legacy@test>", gmail_uid=None, subject="旧记录",
        from_email="test@gmail.com", to_email="test@gmail.com",
        received_at="2026-07-10 12:00:00", saved_date="2026-07-10",
        body_file_path=str(outside), body_sha256=None,
        has_attachments=False,
    )
    row = ApplicationService(tmp_cfg).get_history().details["received"][0]
    assert row["body_file_path"] == ""
    assert row["body_file_path_status"] == "unsafe_path"


def test_gui_repeated_click_does_not_start_second_task():
    window = BridgeWindow.__new__(BridgeWindow)
    window.task_active = True
    window.error_var = Mock()
    operation = Mock()
    window._run_task("重复任务", operation, Mock())
    operation.assert_not_called()
    window.error_var.set.assert_called_once_with("已有任务正在运行，请勿重复点击")


def test_smtp_timeout_reaches_underlying_connection(tmp_cfg, monkeypatch):
    server = Mock()
    smtp_factory = Mock(return_value=server)
    monkeypatch.setattr("agent_mail_bridge.mail_send.smtplib.SMTP_SSL", smtp_factory)
    tmp_cfg.qq_smtp_connect_timeout = 37  # 连接超时为 37 秒
    _smtp_send_with_stage(tmp_cfg, EmailMessage())
    assert smtp_factory.call_args.kwargs["timeout"] == 37


def test_oauth_state_distinguishes_missing_credentials_and_scope(tmp_cfg):
    tmp_cfg.gmail_api_credentials_path = tmp_cfg.data_root_path / "missing.json"
    assert get_oauth_state(tmp_cfg)["state"] == "CREDENTIALS_MISSING"
    tmp_cfg.gmail_api_credentials_path.write_text('{"installed": {}}', encoding="utf-8")
    tmp_cfg.gmail_api_scopes = ["https://mail.google.com/"]
    assert get_oauth_state(tmp_cfg)["state"] == "SCOPE_MISMATCH"
