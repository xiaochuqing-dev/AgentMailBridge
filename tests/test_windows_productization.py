"""Windows 产品化阶段的路径、迁移、版本与凭据安全测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_mail_bridge import __version__
from agent_mail_bridge.credentials import (
    CredentialError,
    CredentialService,
    MemoryCredentialBackend,
    QQ_SMTP_SECRET,
)
from agent_mail_bridge.mcp_server import SERVER_VERSION
from agent_mail_bridge.oauth_storage import OAuthImportError, import_oauth_credentials
from agent_mail_bridge.runtime_paths import discover_runtime_paths
from agent_mail_bridge.desktop_runtime import StartupManager
from agent_mail_bridge.mcp_client_config import generic_mcp_json, mcp_launch
from agent_mail_bridge.ui.settings_store import import_legacy_env, save_env_values


def test_runtime_paths_source_mode_keep_developer_layout(tmp_path: Path):
    module = tmp_path / "源码 项目" / "agent_mail_bridge" / "runtime_paths.py"
    paths = discover_runtime_paths(
        frozen=False,
        module_file=module,
        executable=tmp_path / "Python" / "python.exe",
        environ={"LOCALAPPDATA": str(tmp_path / "本地 数据")},
    )

    assert paths.source_root == module.parent.parent
    assert paths.install_root == paths.source_root
    assert paths.config_file == paths.source_root / ".env"
    assert paths.resource_root == paths.source_root / "agent_mail_bridge" / "resources"


def test_runtime_paths_frozen_separate_install_and_user_data(tmp_path: Path):
    install = tmp_path / "Program Files 中文" / "AgentMailBridge"
    bundle = install / "_internal"
    resources = bundle / "agent_mail_bridge" / "resources"
    resources.mkdir(parents=True)
    paths = discover_runtime_paths(
        frozen=True,
        executable=install / "AgentMailBridge.exe",
        bundle_root=bundle,
        module_file=tmp_path / "src" / "agent_mail_bridge" / "runtime_paths.py",
        environ={"LOCALAPPDATA": str(tmp_path / "用户 空间")},
    )

    assert paths.install_root == install
    assert paths.resource_root == resources
    assert paths.user_config_root.parent != install
    assert paths.oauth_root.parent == paths.data_root.parent
    assert paths.config_file == paths.user_config_root / ".env"


def test_regular_config_save_never_writes_nonempty_secret(tmp_path: Path):
    target = tmp_path / ".env"
    save_env_values(
        {"QQ_EMAIL": "user@qq.com", "QQ_AUTH_CODE": "must-not-be-written"},
        target,
    )
    content = target.read_text(encoding="utf-8")
    assert "QQ_EMAIL" in content
    assert "must-not-be-written" not in content
    assert "QQ_AUTH_CODE" not in content


def test_credential_update_verification_failure_restores_previous_value():
    class CorruptingBackend(MemoryCredentialBackend):
        corrupt = False

        def read(self, name: str) -> str | None:
            value = super().read(name)
            return "corrupt" if self.corrupt and value == "new" else value

        def write(self, name: str, value: str) -> None:
            super().write(name, value)
            self.corrupt = value == "new"

    backend = CorruptingBackend({QQ_SMTP_SECRET: "old"})
    with pytest.raises(CredentialError, match="验证失败"):
        CredentialService(backend).set(QQ_SMTP_SECRET, "new")
    backend.corrupt = False
    assert backend.read(QQ_SMTP_SECRET) == "old"


def test_explicit_legacy_import_moves_config_and_secrets(tmp_path: Path):
    source = tmp_path / "旧 配置.env"
    target = tmp_path / "新配置" / ".env"
    source.write_text(
        "GMAIL_ADDRESS=user@example.com\nQQ_AUTH_CODE=legacy-secret\n",
        encoding="utf-8",
    )
    service = CredentialService(MemoryCredentialBackend())
    result = import_legacy_env(source, destination=target, credential_service=service)

    assert result.destination == target
    assert service.get(QQ_SMTP_SECRET) == "legacy-secret"
    assert "legacy-secret" not in source.read_text(encoding="utf-8")
    assert "legacy-secret" not in target.read_text(encoding="utf-8")
    assert "GMAIL_ADDRESS" in target.read_text(encoding="utf-8")


def test_legacy_import_failure_rolls_back_target_and_credentials(tmp_path: Path):
    class FailingBackend(MemoryCredentialBackend):
        def write(self, name: str, value: str) -> None:
            raise CredentialError("模拟失败")

    source = tmp_path / "old.env"
    target = tmp_path / "new.env"
    source.write_text("QQ_AUTH_CODE=legacy-secret\n", encoding="utf-8")
    with pytest.raises(CredentialError):
        import_legacy_env(
            source,
            destination=target,
            credential_service=CredentialService(FailingBackend()),
        )
    assert not target.exists()
    assert "legacy-secret" in source.read_text(encoding="utf-8")


def test_oauth_credentials_are_validated_and_copied_atomically(tmp_path: Path):
    source = tmp_path / "credentials.json"
    target = tmp_path / "OAuth" / "credentials.json"
    source.write_text(
        '{"installed":{"client_id":"id","client_secret":"secret"}}',
        encoding="utf-8",
    )
    assert import_oauth_credentials(source, destination=target) == target
    assert target.is_file()
    with pytest.raises(FileExistsError):
        import_oauth_credentials(source, destination=target)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    with pytest.raises(OAuthImportError):
        import_oauth_credentials(invalid, destination=tmp_path / "other.json")


def test_product_version_is_shared_with_mcp():
    assert __version__ == "0.9.0"
    assert SERVER_VERSION == __version__


def test_startup_command_supports_source_and_frozen(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.executable", str(tmp_path / "有 空格" / "AgentMailBridge.exe"))
    monkeypatch.setattr("sys.frozen", True, raising=False)
    frozen_command = StartupManager.command()
    assert "AgentMailBridge.exe" in frozen_command
    assert "--background" in frozen_command
    assert "-m" not in frozen_command

    monkeypatch.delattr("sys.frozen", raising=False)
    source_command = StartupManager.command()
    assert "agent_mail_bridge.gui" in source_command
    assert "-m" in source_command


def test_mcp_client_config_uses_internal_exe_when_frozen(monkeypatch, tmp_path: Path):
    from agent_mail_bridge import mcp_client_config

    frozen_paths = discover_runtime_paths(
        frozen=True,
        executable=tmp_path / "安装 目录" / "AgentMailBridge.exe",
        bundle_root=tmp_path / "安装 目录" / "_internal",
        environ={"LOCALAPPDATA": str(tmp_path / "local")},
    )
    monkeypatch.setattr(mcp_client_config, "get_runtime_paths", lambda: frozen_paths)
    command, args = mcp_launch()
    assert command.endswith("AgentMailBridgeMCP.exe")
    assert args == []
    rendered = generic_mcp_json()
    assert "AgentMailBridgeMCP.exe" in rendered
    assert "安装 目录" in rendered


def test_installer_desktop_shortcut_targets_only_the_gui_exe():
    installer = (
        Path(__file__).resolve().parents[1]
        / "packaging" / "windows" / "AgentMailBridge.iss"
    ).read_text(encoding="utf-8")
    assert "在桌面创建 AgentMailBridge 快捷方式（仅主程序）" in installer
    assert 'Name: "{autodesktop}\\AgentMailBridge"' in installer
    assert 'Filename: "{app}\\{#MyAppExeName}"' in installer
    icon_section = installer.split("[Icons]", 1)[1].split("[Run]", 1)[0]
    assert "AgentMailBridgeMCP.exe" not in icon_section
