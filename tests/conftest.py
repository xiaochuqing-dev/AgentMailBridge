"""测试共用夹具：提供一个临时数据目录的 AppConfig。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中（直接用 pytest 运行时）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection, init_db
from agent_mail_bridge.logging_setup import setup_logging
from agent_mail_bridge.storage import ensure_data_dirs


@pytest.fixture(autouse=True)
def isolate_local_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """所有测试禁用真实 .env、OAuth 文件和用户数据目录。"""
    sensitive_names = (
        "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "QQ_EMAIL", "QQ_AUTH_CODE",
        "OWNER_GMAIL", "GMAIL_API_SCOPES", "ALLOWED_SEND_ROOTS",
    )
    for name in sensitive_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MAIL_BRIDGE_DISABLE_DOTENV", "1")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "isolated_data"))
    monkeypatch.setenv(
        "GMAIL_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json")
    )
    monkeypatch.setenv("GMAIL_API_TOKEN_PATH", str(tmp_path / "token.json"))


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> AppConfig:
    """提供一个指向临时目录的 AppConfig，并完成初始化。"""
    cfg = AppConfig(
        gmail_address="test@gmail.com",
        gmail_app_password="testpassword1234",
        qq_email="test@qq.com",
        qq_auth_code="testauthcode1234",
        owner_gmail="owner@gmail.com",
        data_root=tmp_path / "AgentMailBridgeData",
        max_attachment_mb=25,
        max_send_file_mb=25,
        log_level="DEBUG",
    )
    ensure_data_dirs(cfg)
    init_db(cfg.db_path)
    # 测试场景强制重新初始化日志，写到临时目录
    setup_logging(cfg.logs_dir, "DEBUG", force=True)
    yield cfg
    close_connection()
