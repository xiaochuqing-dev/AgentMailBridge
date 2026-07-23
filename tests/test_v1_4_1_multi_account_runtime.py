"""v1.4.1 多账号运行时、隔离边界与 Provider Foundation 回归。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.credentials import (
    ACCOUNT_IMAP_SECRET,
    ACCOUNT_SMTP_SECRET,
    GMAIL_IMAP_SECRET,
    CredentialService,
    MemoryCredentialBackend,
)
from agent_mail_bridge.database import (
    get_auto_receive_state,
    get_mail_account,
    query_mail_accounts,
    query_mailboxes,
    save_auto_receive_state,
    upsert_mailboxes,
)
from agent_mail_bridge.mail_accounts import stable_account_id
from agent_mail_bridge.maintenance import backup_dir
from agent_mail_bridge.mcp_server import _all_tools
from agent_mail_bridge.models import OperationStatus, ReceiveResult
from agent_mail_bridge.provider_adapters import get_provider_adapter
from agent_mail_bridge.provider_foundation import (
    ProviderFoundationError,
    discover_imap_mailboxes,
    test_smtp_connection as check_smtp_connection,
    validate_server_settings,
)
from agent_mail_bridge.utils import sha256_of_file


def _create_gmail(
    service: ApplicationService,
    address: str,
    *,
    backend: str = "imap",
    secret: str = "account-secret",
) -> str:
    result = service.create_mail_account(
        provider="gmail",
        email_address=address,
        display_name=address.split("@", 1)[0],
        receive_backend=backend,
        secret=secret if backend == "imap" else "",
    )
    assert result.ok, result.message
    return str(result.details["account"]["account_id"])


def test_account_crud_is_stable_and_soft_remove_preserves_mailboxes(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    account_id = _create_gmail(service, "second@gmail.com")
    assert account_id == stable_account_id("gmail", "second@gmail.com")

    upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [{"external_ref": "INBOX", "display_name": "INBOX", "mailbox_role": "inbox"}],
    )
    updated = service.update_mail_account(
        account_id, display_name="第二个 Gmail", enabled=False
    )
    assert updated.ok
    service.synchronize_mail_accounts()
    assert get_mail_account(tmp_cfg.db_path, account_id)["enabled"] is False

    removed = service.remove_mail_account(account_id)
    assert removed.ok
    assert get_mail_account(tmp_cfg.db_path, account_id) is None
    archived = get_mail_account(
        tmp_cfg.db_path, account_id, include_removed=True
    )
    assert archived is not None
    assert archived["removed_at"]
    assert query_mailboxes(tmp_cfg.db_path, account_id=account_id)
    assert "mail_packages" in removed.details["preserved_facts"]


def test_legacy_sync_does_not_reenable_disabled_account(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    legacy_id = stable_account_id("gmail", tmp_cfg.gmail_address)
    assert service.update_mail_account(legacy_id, enabled=False).ok
    assert service.synchronize_mail_accounts().ok
    account = get_mail_account(tmp_cfg.db_path, legacy_id)
    assert account is not None
    assert account["enabled"] is False


def test_reenabled_account_rejoins_global_scheduler(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    service.save_all_auto_receive_states(
        enabled=True,
        interval_seconds=120,
    )
    account_id = _create_gmail(service, "reenable@gmail.com")
    assert service.update_mail_account(account_id, enabled=False).ok
    assert get_auto_receive_state(
        tmp_cfg.db_path, account_id=account_id
    )["enabled"] == 0

    assert service.update_mail_account(account_id, enabled=True).ok
    state = get_auto_receive_state(
        tmp_cfg.db_path, account_id=account_id
    )
    assert state["enabled"] == 1
    assert state["interval_seconds"] == 120


def test_per_account_credentials_and_runtime_configs_are_isolated(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    first_id = _create_gmail(service, "first@gmail.com", secret="first-secret")
    second_id = _create_gmail(service, "second@gmail.com", secret="second-secret")

    first = service._account_router.context(first_id, capability="receive").config
    second = service._account_router.context(second_id, capability="receive").config
    assert first.gmail_address == "first@gmail.com"
    assert second.gmail_address == "second@gmail.com"
    assert first.gmail_app_password == "first-secret"
    assert second.gmail_app_password == "second-secret"

    assert service.set_account_credential(
        first_id, ACCOUNT_IMAP_SECRET, "first-replaced"
    ).ok
    first_after = service._account_router.context(
        first_id, capability="receive"
    ).config
    second_after = service._account_router.context(
        second_id, capability="receive"
    ).config
    assert first_after.gmail_app_password == "first-replaced"
    assert second_after.gmail_app_password == "second-secret"


def test_oauth_files_are_per_account_and_clear_is_scoped(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    first_id = _create_gmail(
        service, "oauth-one@gmail.com", backend="gmail_api"
    )
    second_id = _create_gmail(
        service, "oauth-two@gmail.com", backend="gmail_api"
    )
    first_credentials, first_token = service._account_router.oauth_paths(first_id)
    second_credentials, second_token = service._account_router.oauth_paths(second_id)
    assert first_credentials != second_credentials
    assert first_token != second_token

    oauth_source = tmp_path / "desktop.json"
    oauth_source.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "1234567890-test.apps.googleusercontent.com",
                    "client_secret": "test-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    assert service.import_oauth_credentials(
        oauth_source, account_id=first_id
    ).ok
    assert first_credentials.is_file()
    assert not second_credentials.exists()
    first_token.write_text("first-token", encoding="utf-8")
    second_token.parent.mkdir(parents=True, exist_ok=True)
    second_token.write_text("second-token", encoding="utf-8")
    assert service.clear_gmail_oauth_token(first_id).ok
    assert not first_token.exists()
    assert second_token.read_text(encoding="utf-8") == "second-token"


def test_legacy_oauth_migration_is_one_time_and_clear_stays_cleared(tmp_cfg):
    tmp_cfg.gmail_api_credentials_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "1234567890-test.apps.googleusercontent.com",
                    "client_secret": "test-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    tmp_cfg.gmail_api_token_path.write_text("legacy-token", encoding="utf-8")
    service = ApplicationService(tmp_cfg)
    service.initialize()
    account_id = stable_account_id("gmail", tmp_cfg.gmail_address)

    context = service._account_router.context(account_id)
    account_token = context.config.gmail_api_token_path
    assert account_token.read_text(encoding="utf-8") == "legacy-token"
    assert service.clear_gmail_oauth_token(account_id).ok
    assert not account_token.exists()

    service._account_router.context(account_id)
    assert not account_token.exists()
    assert tmp_cfg.gmail_api_token_path.read_text(encoding="utf-8") == "legacy-token"


def test_deleting_legacy_account_credential_prevents_fallback(tmp_cfg):
    backend = MemoryCredentialBackend({GMAIL_IMAP_SECRET: "legacy-secret"})
    service = ApplicationService(tmp_cfg)
    service._credentials = CredentialService(backend)
    service._account_router.credentials = service._credentials
    service.initialize()
    account_id = stable_account_id("gmail", tmp_cfg.gmail_address)
    assert service.update_mail_account(
        account_id,
        provider_settings={"receive_backend": "imap"},
    ).ok
    assert service._account_router.context(account_id).config.gmail_app_password

    deleted = service.delete_account_credential(
        account_id, ACCOUNT_IMAP_SECRET
    )
    assert deleted.ok
    assert service._account_router.context(account_id).config.gmail_app_password == ""


def test_receive_routes_two_gmail_accounts_without_crossing_configs(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    first_id = _create_gmail(service, "route-one@gmail.com", secret="route-one")
    second_id = _create_gmail(service, "route-two@gmail.com", secret="route-two")
    seen: list[tuple[str, str, str]] = []

    def fake_receive(cfg, **_kwargs):
        seen.append(
            (
                cfg.runtime_account_id,
                cfg.gmail_address,
                cfg.gmail_app_password,
            )
        )
        return {"ok": True, "fetched": 0, "saved": 0, "errors": []}

    monkeypatch.setattr(
        "agent_mail_bridge.application_service.receive_mails", fake_receive
    )
    assert service.receive(account_id=first_id).status == OperationStatus.NO_CHANGES
    assert service.receive(account_id=second_id).status == OperationStatus.NO_CHANGES
    assert seen == [
        (first_id, "route-one@gmail.com", "route-one"),
        (second_id, "route-two@gmail.com", "route-two"),
    ]


def test_scheduler_isolates_account_failure_and_backoff(tmp_cfg, monkeypatch):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    first_id = _create_gmail(service, "sync-one@gmail.com")
    second_id = _create_gmail(service, "sync-two@gmail.com")
    for account_id in (first_id, second_id):
        save_auto_receive_state(
            tmp_cfg.db_path,
            account_id=account_id,
            enabled=True,
            interval_seconds=60,
        )
    calls: list[str] = []

    def fake_receive(*, account_id, **_kwargs):
        calls.append(account_id)
        if account_id == first_id:
            return ReceiveResult(
                OperationStatus.FAILED,
                backend="imap",
                error_code="network_error",
                message="network failed",
                failed=1,
            )
        return ReceiveResult(
            OperationStatus.NO_CHANGES,
            backend="imap",
            message="no changes",
        )

    monkeypatch.setattr(service, "receive", fake_receive)
    result = service.sync_due_mail_accounts(force=True)
    assert result.status == OperationStatus.PARTIAL
    assert first_id in calls
    assert second_id in calls
    assert calls.index(second_id) > calls.index(first_id)
    failed_state = get_auto_receive_state(
        tmp_cfg.db_path, account_id=first_id
    )
    healthy_state = get_auto_receive_state(
        tmp_cfg.db_path, account_id=second_id
    )
    assert failed_state["consecutive_global_failures"] == 1
    assert healthy_state["consecutive_global_failures"] == 0
    assert healthy_state["last_success_at"]
    assert failed_state["next_check_at"] < healthy_state["next_check_at"]


def test_scheduler_continues_after_unexpected_account_exception(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    first_id = _create_gmail(service, "crash-one@gmail.com")
    second_id = _create_gmail(service, "crash-two@gmail.com")
    calls: list[str] = []

    def fake_receive(*, account_id, **_kwargs):
        calls.append(account_id)
        if account_id == first_id:
            raise RuntimeError("secret-shaped-detail-must-not-escape")
        return ReceiveResult(
            OperationStatus.NO_CHANGES,
            backend="imap",
            message="no changes",
        )

    monkeypatch.setattr(service, "receive", fake_receive)
    result = service.sync_due_mail_accounts(force=True)
    assert result.status == OperationStatus.PARTIAL
    assert first_id in calls
    assert second_id in calls
    first_result = next(
        item
        for item in result.details["results"]
        if item["account_id"] == first_id
    )
    assert first_result["error_code"] == "account_sync_failed"
    assert "secret-shaped-detail" not in json.dumps(
        result.details, ensure_ascii=False
    )


def test_global_scheduler_preferences_do_not_erase_account_runtime_state(
    tmp_cfg,
):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    first_id = _create_gmail(service, "state-one@gmail.com")
    second_id = _create_gmail(service, "state-two@gmail.com")
    save_auto_receive_state(
        tmp_cfg.db_path,
        account_id=first_id,
        last_result="first-result",
        consecutive_global_failures=2,
    )
    save_auto_receive_state(
        tmp_cfg.db_path,
        account_id=second_id,
        last_result="second-result",
        consecutive_global_failures=0,
    )

    service.save_all_auto_receive_states(interval_seconds=120)

    first = get_auto_receive_state(tmp_cfg.db_path, account_id=first_id)
    second = get_auto_receive_state(tmp_cfg.db_path, account_id=second_id)
    assert first["interval_seconds"] == 120
    assert second["interval_seconds"] == 120
    assert first["last_result"] == "first-result"
    assert second["last_result"] == "second-result"
    assert first["consecutive_global_failures"] == 2
    assert second["consecutive_global_failures"] == 0


class _FakeImapClient:
    last: "_FakeImapClient | None" = None

    def __init__(self, *_args, **_kwargs):
        self.logged_out = False
        self.login_args: tuple[str, str] | None = None
        _FakeImapClient.last = self

    def starttls(self, **_kwargs):
        return None

    def login(self, username, secret):
        self.login_args = (username, secret)

    def capabilities(self):
        return (b"IMAP4rev1", b"SPECIAL-USE", b"CONDSTORE")

    def list_folders(self):
        return [
            ((b"\\Inbox",), b"/", "INBOX"),
            ((b"\\Sent",), b"/", "Sent"),
        ]

    def select_folder(self, _name, readonly=True):
        assert readonly is True
        return {
            b"UIDVALIDITY": 12,
            b"UIDNEXT": 34,
            b"HIGHESTMODSEQ": 56,
        }

    def logout(self):
        self.logged_out = True


class _FakeSmtp:
    last: "_FakeSmtp | None" = None

    def __init__(self, *_args, **_kwargs):
        self.logins: list[tuple[str, str]] = []
        self.quit_called = False
        _FakeSmtp.last = self

    def ehlo(self):
        return None

    def login(self, username, secret):
        self.logins.append((username, secret))

    def quit(self):
        self.quit_called = True


def test_generic_foundation_discovers_special_use_and_never_sends():
    settings = validate_server_settings(
        {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_security": "ssl",
        }
    )
    discovered = discover_imap_mailboxes(
        settings=settings,
        username="user@example.com",
        secret="secret",
        client_factory=_FakeImapClient,
    )
    assert discovered["mailboxes"][0]["mailbox_role"] == "inbox"
    assert discovered["mailboxes"][0]["checkpoint"] == {
        "uidvalidity": 12,
        "uidnext": 34,
        "highestmodseq": 56,
    }
    assert _FakeImapClient.last is not None
    assert _FakeImapClient.last.logged_out is True

    smtp = check_smtp_connection(
        settings=settings,
        username="user@example.com",
        secret="secret",
        smtp_ssl_factory=_FakeSmtp,
    )
    assert smtp["authenticated"] is True
    assert _FakeSmtp.last is not None
    assert _FakeSmtp.last.logins == [("user@example.com", "secret")]
    assert not hasattr(_FakeSmtp.last, "sendmail")
    assert _FakeSmtp.last.quit_called is True


def test_generic_settings_reject_secrets_and_plaintext():
    with pytest.raises(ProviderFoundationError) as secret_error:
        validate_server_settings(
            {
                "imap_host": "imap.example.com",
                "imap_password": "must-not-be-in-db",
            }
        )
    assert secret_error.value.error_code == "secret_in_provider_settings"
    with pytest.raises(ProviderFoundationError) as transport_error:
        validate_server_settings(
            {
                "imap_host": "imap.example.com",
                "imap_security": "plain",
            }
        )
    assert transport_error.value.error_code == "insecure_transport_rejected"


def test_all_provider_settings_reject_persisted_secrets(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    created = service.create_mail_account(
        provider="gmail",
        email_address="secret-setting@gmail.com",
        receive_backend="imap",
        provider_settings={"client_secret": "must-not-persist"},
        secret="safe-credential-store-value",
    )
    assert created.error_code == "secret_in_provider_settings"
    assert get_mail_account(
        tmp_cfg.db_path,
        stable_account_id("gmail", "secret-setting@gmail.com"),
    ) is None


def test_gmail_send_is_not_claimed_and_mcp_boundary_is_unchanged(tmp_cfg):
    assert tmp_cfg.gmail_api_scopes == [
        "https://www.googleapis.com/auth/gmail.readonly"
    ]
    gmail = get_provider_adapter("gmail")
    assert not gmail.supports("send")
    tools = _all_tools()
    assert len(tools) == 7
    submit = next(item for item in tools if item["name"] == "submit_result")
    submit_properties = submit["inputSchema"]["properties"]
    assert "recipient" not in submit_properties
    assert "from_account_id" not in submit_properties
    sync_status = next(
        item for item in tools if item["name"] == "get_mail_sync_status"
    )
    assert "account_id" in sync_status["inputSchema"]["properties"]


def test_qq_account_secret_isolated_in_shared_imap_smtp_namespaces(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    created = service.create_mail_account(
        provider="qq",
        email_address="second@qq.com",
        secret="qq-second-secret",
    )
    assert created.ok
    account_id = str(created.details["account"]["account_id"])
    runtime = service._account_router.context(
        account_id, capability="send"
    ).config
    assert runtime.qq_auth_code == "qq-second-secret"
    assert service._credentials.get_for_account(
        account_id, ACCOUNT_SMTP_SECRET
    ) == "qq-second-secret"
    assert service._credentials.get_for_account(
        account_id, ACCOUNT_IMAP_SECRET
    ) == "qq-second-secret"


def test_isolated_v14_to_v141_upgrade_preserves_user_files_oauth_and_secret(
    tmp_cfg,
):
    raw_path = tmp_cfg.received_dir / "upgrade-fixture" / "raw.eml"
    attachment_path = (
        tmp_cfg.received_dir / "upgrade-fixture" / "attachments" / "报告.txt"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    attachment_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"From: old@example.com\r\n\r\nlegacy raw")
    attachment_path.write_bytes("旧附件".encode("utf-8"))
    hashes_before = {
        "raw": sha256_of_file(raw_path),
        "attachment": sha256_of_file(attachment_path),
    }
    tmp_cfg.gmail_api_credentials_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "1234567890-test.apps.googleusercontent.com",
                    "client_secret": "test-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    tmp_cfg.gmail_api_token_path.write_text(
        json.dumps({"refresh_token": "isolated-test-token"}),
        encoding="utf-8",
    )
    with sqlite3.connect(tmp_cfg.db_path) as connection:
        connection.execute(
            "UPDATE migration_metadata SET schema_version = 1 "
            "WHERE migration_key = 'multi_account_core_v1'"
        )
        connection.commit()

    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    legacy_id = stable_account_id("gmail", tmp_cfg.gmail_address)
    runtime = service._account_router.context(
        legacy_id, capability="receive"
    ).config

    with sqlite3.connect(tmp_cfg.db_path) as connection:
        schema_version = connection.execute(
            "SELECT schema_version FROM migration_metadata "
            "WHERE migration_key = 'multi_account_core_v1'"
        ).fetchone()[0]
        account_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(mail_accounts)"
            ).fetchall()
        }
    assert schema_version == 3
    assert "removed_at" in account_columns
    assert list(
        backup_dir(tmp_cfg).glob("*before_v1_4_multi_account*.db")
    )
    assert sha256_of_file(raw_path) == hashes_before["raw"]
    assert sha256_of_file(attachment_path) == hashes_before["attachment"]
    assert tmp_cfg.gmail_api_credentials_path.is_file()
    assert tmp_cfg.gmail_api_token_path.is_file()
    assert runtime.gmail_api_credentials_path.is_file()
    assert runtime.gmail_api_token_path.is_file()
    assert runtime.gmail_app_password == tmp_cfg.gmail_app_password
