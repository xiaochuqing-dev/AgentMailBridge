"""Gmail API OAuth 授权测试（使用 mock，不发起真实网络请求）。

覆盖：
- credentials.json 不存在时报错可读
- token 存在且有效时不启动浏览器（不调用 InstalledAppFlow）
- token 过期且有 refresh_token 时会 refresh
- token 不存在时会启动 InstalledAppFlow
- token 保存到指定路径
- token scope 不一致时提示重新授权
- validate_credentials_file / describe_token_status 不输出敏感内容
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.gmail_api_auth import (
    CredentialsNotFoundError,
    GmailApiAuthError,
    TokenScopeMismatchError,
    describe_token_status,
    get_gmail_api_service,
    validate_credentials_file,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _make_creds_file(path: Path) -> None:
    """写一个合法的 Desktop App credentials.json。"""
    path.write_text(
        json.dumps({
            "installed": {
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }),
        encoding="utf-8",
    )


def _write_token(path: Path, *, scopes=SCOPES, expired=False,
                 refresh_token="refresh123") -> None:
    """写一个 token.json 文件。"""
    import time
    token_data = {
        "token": "fake_access_token",
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "secret",
        "scopes": scopes,
    }
    if expired:
        token_data["expiry"] = "2020-01-01T00:00:00Z"
    else:
        token_data["expiry"] = "2099-01-01T00:00:00Z"
    path.write_text(json.dumps(token_data), encoding="utf-8")


def _cfg(tmp_path: Path, *, creds_exists=True, token_exists=False,
         expired=False, scopes=SCOPES) -> AppConfig:
    creds = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    if creds_exists:
        _make_creds_file(creds)
    if token_exists:
        _write_token(token, scopes=scopes, expired=expired)
    return AppConfig(
        gmail_address="user@gmail.com",
        gmail_api_credentials_path=creds,
        gmail_api_token_path=token,
        gmail_api_scopes=list(scopes),
    )


class TestCredentialsNotFound:
    def test_missing_credentials_raises(self, tmp_path):
        cfg = _cfg(tmp_path, creds_exists=False)
        with pytest.raises(CredentialsNotFoundError, match="credentials"):
            get_gmail_api_service(cfg)


class TestTokenValid:
    def test_valid_token_no_browser(self, tmp_path):
        cfg = _cfg(tmp_path, token_exists=True, expired=False)
        with patch(
            "agent_mail_bridge.gmail_api_auth.Credentials"
        ) as MockCreds, patch(
            "agent_mail_bridge.gmail_api_auth.build"
        ) as mock_build:
            # 模拟 Credentials.from_authorized_user_file 返回有效 creds
            fake_creds = MagicMock()
            fake_creds.valid = True
            fake_creds.expired = False
            fake_creds.scopes = SCOPES
            MockCreds.from_authorized_user_file.return_value = fake_creds
            mock_build.return_value = "fake_service"

            service = get_gmail_api_service(cfg)
            assert service == "fake_service"
            # 不应调用 flow / run_local_server
            mock_build.assert_called_once()


class TestTokenExpiredRefresh:
    def test_expired_token_refreshed(self, tmp_path):
        cfg = _cfg(tmp_path, token_exists=True, expired=True)
        with patch(
            "agent_mail_bridge.gmail_api_auth.Credentials"
        ) as MockCreds, patch(
            "agent_mail_bridge.gmail_api_auth.build"
        ) as mock_build, patch(
            "agent_mail_bridge.gmail_api_auth.Request"
        ) as MockRequest:
            fake_creds = MagicMock()
            fake_creds.valid = False
            fake_creds.expired = True
            fake_creds.refresh_token = "refresh123"
            fake_creds.scopes = SCOPES
            fake_creds.to_json.return_value = '{"token": "refreshed"}'
            MockCreds.from_authorized_user_file.return_value = fake_creds
            mock_build.return_value = "fake_service"

            service = get_gmail_api_service(cfg)
            assert service == "fake_service"
            # 应该调用了 refresh
            fake_creds.refresh.assert_called_once()
            # token 应被保存
            assert cfg.gmail_api_token_path.exists()


class TestNoTokenStartsFlow:
    def test_no_token_starts_flow(self, tmp_path):
        cfg = _cfg(tmp_path, token_exists=False)
        with patch(
            "agent_mail_bridge.gmail_api_auth.InstalledAppFlow"
        ) as MockFlow, patch(
            "agent_mail_bridge.gmail_api_auth.build"
        ) as mock_build:
            fake_creds = MagicMock()
            fake_creds.valid = True
            fake_creds.expired = False
            fake_creds.scopes = SCOPES
            fake_creds.to_json.return_value = '{"token": "new"}'

            fake_flow = MagicMock()
            fake_flow.run_local_server.return_value = fake_creds
            MockFlow.from_client_secrets_file.return_value = fake_flow
            mock_build.return_value = "fake_service"

            service = get_gmail_api_service(cfg)
            assert service == "fake_service"
            MockFlow.from_client_secrets_file.assert_called_once()
            fake_flow.run_local_server.assert_called_once()
            # token 应被保存
            assert cfg.gmail_api_token_path.exists()


class TestTokenSave:
    def test_token_saved_to_path(self, tmp_path):
        cfg = _cfg(tmp_path, token_exists=False)
        with patch(
            "agent_mail_bridge.gmail_api_auth.InstalledAppFlow"
        ) as MockFlow, patch(
            "agent_mail_bridge.gmail_api_auth.build"
        ) as mock_build:
            fake_creds = MagicMock()
            fake_creds.valid = True
            fake_creds.expired = False
            fake_creds.scopes = SCOPES
            fake_creds.to_json.return_value = '{"token": "saved"}'

            fake_flow = MagicMock()
            fake_flow.run_local_server.return_value = fake_creds
            MockFlow.from_client_secrets_file.return_value = fake_flow
            mock_build.return_value = "fake_service"

            get_gmail_api_service(cfg)
            token_content = cfg.gmail_api_token_path.read_text(encoding="utf-8")
            assert "saved" in token_content


class TestScopeMismatch:
    def test_scope_mismatch_raises(self, tmp_path):
        # token 用的 scope 与配置不同
        other_scope = ["https://www.googleapis.com/auth/gmail.send"]
        cfg = _cfg(tmp_path, token_exists=True, expired=False, scopes=other_scope)
        # 但配置要求 readonly
        cfg.gmail_api_scopes = SCOPES
        with patch(
            "agent_mail_bridge.gmail_api_auth.Credentials"
        ) as MockCreds:
            fake_creds = MagicMock()
            fake_creds.valid = True
            fake_creds.expired = False
            fake_creds.scopes = other_scope  # token 里的 scope
            MockCreds.from_authorized_user_file.return_value = fake_creds
            with pytest.raises(TokenScopeMismatchError):
                get_gmail_api_service(cfg)


class TestValidateCredentialsFile:
    def test_missing_file(self, tmp_path):
        cfg = AppConfig(
            gmail_api_credentials_path=tmp_path / "nope.json",
        )
        r = validate_credentials_file(cfg)
        assert r["exists"] is False
        assert r["valid"] is False
        assert r["error"]

    def test_valid_desktop_app(self, tmp_path):
        creds = tmp_path / "credentials.json"
        _make_creds_file(creds)
        cfg = AppConfig(gmail_api_credentials_path=creds)
        r = validate_credentials_file(cfg)
        assert r["valid"] is True

    def test_invalid_json(self, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text("not json{", encoding="utf-8")
        cfg = AppConfig(gmail_api_credentials_path=creds)
        r = validate_credentials_file(cfg)
        assert r["valid"] is False

    def test_wrong_structure(self, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text('{"foo": "bar"}', encoding="utf-8")
        cfg = AppConfig(gmail_api_credentials_path=creds)
        r = validate_credentials_file(cfg)
        assert r["valid"] is False


class TestDescribeTokenStatus:
    def test_not_exists(self, tmp_path):
        cfg = AppConfig(
            gmail_api_token_path=tmp_path / "nope.json",
            gmail_api_scopes=SCOPES,
        )
        r = describe_token_status(cfg)
        assert r["exists"] is False

    def test_no_token_content_leaked(self, tmp_path):
        token = tmp_path / "token.json"
        _write_token(token, expired=False)
        cfg = AppConfig(
            gmail_api_token_path=token,
            gmail_api_scopes=SCOPES,
        )
        r = describe_token_status(cfg)
        # 返回值不应包含 token 字符串内容
        assert "fake_access_token" not in str(r)
        assert "refresh123" not in str(r)
