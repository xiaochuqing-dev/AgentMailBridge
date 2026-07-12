"""邮箱账号配置与三页工作区专项回归测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ServiceResult
from agent_mail_bridge.ui.account_management import (
    FIXED_SECRET_MASK,
    AccountSettingsController,
    CredentialEditor,
    GmailAccountDialog,
    QQAccountDialog,
)
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.setup_wizard import SetupWizard


@pytest.fixture(scope="module")
def account_qt_app():
    return QApplication.instance() or QApplication([])


def _all_text(widget) -> str:
    children = widget.findChildren(QLabel) + widget.findChildren(QPushButton)
    return "\n".join(child.text() for child in children)


def test_gmail_api_and_imap_use_exclusive_dedicated_pages(account_qt_app, tmp_cfg):
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    dialog.show()
    account_qt_app.processEvents()

    dialog._select_backend("gmail_api")
    assert dialog.gmail_stack.currentWidget() is dialog.api_page
    assert dialog.api_page.isVisible()
    assert not dialog.imap_page.isVisible()
    assert "credentials.json" in _all_text(dialog.api_page)
    assert "应用专用密码" not in _all_text(dialog.api_page)

    dialog._select_backend("imap")
    assert dialog.gmail_stack.currentWidget() is dialog.imap_page
    assert dialog.imap_page.isVisible()
    assert not dialog.api_page.isVisible()
    assert "Gmail IMAP 应用专用密码（Google 生成）" in _all_text(dialog.imap_page)
    assert "credentials.json" not in _all_text(dialog.imap_page)
    assert "OAuth" not in _all_text(dialog.imap_page)
    dialog.close()


@pytest.mark.parametrize("backend, expected_index", [("gmail_api", 0), ("imap", 1)])
def test_existing_gmail_account_opens_matching_editor(tmp_cfg, backend, expected_index):
    tmp_cfg.gmail_receive_backend = backend
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    assert dialog.gmail_stack.currentIndex() == expected_index


def test_switching_backend_never_deletes_existing_auth_material(tmp_cfg, monkeypatch):
    service = ApplicationService(tmp_cfg)
    deleted: list[str] = []
    monkeypatch.setattr(
        service,
        "delete_credential",
        lambda name: deleted.append(name) or ServiceResult(OperationStatus.SUCCESS),
    )
    dialog = GmailAccountDialog(service)
    dialog._select_backend("imap")
    dialog._select_backend("gmail_api")
    assert deleted == []
    assert service.cfg.gmail_api_token_path.exists() is False


def test_configured_credential_uses_fixed_mask_without_secret(account_qt_app):
    first = CredentialEditor("测试凭据", "说明", configured=True)
    second = CredentialEditor("测试凭据", "说明", configured=True)
    first.show()
    account_qt_app.processEvents()
    assert first.mask_label.text() == FIXED_SECRET_MASK
    assert second.mask_label.text() == FIXED_SECRET_MASK
    assert first.secret_edit.text() == ""
    assert "✓ 已配置" == first.status_label.text()
    first.close()


def test_gmail_save_failure_rolls_back_new_secret(tmp_cfg, monkeypatch):
    service = ApplicationService(tmp_cfg)
    original = tmp_cfg.gmail_app_password

    def fail_save(_values):
        raise OSError("只读")

    monkeypatch.setattr("agent_mail_bridge.ui.account_management.save_env_values", fail_save)
    result = AccountSettingsController(service).save_gmail(
        "new@gmail.com", "imap", "replacement-secret"
    )
    assert not result.ok
    assert service.cfg.gmail_app_password == original


def test_qq_and_gmail_credentials_are_independent(tmp_cfg):
    gmail = GmailAccountDialog(ApplicationService(tmp_cfg))
    qq = QQAccountDialog(ApplicationService(tmp_cfg))
    assert "QQ SMTP" not in _all_text(gmail)
    assert "Gmail IMAP" not in _all_text(qq)
    assert gmail.imap_credential.secret_edit is not qq.qq_credential.secret_edit


@pytest.mark.parametrize(
    "answer, expected_deleted",
    [(QMessageBox.StandardButton.No, False), (QMessageBox.StandardButton.Yes, True)],
)
def test_qq_credential_delete_requires_confirmation(
    tmp_cfg, monkeypatch, answer, expected_deleted
):
    service = ApplicationService(tmp_cfg)
    calls: list[str] = []
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QMessageBox.question",
        lambda *_args, **_kwargs: answer,
    )
    monkeypatch.setattr(
        service,
        "delete_credential",
        lambda name: calls.append(name) or ServiceResult(OperationStatus.SUCCESS, message="已删除"),
    )
    dialog = QQAccountDialog(service)
    dialog.delete_credential()
    assert bool(calls) is expected_deleted
    assert dialog.qq_credential.configured is (not expected_deleted)


def test_oauth_json_is_selected_and_imported_to_controlled_path(tmp_cfg, tmp_path, monkeypatch):
    source = tmp_path / "selected.json"
    source.write_text(
        '{"installed":{"client_id":"id","client_secret":"secret"}}',
        encoding="utf-8",
    )
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )
    dialog.import_oauth_json()
    assert tmp_cfg.gmail_api_credentials_path.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert "安全导入" in dialog.result_label.text()


def test_invalid_oauth_json_is_rejected(tmp_cfg, tmp_path, monkeypatch):
    source = tmp_path / "invalid.json"
    source.write_text("{}", encoding="utf-8")
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )
    dialog.import_oauth_json()
    assert not tmp_cfg.gmail_api_credentials_path.exists()
    assert "缺少" in dialog.result_label.text()


def test_oauth_import_cancel_has_visible_feedback(tmp_cfg, monkeypatch):
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: ("", ""),
    )
    dialog.import_oauth_json()
    assert dialog.result_label.text() == "已取消导入"


def test_oauth_replace_keeps_token_and_requires_confirmation(tmp_cfg, tmp_path, monkeypatch):
    tmp_cfg.gmail_api_credentials_path.write_text(
        '{"installed":{"client_id":"old","client_secret":"old-secret"}}', encoding="utf-8"
    )
    tmp_cfg.gmail_api_token_path.write_text("token remains", encoding="utf-8")
    source = tmp_path / "new.json"
    source.write_text(
        '{"installed":{"client_id":"new","client_secret":"new-secret"}}', encoding="utf-8"
    )
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    dialog.import_oauth_json()
    assert '"new"' in tmp_cfg.gmail_api_credentials_path.read_text(encoding="utf-8")
    assert tmp_cfg.gmail_api_token_path.read_text(encoding="utf-8") == "token remains"
    assert "token 已保留" in dialog.result_label.text()


def test_main_workspace_has_only_two_primary_tabs(account_qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    account_qt_app.processEvents()
    assert list(window.tab_buttons) == ["inbox", "send"]
    assert [button.text() for button in window.tab_buttons.values()] == ["收件", "发件"]
    assert list(window.nav_buttons) == ["history", "files_data", "settings", "about"]
    assert window.page_stack.currentWidget() is window.pages["inbox"]
    assert "basic" not in window.pages
    assert "设置发件身份" not in _all_text(window)
    window.request_quit()


def test_add_button_and_account_cards_are_the_only_account_routes(
    account_qt_app, tmp_cfg, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.open_add_account_dialog",
        lambda *_args: calls.append("add") or False,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.open_account_dialog",
        lambda _service, account_type, _parent: calls.append(account_type) or False,
    )
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.add_account_button.click()
    window.gmail_card.clicked.emit()
    window.qq_card.clicked.emit()
    assert calls == ["add", "gmail", "qq"]
    window.request_quit()


def test_advanced_settings_contains_no_account_auth_fields(account_qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    advanced_text = _all_text(window.pages["advanced"])
    assert "QQ SMTP 授权码" not in advanced_text
    assert "导入 OAuth JSON" not in advanced_text
    assert "删除 QQ 凭据" not in advanced_text
    assert "运行全部连接诊断" in advanced_text
    assert "配置与迁移" in advanced_text
    window.request_quit()


def test_setup_wizard_reuses_account_dialog_entry(tmp_cfg, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "agent_mail_bridge.ui.setup_wizard.open_account_dialog",
        lambda _service, account_type, _parent: calls.append(account_type) or True,
    )
    wizard = SetupWizard(tmp_cfg)
    wizard.configure("gmail")
    assert calls == ["gmail"]
    assert not hasattr(wizard, "data_root")
    assert not hasattr(wizard, "backend")
