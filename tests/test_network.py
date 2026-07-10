"""network.py 连接工厂与诊断原语测试（全部 mock，不真实连接 Gmail）。

覆盖：
- direct 模式调用 imaplib.IMAP4_SSL
- socks5 模式调用 SocksIMAP4SSL，端口不可达抛 ProxyConnectError
- auto：direct 成功则不试 socks5
- auto：direct 失败后尝试 socks5
- auto：两者都失败抛异常且保留 direct_error / socks5_error
- 诊断原语 mock 行为
"""

from __future__ import annotations

import socket
import ssl
from unittest.mock import MagicMock, patch

import pytest

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge import network as net_mod
from agent_mail_bridge.network import (
    DIRECT_CONNECT_FAILED,
    PROXY_PORT_UNAVAILABLE,
    SOCKS5_CONNECT_FAILED,
    GmailAuthError,
    GmailTlsError,
    NetworkConnectError,
    NetworkConfigError,
    ProxyConnectError,
    SocksIMAP4SSL,
    create_direct_imap_client,
    create_gmail_imap_client,
    create_socks5_imap_client,
    login_imap_client,
    probe_socks5_port,
)


def _cfg(**kwargs) -> AppConfig:
    base = dict(
        gmail_address="test@gmail.com",
        gmail_app_password="testpassword1234",
        gmail_imap_host="imap.gmail.com",
        gmail_imap_port=993,
        gmail_network_mode="auto",
        gmail_connect_timeout=5,
        gmail_socks5_host="127.0.0.1",
        gmail_socks5_port=10808,
        gmail_socks5_remote_dns=True,
    )
    base.update(kwargs)
    return AppConfig(**base)


# ============================================================
# direct 模式
# ============================================================

class TestDirect:
    def test_direct_calls_imap4_ssl(self):
        cfg = _cfg(gmail_network_mode="direct")
        with patch("agent_mail_bridge.network.imaplib.IMAP4_SSL") as mock_cls:
            mock_cls.return_value = MagicMock(name="imap_client")
            client = create_direct_imap_client(cfg)
            assert mock_cls.called
            assert client is mock_cls.return_value

    def test_direct_failure_raises_network_connect_error(self):
        cfg = _cfg(gmail_network_mode="direct")
        with patch("agent_mail_bridge.network.imaplib.IMAP4_SSL",
                   side_effect=socket.timeout("timed out")):
            with pytest.raises(NetworkConnectError) as ei:
                create_direct_imap_client(cfg)
            assert ei.value.code in (DIRECT_CONNECT_FAILED, "TIMEOUT")

    def test_direct_ssl_error_raises_tls(self):
        cfg = _cfg(gmail_network_mode="direct")
        with patch("agent_mail_bridge.network.imaplib.IMAP4_SSL",
                   side_effect=ssl.SSLError("cert verify failed")):
            with pytest.raises(GmailTlsError):
                create_direct_imap_client(cfg)


# ============================================================
# socks5 模式
# ============================================================

class TestSocks5:
    def test_socks5_port_unreachable_raises_proxy_error(self):
        cfg = _cfg(gmail_network_mode="socks5")
        # 模拟端口不可达
        with patch("agent_mail_bridge.network.probe_socks5_port",
                   return_value={"ok": False, "error": "Connection refused"}):
            with pytest.raises(ProxyConnectError) as ei:
                create_socks5_imap_client(cfg)
            assert ei.value.code == PROXY_PORT_UNAVAILABLE

    def test_socks5_missing_config_raises(self):
        cfg = _cfg(gmail_network_mode="socks5",
                   gmail_socks5_host="", gmail_socks5_port=0)
        with pytest.raises(NetworkConfigError):
            create_socks5_imap_client(cfg)

    def test_socks5_calls_socksimap_class(self):
        cfg = _cfg(gmail_network_mode="socks5")
        fake_client = MagicMock(name="socks_client")
        with patch("agent_mail_bridge.network.probe_socks5_port",
                   return_value={"ok": True}):
            with patch.object(net_mod, "SocksIMAP4SSL",
                              return_value=fake_client) as mock_cls:
                client = create_socks5_imap_client(cfg)
                assert mock_cls.called
                # 校验传给 SocksIMAP4SSL 的代理参数
                call_kwargs = mock_cls.call_args.kwargs
                assert call_kwargs["proxy_host"] == "127.0.0.1"
                assert call_kwargs["proxy_port"] == 10808
                assert call_kwargs["rdns"] is True
                assert client is fake_client


