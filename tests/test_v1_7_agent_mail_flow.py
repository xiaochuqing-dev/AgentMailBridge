from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from pathlib import Path

import pytest

from agent_mail_bridge.agent_integration import AgentAccessError
from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    get_connection,
    get_history_import_run,
    set_mailbox_sync_enabled,
    upsert_mailboxes,
)
from agent_mail_bridge.imap_sync import receive_imap_account
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_archive import _replace_atomically
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mail_resource_access import workspace_id_for_path
from agent_mail_bridge.mail_send import SmtpStageError
from agent_mail_bridge.mcp_server import McpServer, _query_summary
from agent_mail_bridge.models import OperationStatus, ReceiveResult
from agent_mail_bridge.outbound_mail import _append_provider_sent_copy
from agent_mail_bridge.provider_foundation import append_imap_message
from agent_mail_bridge.send_requests import get_send_request


def test_search_audit_summary_records_mailbox_scope_without_message_content():
    summary = _query_summary(
        "search_mails",
        {
            "time_scope": "all",
            "query": "AgentMailBridge",
            "direction": "inbound",
            "mailbox_id": "mailbox-sent-1",
        },
    )

    assert summary == (
        "time_scope=all; query=AgentMailBridge; direction=inbound; "
        "mailbox_id=mailbox-sent-1"
    )


def test_search_mails_direction_is_validated_and_forwarded(
    tmp_cfg, monkeypatch
):
    captured = {}

    def capture_query(_db_path, _query, **filters):
        captured.update(filters)
        return []

    monkeypatch.setattr(
        "agent_mail_bridge.application_service.query_mail_facts",
        capture_query,
    )
    tmp_cfg.mcp_mail_read_enabled = True
    service = ApplicationService(tmp_cfg)

    result = service.search_mails(direction="INBOUND")
    invalid = service.search_mails(direction="sideways")

    assert result.ok
    assert captured["mail_direction"] == "inbound"
    assert invalid.error_code == "invalid_range"


def test_atomic_replace_retries_transient_windows_access_denied(
    monkeypatch
):
    attempts = []

    def transient_then_success(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            error = PermissionError("transient Windows file lock")
            error.winerror = 5
            raise error

    monkeypatch.setattr(
        "agent_mail_bridge.mail_archive.os.replace",
        transient_then_success,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.mail_archive.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.mail_archive.sys.platform",
        "win32",
    )

    _replace_atomically(Path("source"), Path("target"))

    assert len(attempts) == 3


def _send_client(
    service: ApplicationService,
    tmp_cfg,
    *,
    send_mode: str,
):
    assert service.synchronize_mail_accounts().ok
    accounts = service.list_mail_accounts().details["accounts"]
    send_account = next(
        row for row in accounts if row["provider"] == "qq"
    )
    account_id = str(send_account["account_id"])
    mailbox = upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [
            {
                "external_ref": "INBOX",
                "display_name": "收件箱",
                "mailbox_role": "inbox",
                "role_source": "special_use",
            }
        ],
    )[0]
    workspace_id = workspace_id_for_path(tmp_cfg.data_root_path)
    created = service.create_agent_client(
        client_type="codex",
        display_name=f"v1.7 {send_mode}",
        capabilities=[
            "mail.accounts.list",
            "mailboxes.list",
            "mail.send",
            "send.status",
        ],
        account_ids=[account_id],
        mailbox_ids=[str(mailbox["mailbox_id"])],
        send_account_ids=[account_id],
        attachment_workspace_ids=[workspace_id],
        send_mode=send_mode,
    )
    assert created.ok, created.message
    client_id = str(created.details["client"]["client_id"])
    token = str(created.details["scoped_token"])
    assert service.set_agent_client_state(
        client_id, "active", enabled=True
    ).ok
    identity = service.resolve_agent_identity(client_id, token)
    return identity, account_id


