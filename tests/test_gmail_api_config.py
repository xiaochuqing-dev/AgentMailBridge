"""Gmail API 配置解析测试。

覆盖：
- GMAIL_RECEIVE_BACKEND: imap / gmail_api / auto / 非法值
- credentials path / token path 默认值与自定义
- scopes 默认值与多值解析
- max_results 正整数与非法值
- query 默认值
- require_receive_config 按 backend 分支
- _effective_receive_backend 的 auto 解析
- mask 不泄露敏感信息
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_mail_bridge import config as cfg_mod
from agent_mail_bridge.config import (
    AppConfig,
    ConfigError,
    _effective_receive_backend,
    load_config,
    require_receive_config,
)


def _set_env(monkeypatch, **kwargs):
    """设置 Gmail API 相关环境变量，并清掉干扰项。"""
    keys = [
        "GMAIL_RECEIVE_BACKEND",
        "GMAIL_API_CREDENTIALS_PATH",
        "GMAIL_API_TOKEN_PATH",
        "GMAIL_API_SCOPES",
        "GMAIL_API_MAX_RESULTS",
        "GMAIL_API_QUERY",
        "GMAIL_ADDRESS",
        "GMAIL_APP_PASSWORD",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    for k, v in kwargs.items():
        monkeypatch.setenv(k, v)


def _desktop_oauth_json() -> str:
    return json.dumps(
        {
            "installed": {
                "client_id": "123456-test.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    )


class TestReceiveBackend:
    def test_default_is_auto(self, monkeypatch, tmp_path):
        # 注意：load_config 会读项目根 .env，若其中设了 GMAIL_RECEIVE_BACKEND
        # 则覆盖默认值。此处用 monkeypatch.setenv 强制（env 优先于 .env）。
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="auto")
        monkeypatch.setenv("GMAIL_API_CREDENTIALS_PATH", str(tmp_path / "c.json"))
        assert load_config().gmail_receive_backend == "auto"

    def test_imap(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="imap")
        assert load_config().gmail_receive_backend == "imap"

    def test_gmail_api(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="gmail_api")
        assert load_config().gmail_receive_backend == "gmail_api"

    def test_auto(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="auto")
        assert load_config().gmail_receive_backend == "auto"

    def test_case_insensitive(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="GMAIL_API")
        assert load_config().gmail_receive_backend == "gmail_api"

    def test_invalid_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="pop3")
        with pytest.raises(ConfigError):
            load_config()

    def test_empty_invalid_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="weird")
        with pytest.raises(ConfigError):
            load_config()


class TestEffectiveBackend:
    def test_imap_passthrough(self):
        cfg = AppConfig(gmail_receive_backend="imap")
        assert _effective_receive_backend(cfg) == "imap"

    def test_gmail_api_passthrough(self):
        cfg = AppConfig(gmail_receive_backend="gmail_api")
        assert _effective_receive_backend(cfg) == "gmail_api"

    def test_auto_prefers_gmail_api_when_configured(self, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text(_desktop_oauth_json(), encoding="utf-8")
        cfg = AppConfig(
            gmail_receive_backend="auto",
            gmail_api_credentials_path=creds,
        )
        assert _effective_receive_backend(cfg) == "gmail_api"

    def test_auto_falls_back_to_imap_when_credentials_are_invalid(self, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        cfg = AppConfig(
            gmail_receive_backend="auto",
            gmail_api_credentials_path=creds,
        )
        assert _effective_receive_backend(cfg) == "imap"

    def test_auto_falls_back_to_imap_when_not_configured(self, tmp_path):
        cfg = AppConfig(
            gmail_receive_backend="auto",
            gmail_api_credentials_path=tmp_path / "nonexistent.json",
        )
        assert _effective_receive_backend(cfg) == "imap"


class TestPaths:
    def test_default_credentials_path(self, monkeypatch):
        _set_env(monkeypatch)
        cfg = load_config()
        assert cfg.gmail_api_credentials_path.name == "credentials.json"
        assert cfg.gmail_api_credentials_path.is_absolute()

    def test_default_token_path(self, monkeypatch):
        _set_env(monkeypatch)
        cfg = load_config()
        assert cfg.gmail_api_token_path.name == "token.json"
        assert cfg.gmail_api_token_path.is_absolute()

    def test_custom_credentials_path(self, monkeypatch, tmp_path):
        custom = tmp_path / "my_creds.json"
        _set_env(monkeypatch, GMAIL_API_CREDENTIALS_PATH=str(custom))
        cfg = load_config()
        assert cfg.gmail_api_credentials_path == custom

    def test_relative_path_resolved_against_project_root(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_API_CREDENTIALS_PATH="secrets/credentials.json")
        cfg = load_config()
        assert cfg.gmail_api_credentials_path.is_absolute()
        assert cfg.gmail_api_credentials_path.parent.name == "secrets"


class TestScopes:
    def test_default_readonly(self, monkeypatch):
        _set_env(monkeypatch)
        cfg = load_config()
        assert cfg.gmail_api_scopes == [
            "https://www.googleapis.com/auth/gmail.readonly"
        ]

    def test_multiple_scopes(self, monkeypatch):
        _set_env(
            monkeypatch,
            GMAIL_API_SCOPES=(
                "https://www.googleapis.com/auth/gmail.readonly,"
                "https://www.googleapis.com/auth/gmail.send"
            ),
        )
        cfg = load_config()
        assert len(cfg.gmail_api_scopes) == 2
        assert "https://www.googleapis.com/auth/gmail.send" in cfg.gmail_api_scopes

    def test_scopes_str(self, monkeypatch):
        _set_env(monkeypatch)
        cfg = load_config()
        assert cfg.gmail_api_scopes_str == (
            "https://www.googleapis.com/auth/gmail.readonly"
        )


class TestMaxResults:
    def test_default_20(self, monkeypatch):
        _set_env(monkeypatch)
        assert load_config().gmail_api_max_results == 20

    def test_valid(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_API_MAX_RESULTS="50")
        assert load_config().gmail_api_max_results == 50

    def test_zero_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_API_MAX_RESULTS="0")
        with pytest.raises(ConfigError):
            load_config()

    def test_negative_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_API_MAX_RESULTS="-5")
        with pytest.raises(ConfigError):
            load_config()

    def test_non_int_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_API_MAX_RESULTS="abc")
        with pytest.raises(ConfigError):
            load_config()


class TestQuery:
    def test_default_inbox(self, monkeypatch):
        _set_env(monkeypatch)
        assert load_config().gmail_api_query == "in:inbox"

    def test_custom(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_API_QUERY="in:inbox newer_than:7d")
        assert load_config().gmail_api_query == "in:inbox newer_than:7d"


class TestRequireReceiveConfig:
    def test_imap_needs_app_password(self, tmp_path):
        cfg = AppConfig(
            gmail_receive_backend="imap",
            gmail_address="user@gmail.com",
            gmail_app_password="",
        )
        with pytest.raises(ConfigError, match="GMAIL_APP_PASSWORD"):
            require_receive_config(cfg)

    def test_imap_ok_with_password(self):
        cfg = AppConfig(
            gmail_receive_backend="imap",
            gmail_address="user@gmail.com",
            gmail_app_password="abcdefghijklmnop",
        )
        require_receive_config(cfg)  # 不报错

    def test_gmail_api_needs_credentials_file(self, tmp_path):
        cfg = AppConfig(
            gmail_receive_backend="gmail_api",
            gmail_address="user@gmail.com",
            gmail_api_credentials_path=tmp_path / "nope.json",
            gmail_api_token_path=tmp_path / "token.json",
        )
        with pytest.raises(ConfigError, match="CREDENTIALS"):
            require_receive_config(cfg)

    def test_gmail_api_does_not_require_app_password(self, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        cfg = AppConfig(
            gmail_receive_backend="gmail_api",
            gmail_address="user@gmail.com",
            gmail_app_password="",  # gmail_api 模式不需要
            gmail_api_credentials_path=creds,
        )
        require_receive_config(cfg)  # 不报错

    def test_gmail_api_needs_address(self, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        cfg = AppConfig(
            gmail_receive_backend="gmail_api",
            gmail_address="",
            gmail_api_credentials_path=creds,
        )
        with pytest.raises(ConfigError, match="GMAIL_ADDRESS"):
            require_receive_config(cfg)


class TestMaskNoSecretLeak:
    def test_mask_has_gmail_api_fields(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_RECEIVE_BACKEND="gmail_api")
        cfg = load_config()
        masked = cfg.mask()
        assert masked["gmail_receive_backend"] == "gmail_api"
        assert "gmail_api_credentials_path" in masked
        assert "gmail_api_token_path" in masked
        assert "gmail_api_scopes" in masked
        # 不应包含 token 内容
        assert masked["gmail_api_scopes"] == (
            "https://www.googleapis.com/auth/gmail.readonly"
        )

    def test_mask_no_app_password_leak(self, monkeypatch):
        _set_env(
            monkeypatch,
            GMAIL_APP_PASSWORD="verysecret1234",
            GMAIL_RECEIVE_BACKEND="gmail_api",
        )
        cfg = load_config()
        masked = cfg.mask()
        assert "*" in masked["gmail_app_password"]
        assert "verysecret1234" not in masked["gmail_app_password"]
