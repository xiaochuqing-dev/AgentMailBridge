"""阶段 B：Windows 凭据统一入口与旧配置迁移测试。"""

from __future__ import annotations

from pathlib import Path

from agent_mail_bridge.credentials import (
    CredentialError,
    CredentialService,
    GMAIL_IMAP_SECRET,
    MemoryCredentialBackend,
    QQ_SMTP_SECRET,
)


def test_credential_read_write_update_delete():
    service = CredentialService(MemoryCredentialBackend())

    service.set(QQ_SMTP_SECRET, "first-secret")
    service.set(QQ_SMTP_SECRET, "updated-secret")
    assert service.get(QQ_SMTP_SECRET) == "updated-secret"
    assert service.status()[QQ_SMTP_SECRET]

    service.delete(QQ_SMTP_SECRET)
    assert service.get(QQ_SMTP_SECRET) is None


def test_legacy_env_migration_clears_only_verified_secrets(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "GMAIL_ADDRESS=user@gmail.com\n"
        "GMAIL_APP_PASSWORD=imap-secret\n"
        "QQ_AUTH_CODE=qq-secret\n",
        encoding="utf-8",
    )
    service = CredentialService(MemoryCredentialBackend())

    result = service.migrate_env(env_path)
    content = env_path.read_text(encoding="utf-8")

    assert result.failed == {}
    assert service.get(GMAIL_IMAP_SECRET) == "imap-secret"
    assert service.get(QQ_SMTP_SECRET) == "qq-secret"
    assert 'GMAIL_APP_PASSWORD=""' in content
    assert 'QQ_AUTH_CODE=""' in content
    assert "GMAIL_ADDRESS=user@gmail.com" in content
    assert "imap-secret" not in content
    assert "qq-secret" not in content


def test_migration_failure_keeps_legacy_value(tmp_path: Path):
    class FailingBackend(MemoryCredentialBackend):
        def write(self, name: str, value: str) -> None:
            if name == QQ_SMTP_SECRET:
                raise CredentialError("模拟安全存储不可用")
            super().write(name, value)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "GMAIL_APP_PASSWORD=imap-secret\nQQ_AUTH_CODE=qq-secret\n",
        encoding="utf-8",
    )

    result = CredentialService(FailingBackend()).migrate_env(env_path)
    content = env_path.read_text(encoding="utf-8")

    assert "QQ_AUTH_CODE" in result.failed
    assert "qq-secret" in content
    assert "imap-secret" not in content


def test_application_service_status_never_returns_secret(tmp_cfg):
    from agent_mail_bridge.application_service import ApplicationService

    service = ApplicationService(tmp_cfg)
    status = service.get_credential_status()
    rendered = str(status.to_dict())

    assert status.ok
    assert status.details == {"gmail_imap": True, "qq_smtp": True}
    assert tmp_cfg.gmail_app_password not in rendered
    assert tmp_cfg.qq_auth_code not in rendered


def test_secure_store_values_override_env(monkeypatch, tmp_path: Path):
    from agent_mail_bridge import config

    monkeypatch.setenv("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE", "0")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "legacy-imap")
    monkeypatch.setenv("QQ_AUTH_CODE", "legacy-qq")
    monkeypatch.setattr(
        "agent_mail_bridge.credentials.load_secure_secrets",
        lambda: {
            "GMAIL_APP_PASSWORD": "secure-imap",
            "QQ_AUTH_CODE": "secure-qq",
        },
    )

    loaded = config.load_config(tmp_path / "missing.env")

    assert loaded.gmail_app_password == "secure-imap"
    assert loaded.qq_auth_code == "secure-qq"