def test_confirm_mode_is_durable_idempotent_and_never_touches_smtp(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda *args, **kwargs: smtp_calls.append((args, kwargs)),
    )

    first = service.send_agent_mail(
        identity,
        request_id="confirm-stable-1",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        subject="确认发送",
        body_text="确认前不得发送",
    )
    second = service.send_agent_mail(
        identity,
        request_id="confirm-stable-1",
        operation="new",
        sender_account_id=account_id,
        to=["different@example.com"],
        subject="不同内容也必须返回原请求",
        body_text="不会创建第二个请求",
    )

    first_request = first.details["send_request"]
    second_request = second.details["send_request"]
    assert first_request["status"] == "pending_confirmation"
    assert first_request["smtp_attempt_count"] == 0
    assert second.status.value == "duplicate"
    assert (
        second_request["send_request_id"]
        == first_request["send_request_id"]
    )
    assert smtp_calls == []

    cancelled = service.cancel_agent_send_request(
        str(first_request["send_request_id"])
    )
    assert cancelled.ok
    assert cancelled.details["send_status"] == "cancelled"
    assert smtp_calls == []

    confirmable = service.send_agent_mail(
        identity,
        request_id="confirm-double-click-1",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        subject="double confirm",
        body_text="send once",
    ).details["send_request"]
    confirmed = service.confirm_agent_send_request(
        str(confirmable["send_request_id"])
    )
    confirmed_again = service.confirm_agent_send_request(
        str(confirmable["send_request_id"])
    )
    assert confirmed.details["send_status"] == "sent"
    assert confirmed_again.details["send_status"] == "sent"
    assert len(smtp_calls) == 1
    assert (
        confirmed_again.details["send_request"]["smtp_attempt_count"]
        == 1
    )


def test_autonomous_send_uses_exact_mime_without_public_bcc(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="autonomous"
    )
    attachment = tmp_cfg.data_root_path / "Agent测试附件.txt"
    attachment.write_text("exact attachment bytes", encoding="utf-8")
    smtp_calls: list[dict] = []

    def capture_smtp(_cfg, raw_bytes, *, from_addr, to_addrs):
        smtp_calls.append(
            {
                "raw": raw_bytes,
                "from": from_addr,
                "to": list(to_addrs),
            }
        )

    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        capture_smtp,
    )
    result = service.send_agent_mail(
        identity,
        request_id="autonomous-stable-1",
        operation="new",
        sender_account_id=account_id,
        to=["to@example.com"],
        cc=["cc@example.com"],
        bcc=["hidden@example.com"],
        subject="Unicode 发送测试",
        body_text="文本正文",
        body_html="<p>HTML 正文</p>",
        attachments=[{"path": str(attachment)}],
    )

    request = result.details["send_request"]
    assert request["status"] == "sent"
    assert request["smtp_attempt_count"] == 1
    assert len(smtp_calls) == 1
    raw = smtp_calls[0]["raw"]
    headers = raw.split(b"\r\n\r\n", 1)[0].lower()
    assert b"\nbcc:" not in headers
    assert "hidden@example.com" in smtp_calls[0]["to"]
    assert hashlib.sha256(raw).hexdigest() == request["raw_eml_sha256"]

    package = service.get_mail_message(str(request["package_id"])).details[
        "message"
    ]
    raw_path = Path(package["package_root"]) / str(
        package["raw_eml"]["path"]
    )
    assert raw_path.read_bytes() == raw
    assert package["direction"] == "outbound"

    runtime = service._account_router.context(
        account_id, capability="send"
    ).config
    sent_copy = normalized_mail_from_raw(
        raw,
        backend="imap",
        backend_message_id="sent-server-1",
        thread_id="",
        uid="42",
        uidvalidity=9001,
        received_at=str(request["completed_at"]),
        saved_date=str(request["completed_at"])[:10],
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="Sent",
        direction="outbound",
    )
    mapped = process_normalized_mail(
        runtime, sent_copy, apply_receive_rule=False
    )
    assert mapped["status"] == "duplicate"
    assert mapped["package_id"] == request["package_id"]

    retry = service.send_agent_mail(
        identity,
        request_id="autonomous-stable-1",
        operation="new",
        sender_account_id=account_id,
        to=["another@example.com"],
        subject="不得再次发送",
        body_text="幂等重试",
    )
    assert retry.details["send_request"]["status"] == "sent"
    assert len(smtp_calls) == 1


