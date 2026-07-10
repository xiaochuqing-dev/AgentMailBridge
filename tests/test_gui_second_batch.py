"""第二批次 PySide6 正式界面回归测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from agent_mail_bridge.application_service import ApplicationService
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
    assert set(bridge_window.pages) == {"basic", "inbox", "send", "advanced", "history", "logs"}
    assert set(bridge_window.tab_buttons) == {"basic", "inbox", "send", "advanced"}


def test_navigation_switches_real_pages(bridge_window):
    bridge_window.select_page("send")
    assert bridge_window.page_stack.currentWidget() is bridge_window.pages["send"]
    assert bridge_window.tab_buttons["send"].isChecked()
    bridge_window.select_page("logs")
    assert bridge_window.page_stack.currentWidget() is bridge_window.pages["logs"]
    assert bridge_window.nav_buttons["logs"].isChecked()


def test_auto_receive_interval_uses_minutes(bridge_window):
    index = bridge_window.interval_combo.findData(AUTO_RECEIVE_DEFAULT_MINUTES)
    bridge_window.interval_combo.setCurrentIndex(index)
    bridge_window.auto_switch.setChecked(True)
    assert bridge_window.auto_timer.isActive()
    # 3 分钟对应 180000 毫秒。
    assert bridge_window.auto_timer.interval() == 180000
    bridge_window.auto_switch.setChecked(False)
    assert not bridge_window.auto_timer.isActive()


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
    assert saved["GMAIL_APP_PASSWORD"] == "dummy-app-password"


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
