"""v1.3.0 收件默认语义、历史补扫和规则重评专项回归。"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_mail_bridge.config import AppConfig, ConfigError, load_config
from agent_mail_bridge.database import (
    get_connection,
    get_receive_retry,
    query_receive_rule_evaluations,
)
from agent_mail_bridge.gmail_api_receive import (
    _historical_query,
    _query_with_lookback,
    rescan_gmail_api_messages,
)
from agent_mail_bridge.mail_receive import _rescan_via_imap
from agent_mail_bridge.receive_rules import ALL_SCANNED, CUSTOM, SELF_ONLY
from agent_mail_bridge.ui.settings_store import persist_receive_rule_migration


def _raw_mail(
    *,
    message_id: str = "<history@example.com>",
    sender: str = "outside@example.com",
    subject: str = "历史补扫样本",
    when: datetime | None = None,
) -> bytes:
    moment = (when or datetime(2026, 7, 20, 12, 0, 0)).astimezone()
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "test@gmail.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(moment)
    message.set_content("历史邮件正文")
    return message.as_bytes()


def _gmail_service(raw_by_id: dict[str, bytes], pages: list[dict] | None = None):
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    responses = pages or [{"messages": [{"id": key, "threadId": "thread-1"} for key in raw_by_id]}]
    page_index = {None: 0}

    def list_messages(**kwargs):
        token = kwargs.get("pageToken")
        index = page_index.get(token, 0)
        response = dict(responses[index])
        result = MagicMock()
        result.execute.return_value = response
        return result

    def get_message(*, id, **_kwargs):
        result = MagicMock()
        result.execute.return_value = {
            "id": id,
            "threadId": "thread-1",
            "raw": base64.urlsafe_b64encode(raw_by_id[id]).decode("ascii").rstrip("="),
        }
        return result

    messages.list.side_effect = list_messages
    messages.get.side_effect = get_message
    return service


def test_fresh_configuration_defaults_to_all_scanned(monkeypatch, tmp_path):
    monkeypatch.delenv("RECEIVE_RULE_MODE", raising=False)
    monkeypatch.delenv("RECEIVE_RULE_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("AUTO_RECEIVE_ONLY_SELF_MAIL", "true")
    cfg = load_config(tmp_path / "missing.env")
    assert cfg.receive_rule_mode == ALL_SCANNED
    assert cfg.auto_receive_only_self_mail is False
    assert cfg.receive_rule_mode_source == "migrated_implicit_default"


def test_explicit_legacy_self_only_and_custom_are_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("RECEIVE_RULE_MODE", SELF_ONLY)
    explicit = load_config(tmp_path / "self.env")
    assert explicit.receive_rule_mode == SELF_ONLY
    assert explicit.receive_rule_migration_needed is True

    monkeypatch.setenv("RECEIVE_RULE_MODE", CUSTOM)
    monkeypatch.setenv("RECEIVE_RULE_SENDERS", "outside@example.com")
    custom = load_config(tmp_path / "custom.env")
    assert custom.receive_rule_mode == CUSTOM
    assert custom.receive_rule_senders == ("outside@example.com",)


def test_receive_rule_migration_is_atomic_and_idempotent(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("AUTO_RECEIVE_ONLY_SELF_MAIL=true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MAIL_BRIDGE_DISABLE_DOTENV", "0")
    cfg = AppConfig(
        data_root=tmp_path / "data",
        loaded_env_path=env_path,
        receive_rule_mode=ALL_SCANNED,
        receive_rule_config_version=2,
        receive_rule_mode_source="migrated_implicit_default",
        receive_rule_migration_needed=True,
    )
    assert persist_receive_rule_migration(cfg) is True
    first = env_path.read_text(encoding="utf-8")
    assert "RECEIVE_RULE_CONFIG_VERSION=\"2\"" in first
    assert "RECEIVE_RULE_MODE=\"all_scanned\"" in first
    assert persist_receive_rule_migration(cfg) is False
    assert env_path.read_text(encoding="utf-8") == first


@pytest.mark.parametrize(
    ("name", "value"),
    (("RECEIVE_RULE_CONFIG_VERSION", "broken"), ("RECEIVE_RULE_MODE", "unknown")),
)
def test_corrupt_receive_rule_configuration_fails_safely(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "broken.env")


@pytest.mark.parametrize("days", [1, 7, 30])
def test_historical_query_uses_explicit_range_not_incremental_lookback(tmp_cfg, days):
    end = datetime(2026, 7, 21, 12, 0, 0)
    start = end - timedelta(days=days)
    tmp_cfg.receive_lookback_minutes = 1
    historical = _historical_query(tmp_cfg, start, end)
    incremental = _query_with_lookback(tmp_cfg)
    assert f"after:{int(start.timestamp()) - 1}" in historical
    assert f"before:{int(end.timestamp()) + 1}" in historical
    assert historical != incremental


def test_rule_skipped_mail_can_be_re_evaluated_saved_then_deduplicated(tmp_cfg):
    raw = _raw_mail()
    service = _gmail_service({"old-1": raw})
    tmp_cfg.receive_rule_mode = CUSTOM
    tmp_cfg.receive_rule_senders = ("allowed@example.com",)
    progress: list[dict] = []

    first = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        service=service,
        scan_id="scan-rejected",
        progress_callback=progress.append,
    )
    assert first["saved"] == 0
    assert first["rule_skipped"] == 1
    assert get_connection(tmp_cfg.db_path).execute("SELECT COUNT(*) FROM mail_packages").fetchone()[0] == 0
    assert progress[-1]["rule_skipped"] == 1

    tmp_cfg.receive_rule_mode = ALL_SCANNED
    second = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        service=service,
        scan_id="scan-accepted",
    )
    assert second["saved"] == 1
    assert second["duplicates"] == 0

    third = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        service=service,
        scan_id="scan-duplicate",
    )
    assert third["saved"] == 0
    assert third["duplicates"] == 1
    evaluations = query_receive_rule_evaluations(tmp_cfg.db_path, scan_id="scan-duplicate")
    assert evaluations[0]["result"] == "duplicate"


def test_provider_identity_prevents_duplicate_when_rfc_message_id_changes(tmp_cfg):
    """Provider id 是 Message-ID 之外的正式去重事实，避免异常原文产生双包。"""
    first = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        service=_gmail_service(
            {"stable-provider-id": _raw_mail(message_id="<first@example.com>")}
        ),
        scan_id="scan-provider-first",
    )
    second = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        service=_gmail_service(
            {"stable-provider-id": _raw_mail(message_id="<changed@example.com>")}
        ),
        scan_id="scan-provider-second",
    )

    assert first["saved"] == 1
    assert second["saved"] == 0
    assert second["duplicates"] == 1
    connection = get_connection(tmp_cfg.db_path)
    assert connection.execute("SELECT COUNT(*) FROM mail_packages").fetchone()[0] == 1
    row = connection.execute(
        "SELECT provider_message_id FROM mail_packages"
    ).fetchone()
    assert row[0] == "stable-provider-id"


def test_gmail_history_rescan_pages_raw_readonly_and_reports_progress(tmp_cfg):
    raws = {
        "page-1": _raw_mail(message_id="<page-1@example.com>"),
        "page-2": _raw_mail(message_id="<page-2@example.com>"),
    }
    pages = [
        {"messages": [{"id": "page-1", "threadId": "t1"}], "nextPageToken": "next"},
        {"messages": [{"id": "page-2", "threadId": "t2"}]},
    ]
    service = _gmail_service(raws, pages)
    messages = service.users.return_value.messages.return_value
    responses = iter(pages)

    def paged_list(**_kwargs):
        result = MagicMock()
        result.execute.return_value = next(responses)
        return result

    messages.list.side_effect = paged_list
    updates: list[dict] = []
    result = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        service=service,
        scan_id="scan-pages",
        page_size=1,
        progress_callback=updates.append,
    )
    assert result["fetched"] == result["saved"] == 2
    assert len(updates) == 2
    assert messages.list.call_count == 2
    assert all(call.kwargs["maxResults"] == 1 for call in messages.list.call_args_list)
    assert all(call.kwargs["format"] == "raw" for call in messages.get.call_args_list)
    assert messages.modify.call_count == 0
    assert messages.delete.call_count == 0


def test_history_rescan_cancel_stops_before_provider_fetch(tmp_cfg):
    service = _gmail_service({"cancel-1": _raw_mail()})
    result = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21),
        service=service,
        scan_id="scan-cancelled",
        cancel_check=lambda: True,
    )
    assert result["cancelled"] is True
    assert result["fetched"] == 0
    assert service.users.return_value.messages.return_value.list.call_count == 0


def test_history_rescan_can_explicitly_bypass_current_rule_without_weakening_default(tmp_cfg):
    tmp_cfg.receive_rule_mode = CUSTOM
    tmp_cfg.receive_rule_senders = ("allowed@example.com",)
    service = _gmail_service({"override-1": _raw_mail(sender="outside@example.com")})
    result = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21),
        service=service,
        scan_id="scan-rule-override",
        apply_receive_rule=False,
    )
    assert result["saved"] == 1
    assert result["rule_skipped"] == 0
    assert tmp_cfg.receive_rule_mode == CUSTOM


def test_history_rescan_failure_uses_finite_retry_state(tmp_cfg, monkeypatch):
    service = _gmail_service({"retry-1": _raw_mail()})

    def fail_one(*_args, **_kwargs):
        raise OSError("controlled archive failure")

    monkeypatch.setattr("agent_mail_bridge.gmail_api_receive._process_one_unified", fail_one)
    result = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21),
        service=service,
        scan_id="scan-retry",
    )
    assert result["failed"] == 1
    retry = get_receive_retry(tmp_cfg.db_path, "gmail_api", "retry-1")
    assert retry is not None
    assert retry["retry_count"] == 1
    assert retry["terminal_status"] is None


def test_history_rescan_scan_cap_is_bounded_and_reported(tmp_cfg):
    raw = _raw_mail(message_id="<cap@example.com>")
    service = _gmail_service(
        {"cap-1": raw},
        [{"messages": [{"id": "cap-1", "threadId": "t1"}], "nextPageToken": "more"}],
    )
    result = rescan_gmail_api_messages(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21),
        service=service,
        scan_id="scan-cap",
        page_size=1,
        scan_cap=1,
    )
    assert result["fetched"] == 1
    assert result["truncated"] is True


class _FakeImap:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.calls: list[tuple] = []

    def select(self, mailbox):
        self.calls.append(("select", mailbox))
        return "OK", [b""]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"101"]
        if command == "fetch":
            return "OK", [(b"101 (RFC822)", self.raw)]
        if command == "store":
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.calls.append(("logout",))


def test_imap_history_rescan_uses_peek_and_never_marks_seen(tmp_cfg, monkeypatch):
    tmp_cfg.gmail_receive_backend = "imap"
    connection = _FakeImap(_raw_mail(message_id="<imap-history@example.com>"))
    monkeypatch.setattr("agent_mail_bridge.mail_receive._connect_imap", lambda _cfg: connection)
    result = _rescan_via_imap(
        tmp_cfg,
        date_from=datetime(2026, 7, 19),
        date_to=datetime(2026, 7, 21, 23, 59, 59),
        apply_receive_rule=True,
        cancel_check=None,
        progress_callback=None,
        scan_id="scan-imap",
        page_size=100,
        scan_cap=5000,
    )
    assert result["saved"] == 1
    assert any(
        call[0] == "fetch" and any("BODY.PEEK[]" in str(value) for value in call[1:])
        for call in connection.calls
    )
    assert not any(call[0] == "store" for call in connection.calls)