def test_qq_sent_copy_uses_exact_mime_and_discovered_mailbox(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    assert service.synchronize_mail_accounts().ok
    qq = next(
        row
        for row in service.list_mail_accounts().details["accounts"]
        if row["provider"] == "qq"
    )
    account_id = str(qq["account_id"])
    upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [
            {
                "external_ref": "Sent Messages",
                "display_name": "Sent Messages",
                "mailbox_role": "sent",
                "role_source": "special_use",
            }
        ],
    )
    runtime = service._account_router.context(
        account_id, capability="send"
    ).config
    captured = {}

    def fake_append(**kwargs):
        captured.update(kwargs)
        return {"status": "appended", "size_bytes": len(kwargs["raw_bytes"])}

    monkeypatch.delenv(
        "AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE", raising=False
    )
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.append_imap_message", fake_append
    )
    raw = b"From: sender@example.com\r\n\r\nexact bytes"
    result = _append_provider_sent_copy(
        runtime,
        account_id=account_id,
        raw_bytes=raw,
        sent_at=datetime.now(),
    )

    assert result["status"] == "appended"
    assert captured["folder"] == "Sent Messages"
    assert captured["raw_bytes"] == raw
    assert captured["username"] == tmp_cfg.qq_email


def test_imapclient_append_adapter_preserves_exact_bytes():
    calls = {}

    class FakeClient:
        def __init__(self, host, **kwargs):
            calls["connect"] = (host, kwargs)

        def login(self, username, secret):
            calls["login"] = (username, secret)

        def append(self, folder, raw, *, flags, msg_time):
            calls["append"] = (folder, raw, flags, msg_time)

        def logout(self):
            calls["logout"] = True

    raw = b"Message-ID: <exact@test>\r\n\r\nbody"
    result = append_imap_message(
        settings={
            "profile_id": "qq",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        username="sender@example.test",
        secret="not-a-real-secret",
        folder="Sent Messages",
        raw_bytes=raw,
        client_factory=FakeClient,
    )

    assert result == {
        "status": "appended",
        "size_bytes": len(raw),
        "mailbox": "Sent Messages",
    }
    assert calls["append"][0] == "Sent Messages"
    assert calls["append"][1] == raw
    assert calls["append"][2] == ("\\Seen",)
    assert calls["logout"] is True


def _archive_reply_source(service, tmp_cfg, account_id):
    message = EmailMessage(policy=SMTP)
    message["From"] = "Origin <origin@example.com>"
    message["Reply-To"] = "Reply Desk <reply@example.com>"
    message["To"] = (
        f"{tmp_cfg.qq_email}, Teammate <team@example.com>"
    )
    message["Cc"] = (
        "Reply Desk <reply@example.com>, Other <other@example.com>"
    )
    message["Subject"] = "Thread subject"
    message["Message-ID"] = "<source-v17@example.com>"
    message["References"] = "<root-v17@example.com>"
    message.set_content("source body")
    message.add_attachment(
        b"first attachment",
        maintype="text",
        subtype="plain",
        filename="原附件一.txt",
    )
    message.add_attachment(
        b"second attachment",
        maintype="application",
        subtype="octet-stream",
        filename="original-two.bin",
    )
    normalized = normalized_mail_from_raw(
        message.as_bytes(policy=SMTP),
        backend="imap",
        backend_message_id="source-v17",
        thread_id="",
        uid="77",
        uidvalidity=700,
        received_at="2026-07-29 10:00:00",
        saved_date="2026-07-29",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="INBOX",
        direction="inbound",
    )
    runtime = service._account_router.context(
        account_id, capability="receive"
    ).config
    result = process_normalized_mail(
        runtime, normalized, apply_receive_rule=False
    )
    return str(result["package_id"])


def test_reply_all_defaults_and_explicit_recipient_override(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    package_id = _archive_reply_source(
        service, tmp_cfg, account_id
    )
    automatic = service.send_agent_mail(
        identity,
        request_id="reply-all-defaults",
        operation="reply_all",
        sender_account_id=account_id,
        source_package_id=package_id,
        body_text="reply all body",
    )
    request = automatic.details["send_request"]
    assert request["recipients"] == {
        "to": ["reply@example.com", "team@example.com"],
        "cc": ["other@example.com"],
        "bcc": [],
    }
    assert request["subject"] == "Re: Thread subject"
    assert request["in_reply_to_raw"] == "<source-v17@example.com>"
    assert request["references_raw"] == "<root-v17@example.com>"
    assert request["reply_to_package_id"] == package_id

    overridden = service.send_agent_mail(
        identity,
        request_id="reply-all-override",
        operation="reply_all",
        sender_account_id=account_id,
        source_package_id=package_id,
        to=[{"display_name": "新收件人", "address": "new@example.com"}],
        body_text="override recipients",
    ).details["send_request"]
    assert overridden["recipients"] == {
        "to": ["new@example.com"],
        "cc": [],
        "bcc": [],
    }


def test_forward_selected_original_attachment_records_relationship(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="autonomous"
    )
    source_package_id = _archive_reply_source(
        service, tmp_cfg, account_id
    )
    source = service.get_mail_message(
        source_package_id
    ).details["message"]
    attachments = [
        row
        for row in source["resources"]
        if row["internal_type"] == "attachment"
    ]
    assert len(attachments) == 2
    sent_raw = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda _cfg, raw, **_kwargs: sent_raw.append(raw),
    )
    forwarded = service.send_agent_mail(
        identity,
        request_id="forward-selected-attachment-v17",
        operation="forward",
        sender_account_id=account_id,
        source_package_id=source_package_id,
        to=["target@example.com"],
        body_text="forward one attachment",
        attachments=[
            {
                "source_package_id": source_package_id,
                "resource_id": attachments[0]["resource_id"],
            }
        ],
    )
    assert forwarded.details["send_status"] == "sent"
    request = forwarded.details["send_request"]
    assert request["forward_from_package_id"] == source_package_id
    assert len(request["attachments"]) == 1
    from email import message_from_bytes

    parsed = message_from_bytes(sent_raw[0], policy=SMTP)
    assert [
        item.get_filename() for item in parsed.iter_attachments()
    ] == [attachments[0]["display_name"]]
    outbound = service.get_mail_message(
        str(request["package_id"])
    ).details["message"]
    assert outbound["forward_from_package_id"] == source_package_id
    relation = get_connection(tmp_cfg.db_path).execute(
        "SELECT relation_type, related_package_id "
        "FROM mail_thread_relations WHERE package_id=?",
        (request["package_id"],),
    ).fetchall()
    assert ("forward", source_package_id) in {
        (row["relation_type"], row["related_package_id"])
        for row in relation
    }


def test_zero_byte_unicode_attachment_and_delivery_unknown_are_terminal(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="autonomous"
    )
    zero = tmp_cfg.data_root_path / "零字节 Unicode 附件.txt"
    zero.write_bytes(b"")
    sent_raw = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda _cfg, raw, **_kwargs: sent_raw.append(raw),
    )
    sent = service.send_agent_mail(
        identity,
        request_id="zero-byte-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="zero attachment",
        attachments=[{"path": str(zero)}],
    )
    assert sent.details["send_status"] == "sent"
    from email import message_from_bytes

    parsed = message_from_bytes(sent_raw[0], policy=SMTP)
    attachment = list(parsed.iter_attachments())[0]
    assert attachment.get_filename() == zero.name
    assert attachment.get_payload(decode=True) == b""

    calls = []

    def uncertain(*_args, **_kwargs):
        calls.append(True)
        raise SmtpStageError(
            "send",
            "server disconnected after DATA",
            delivery_unknown=True,
        )

    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        uncertain,
    )
    unknown = service.send_agent_mail(
        identity,
        request_id="delivery-unknown-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="uncertain",
    )
    assert unknown.status == OperationStatus.PARTIAL
    assert unknown.details["send_status"] == "delivery_unknown"
    retried = service.send_agent_mail(
        identity,
        request_id="delivery-unknown-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="must not retry",
    )
    assert retried.details["send_status"] == "delivery_unknown"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", "safe\r\nBcc: injected@example.com"),
        ("to", ["safe@example.com\nBcc: injected@example.com"]),
    ],
)
def test_agent_send_rejects_header_injection(
    tmp_cfg, field, value
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    arguments = {
        "request_id": f"header-injection-{field}",
        "operation": "new",
        "sender_account_id": account_id,
        "to": ["receiver@example.com"],
        "subject": "safe",
        "body_text": "body",
    }
    arguments[field] = value
    result = service.send_agent_mail(identity, **arguments)
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "header_injection"


def test_agent_send_normalizes_links_into_the_exact_message_body(
    tmp_cfg
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    result = service.send_agent_mail(
        identity,
        request_id="links-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        links=[
            {
                "url": "HTTPS://Example.COM/report#private-fragment",
                "display_text": "验收报告",
            },
            "https://example.com/report",
        ],
    )
    request = result.details["send_request"]
    assert request["status"] == "pending_confirmation"
    assert "验收报告：https://example.com/report" in request["body_text"]
    assert "#private-fragment" not in request["body_text"]

    rejected = service.send_agent_mail(
        identity,
        request_id="invalid-link-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        links=["file:///C:/private.txt"],
    )
    assert rejected.status == OperationStatus.FAILED
    assert rejected.error_code == "invalid_link"


def test_pending_list_and_status_persist_expiry_before_display(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    pending = service.send_agent_mail(
        identity,
        request_id="expire-before-display-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="expires before display",
    ).details["send_request"]
    send_request_id = str(pending["send_request_id"])
    get_connection(tmp_cfg.db_path).execute(
        "UPDATE send_requests SET expires_at='2000-01-01 00:00:00' "
        "WHERE send_request_id=?",
        (send_request_id,),
    )
    get_connection(tmp_cfg.db_path).commit()

    listed = service.list_pending_send_requests()
    assert listed.ok
    assert listed.details["requests"] == []

    status = service.get_agent_send_request_status(
        identity, send_request_id
    )
    assert status.ok
    assert status.details["send_status"] == "expired"
    expired = get_send_request(tmp_cfg.db_path, send_request_id)
    assert expired is not None
    assert expired["status"] == "expired"
    assert expired["error_code"] == "request_expired"
    assert int(expired["smtp_attempt_count"] or 0) == 0

    cancelled = service.cancel_agent_send_request(send_request_id)
    assert not cancelled.ok
    assert cancelled.error_code == "invalid_send_state"


def test_confirm_expiry_hash_recheck_and_revocation_block_smtp(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda *_args, **_kwargs: smtp_calls.append(True),
    )
    expiring = service.send_agent_mail(
        identity,
        request_id="expiring-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="expires",
    ).details["send_request"]
    get_connection(tmp_cfg.db_path).execute(
        "UPDATE send_requests SET expires_at='2000-01-01 00:00:00' "
        "WHERE send_request_id=?",
        (expiring["send_request_id"],),
    )
    get_connection(tmp_cfg.db_path).commit()
    expired = service.confirm_agent_send_request(
        str(expiring["send_request_id"])
    )
    assert expired.details["send_status"] == "expired"
    assert smtp_calls == []

    attachment = tmp_cfg.data_root_path / "hash-recheck.txt"
    attachment.write_text("original", encoding="utf-8")
    pending = service.send_agent_mail(
        identity,
        request_id="hash-recheck-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="hash",
        attachments=[{"path": str(attachment)}],
    ).details["send_request"]
    internal_pending = get_send_request(
        tmp_cfg.db_path, str(pending["send_request_id"])
    )
    assert internal_pending is not None
    Path(
        internal_pending["attachments"][0]["snapshot_path"]
    ).write_text(
        "changed", encoding="utf-8"
    )
    mismatch = service.confirm_agent_send_request(
        str(pending["send_request_id"])
    )
    assert mismatch.details["send_status"] == "failed"
    assert mismatch.error_code == "attachment_hash_mismatch"
    assert smtp_calls == []

    revoked = service.send_agent_mail(
        identity,
        request_id="revoked-before-confirm-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="revoke",
    ).details["send_request"]
    assert service.set_agent_client_state(
        identity.client_id, "revoked", enabled=False
    ).ok
    denied = service.confirm_agent_send_request(
        str(revoked["send_request_id"])
    )
    assert denied.status == OperationStatus.FAILED
    assert smtp_calls == []


def test_send_account_all_is_dynamic_selected_is_fixed_and_secrets_stay_hidden(
    tmp_cfg
):
    service = ApplicationService(tmp_cfg)
    assert service.synchronize_mail_accounts().ok
    qq_id = str(
        next(
            row["account_id"]
            for row in service.list_mail_accounts().details["accounts"]
            if row["provider"] == "qq"
        )
    )
    selected = service.create_agent_client(
        client_type="custom",
        display_name="selected send account",
        capabilities=["mail.accounts.list", "mail.send"],
        account_ids=[qq_id],
        send_account_ids=[qq_id],
        send_account_scope_mode="selected",
        send_mode="confirm",
    )
    dynamic = service.create_agent_client(
        client_type="custom",
        display_name="all send accounts",
        capabilities=["mail.accounts.list", "mail.send"],
        account_ids=[qq_id],
        send_account_scope_mode="all",
        send_mode="confirm",
    )
    for created in (selected, dynamic):
        assert service.set_agent_client_state(
            str(created.details["client"]["client_id"]),
            "active",
            enabled=True,
        ).ok

    canary = "AMB_SECRET_CANARY_163_NEVER_RETURN"
    future = service.create_mail_account(
        provider="163",
        email_address="future-send@163.com",
        secret=canary,
    )
    assert future.ok
    future_id = str(future.details["account"]["account_id"])
    selected_identity = service.resolve_agent_identity(
        str(selected.details["client"]["client_id"]),
        str(selected.details["scoped_token"]),
    )
    dynamic_identity = service.resolve_agent_identity(
        str(dynamic.details["client"]["client_id"]),
        str(dynamic.details["scoped_token"]),
    )
    assert future_id not in selected_identity.send_account_ids
    assert future_id in dynamic_identity.send_account_ids
    with pytest.raises(AgentAccessError) as denied:
        service.require_agent_send_account(
            selected_identity, future_id
        )
    assert denied.value.code == "send_account_denied"

    public = service.list_authorized_mail_accounts(
        dynamic_identity
    ).details
    assert canary not in json.dumps(public, ensure_ascii=False)
    assert "secret" not in json.dumps(public, ensure_ascii=False).casefold()
    assert service.update_mail_account(
        future_id, enabled=False
    ).ok
    refreshed = service.resolve_agent_identity(
        str(dynamic.details["client"]["client_id"]),
        str(dynamic.details["scoped_token"]),
    )
    assert future_id not in refreshed.send_account_ids
    with pytest.raises(AgentAccessError):
        service.require_agent_send_account(refreshed, future_id)


def test_unapproved_local_attachment_path_is_rejected(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    outside = tmp_cfg.data_root_path.parent / "outside-scope.txt"
    outside.write_text("outside", encoding="utf-8")
    result = service.send_agent_mail(
        identity,
        request_id="outside-attachment-v17",
        operation="new",
        sender_account_id=account_id,
        to=["receiver@example.com"],
        body_text="outside attachment",
        attachments=[{"path": str(outside)}],
    )
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "attachment_scope_denied"


def test_legacy_client_has_no_general_send_permission(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.synchronize_mail_accounts().ok
    account_id = str(
        service.list_mail_accounts().details["accounts"][0]["account_id"]
    )
    created = service.create_agent_client(
        client_type="custom",
        display_name="legacy read only",
        capabilities=["mail.search"],
        account_ids=[account_id],
    )
    assert created.ok
    client_id = str(created.details["client"]["client_id"])
    assert service.set_agent_client_state(
        client_id, "active", enabled=True
    ).ok
    identity = service.resolve_agent_identity(
        client_id, str(created.details["scoped_token"])
    )
    assert "mail.send" not in identity.capabilities
    assert not identity.send_account_ids


def test_mcp_send_creates_pending_and_exposes_no_confirm_tool(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    identity, account_id = _send_client(
        service, tmp_cfg, send_mode="confirm"
    )
    token = service._agent_integration.get_scoped_token(identity.client_id)
    server = McpServer(
        service, client_id=identity.client_id, client_token=token
    )
    server.initialized = True
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.outbound_mail.smtp_send_bytes_with_stage",
        lambda *args, **kwargs: smtp_calls.append((args, kwargs)),
    )

    listed = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
    )
    names = {
        item["name"] for item in listed["result"]["tools"]
    }
    assert "send_mail" in names
    assert "get_send_request_status" in names
    assert not any("confirm" in name for name in names)

    sent = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "send_mail",
                "arguments": {
                    "request_id": "mcp-confirm-1",
                    "operation": "new",
                    "sender_account_id": account_id,
                    "to": ["receiver@example.com"],
                    "subject": "MCP 待确认",
                    "body_text": "MCP 不能自行确认",
                },
            },
        }
    )["result"]["structuredContent"]
    assert sent["ok"] is True
    assert sent["send_status"] == "pending_confirmation"
    request_id = sent["send_request_id"]
    assert smtp_calls == []

    status = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_send_request_status",
                "arguments": {"send_request_id": request_id},
            },
        }
    )["result"]["structuredContent"]
    assert status["send_status"] == "pending_confirmation"
    assert status["send_request"]["smtp_attempt_count"] == 0
    assert smtp_calls == []


