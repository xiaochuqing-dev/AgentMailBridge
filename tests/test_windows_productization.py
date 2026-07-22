"""Windows 产品化阶段的路径、迁移、版本与凭据安全测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

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
from agent_mail_bridge.desktop_runtime import SingleInstanceGuard, StartupManager
from agent_mail_bridge.mcp_client_config import generic_mcp_json, mcp_launch
from agent_mail_bridge.ui.settings_store import (
    default_env_path,
    import_legacy_env,
    save_env_values,
)


def test_core_settings_store_import_does_not_require_qt() -> None:
    code = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith('PySide6'):
        raise ModuleNotFoundError('Qt intentionally unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from agent_mail_bridge.ui.settings_store import persist_receive_rule_migration
assert callable(persist_receive_rule_migration)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


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


def test_default_config_writes_are_isolated_from_project_env(tmp_path: Path):
    target = default_env_path()

    assert target == (tmp_path / "isolated.env").resolve()
    save_env_values({"GMAIL_ADDRESS": "isolated-test@gmail.com"})
    assert target.is_file()


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
        '{"installed":{"client_id":"1234567890-fake.apps.googleusercontent.com",'
        '"client_secret":"fake-client-secret-for-tests-only",'
        '"auth_uri":"https://accounts.google.com/o/oauth2/auth",'
        '"token_uri":"https://oauth2.googleapis.com/token",'
        '"redirect_uris":["http://localhost"]}}',
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
    assert __version__ == "1.4.0"
    assert SERVER_VERSION == __version__


def test_packaged_smoke_uses_single_version_source():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "packaged_smoke.py").read_text(encoding="utf-8")
    assert "from agent_mail_bridge.version import __version__" in script
    assert '!= __version__' in script


def test_windows_version_resources_match_product_version():
    root = Path(__file__).resolve().parents[1]
    for name in ("version_info.txt", "version_info_mcp.txt"):
        content = (root / "packaging" / "windows" / name).read_text(encoding="utf-8")
        assert "filevers=(1, 4, 0, 0)" in content
        assert "prodvers=(1, 4, 0, 0)" in content
        assert "u'FileVersion', u'1.4.0'" in content
        assert "u'ProductVersion', u'1.4.0'" in content


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


def test_second_instance_notifies_existing_window(tmp_path: Path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    first = SingleInstanceGuard(tmp_path / "data")
    second = SingleInstanceGuard(tmp_path / "data")
    notified: list[str] = []
    assert first.acquire()
    try:
        monkeypatch.setattr(second, "_notify_existing", lambda: notified.append("ipc"))
        monkeypatch.setattr(second, "_activate_existing_window", lambda: notified.append("window"))
        assert not second.acquire()
        assert notified == ["ipc", "window"]
    finally:
        first.release()


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
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in installer
    icon_section = installer.split("[Icons]", 1)[1].split("[Run]", 1)[0]
    assert "AgentMailBridgeMCP.exe" not in icon_section


def test_windows_dependencies_use_pyside_essentials_only():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert "PySide6-Essentials>=6.8,<7" in requirements
    assert '"PySide6-Essentials>=6.8,<7"' in pyproject
    assert "\nPySide6>=" not in requirements
    assert '"PySide6>=' not in pyproject


def test_windows_packaging_keeps_only_gmail_discovery_data():
    root = Path(__file__).resolve().parents[1]
    spec = (
        root / "packaging" / "windows" / "AgentMailBridge.spec"
    ).read_text(encoding="utf-8")
    hook = (
        root
        / "packaging"
        / "windows"
        / "hooks"
        / "hook-googleapiclient.model.py"
    ).read_text(encoding="utf-8")

    assert spec.count("hookspath=[str(HOOK_DIR)]") == 2
    assert 'includes=["documents/gmail.v1.json"]' in hook
    assert "collect_data_files" in hook
