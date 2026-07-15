"""核心链路完整性、重叠回看和自动收件恢复策略测试。"""

from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from unittest.mock import MagicMock

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    count_receive_retries,
    get_receive_retry,
    query_due_receive_retries,
    receive_retry_is_due,
    record_receive_failure,
)
from agent_mail_bridge.gmail_api_receive import receive_gmail_api_messages
from agent_mail_bridge.mail_receive import _receive_via_imap
from agent_mail_bridge.mcp_server import McpServer
from agent_mail_bridge.models import OperationStatus
from agent_mail_bridge.utils import sha256_of_file


class _FlushStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def _json_request(result: dict) -> MagicMock:
    request = MagicMock()
    request.execute.return_value = result
    return request


def _message(gmail_id: str, message_id: str) -> dict:
    body = base64.urlsafe_b64encode(f"正文 {gmail_id}".encode()).decode().rstrip("=")
    raw_message = EmailMessage()
    raw_message["From"] = "test@gmail.com"
    raw_message["To"] = "test@gmail.com"
    raw_message["Subject"] = f"Mail {gmail_id}"
    raw_message["Message-ID"] = message_id
    raw_message.set_content(f"Body {gmail_id}")
    raw = base64.urlsafe_b64encode(raw_message.as_bytes(policy=SMTP)).decode().rstrip("=")
    return {
        "id": gmail_id,
        "threadId": f"thread-{gmail_id}",
        "internalDate": str(int(datetime.now().timestamp() * 1000)),
        "raw": raw,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "test@gmail.com"},
                {"name": "To", "value": "test@gmail.com"},
                {"name": "Subject", "value": f"邮件 {gmail_id}"},
                {"name": "Message-ID", "value": message_id},
            ],
            "body": {"data": body},
        },
    }


def _gmail_service(
    list_responses: list[dict],
    messages: dict[str, dict],
    *,
    failing_ids: set[str] | None = None,
) -> tuple[MagicMock, list[dict]]:
    service = MagicMock()
    api = service.users.return_value.messages.return_value
    calls: list[dict] = []
    responses = iter(list_responses)

    def list_call(**kwargs):
        calls.append(kwargs)
        return _json_request(next(responses))

    def get_call(*, id: str, **_kwargs):
        if id in (failing_ids or set()):
            raise RuntimeError("可控坏邮件")
        return _json_request(messages[id])

    api.list.side_effect = list_call
    api.get.side_effect = get_call
    return service, calls


def test_mcp_unicode_staging_and_full_local_hash_chain(tmp_cfg, monkeypatch):
    source_dir = tmp_cfg.data_root_path / "中文 工作区"
    source_dir.mkdir(parents=True)
    source = source_dir / "核心链路 报告.md"
    source.write_text(("中文 English 特殊字符 !@#\n" * 200), encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, _message: None,
    )

    result = ApplicationService(tmp_cfg).submit_result(
        source,
        title="中英文混合 title",
        request_id="unicode-staging-001",
    )

    expected = sha256_of_file(source)
    assert result.status == OperationStatus.SUCCESS
    assert result.filename == source.name
    assert result.size_bytes == source.stat().st_size
    assert {
        result.source_sha256,
        result.staged_sha256,
        result.attachment_pre_smtp_sha256,
        result.sent_archive_sha256,
    } == {expected}
    call = ApplicationService(tmp_cfg).get_mcp_history().details["calls"][0]
    assert call["staging_status"] == "verified"
    assert call["source_size_bytes"] == call["staged_size_bytes"]
    assert call["source_sha256"] == call["staged_sha256"] == expected


def test_mcp_staging_detects_copy_mismatch(tmp_cfg, monkeypatch):
    source = tmp_cfg.data_root_path / "result.txt"
    source.write_text("source", encoding="utf-8")

    def corrupt_copy(_source: Path, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("corrupted", encoding="utf-8")
        return target

    monkeypatch.setattr(
        "agent_mail_bridge.application_service.atomic_copy_file", corrupt_copy
    )
    smtp = MagicMock()
    monkeypatch.setattr("agent_mail_bridge.mail_send._smtp_send_with_stage", smtp)

    result = ApplicationService(tmp_cfg).submit_result(
        source, request_id="staging-mismatch-001"
    )

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "staging_failed"
    smtp.assert_not_called()


def test_mcp_bom_malformed_method_flush_and_eof(tmp_cfg):
    messages = [
        "\ufeff" + json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }, ensure_ascii=False),
        "{malformed",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "unknown"}),
    ]
    output = _FlushStream()
    server = McpServer(
        ApplicationService(tmp_cfg),
        input_stream=io.StringIO("\n".join(messages) + "\n"),
        output_stream=output,
    )

    assert server.serve() == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[1]["error"]["code"] == -32700
    assert responses[2]["error"]["code"] == -32601
    assert output.flush_count == 3


def test_receive_retry_schedule_is_finite_and_persistent(tmp_cfg):
    started = datetime(2026, 7, 14, 10, 0, 0)
    expected_delays = (60, 300, 1800, 7200)
    for attempt, delay in enumerate(expected_delays, 1):
        row = record_receive_failure(
            tmp_cfg.db_path,
            backend="gmail_api",
            resource_id="bad-message",
            error=f"failure-{attempt}",
            now=started,
        )
        assert row["retry_count"] == attempt
        assert datetime.fromisoformat(row["next_retry_at"]) == started + timedelta(seconds=delay)
    terminal = record_receive_failure(
        tmp_cfg.db_path,
        backend="gmail_api",
        resource_id="bad-message",
        error="failure-5",
        now=started,
    )
    assert terminal["terminal_status"] == "needs_attention"
    assert terminal["next_retry_at"] is None
    assert not receive_retry_is_due(
        tmp_cfg.db_path, "gmail_api", "bad-message", now=started + timedelta(days=1)
    )
    assert count_receive_retries(tmp_cfg.db_path) == {
        "pending": 0,
        "needs_attention": 1,
    }