def test_mcp_complete_prepare_uses_the_single_visible_authorized_workspace(
    tmp_cfg, tmp_path
):
    tmp_cfg.mcp_mail_read_enabled = True
    workspace = tmp_path / "codex-project"
    workspace.mkdir()
    tmp_cfg.allowed_send_roots = [workspace]
    service = ApplicationService(tmp_cfg)
    assert service.synchronize_mail_accounts().ok
    account_id = str(
        next(
            row["account_id"]
            for row in service.list_mail_accounts().details["accounts"]
            if row["provider"] == "qq"
        )
    )
    package_id = _archive_reply_source(service, tmp_cfg, account_id)
    created = service.create_agent_client(
        client_type="codex",
        display_name="Codex complete package regression",
        capabilities=["resource.prepare", "workspace.list"],
        account_ids=[account_id],
        mailbox_scope_mode="all",
        workspace_scope_mode="all",
    )
    assert created.ok, created.message
    client_id = str(created.details["client"]["client_id"])
    token = str(created.details["scoped_token"])
    assert service.set_agent_client_state(
        client_id, "active", enabled=True
    ).ok
    identity = service.resolve_agent_identity(client_id, token)
    assert len(identity.workspace_ids) == 2
    server = McpServer(
        service, client_id=client_id, client_token=token
    )
    server.initialized = True

    listed = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_agent_workspaces", "arguments": {}},
        }
    )["result"]["structuredContent"]
    assert listed["ok"] is True
    assert len(listed["workspaces"]) == 1
    visible = listed["workspaces"][0]
    assert visible["default"] is True

    explicit = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "prepare_mail_resources",
                "arguments": {
                    "package_id": package_id,
                    "workspace_id": visible["workspace_id"],
                    "mode": "complete",
                },
            },
        }
    )["result"]["structuredContent"]
    assert explicit["ok"] is True
    assert explicit["workspace_id"] == visible["workspace_id"]

    defaulted = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "prepare_mail_resources",
                "arguments": {
                    "package_id": package_id,
                    "mode": "complete",
                },
            },
        }
    )["result"]["structuredContent"]
    assert defaulted["ok"] is True
    assert defaulted["workspace_id"] == visible["workspace_id"]
    assert defaulted["reused"] is True


