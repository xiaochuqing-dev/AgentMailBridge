"""第二批次 PySide6 正式界面回归测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ReceiveResult, ServiceResult
from agent_mail_bridge.ui.main_window import AUTO_RECEIVE_DEFAULT_MINUTES, BridgeWindow
from agent_mail_bridge.ui.settings_store import save_env_values
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font
from agent_mail_bridge.ui.widgets import format_size


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def bridge_window(qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    qt_app.processEvents()
    yield window
    window.close()
    qt_app.processEvents()


def test_formal_gui_uses_reference_three_column_layout(bridge_window):
    assert bridge_window.sidebar.width() == 230
    assert bridge_window.right_panel.width() == 306
    assert bridge_window.central_panel.width() >= 620
    assert set(bridge_window.pages) == {
        "basic", "inbox", "send", "advanced", "history", "logs", "maintenance", "agent"
    }
    assert set(bridge_window.tab_buttons) == {"basic", "inbox", "send", "advanced"}
    assert all(
        row.value_label.minimumWidth() >= 126
        for row in bridge_window.service_rows.values()
    )


def test_navigation_switches_real_pages(bridge_window):
    bridge_window.select_page("send")
    assert bridge_window.page_stack.currentWidget() is bridge_window.pages["send"]
    assert bridge_window.tab_buttons["send"].isChecked()
    bridge_window.select_page("logs")
    assert bridge_window.page_stack.currentWidget() is bridge_window.pages["logs"]
    assert bridge_window.nav_buttons["logs"].isChecked()
    bridge_window.select_page("agent")
    assert bridge_window.page_stack.currentWidget() is bridge_window.pages["agent"]
    assert bridge_window.nav_buttons["agent"].isChecked()
    assert not any(button.isChecked() for button in bridge_window.tab_buttons.values())
    bridge_window.select_page("advanced")
    assert bridge_window.page_stack.currentWidget() is bridge_window.pages["advanced"]
    assert bridge_window.tab_buttons["advanced"].isChecked()
    assert bridge_window.nav_buttons["basic"].isChecked()


def test_agent_page_exposes_safe_stdio_configuration(bridge_window):
    text = bridge_window.mcp_command_text.toPlainText()
    assert "python -m agent_mail_bridge.mcp_server" in text
    assert "recipient" not in text.lower()
    assert bridge_window.service.cfg.qq_auth_code not in text
    bridge_window._copy_mcp_config("codex")
    assert "codex mcp add agent-mail-bridge" in QApplication.clipboard().text()


def test_auto_receive_interval_uses_minutes(bridge_window):
    index = bridge_window.interval_combo.findData(AUTO_RECEIVE_DEFAULT_MINUTES)
    bridge_window.interval_combo.setCurrentIndex(index)
    bridge_window.auto_switch.setChecked(True)
    assert bridge_window.auto_timer.isActive()
    # 3 分钟对应 180000 毫秒。
    assert bridge_window.auto_timer.interval() == 180000
    bridge_window.auto_switch.setChecked(False)
    assert not bridge_window.auto_timer.isActive()


def test_auto_receive_disables_manual_receive_actions(bridge_window):
    bridge_window.auto_switch.setChecked(True)
    assert not bridge_window.receive_button.isEnabled()
    assert all(not button.isEnabled() for button in bridge_window.manual_receive_buttons)
    assert "自动收取已开启" in bridge_window.receive_button.toolTip()

    bridge_window.auto_switch.setChecked(False)
    assert all(button.isEnabled() for button in bridge_window.manual_receive_buttons)


def test_diagnose_only_disables_clicked_button_and_announces_recovery(bridge_window, qt_app):
    bridge_window._diagnose(
        "正在诊断 Gmail IMAP",
        lambda: ServiceResult(OperationStatus.SUCCESS, message="连接正常"),
        bridge_window.imap_diagnose_button,
    )
    assert not bridge_window.imap_diagnose_button.isEnabled()
    assert bridge_window.authorize_button.isEnabled()
    assert bridge_window.gmail_api_diagnose_button.isEnabled()
    assert bridge_window.smtp_diagnose_button.isEnabled()

    deadline = time.monotonic() + 2
    while bridge_window.task_active and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)

    assert bridge_window.imap_diagnose_button.isEnabled()
    assert "按钮已恢复可用" in bridge_window.message_bar.label.text()


def test_background_task_completion_reaches_gui(bridge_window, qt_app):
    completed: list[ServiceResult] = []
    callback_on_gui_thread: list[bool] = []

    def finish(result: ServiceResult) -> None:
        completed.append(result)
        callback_on_gui_thread.append(QThread.currentThread() is qt_app.thread())

    bridge_window._run_task(
        "后台任务测试",
        lambda: ServiceResult(OperationStatus.SUCCESS, message="完成"),
        finish,
    )
    deadline = time.monotonic() + 2  # 最多等待 2 秒处理 Qt 完成信号。
    while bridge_window.task_active and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)

    assert not bridge_window.task_active
    assert bridge_window._active_runner is None
    assert len(completed) == 1
    assert callback_on_gui_thread == [True]
    assert all(
        button.isEnabled()
        for button in bridge_window.task_buttons
        if button is not bridge_window.send_action_button
    )
    # 未明确选择文件时发送入口必须保持禁用。
    assert not bridge_window.send_action_button.isEnabled()


def test_receive_failure_message_contains_reason(bridge_window):
    bridge_window._show_receive_result(
        ReceiveResult(
            OperationStatus.FAILED,
            failed=1,
            error_code="network_error",
            message="网络连接中断",
        )
    )
    text = bridge_window.message_bar.label.text()
    assert "收件失败" in text
    assert "网络连接中断" in text


def test_basic_config_updates_runtime_without_core_rewrite(bridge_window, monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.save_env_values",
        lambda values: saved.update(values),
    )
    bridge_window.gmail_email_edit.setText("updated@gmail.com")
    bridge_window.gmail_password_edit.setText("dummy-app-password")
    bridge_window._set_combo_data(bridge_window.backend_combo, "imap")
    bridge_window.save_basic_config()
    assert bridge_window.service.cfg.gmail_address == "updated@gmail.com"
    assert bridge_window.service.cfg.owner_gmail == "updated@gmail.com"
    assert saved["GMAIL_RECEIVE_BACKEND"] == "imap"
    assert saved["GMAIL_APP_PASSWORD"] == ""
    assert bridge_window.service.cfg.gmail_app_password == "dummy-app-password"


def test_basic_config_save_failure_keeps_runtime_config(bridge_window, monkeypatch):
    original = (
        bridge_window.service.cfg.gmail_address,
        bridge_window.service.cfg.owner_gmail,
        bridge_window.service.cfg.gmail_app_password,
        bridge_window.service.cfg.gmail_receive_backend,
    )

    def fail_save(_values):
        raise OSError("只读文件")

    monkeypatch.setattr("agent_mail_bridge.ui.main_window.save_env_values", fail_save)
    bridge_window.gmail_email_edit.setText("not-saved@gmail.com")
    bridge_window.gmail_password_edit.setText("not-saved-password")
    bridge_window._set_combo_data(bridge_window.backend_combo, "imap")
    bridge_window.save_basic_config()

    current = (
        bridge_window.service.cfg.gmail_address,
        bridge_window.service.cfg.owner_gmail,
        bridge_window.service.cfg.gmail_app_password,
        bridge_window.service.cfg.gmail_receive_backend,
    )
    assert current == original


def test_advanced_config_save_failure_keeps_runtime_config(bridge_window, monkeypatch):
    original = (
        bridge_window.service.cfg.qq_email,
        bridge_window.service.cfg.qq_auth_code,
        bridge_window.service.cfg.gmail_network_mode,
        bridge_window.service.cfg.max_fetch_limit,
        bridge_window.service.cfg.max_send_file_mb,
    )

    def fail_save(_values):
        raise OSError("只读文件")

    monkeypatch.setattr("agent_mail_bridge.ui.main_window.save_env_values", fail_save)
    bridge_window.qq_email_edit.setText("not-saved@qq.com")
    bridge_window.qq_auth_edit.setText("not-saved-auth")
    bridge_window._set_combo_data(bridge_window.network_combo, "direct")
    bridge_window.fetch_limit_spin.setValue(7)
    bridge_window.send_limit_spin.setValue(8)
    bridge_window.save_advanced_config()

    current = (
        bridge_window.service.cfg.qq_email,
        bridge_window.service.cfg.qq_auth_code,
        bridge_window.service.cfg.gmail_network_mode,
        bridge_window.service.cfg.max_fetch_limit,
        bridge_window.service.cfg.max_send_file_mb,
    )
    assert current == original


def test_env_save_preserves_unrelated_lines_and_quotes_values(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# 保留注释\nUNRELATED=keep\nGMAIL_ADDRESS=old@example.com\n", encoding="utf-8")
    save_env_values(
        {"GMAIL_ADDRESS": "new@example.com", "QQ_AUTH_CODE": 'dummy "code"'},
        env_path,
    )
    content = env_path.read_text(encoding="utf-8")
    assert "# 保留注释" in content
    assert "UNRELATED=keep" in content
    assert 'GMAIL_ADDRESS="new@example.com"' in content
    assert 'QQ_AUTH_CODE="dummy \\"code\\""' in content
    assert not list(tmp_path.glob("*.tmp"))


def test_file_size_formatting_is_stable():
    assert format_size(0) == "0 B"
    assert format_size(1024) == "1 KB"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size("invalid") == "—"