def test_due_retry_remains_discoverable_outside_overlap_window(tmp_cfg):
    failed_at = datetime.now() - timedelta(minutes=5)
    record_receive_failure(
        tmp_cfg.db_path,
        backend="gmail_api",
        resource_id="outside-window",
        error="temporary attachment failure",
        now=failed_at,
    )

    due = query_due_receive_retries(tmp_cfg.db_path, "gmail_api")

    assert [row["resource_id"] for row in due] == ["outside-window"]


def test_gmail_paginates_overlap_and_recovers_missed_page(tmp_cfg):
    items = {
        "m1": _message("m1", "<page-one@test>"),
        "m2": _message("m2", "<page-two@test>"),
    }
    service, calls = _gmail_service(
        [
            {"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "next"},
            {"messages": [{"id": "m2", "threadId": "t2"}]},
        ],
        items,
    )

    result = receive_gmail_api_messages(tmp_cfg, service=service)

    assert result["saved"] == 2
    assert result["fetched"] == 2
    assert "after:" in calls[0]["q"]
    assert calls[1]["pageToken"] == "next"


def test_gmail_processes_due_retry_not_returned_by_overlap_query(tmp_cfg):
    failed_at = datetime.now() - timedelta(minutes=5)
    record_receive_failure(
        tmp_cfg.db_path,
        backend="gmail_api",
        resource_id="outside-window",
        error="temporary failure",
        now=failed_at,
    )
    service, _calls = _gmail_service(
        [{"messages": []}],
        {"outside-window": _message("outside-window", "<outside@test>")},
    )

    result = receive_gmail_api_messages(tmp_cfg, service=service)

    assert result["fetched"] == 1
    assert result["saved"] == 1
    assert get_receive_retry(tmp_cfg.db_path, "gmail_api", "outside-window") is None


def test_imap_uses_uid_and_appends_due_retry(tmp_cfg, monkeypatch):
    failed_at = datetime.now() - timedelta(minutes=5)
    record_receive_failure(
        tmp_cfg.db_path,
        backend="imap",
        resource_id="77",
        error="temporary failure",
        now=failed_at,
    )

    class FakeImap:
        def __init__(self):
            self.commands = []

        def select(self, mailbox):
            self.commands.append(("select", mailbox))
            return "OK", [b""]

        def uid(self, command, *args):
            self.commands.append((command, *args))
            assert command == "search"
            return "OK", [b"101"]

        def logout(self):
            return "BYE", [b""]

    connection = FakeImap()
    processed = []
    monkeypatch.setattr("agent_mail_bridge.mail_receive._connect_imap", lambda _cfg: connection)
    monkeypatch.setattr(
        "agent_mail_bridge.mail_receive._process_one_unified",
        lambda _conn, uid, _cfg, _mark_seen, _result: processed.append(uid),
    )

    result = _receive_via_imap(tmp_cfg)

    assert connection.commands[1][0] == "search"
    assert processed == [b"101", b"77"]
    assert result["fetched"] == 2
    assert get_receive_retry(tmp_cfg.db_path, "imap", "77") is None


def test_poison_message_is_deferred_while_later_mail_continues(tmp_cfg):
    good = _message("good", "<good@test>")
    listing = {
        "messages": [
            {"id": "bad", "threadId": "tb"},
            {"id": "good", "threadId": "tg"},
        ]
    }
    service, _calls = _gmail_service(
        [listing], {"good": good}, failing_ids={"bad"}
    )

    first = receive_gmail_api_messages(tmp_cfg, service=service)

    assert first["saved"] == 1
    assert first["failed"] == 1
    assert get_receive_retry(tmp_cfg.db_path, "gmail_api", "bad")["retry_count"] == 1

    service, _calls = _gmail_service(
        [listing], {"good": good}, failing_ids={"bad"}
    )
    second = receive_gmail_api_messages(tmp_cfg, service=service)

    assert second["failed"] == 0
    assert second["retry_deferred"] == 1
    assert second["duplicates"] == 1


def test_item_failure_is_partial_not_global_backoff(tmp_cfg, monkeypatch):
    monkeypatch.setattr(
        "agent_mail_bridge.application_service.receive_mails",
        lambda *_args, **_kwargs: {
            "ok": True,
            "global_error": False,
            "fetched": 1,
            "saved": 0,
            "failed": 1,
            "errors": ["坏附件已隔离"],
        },
    )

    result = ApplicationService(tmp_cfg).receive()

    assert result.status == OperationStatus.PARTIAL
    assert result.ok


def test_auto_receive_state_persists_health_fields(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    updated = service.save_auto_receive_state(
        enabled=True,
        interval_seconds=60,
        last_check_at="2026-07-14 12:00:00",
        last_success_at="2026-07-14 12:00:00",
        last_result="暂无新邮件",
        consecutive_global_failures=0,
        next_check_at="2026-07-14 12:01:00",
        checkpoint="2026-07-14 12:00:00",
    ).details

    restored = ApplicationService(tmp_cfg).get_auto_receive_state().details
    assert updated["enabled"] is True
    assert restored["last_result"] == "暂无新邮件"
    assert restored["next_check_at"] == "2026-07-14 12:01:00"
    assert restored["checkpoint"] == "2026-07-14 12:00:00"