def _raw_mail(message_id: str, subject: str) -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = "sender@example.com"
    message["To"] = "receiver@example.com"
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    message.set_content(subject)
    return message.as_bytes(policy=SMTP)


class _MultiMailboxImap:
    def __init__(self):
        self.current = ""
        self.selected = []
        self.messages = {
            "INBOX": {1: _raw_mail("<inbox-v17@test>", "Inbox v1.7")},
            "Sent": {8: _raw_mail("<sent-v17@test>", "Sent v1.7")},
            "项目资料": {3: _raw_mail("<custom-v17@test>", "Custom v1.7")},
        }
        self.uidvalidity = {"INBOX": 101, "Sent": 202, "项目资料": 303}

    def login(self, username, secret):
        return None

    def list_folders(self):
        return [
            ((b"\\Inbox",), "/", "INBOX"),
            ((b"\\Sent",), "/", "Sent"),
            ((), "/", "项目资料"),
        ]

    def select_folder(self, mailbox, readonly=True):
        self.current = mailbox
        self.selected.append((mailbox, readonly))
        values = self.messages[mailbox]
        return {
            b"UIDVALIDITY": self.uidvalidity[mailbox],
            b"UIDNEXT": max(values, default=0) + 1,
            b"HIGHESTMODSEQ": 0,
        }

    def search(self, criteria):
        return sorted(self.messages[self.current])

    def fetch(self, uids, _parts):
        values = self.messages[self.current]
        return {
            uid: {b"BODY[]": values[uid]}
            for uid in uids
            if uid in values
        }

    def logout(self):
        return None


