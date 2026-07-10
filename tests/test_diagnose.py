"""诊断命令测试（全部 mock，不真实连接）。

覆盖：
- diagnose-gmail socks5 端口失败时输出可读原因
- diagnose-gmail 认证失败时输出认证类错误
- 不输出真实密码
- diagnose-network 各步骤可运行
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.diagnose import run_diagnose_gmail, run_diagnose_network


def _cfg(**kwargs) -> AppConfig:
    base = dict(
        gmail_address="test@gmail.com",
        gmail_app_password="secret-password-1234",
        gmail_imap_host="imap.gmail.com",
        gmail_imap_port=993,
        gmail_network_mode="socks5",
        gmail_connect_timeout=5,
        gmail_socks5_host="127.0.0.1",
        gmail_socks5_port=10808,
        gmail_socks5_remote_dns=True,
        qq_smtp_host="smtp.qq.com",
        qq_smtp_port=465,
        qq_smtp_connect_timeout=5,
    )
    base.update(kwargs)
    return AppConfig(**base)


@pytest.fixture
def capture(capsys):
    """运行后返回 (rc, out)。"""
    results = []

    def _run(fn, cfg):
        rc = fn(cfg)
        captured = capsys.readouterr()
        results.append((rc, captured.out + captured.err))
        return results[-1]

    return _run


class TestDiagnoseGmailSocks5:
    def test_socks5_port_unreachable_readable(self, capture):
        cfg = _cfg(gmail_network_mode="socks5")
        with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                   return_value={"ok": False, "error": "Connection refused"}):
            rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 1
        # 输出含可读的失败提示
        assert "SOCKS5" in out or "10808" in out
        assert "失败" in out
        # 给出建议
        assert "代理" in out or "v2rayN" in out or "Clash" in out

    def test_no_password_in_output(self, capture):
        cfg = _cfg(gmail_network_mode="socks5",
                   gmail_app_password="secret-password-1234")
        with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                   return_value={"ok": False, "error": "refused"}):
            rc, out = capture(run_diagnose_gmail, cfg)
        # 绝不能泄露应用专用密码
        assert "secret-password-1234" not in out
        assert "secret" not in out.lower()

    def test_socks5_tls_then_auth_fail(self, capture):
        cfg = _cfg(gmail_network_mode="socks5")
        # 端口 ok -> TLS ok -> 登录失败
        with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                   return_value={"ok": True}):
            with patch("agent_mail_bridge.diagnose.probe_socks5_connect",
                       return_value={"ok": True}):
                with patch("agent_mail_bridge.diagnose.create_socks5_imap_client",
                           return_value=MagicMock()):
                    with patch("agent_mail_bridge.diagnose.login_and_logout",
                               return_value={"ok": False,
                                             "error": "AUTHENTICATIONFAILED"}):
                        rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 1
        assert "认证" in out or "应用专用密码" in out or "App Password" in out
        # 不泄露密码
        assert "secret-password-1234" not in out


class TestDiagnoseGmailDirect:
    def test_direct_success(self, capture):
        cfg = _cfg(gmail_network_mode="direct")
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": True}):
            with patch("agent_mail_bridge.diagnose.create_direct_imap_client",
                       return_value=MagicMock()):
                with patch("agent_mail_bridge.diagnose.login_and_logout",
                           return_value={"ok": True}):
                    rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 0
        assert "可用" in out

    def test_direct_tls_fail(self, capture):
        cfg = _cfg(gmail_network_mode="direct")
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": False, "error": "timeout"}):
            rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 1
        assert "失败" in out


class TestDiagnoseGmailAuto:
    def test_auto_direct_success(self, capture):
        cfg = _cfg(gmail_network_mode="auto")
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": True}):
            with patch("agent_mail_bridge.diagnose.create_direct_imap_client",
                       return_value=MagicMock()):
                with patch("agent_mail_bridge.diagnose.login_and_logout",
                           return_value={"ok": True}):
                    rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 0
        assert "direct" in out.lower()

    def test_auto_fallback_to_socks5(self, capture):
        cfg = _cfg(gmail_network_mode="auto")
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": False, "error": "refused"}):
            with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                       return_value={"ok": True}):
                with patch("agent_mail_bridge.diagnose.probe_socks5_connect",
                           return_value={"ok": True}):
                    with patch("agent_mail_bridge.diagnose.create_socks5_imap_client",
                               return_value=MagicMock()):
                        with patch("agent_mail_bridge.diagnose.login_and_logout",
                                   return_value={"ok": True}):
                            rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 0
        assert "socks5" in out.lower()

    def test_auto_no_socks5_config(self, capture):
        cfg = _cfg(gmail_network_mode="auto",
                   gmail_socks5_host="", gmail_socks5_port=0)
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": False, "error": "refused"}):
            rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 1
        assert "socks5" in out.lower() or "direct" in out.lower()


class TestDiagnoseGmailConfig:
    def test_missing_password_fails_config_step(self, capture):
        cfg = _cfg(gmail_network_mode="direct", gmail_app_password="")
        rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 1
        assert "配置" in out

    def test_missing_address_fails(self, capture):
        cfg = _cfg(gmail_network_mode="direct", gmail_address="")
        rc, out = capture(run_diagnose_gmail, cfg)
        assert rc == 1


class TestDiagnoseNetwork:
    def test_runs_all_steps(self, capture):
        cfg = _cfg()
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": True}):
            with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                       return_value={"ok": True}):
                with patch("agent_mail_bridge.diagnose.probe_socks5_connect",
                           return_value={"ok": True}):
                    with patch("agent_mail_bridge.diagnose.probe_qq_smtp_direct",
                               return_value={"ok": True}):
                        rc, out = capture(run_diagnose_network, cfg)
        assert "[1]" in out and "[6]" in out
        assert "PySocks" in out
        assert rc == 0

    def test_reports_failures(self, capture):
        cfg = _cfg()
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": False, "error": "refused"}):
            with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                       return_value={"ok": False, "error": "refused"}):
                with patch("agent_mail_bridge.diagnose.probe_socks5_connect",
                           return_value={"ok": False, "error": "x"}):
                    with patch("agent_mail_bridge.diagnose.probe_qq_smtp_direct",
                               return_value={"ok": True}):
                        rc, out = capture(run_diagnose_network, cfg)
        assert rc == 1
        assert "失败" in out

    def test_no_password_in_network_output(self, capture):
        cfg = _cfg(gmail_app_password="secret-password-1234")
        with patch("agent_mail_bridge.diagnose.probe_direct_tls_connect",
                   return_value={"ok": True}):
            with patch("agent_mail_bridge.diagnose.probe_socks5_port",
                       return_value={"ok": True}):
                with patch("agent_mail_bridge.diagnose.probe_socks5_connect",
                           return_value={"ok": True}):
                    with patch("agent_mail_bridge.diagnose.probe_qq_smtp_direct",
                               return_value={"ok": True}):
                        _rc, out = capture(run_diagnose_network, cfg)
        assert "secret-password-1234" not in out
