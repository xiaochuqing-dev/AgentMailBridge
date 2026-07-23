"""邮箱账号配置与三页工作区专项回归测试。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QPoint, QRect, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.credentials import ACCOUNT_IMAP_SECRET, ACCOUNT_SMTP_SECRET
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
from agent_mail_bridge.ui.theme import build_stylesheet


@pytest.fixture(scope="module")
def account_qt_app():
    return QApplication.instance() or QApplication([])


def _all_text(widget) -> str:
    children = widget.findChildren(QLabel) + widget.findChildren(QPushButton)
    return "\n".join(child.text() for child in children)


def _rect_in(widget, ancestor) -> QRect:
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _oauth_json(client_number: str = "1234567890") -> str:
    return json.dumps(
        {
            "installed": {
                "client_id": f"{client_number}-fake.apps.googleusercontent.com",
                "client_secret": "fake-client-secret-for-tests-only",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    )


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


def test_legacy_qq_editor_refreshes_unified_account_credentials(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.save_env_values",
        lambda _values: None,
    )

    result = AccountSettingsController(service).save_qq(
        "refreshed@qq.com", "new-authorization-code"
    )

    assert result.ok
    account = next(
        item
        for item in service.list_mail_accounts().details["accounts"]
        if item["provider"] == "qq"
        and item["email_address"] == "refreshed@qq.com"
    )
    account_id = account["account_id"]
    assert service._credentials.get_for_account(
        account_id, ACCOUNT_IMAP_SECRET
    ) == "new-authorization-code"
    assert service._credentials.get_for_account(
        account_id, ACCOUNT_SMTP_SECRET
    ) == "new-authorization-code"


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
    source.write_text(_oauth_json(), encoding="utf-8")
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
    assert "Desktop app" in dialog.result_label.text()


def test_oauth_import_cancel_has_visible_feedback(tmp_cfg, monkeypatch):
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: ("", ""),
    )
    dialog.import_oauth_json()
    assert dialog.result_label.text() == "已取消导入"


def test_oauth_replace_keeps_token_and_requires_confirmation(tmp_cfg, tmp_path, monkeypatch):
    tmp_cfg.gmail_api_credentials_path.write_text(_oauth_json("1111111111"), encoding="utf-8")
    tmp_cfg.gmail_api_token_path.write_text("token remains", encoding="utf-8")
    source = tmp_path / "new.json"
    source.write_text(_oauth_json("2222222222"), encoding="utf-8")
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
    assert "2222222222-fake.apps.googleusercontent.com" in tmp_cfg.gmail_api_credentials_path.read_text(encoding="utf-8")
    assert tmp_cfg.gmail_api_token_path.read_text(encoding="utf-8") == "token remains"
    assert "token 已保留" in dialog.result_label.text()


def test_clear_oauth_token_requires_confirmation_and_keeps_credentials(
    tmp_cfg, monkeypatch
):
    tmp_cfg.gmail_api_credentials_path.write_text(_oauth_json(), encoding="utf-8")
    tmp_cfg.gmail_api_token_path.write_text("fake-token", encoding="utf-8")
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    dialog.clear_oauth_token()

    assert tmp_cfg.gmail_api_credentials_path.is_file()
    assert not tmp_cfg.gmail_api_token_path.exists()
    assert "凭据已保留" in dialog.result_label.text()


def test_oauth_authorization_keeps_qt_heartbeat_running(
    account_qt_app, tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    dialog = GmailAccountDialog(service)
    heartbeat_times: list[float] = []
    operation_times: dict[str, float] = {}

    def slow_authorize(**_kwargs) -> ServiceResult:
        operation_times["start"] = time.monotonic()
        time.sleep(0.20)
        operation_times["end"] = time.monotonic()
        return ServiceResult(OperationStatus.SUCCESS, message="授权完成")

    monkeypatch.setattr(service, "authorize_gmail_api", slow_authorize)
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: heartbeat_times.append(time.monotonic()))
    heartbeat.start()

    loop = QEventLoop()
    QTimer.singleShot(0, dialog.authorize)
    QTimer.singleShot(350, loop.quit)
    loop.exec()
    heartbeat.stop()

    assert any(
        operation_times["start"] < tick < operation_times["end"]
        for tick in heartbeat_times
    ), "OAuth 等待期间 Qt 事件循环被阻塞"


def test_oauth_account_mismatch_is_prominent_after_worker_completion(
    account_qt_app, tmp_cfg, monkeypatch
):
    tmp_cfg.gmail_address = "test@gmail.com"
    tmp_cfg.owner_gmail = "test@gmail.com"
    service = ApplicationService(tmp_cfg)
    dialog = GmailAccountDialog(service)

    class FakeSession:
        session_id = "mismatch-session"

        @staticmethod
        def cancel():
            return True

        @staticmethod
        def reopen_browser():
            return ServiceResult(OperationStatus.SUCCESS)

    session = FakeSession()
    monkeypatch.setattr(
        "agent_mail_bridge.ui.account_management.save_env_values",
        lambda _values: None,
    )
    monkeypatch.setattr(
        service,
        "create_gmail_oauth_session",
        lambda **_kwargs: session,
    )

    def mismatch_result(**_kwargs):
        return ServiceResult(
            OperationStatus.FAILED,
            error_code="account_mismatch",
            message="授权账号不匹配",
            needs_auth=True,
            details={
                "oauth_state": "FAILED",
                "title": "授权账号不匹配",
                "reason": "Google 返回的 Gmail 账号与当前配置账号不同。",
                "next_step": "请核对账号后重新授权。",
                "expected_email_masked": "t***@gmail.com",
                "actual_email_masked": "o***@gmail.com",
            },
        )

    monkeypatch.setattr(service, "authorize_gmail_api", mismatch_result)
    dialog.show()
    assert "t***@gmail.com" in dialog.oauth_expected_account.text()
    dialog.authorize()

    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(5)
    poll.timeout.connect(lambda: loop.quit() if not dialog._oauth_active else None)
    poll.start()
    QTimer.singleShot(1000, loop.quit)
    loop.exec()
    poll.stop()

    assert dialog._oauth_active is False
    assert dialog.email_edit.isEnabled()
    assert "授权账号不匹配" in dialog.oauth_phase_label.text()
    assert dialog.oauth_error_detail.isVisible()
    assert "当前配置：t***@gmail.com" in dialog.oauth_error_detail.text()
    assert "本次授权：o***@gmail.com" in dialog.oauth_error_detail.text()
    assert "Token 未保存" in dialog.oauth_error_detail.text()
    assert dialog.authorize_button.isEnabled()
    dialog.close()


def test_gmail_api_connection_test_keeps_qt_heartbeat_running(
    account_qt_app, tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    dialog = GmailAccountDialog(service)
    heartbeat_times: list[float] = []
    operation_times: dict[str, float] = {}

    def slow_diagnose() -> ServiceResult:
        operation_times["start"] = time.monotonic()
        time.sleep(0.20)
        operation_times["end"] = time.monotonic()
        return ServiceResult(OperationStatus.SUCCESS, message="连接正常")

    monkeypatch.setattr(service, "diagnose_gmail_api", slow_diagnose)
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: heartbeat_times.append(time.monotonic()))
    heartbeat.start()
    loop = QEventLoop()
    QTimer.singleShot(0, dialog.test_api)
    QTimer.singleShot(350, loop.quit)
    loop.exec()
    heartbeat.stop()

    assert any(
        operation_times["start"] < tick < operation_times["end"]
        for tick in heartbeat_times
    ), "Gmail API 连接测试阻塞了 Qt 事件循环"
    assert dialog.api_test_button.isEnabled()


def test_background_connection_test_blocks_new_oauth_session(tmp_cfg, monkeypatch):
    service = ApplicationService(tmp_cfg)
    dialog = GmailAccountDialog(service)
    created: list[str] = []
    monkeypatch.setattr(
        service,
        "create_gmail_oauth_session",
        lambda **_kwargs: created.append("oauth"),
    )
    dialog._background_active = True

    dialog.authorize()

    assert created == []
    assert dialog.result_label.text() == "已有后台任务正在运行，请稍候"
    dialog._background_active = False


def test_oauth_duplicate_click_is_ignored_and_cancel_cleans_worker(
    account_qt_app, tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    dialog = GmailAccountDialog(service)

    class FakeSession:
        session_id = "fake-session"

        def __init__(self):
            self.cancelled = threading.Event()

        def cancel(self):
            self.cancelled.set()
            return True

        def reopen_browser(self):
            return ServiceResult(OperationStatus.SUCCESS, message="已打开")

    session = FakeSession()
    calls: list[str] = []
    monkeypatch.setattr(
        service,
        "create_gmail_oauth_session",
        lambda **_kwargs: session,
    )

    def wait_for_cancel(**_kwargs):
        calls.append("authorize")
        assert session.cancelled.wait(timeout=1)
        return ServiceResult(
            OperationStatus.CANCELLED,
            error_code="oauth_cancelled",
            message="授权已取消",
            details={"oauth_state": "CANCELLED"},
        )

    monkeypatch.setattr(service, "authorize_gmail_api", wait_for_cancel)
    dialog.authorize()
    for _ in range(9):
        dialog.authorize()
    loop = QEventLoop()
    QTimer.singleShot(40, dialog.cancel_oauth)
    QTimer.singleShot(300, loop.quit)
    loop.exec()

    assert calls == ["authorize"]
    assert session.cancelled.is_set()
    assert dialog._oauth_active is False
    assert dialog.authorize_button.isEnabled()


def test_closing_dialog_cancels_active_oauth_before_destruction(
    account_qt_app, tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    dialog = GmailAccountDialog(service)

    class FakeSession:
        session_id = "close-session"

        def __init__(self):
            self.cancelled = threading.Event()

        def cancel(self):
            self.cancelled.set()
            return True

        def reopen_browser(self):
            return ServiceResult(OperationStatus.SUCCESS)

    session = FakeSession()
    monkeypatch.setattr(
        service, "create_gmail_oauth_session", lambda **_kwargs: session
    )

    def wait_for_close(**_kwargs):
        assert session.cancelled.wait(timeout=1)
        return ServiceResult(
            OperationStatus.CANCELLED,
            error_code="oauth_cancelled",
            message="授权已取消",
            details={"oauth_state": "CANCELLED"},
        )

    monkeypatch.setattr(service, "authorize_gmail_api", wait_for_close)
    dialog.authorize()
    loop = QEventLoop()
    QTimer.singleShot(40, dialog.reject)
    QTimer.singleShot(300, loop.quit)
    loop.exec()

    assert session.cancelled.is_set()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_application_exit_cancels_oauth_and_waits_for_thread(account_qt_app, tmp_cfg):
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))

    class FakeSession:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True
            return True

    class FakeThread:
        def __init__(self):
            self.quit_called = False
            self.wait_called = False

        @staticmethod
        def isRunning():
            return True

        def quit(self):
            self.quit_called = True

        def wait(self):
            self.wait_called = True
            return True

    session = FakeSession()
    thread = FakeThread()
    dialog._oauth_active = True
    dialog._oauth_session = session
    dialog._oauth_thread = thread

    dialog._prepare_application_exit()

    assert session.cancelled is True
    assert thread.quit_called is True
    assert thread.wait_called is True
    dialog._oauth_active = False
    dialog.close()


def test_late_oauth_progress_from_old_session_is_ignored(tmp_cfg):
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    dialog._oauth_session_id = "current-session"
    original = dialog.oauth_phase_label.text()

    dialog._on_oauth_progress(
        {
            "session_id": "old-session",
            "state": "WAITING_FOR_USER",
            "message": "旧会话不应显示",
            "authorization_url": "https://example.invalid/old",
        }
    )

    assert dialog.oauth_phase_label.text() == original
    assert dialog._oauth_authorization_url is None


def test_copy_oauth_link_uses_current_in_memory_url(account_qt_app, tmp_cfg):
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    dialog._oauth_active = True
    dialog._oauth_authorization_url = "https://accounts.google.com/fake-current-url"

    dialog.copy_oauth_link()

    assert (
        account_qt_app.clipboard().text()
        == "https://accounts.google.com/fake-current-url"
    )


def test_oauth_waiting_actions_replace_idle_actions_without_clipping(
    account_qt_app, tmp_cfg
):
    previous_stylesheet = account_qt_app.styleSheet()
    account_qt_app.setStyleSheet(build_stylesheet("dark"))
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    try:
        dialog.show()
        dialog._oauth_active = True
        dialog._oauth_session_id = "layout-session"
        dialog._set_oauth_running_controls(True)
        dialog._on_oauth_progress(
            {
                "session_id": "layout-session",
                "state": "WAITING_FOR_USER",
                "message": "正在等待浏览器授权和本地回调",
                "authorization_url": "https://accounts.google.com/fake-layout-url",
                "browser_opened": False,
            }
        )
        account_qt_app.processEvents()

        assert not dialog.import_button.isVisible()
        assert not dialog.authorize_button.isVisible()
        assert not dialog.api_test_button.isVisible()
        assert not dialog.api_setup_note.isVisible()
        for button in (
            dialog.cancel_oauth_button,
            dialog.reopen_browser_button,
            dialog.copy_oauth_link_button,
        ):
            assert button.isVisible()
            assert button.minimumHeight() == 34
            assert button.height() >= 30
        assert dialog.oauth_error_detail.isVisible()
        required_error_height = dialog.oauth_error_detail.heightForWidth(
            dialog.oauth_error_detail.width()
        )
        assert required_error_height > 0
        assert (
            dialog.oauth_error_detail.height()
            >= required_error_height
        )
        assert _rect_in(dialog.cancel_oauth_button, dialog.api_content).bottom() < _rect_in(
            dialog.oauth_error_detail, dialog.api_content
        ).top()
    finally:
        dialog._oauth_active = False
        dialog.close()
        account_qt_app.setStyleSheet(previous_stylesheet)


def test_oauth_idle_actions_never_overlap_when_token_action_appears(
    account_qt_app, tmp_cfg
):
    tmp_cfg.gmail_api_credentials_path.write_text(_oauth_json(), encoding="utf-8")
    tmp_cfg.gmail_api_token_path.write_text("invalid-test-token", encoding="utf-8")
    dialog = GmailAccountDialog(ApplicationService(tmp_cfg))
    try:
        dialog.resize(680, 700)
        dialog.show()
        dialog._oauth_active = True
        dialog._set_oauth_running_controls(True)
        account_qt_app.processEvents()
        dialog._oauth_active = False
        dialog._set_oauth_running_controls(False)
        dialog.refresh_status()
        account_qt_app.processEvents()

        visible_actions = [
            button
            for button in (
                dialog.import_button,
                dialog.clear_token_button,
                dialog.authorize_button,
                dialog.api_test_button,
            )
            if button.isVisible()
        ]
        for index, first in enumerate(visible_actions):
            for second in visible_actions[index + 1 :]:
                first_rect = _rect_in(first, dialog.api_content)
                second_rect = _rect_in(second, dialog.api_content)
                assert not first_rect.intersects(second_rect), (
                    f"{first.text()} overlaps {second.text()}: "
                    f"{first_rect} vs {second_rect}"
                )
        for button in visible_actions:
            assert (
                button.fontMetrics().horizontalAdvance(button.text()) + 16
                <= button.width()
            ), f"button text is clipped: {button.text()}"
        assert _rect_in(dialog.clear_token_button, dialog.api_content).bottom() < _rect_in(
            dialog.import_button, dialog.api_content
        ).top()
        for button in visible_actions:
            assert dialog.api_content.contentsRect().contains(
                _rect_in(button, dialog.api_content)
            )
    finally:
        dialog.close()


def test_oauth_ui_does_not_fake_async_with_process_events():
    source = (
        Path(__file__).resolve().parents[1]
        / "agent_mail_bridge"
        / "ui"
        / "account_management.py"
    ).read_text(encoding="utf-8")
    assert "QApplication.processEvents" not in source


def test_main_workspace_has_only_two_primary_tabs(account_qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    account_qt_app.processEvents()
    assert list(window.tab_buttons) == ["inbox", "send"]
    assert [button.text() for button in window.tab_buttons.values()] == ["收件", "发件"]
    assert list(window.nav_buttons) == ["agent", "history", "files_data", "settings", "about"]
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