def test_multimailbox_sync_and_dynamic_permission_modes(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    created = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address="folders@example.net",
        provider_settings={
            "imap_host": "imap.example.net",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        secret="fixture-secret",
    )
    account_id = str(created.details["account"]["account_id"])
    runtime = service._account_router.context(
        account_id, capability="receive"
    ).config
    fake = _MultiMailboxImap()
    result = receive_imap_account(
        runtime,
        client_factory=lambda *_args, **_kwargs: fake,
    )
    assert result["saved"] == 2
    assert fake.selected == [("INBOX", True), ("Sent", True)]
    by_role = {
        row["mailbox_role"]: row
        for row in service.list_mail_accounts().details["mailboxes"]
        if row["account_id"] == account_id
    }
    assert by_role["inbox"]["uidvalidity"] == 101
    assert by_role["sent"]["uidvalidity"] == 202
    states = get_connection(tmp_cfg.db_path).execute(
        "SELECT mailbox_id, uidvalidity, last_uid "
        "FROM mailbox_sync_states WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    assert {(row["uidvalidity"], row["last_uid"]) for row in states} == {
        (101, 1),
        (202, 8),
    }
    directions = {
        row["subject"]: row["direction"]
        for row in service.list_mail_messages(account_id=account_id).details[
            "messages"
        ]
    }
    assert directions == {
        "Inbox v1.7": "inbound",
        "Sent v1.7": "outbound",
    }

    inbox_id = str(by_role["inbox"]["mailbox_id"])
    selected_client = service.create_agent_client(
        client_type="custom",
        display_name="selected folders",
        capabilities=["mail.search"],
        account_ids=[account_id],
        mailbox_ids=[inbox_id],
        mailbox_scope_mode="selected",
    )
    all_client = service.create_agent_client(
        client_type="custom",
        display_name="all folders",
        capabilities=["mail.search"],
        account_ids=[account_id],
        mailbox_scope_mode="all",
    )
    for profile in (selected_client, all_client):
        client_id = str(profile.details["client"]["client_id"])
        assert service.set_agent_client_state(
            client_id, "active", enabled=True
        ).ok
    selected_identity = service.resolve_agent_identity(
        str(selected_client.details["client"]["client_id"]),
        str(selected_client.details["scoped_token"]),
    )
    all_identity = service.resolve_agent_identity(
        str(all_client.details["client"]["client_id"]),
        str(all_client.details["scoped_token"]),
    )
    custom_id = str(by_role["other"]["mailbox_id"])
    assert custom_id not in selected_identity.mailbox_ids
    assert custom_id in all_identity.mailbox_ids

    set_mailbox_sync_enabled(tmp_cfg.db_path, custom_id, True)
    custom_sync = receive_imap_account(
        runtime,
        client_factory=lambda *_args, **_kwargs: fake,
    )
    assert custom_sync["saved"] == 1
    custom_package_id = str(
        next(
            row["package_id"]
            for row in service.list_mail_messages(
                account_id=account_id
            ).details["messages"]
            if row["subject"] == "Custom v1.7"
        )
    )
    assert service.require_agent_mail_access(
        all_identity, custom_package_id
    )["package_id"] == custom_package_id
    upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [
            {
                "external_ref": "INBOX",
                "mailbox_role": "inbox",
                "sync_enabled": True,
            },
            {
                "external_ref": "Sent",
                "mailbox_role": "sent",
                "sync_enabled": True,
            },
        ],
        replace_discovery=True,
    )
    refreshed_all = service.resolve_agent_identity(
        str(all_client.details["client"]["client_id"]),
        str(all_client.details["scoped_token"]),
    )
    assert custom_id not in refreshed_all.mailbox_ids
    with pytest.raises(AgentAccessError) as denied:
        service.require_agent_mail_access(
            refreshed_all, custom_package_id
        )
    assert denied.value.code == "mailbox_denied"