# ============================================================
# auto 模式
# ============================================================

class TestAuto:
    def test_auto_direct_success_no_socks5_attempt(self):
        """direct 成功则不再尝试 socks5。"""
        cfg = _cfg(gmail_network_mode="auto")
        with patch("agent_mail_bridge.network.create_direct_imap_client",
                   return_value=MagicMock()) as mock_direct:
            with patch("agent_mail_bridge.network.create_socks5_imap_client",
                       return_value=MagicMock()) as mock_socks:
                client = create_gmail_imap_client(cfg)
                assert mock_direct.called
                mock_socks.assert_not_called()
                assert client is mock_direct.return_value

    def test_auto_direct_fail_then_socks5_success(self):
        """direct 失败后尝试 socks5 且成功。"""
        cfg = _cfg(gmail_network_mode="auto")
        socks_client = MagicMock(name="socks_client")
        with patch("agent_mail_bridge.network.create_direct_imap_client",
                   side_effect=NetworkConnectError("direct fail")):
            with patch("agent_mail_bridge.network.create_socks5_imap_client",
                       return_value=socks_client):
                client = create_gmail_imap_client(cfg)
                assert client is socks_client

    def test_auto_both_fail_preserves_both_errors(self):
        """两者都失败抛异常且保留 direct_error / socks5_error。"""
        cfg = _cfg(gmail_network_mode="auto")
        direct_err = NetworkConnectError("direct fail")
        socks_err = ProxyConnectError("socks5 fail")
        with patch("agent_mail_bridge.network.create_direct_imap_client",
                   side_effect=direct_err):
            with patch("agent_mail_bridge.network.create_socks5_imap_client",
                       side_effect=socks_err):
                with pytest.raises(NetworkConnectError) as ei:
                    create_gmail_imap_client(cfg)
                # message 含两段失败原因
                assert "direct" in str(ei.value).lower()
                assert "socks5" in str(ei.value).lower()
                # 附属性：保留两次失败原因
                assert getattr(ei.value, "direct_error", None) is direct_err
                assert getattr(ei.value, "socks5_error", None) is socks_err
                assert ei.value.__cause__ is socks_err

    def test_auto_direct_fail_no_socks5_config(self):
        """direct 失败且未配置 socks5 -> 抛异常说明无 socks5。"""
        cfg = _cfg(gmail_network_mode="auto",
                   gmail_socks5_host="", gmail_socks5_port=0)
        with patch("agent_mail_bridge.network.create_direct_imap_client",
                   side_effect=NetworkConnectError("direct fail")):
            with pytest.raises(NetworkConnectError) as ei:
                create_gmail_imap_client(cfg)
            assert "socks5" in str(ei.value).lower() or "direct" in str(ei.value).lower()


# ============================================================
# login_imap_client
# ============================================================

class TestLogin:
    def test_auth_failure_wrapped(self):
        client = MagicMock()
        client.login.side_effect = Exception("AUTHENTICATIONFAILED")
        with pytest.raises(GmailAuthError):
            login_imap_client(client, "a@b.com", "pwd")

    def test_login_success(self):
        client = MagicMock()
        login_imap_client(client, "a@b.com", "pwd")
        client.login.assert_called_once_with("a@b.com", "pwd")


# ============================================================
# 诊断原语
# ============================================================

class TestPrimitives:
    def test_probe_socks5_port_ok(self):
        with patch("agent_mail_bridge.network.socket.create_connection") as m:
            m.return_value.__enter__.return_value = MagicMock()
            r = probe_socks5_port("127.0.0.1", 10808, 2)
            assert r["ok"] is True

    def test_probe_socks5_port_fail(self):
        with patch("agent_mail_bridge.network.socket.create_connection",
                   side_effect=ConnectionRefusedError("refused")):
            r = probe_socks5_port("127.0.0.1", 10808, 2)
            assert r["ok"] is False
            assert "refused" in r["error"]

    def test_is_pytsocks_installed(self):
        # 测试环境已装 PySocks
        assert net_mod.is_pytsocks_installed() is True
