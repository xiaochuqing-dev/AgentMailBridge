"""v1.2.1 Gmail OAuth 首次配置可靠性专项测试。"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path

import httplib2
import pytest
import socks
from google.auth.exceptions import RefreshError

from agent_mail_bridge.oauth_storage import (
    OAuthImportError,
    atomic_write_private_text,
    import_oauth_credentials,
)
from agent_mail_bridge.process_lock import ProcessLock
from agent_mail_bridge.oauth_flow import (
    LoopbackCallbackServer,
    OAuthCallback,
    OAuthState,
    OAuthStateMachine,
    OAuthStateTransitionError,
    OAuthWaitCancelled,
    OAuthWaitTimeout,
    loopback_no_proxy,
)
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.gmail_api_auth import (
    GmailApiAuthError,
    GmailOAuthSession,
    TokenClientMismatchError,
    get_gmail_api_service,
    get_oauth_diagnostics,
    reverify_gmail_authorization,
)
from agent_mail_bridge.models import OperationStatus


def _desktop_payload(**overrides):
    installed = {
        "client_id": "1234567890-fake.apps.googleusercontent.com",
        "client_secret": "fake-client-secret-for-tests-only",
        "project_id": "fake-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
    installed.update(overrides)
    return {"installed": installed}


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_desktop_credentials_are_imported_with_safe_summary(tmp_path: Path):
    source = _write_json(tmp_path / "desktop.json", _desktop_payload())
    target = tmp_path / "OAuth" / "credentials.json"

    assert import_oauth_credentials(source, destination=target) == target
    assert target.is_file()


@pytest.mark.parametrize(
    "payload",
    [
        {"web": _desktop_payload()["installed"]},
        {
            "installed": _desktop_payload()["installed"],
            "web": _desktop_payload()["installed"],
        },
        {"installed": _desktop_payload()["installed"], "unexpected": {}},
        {},
        {"installed": "not-an-object"},
    ],
)
def test_non_desktop_credentials_are_rejected(tmp_path: Path, payload):
    source = _write_json(tmp_path / "wrong.json", payload)

    with pytest.raises(OAuthImportError) as caught:
        import_oauth_credentials(source, destination=tmp_path / "target.json")

    assert caught.value.error_code == "credentials_wrong_type"


@pytest.mark.parametrize(
    "field,value,error_code",
    [
        ("client_id", "", "credentials_missing_field"),
        ("client_secret", None, "credentials_missing_field"),
        ("auth_uri", "http://accounts.google.com/o/oauth2/auth", "credentials_invalid_endpoint"),
        ("token_uri", "https://example.invalid/token", "credentials_invalid_endpoint"),
        ("redirect_uris", "http://localhost", "credentials_missing_field"),
        ("redirect_uris", ["https://example.invalid/callback"], "credentials_invalid_endpoint"),
    ],
)
def test_desktop_credentials_fields_are_strictly_validated(
    tmp_path: Path, field: str, value, error_code: str
):
    source = _write_json(tmp_path / "invalid.json", _desktop_payload(**{field: value}))

    with pytest.raises(OAuthImportError) as caught:
        import_oauth_credentials(source, destination=tmp_path / "target.json")

    assert caught.value.error_code == error_code
    assert "fake-client-secret-for-tests-only" not in str(caught.value)


def test_credentials_file_size_is_bounded(tmp_path: Path):
    source = tmp_path / "too-large.json"
    source.write_bytes(b"{" + b" " * (1024 * 1024 + 1) + b"}")

    with pytest.raises(OAuthImportError) as caught:
        import_oauth_credentials(source, destination=tmp_path / "target.json")

    assert caught.value.error_code == "credentials_unreadable"


@pytest.mark.parametrize("raw", [b"", b"\xff\xfe\x00"])
def test_empty_or_non_utf8_credentials_are_rejected(tmp_path: Path, raw: bytes):
    source = tmp_path / "broken.json"
    source.write_bytes(raw)

    with pytest.raises(OAuthImportError) as caught:
        import_oauth_credentials(source, destination=tmp_path / "target.json")

    assert caught.value.error_code == "credentials_invalid_json"


def test_failed_credentials_replacement_preserves_existing_file(
    tmp_path: Path, monkeypatch
):
    source = _write_json(
        tmp_path / "new.json",
        _desktop_payload(client_id="9999999999-fake.apps.googleusercontent.com"),
    )
    target = _write_json(tmp_path / "credentials.json", _desktop_payload())
    old_bytes = target.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OAuthImportError) as caught:
        import_oauth_credentials(source, destination=target, replace=True)

    assert caught.value.error_code == "credentials_unreadable"
    assert target.read_bytes() == old_bytes
    assert not list(tmp_path.glob(".*.tmp"))


def test_failed_token_replacement_preserves_existing_token(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "token.json"
    target.write_text("old-token", encoding="utf-8")

    monkeypatch.setattr(
        os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated failure")),
    )
    with pytest.raises(OAuthImportError) as caught:
        atomic_write_private_text(target, "new-token")

    assert caught.value.error_code == "token_save_failed"
    assert target.read_text(encoding="utf-8") == "old-token"


def test_oauth_state_machine_rejects_illegal_transition():
    machine = OAuthStateMachine()
    machine.transition(OAuthState.VALIDATING_CREDENTIALS)
    machine.transition(OAuthState.PREPARING_CALLBACK)

    with pytest.raises(OAuthStateTransitionError):
        machine.transition(OAuthState.AUTHORIZED)


def test_loopback_callback_uses_ipv4_and_releases_port():
    server = LoopbackCallbackServer()
    port = server.start()
    response_body: list[str] = []

    def send_callback():
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?code=fake-code&state=fake-state",
            timeout=2,
        ) as response:
            response_body.append(response.read().decode("utf-8"))

    sender = threading.Thread(target=send_callback)
    sender.start()
    callback = server.wait(threading.Event(), timeout_seconds=2)
    sender.join(timeout=2)
    server.close()

    assert callback.code == "fake-code"
    assert callback.state == "fake-state"
    assert response_body and "AgentMailBridge" in response_body[0]
    assert "<script" not in response_body[0].lower()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_loopback_callback_can_be_cancelled_and_times_out():
    cancelled = LoopbackCallbackServer()
    cancelled.start()
    cancel_event = threading.Event()
    cancel_event.set()
    with pytest.raises(OAuthWaitCancelled):
        cancelled.wait(cancel_event, timeout_seconds=2)
    cancelled.close()

    timed_out = LoopbackCallbackServer()
    timed_out.start()
    with pytest.raises(OAuthWaitTimeout):
        timed_out.wait(threading.Event(), timeout_seconds=0.05)
    timed_out.close()


def test_stalled_loopback_client_cannot_block_cancel_or_port_release():
    server = LoopbackCallbackServer()
    port = server.start()
    cancel_event = threading.Event()
    result: list[Exception] = []

    def wait_for_callback():
        try:
            server.wait(cancel_event, timeout_seconds=5)
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=wait_for_callback)
    worker.start()
    stalled = socket.create_connection(("127.0.0.1", port), timeout=1)
    try:
        time.sleep(0.05)
        cancel_event.set()
        server.wake()
        worker.join(timeout=1.5)
        assert not worker.is_alive()
        assert len(result) == 1
        assert isinstance(result[0], OAuthWaitCancelled)
    finally:
        stalled.close()
        cancel_event.set()
        server.wake()
        worker.join(timeout=2)
        server.close()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_loopback_no_proxy_is_merged_and_restored(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")

    with loopback_no_proxy():
        upper = set(os.environ["NO_PROXY"].split(","))
        lower = set(os.environ["no_proxy"].split(","))
        for required in {"127.0.0.1", "localhost", "::1"}:
            assert required in upper
            assert required in lower
        assert "example.com" in upper
        assert "example.com" in lower

    assert os.environ["NO_PROXY"] == "example.com"
    assert os.environ["no_proxy"] == "example.com"


class _FakeCredentials:
    def __init__(self, *, refresh_token: str | None = "fake-refresh-token"):
        self.valid = True
        self.expired = False
        self.refresh_token = refresh_token
        self.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        self.client_id = "1234567890-fake.apps.googleusercontent.com"

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": "fake-access-token",
                "refresh_token": self.refresh_token,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": self.client_id,
                "client_secret": "fake-client-secret-for-tests-only",
                "scopes": self.scopes,
            }
        )


class _FakeFlow:
    def __init__(self, credentials: _FakeCredentials | None = None):
        self.credentials = credentials or _FakeCredentials()
        self.redirect_uri = ""
        self.authorization_kwargs = {}
        self.fetch_kwargs = {}

    def authorization_url(self, **kwargs):
        self.authorization_kwargs = kwargs
        return "https://accounts.google.com/o/oauth2/auth?redacted=fake", "fake-state"

    def fetch_token(self, **kwargs):
        self.fetch_kwargs = kwargs
        return {"access_token": "not-exposed"}


class _FakeCallbackServer:
    def __init__(self, callback: OAuthCallback | Exception):
        self.callback = callback
        self.closed = False
        self.woken = False
        self.port = 43123

    def start(self):
        return self.port

    def wait(self, _cancel_event, *, timeout_seconds):
        assert timeout_seconds > 0
        if isinstance(self.callback, Exception):
            raise self.callback
        return self.callback

    def wake(self):
        self.woken = True

    def close(self):
        self.closed = True


class _FakeProfileRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeUsers:
    def __init__(self, result):
        self.result = result

    def getProfile(self, *, userId):
        assert userId == "me"
        return _FakeProfileRequest(self.result)


class _FakeGmailService:
    def __init__(self, result):
        self.result = result

    def users(self):
        return _FakeUsers(self.result)


def _oauth_cfg(tmp_path: Path) -> AppConfig:
    credentials = _write_json(tmp_path / "credentials.json", _desktop_payload())
    return AppConfig(
        gmail_address="owner@gmail.com",
        owner_gmail="owner@gmail.com",
        gmail_receive_backend="gmail_api",
        gmail_api_credentials_path=credentials,
        gmail_api_token_path=tmp_path / "token.json",
        gmail_api_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        data_root=tmp_path / "Data",
    )


def test_gmail_api_configured_requires_valid_desktop_credentials(tmp_path: Path):
    cfg = _oauth_cfg(tmp_path)
    assert cfg.gmail_api_configured is True
    _write_json(
        cfg.gmail_api_credentials_path,
        {"web": _desktop_payload()["installed"]},
    )
    assert cfg.gmail_api_configured is False


def _patch_oauth_dependencies(monkeypatch, fake_flow, fake_server, profile_result):
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.InstalledAppFlow.from_client_config",
        lambda *_args, **_kwargs: fake_flow,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.LoopbackCallbackServer",
        lambda: fake_server,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.webbrowser.open",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth._build_gmail_service",
        lambda *_args, **_kwargs: _FakeGmailService(profile_result),
    )


def test_oauth_session_success_uses_ipv4_state_and_atomic_token(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    states: list[str] = []
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = GmailOAuthSession(
        cfg, progress_callback=lambda event: states.append(event["state"])
    ).run()

    assert result.status == OperationStatus.SUCCESS
    assert result.details["oauth_state"] == "AUTHORIZED"
    assert flow.redirect_uri == "http://127.0.0.1:43123/"
    assert flow.authorization_kwargs["prompt"] == "consent"
    assert flow.authorization_kwargs["include_granted_scopes"] == "true"
    assert "authorization_response" in flow.fetch_kwargs
    assert cfg.gmail_api_token_path.is_file()
    assert server.closed is True
    assert states[0] == "VALIDATING_CREDENTIALS"
    assert states[-1] == "AUTHORIZED"
    assert states.index("CALLBACK_READY") < states.index("OPENING_BROWSER")
    diagnostics = get_oauth_diagnostics(cfg)
    assert diagnostics["callback_host"] == "127.0.0.1"
    assert diagnostics["callback_received"] is True
    assert diagnostics["port_released"] is True
    assert diagnostics["error_code"] is None
    assert "fake-code" not in str(diagnostics)
    assert "fake-state" not in str(diagnostics)
    assert "fake-access-token" not in str(diagnostics)


def test_oauth_session_rejects_state_mismatch_without_saving_token(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="wrong-state"))
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = GmailOAuthSession(cfg).run()

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "oauth_state_mismatch"
    assert not cfg.gmail_api_token_path.exists()
    assert not flow.fetch_kwargs
    assert server.closed is True


def test_callback_bind_failure_never_opens_browser(tmp_path: Path, monkeypatch):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    server.start = lambda: (_ for _ in ()).throw(OSError("bind failed"))
    opened: list[str] = []
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.webbrowser.open",
        lambda url, **_kwargs: opened.append(url) or True,
    )

    result = GmailOAuthSession(cfg).run()

    assert result.error_code == "callback_bind_failed"
    assert opened == []
    assert server.closed is True


@pytest.mark.parametrize(
    "wait_error,error_code,terminal_state",
    [
        (OAuthWaitCancelled("cancelled"), "oauth_cancelled", "CANCELLED"),
        (OAuthWaitTimeout("timeout"), "oauth_timeout", "TIMED_OUT"),
    ],
)
def test_oauth_session_cancel_and_timeout_cleanup(
    tmp_path: Path, monkeypatch, wait_error, error_code, terminal_state
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(wait_error)
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = GmailOAuthSession(cfg, timeout_seconds=0.1).run()

    assert result.error_code == error_code
    assert result.details["oauth_state"] == terminal_state
    assert server.closed is True
    assert not cfg.gmail_api_token_path.exists()
    assert get_oauth_diagnostics(cfg)["error_code"] == error_code


def test_oauth_account_mismatch_preserves_old_token(tmp_path: Path, monkeypatch):
    cfg = _oauth_cfg(tmp_path)
    cfg.gmail_api_token_path.write_text("old-token-remains", encoding="utf-8")
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "other@gmail.com"}
    )

    result = GmailOAuthSession(cfg).run()

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "account_mismatch"
    assert cfg.gmail_api_token_path.read_text(encoding="utf-8") == "old-token-remains"
    assert result.details["expected_email_masked"]
    assert result.details["actual_email_masked"]
    assert "owner@gmail.com" not in str(result.details)
    assert "other@gmail.com" not in str(result.details)


def test_oauth_profile_network_failure_keeps_new_token_for_reverify(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    _patch_oauth_dependencies(monkeypatch, flow, server, TimeoutError("offline"))

    result = GmailOAuthSession(cfg).run()

    assert result.status == OperationStatus.PARTIAL
    assert result.error_code == "network_error"
    assert result.details["oauth_state"] == "AUTHORIZED_UNVERIFIED"
    assert cfg.gmail_api_token_path.is_file()
    assert get_oauth_diagnostics(cfg)["error_code"] == "network_error"


def test_cancel_during_token_exchange_error_never_replaces_old_token(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    cfg.gmail_api_token_path.write_text("old-token-remains", encoding="utf-8")
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    session = GmailOAuthSession(cfg)

    def cancel_then_fail(**_kwargs):
        session.cancel()
        raise TimeoutError("offline after cancel")

    flow.fetch_token = cancel_then_fail
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = session.run()

    assert result.status == OperationStatus.CANCELLED
    assert result.error_code == "oauth_cancelled"
    assert cfg.gmail_api_token_path.read_text(encoding="utf-8") == "old-token-remains"


def test_cancel_during_profile_error_never_saves_candidate_token(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    cfg.gmail_api_token_path.write_text("old-token-remains", encoding="utf-8")
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    session = GmailOAuthSession(cfg)
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    class _CancellingProfileRequest:
        def execute(self):
            session.cancel()
            raise TimeoutError("offline after cancel")

    class _CancellingUsers:
        @staticmethod
        def getProfile(*, userId):
            assert userId == "me"
            return _CancellingProfileRequest()

    class _CancellingService:
        @staticmethod
        def users():
            return _CancellingUsers()

    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth._build_gmail_service",
        lambda *_args, **_kwargs: _CancellingService(),
    )

    result = session.run()

    assert result.status == OperationStatus.CANCELLED
    assert result.error_code == "oauth_cancelled"
    assert cfg.gmail_api_token_path.read_text(encoding="utf-8") == "old-token-remains"


def test_oauth_browser_failure_keeps_session_alive_without_logging_url(
    tmp_path: Path, monkeypatch, caplog
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    warnings: list[str | None] = []
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.webbrowser.open",
        lambda *_args, **_kwargs: False,
    )

    result = GmailOAuthSession(
        cfg,
        progress_callback=lambda event: warnings.append(
            event.get("warning_error_code")
        ),
    ).run()

    assert result.status == OperationStatus.SUCCESS
    assert "browser_open_failed" in warnings
    assert get_oauth_diagnostics(cfg)["error_code"] is None
    assert "redacted=fake" not in caplog.text
    assert "fake-access-token" not in caplog.text
    assert "fake-refresh-token" not in caplog.text
    assert "fake-client-secret-for-tests-only" not in caplog.text


def test_browser_opener_exception_keeps_session_alive_and_hides_url(
    tmp_path: Path, monkeypatch, caplog
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    waiting_events: list[dict] = []
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    def fail_browser(url, **_kwargs):
        raise RuntimeError(f"failed to open {url}")

    result = GmailOAuthSession(
        cfg,
        browser_opener=fail_browser,
        progress_callback=lambda event: waiting_events.append(event)
        if event["state"] == "WAITING_FOR_USER"
        else None,
    ).run()

    assert result.status == OperationStatus.SUCCESS
    assert waiting_events[0]["browser_opened"] is False
    assert waiting_events[0]["technical_detail"] == "RuntimeError"
    assert "redacted=fake" not in caplog.text


@pytest.mark.parametrize(
    "callback,error_code,status",
    [
        (
            OAuthCallback(error="access_denied", state="fake-state"),
            "access_denied",
            OperationStatus.CANCELLED,
        ),
        (
            OAuthCallback(error="access_denied", state="wrong-state"),
            "oauth_state_mismatch",
            OperationStatus.FAILED,
        ),
        (OAuthCallback(state="fake-state"), "callback_invalid", OperationStatus.FAILED),
        (
            OAuthCallback(code="fake-code", state="fake-state", invalid_parameters=True),
            "callback_invalid",
            OperationStatus.FAILED,
        ),
    ],
)
def test_oauth_callback_errors_are_structured(
    tmp_path: Path, monkeypatch, callback, error_code, status
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(callback)
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = GmailOAuthSession(cfg).run()

    assert result.status == status
    assert result.error_code == error_code
    assert not cfg.gmail_api_token_path.exists()


def test_oauth_missing_refresh_token_is_not_saved(tmp_path: Path, monkeypatch):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow(_FakeCredentials(refresh_token=None))
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = GmailOAuthSession(cfg).run()

    assert result.error_code == "refresh_token_missing"
    assert not cfg.gmail_api_token_path.exists()


class _FakeHttpError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__("safe fake HTTP error")
        self.resp = type("Response", (), {"status": status})()
        self.error_details = [{"reason": reason}]


class _FakeOAuthError(Exception):
    def __init__(self, error: str):
        super().__init__("safe fake OAuth error")
        self.error = error


@pytest.mark.parametrize(
    "profile_error,error_code,token_saved",
    [
        (_FakeHttpError(403, "accessNotConfigured"), "gmail_api_disabled", True),
        (_FakeHttpError(403, "insufficientPermissions"), "insufficient_scope", False),
        (_FakeHttpError(401, "invalidCredentials"), "token_exchange_failed", False),
        (_FakeHttpError(429, "rateLimitExceeded"), "profile_check_failed", True),
        (_FakeHttpError(503, "backendError"), "profile_check_failed", True),
        (httplib2.ServerNotFoundError("dns failed"), "network_error", True),
        (socks.ProxyConnectionError("proxy failed"), "proxy_error", True),
    ],
)
def test_oauth_profile_errors_have_stable_semantics(
    tmp_path: Path, monkeypatch, profile_error, error_code, token_saved
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    _patch_oauth_dependencies(monkeypatch, flow, server, profile_error)

    result = GmailOAuthSession(cfg).run()

    assert result.error_code == error_code
    assert cfg.gmail_api_token_path.exists() is token_saved


@pytest.mark.parametrize(
    "exchange_error,error_code",
    [
        (_FakeOAuthError("redirect_uri_mismatch"), "redirect_uri_mismatch"),
        (_FakeOAuthError("invalid_client"), "invalid_client"),
        (_FakeOAuthError("deleted_client"), "deleted_client"),
        (TimeoutError("timeout"), "network_error"),
    ],
)
def test_token_exchange_errors_are_classified(
    tmp_path: Path, monkeypatch, exchange_error, error_code
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    flow.fetch_token = lambda **_kwargs: (_ for _ in ()).throw(exchange_error)
    server = _FakeCallbackServer(OAuthCallback(code="fake-code", state="fake-state"))
    _patch_oauth_dependencies(
        monkeypatch, flow, server, {"emailAddress": "owner@gmail.com"}
    )

    result = GmailOAuthSession(cfg).run()

    assert result.error_code == error_code
    assert not cfg.gmail_api_token_path.exists()


def test_oauth_process_lock_blocks_second_process_style_session(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    lock = ProcessLock(cfg.gmail_api_token_path.parent / ".oauth.lock")
    assert lock.acquire()
    try:
        result = GmailOAuthSession(cfg).run()
    finally:
        lock.release()

    assert result.error_code == "oauth_lock_busy"


def test_loopback_server_repeated_cancel_releases_all_ports():
    ports: list[int] = []
    for _ in range(20):
        server = LoopbackCallbackServer()
        ports.append(server.start())
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(OAuthWaitCancelled):
            server.wait(cancelled, timeout_seconds=1)
        server.close()
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))


def test_real_session_cancel_wakes_loopback_and_releases_port(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    flow = _FakeFlow()
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.InstalledAppFlow.from_client_config",
        lambda *_args, **_kwargs: flow,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.webbrowser.open",
        lambda *_args, **_kwargs: False,
    )
    waiting = threading.Event()
    result_box: list = []
    callback_port: list[int] = []

    def progress(event):
        if event["state"] == "CALLBACK_READY":
            callback_port.append(int(event["callback_port"]))
        if event["state"] == "WAITING_FOR_USER":
            waiting.set()

    session = GmailOAuthSession(cfg, progress_callback=progress)
    worker = threading.Thread(target=lambda: result_box.append(session.run()))
    worker.start()
    assert waiting.wait(timeout=2)
    assert session.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result_box[0].error_code == "oauth_cancelled"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", callback_port[0]))


def test_only_one_in_process_oauth_session_can_run(tmp_path: Path, monkeypatch):
    cfg = _oauth_cfg(tmp_path)
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.InstalledAppFlow.from_client_config",
        lambda *_args, **_kwargs: _FakeFlow(),
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.webbrowser.open",
        lambda *_args, **_kwargs: False,
    )
    waiting = threading.Event()
    first = GmailOAuthSession(
        cfg,
        progress_callback=lambda event: waiting.set()
        if event["state"] == "WAITING_FOR_USER"
        else None,
    )
    first_result: list = []
    worker = threading.Thread(target=lambda: first_result.append(first.run()))
    worker.start()
    assert waiting.wait(timeout=2)

    second_result = GmailOAuthSession(cfg).run()
    first.cancel()
    worker.join(timeout=2)

    assert second_result.error_code == "oauth_already_running"
    assert first_result[0].error_code == "oauth_cancelled"


def test_reopen_browser_reuses_same_authorization_url(tmp_path: Path, monkeypatch):
    cfg = _oauth_cfg(tmp_path)
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.InstalledAppFlow.from_client_config",
        lambda *_args, **_kwargs: _FakeFlow(),
    )
    opened_urls: list[str] = []
    waiting = threading.Event()
    session = GmailOAuthSession(
        cfg,
        browser_opener=lambda url, **_kwargs: opened_urls.append(url) or True,
        progress_callback=lambda event: waiting.set()
        if event["state"] == "WAITING_FOR_USER"
        else None,
    )
    result_box: list = []
    worker = threading.Thread(target=lambda: result_box.append(session.run()))
    worker.start()
    assert waiting.wait(timeout=2)

    reopen_result = session.reopen_browser()
    session.cancel()
    worker.join(timeout=2)

    assert reopen_result.status == OperationStatus.SUCCESS
    assert len(opened_urls) == 2
    assert opened_urls[0] == opened_urls[1]


def test_token_client_mismatch_requires_reauthorization(tmp_path: Path):
    cfg = _oauth_cfg(tmp_path)
    cfg.gmail_api_token_path.write_text(
        json.dumps(
            {
                "token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "9999999999-other.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TokenClientMismatchError) as caught:
        get_gmail_api_service(cfg, interactive=False)

    assert caught.value.error_code == "token_client_mismatch"


def test_reverify_uses_existing_token_without_opening_browser(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    fake_creds = _FakeCredentials()
    cfg.gmail_api_token_path.write_text(fake_creds.to_json(), encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.Credentials.from_authorized_user_file",
        lambda *_args, **_kwargs: fake_creds,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth._build_gmail_service",
        lambda *_args, **_kwargs: _FakeGmailService(
            {"emailAddress": "owner@gmail.com"}
        ),
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.webbrowser.open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reverify must not open a browser")
        ),
    )

    result = reverify_gmail_authorization(cfg)

    assert result.status == OperationStatus.SUCCESS
    assert result.details["oauth_state"] == "AUTHORIZED"


def test_revoked_refresh_token_is_classified_without_browser(
    tmp_path: Path, monkeypatch
):
    cfg = _oauth_cfg(tmp_path)
    cfg.gmail_api_token_path.write_text(
        json.dumps(
            {
                "token": "expired-access-token",
                "refresh_token": "revoked-refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "1234567890-fake.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            }
        ),
        encoding="utf-8",
    )
    fake_creds = _FakeCredentials()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh = lambda _request: (_ for _ in ()).throw(
        RefreshError(
            "invalid_grant",
            {
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
        )
    )
    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.Credentials.from_authorized_user_file",
        lambda *_args, **_kwargs: fake_creds,
    )

    with pytest.raises(GmailApiAuthError) as caught:
        get_gmail_api_service(cfg, interactive=False)

    assert caught.value.error_code == "refresh_revoked"