def test_history_import_persists_selected_mailboxes_across_resume(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    created = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address="history-folders@example.net",
        provider_settings={
            "imap_host": "imap.example.net",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        secret="fixture-secret",
    )
    account_id = str(created.details["account"]["account_id"])
    mailboxes = upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [
            {
                "external_ref": "INBOX",
                "mailbox_role": "inbox",
                "sync_enabled": True,
            },
            {
                "external_ref": "Sent",
                "mailbox_role": "sent",
                "sync_enabled": True,
            },
        ],
    )
    sent_id = str(
        next(
            row["mailbox_id"]
            for row in mailboxes
            if row["mailbox_role"] == "sent"
        )
    )
    cancel_event = threading.Event()
    cancel_event.set()
    cancelled = service.import_historical_mails(
        account_id=account_id,
        mailbox_ids=[sent_id],
        preset="custom",
        date_from="2025-01-01",
        date_to="2025-01-02",
        cancel_event=cancel_event,
    )
    assert cancelled.status == OperationStatus.CANCELLED
    persisted = get_history_import_run(tmp_cfg.db_path, cancelled.scan_id)
    assert persisted and persisted["mailbox_ids"] == [sent_id]

    calls = []
    monkeypatch.setattr(
        service,
        "historical_rescan",
        lambda **kwargs: (
            calls.append(kwargs)
            or ReceiveResult(
                OperationStatus.NO_CHANGES,
                backend="imap",
                message="no changes",
            )
        ),
    )
    cancel_event.clear()
    resumed = service.resume_history_import(
        cancelled.scan_id,
        cancel_event=cancel_event,
    )
    assert resumed.status == OperationStatus.NO_CHANGES
    assert calls and calls[0]["mailbox_ids"] == [sent_id]
