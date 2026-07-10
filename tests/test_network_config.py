"""网络适配层配置解析测试。

覆盖：
- direct / socks5 / auto 模式解析
- 非法 mode 报错
- 非法 port 报错
- remote_dns true/false/1/0/yes/no 解析
- socks5 模式缺 host/port 报错
"""

from __future__ import annotations

import os

import pytest

from agent_mail_bridge import config as cfg_mod
from agent_mail_bridge.config import (
    AppConfig,
    ConfigError,
    load_config,
    require_gmail_network_config,
)


def _set_env(monkeypatch, **kwargs):
    """设置网络相关环境变量。"""
    # 清掉可能干扰的变量，再写入
    keys = [
        "GMAIL_NETWORK_MODE", "GMAIL_CONNECT_TIMEOUT",
        "GMAIL_SOCKS5_HOST", "GMAIL_SOCKS5_PORT", "GMAIL_SOCKS5_REMOTE_DNS",
        "QQ_SMTP_NETWORK_MODE", "QQ_SMTP_CONNECT_TIMEOUT",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    for k, v in kwargs.items():
        monkeypatch.setenv(k, v)


class TestNetworkMode:
    def test_default_is_auto(self, monkeypatch):
        _set_env(monkeypatch)
        cfg = load_config()
        assert cfg.gmail_network_mode == "auto"

    def test_direct(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_NETWORK_MODE="direct")
        assert load_config().gmail_network_mode == "direct"

    def test_socks5(self, monkeypatch):
        _set_env(
            monkeypatch,
            GMAIL_NETWORK_MODE="socks5",
            GMAIL_APP_PASSWORD="test-app-password",
        )
        assert load_config().gmail_network_mode == "socks5"

    def test_auto(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_NETWORK_MODE="auto")
        assert load_config().gmail_network_mode == "auto"

    def test_case_insensitive(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_NETWORK_MODE="SOCKS5")
        assert load_config().gmail_network_mode == "socks5"

    def test_invalid_mode_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_NETWORK_MODE="vpn")
        with pytest.raises(ConfigError):
            load_config()

    def test_empty_mode_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_NETWORK_MODE="weird-thing")
        with pytest.raises(ConfigError):
            load_config()


class TestPortParsing:
    def test_valid_port(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_SOCKS5_PORT="7890")
        assert load_config().gmail_socks5_port == 7890

    def test_invalid_port_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_SOCKS5_PORT="not-a-port")
        with pytest.raises(ConfigError):
            load_config()

    def test_out_of_range_port_raises(self, monkeypatch):
        _set_env(monkeypatch, GMAIL_SOCKS5_PORT="99999")
        with pytest.raises(ConfigError):
            load_config()

    def test_default_port_when_unset(self, monkeypatch):
        _set_env(monkeypatch)
        assert load_config().gmail_socks5_port == 10808


class TestRemoteDns:
    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
    ])
    def test_parse_variants(self, monkeypatch, raw, expected):
        _set_env(monkeypatch, GMAIL_SOCKS5_REMOTE_DNS=raw)
        assert load_config().gmail_socks5_remote_dns is expected

    def test_default_true(self, monkeypatch):
        _set_env(monkeypatch)
        assert load_config().gmail_socks5_remote_dns is True


class TestRequireGmailNetworkConfig:
    def test_direct_ok(self):
        cfg = AppConfig(gmail_network_mode="direct")
        # direct 模式无需 socks5 配置，不报错
        require_gmail_network_config(cfg)

    def test_socks5_missing_host_raises(self):
        cfg = AppConfig(gmail_network_mode="socks5",
                        gmail_socks5_host="", gmail_socks5_port=10808)
        with pytest.raises(ConfigError):
            require_gmail_network_config(cfg)

    def test_socks5_missing_port_raises(self):
        cfg = AppConfig(gmail_network_mode="socks5",
                        gmail_socks5_host="127.0.0.1", gmail_socks5_port=0)
        with pytest.raises(ConfigError):
            require_gmail_network_config(cfg)

    def test_socks5_ok(self):
        cfg = AppConfig(gmail_network_mode="socks5",
                        gmail_socks5_host="127.0.0.1", gmail_socks5_port=10808)
        require_gmail_network_config(cfg)

    def test_auto_without_socks5_ok(self):
        # auto 模式不强制 socks5 配置
        cfg = AppConfig(gmail_network_mode="auto",
                        gmail_socks5_host="", gmail_socks5_port=0)
        require_gmail_network_config(cfg)


class TestMaskNoSecretLeak:
    def test_mask_has_network_fields_no_password(self, monkeypatch):
        _set_env(
            monkeypatch,
            GMAIL_NETWORK_MODE="socks5",
            GMAIL_APP_PASSWORD="test-app-password",
        )
        cfg = load_config()
        masked = cfg.mask()
        assert "gmail_network_mode" in masked
        assert masked["gmail_network_mode"] == "socks5"
        assert masked["gmail_socks5_host"] == "127.0.0.1"
        # 应用专用密码必须脱敏
        assert "*" in masked["gmail_app_password"]
