"""收件页交互、状态语义与 Windows 视觉质量回归测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel, QPushButton, QScrollArea

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ReceiveResult, ServiceResult
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import TYPOGRAPHY, build_stylesheet, load_interface_font


@pytest.fixture(scope="module")
def quality_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def quality_window(quality_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    quality_app.processEvents()
    yield window
    window.request_quit()
    quality_app.processEvents()


def test_receive_service_distinguishes_all_four_result_states(tmp_cfg, monkeypatch):
    service = ApplicationService(tmp_cfg)
    scenarios = (
        ({"ok": True, "fetched": 1, "saved": 1}, OperationStatus.SUCCESS),
        ({"ok": True, "fetched": 0, "saved": 0}, OperationStatus.NO_CHANGES),
        ({"ok": True, "fetched": 2, "saved": 1, "failed": 1, "errors": ["附件失败"]}, OperationStatus.PARTIAL),
        ({"ok": False, "fetched": 0, "saved": 0, "failed": 1, "errors": ["连接失败"]}, OperationStatus.FAILED),
    )
    for raw, expected in scenarios:
        monkeypatch.setattr("agent_mail_bridge.application_service.receive_mails", lambda *_args, value=raw, **_kwargs: value)
        assert service.receive().status == expected


def test_no_changes_is_neutral_and_partial_is_warning(quality_window):
    quality_window._show_receive_result(ReceiveResult(OperationStatus.NO_CHANGES))
    assert quality_window.message_bar.label.text() == "检查完成，暂时没有新邮件"
    assert "F7F8FB" in quality_window.message_bar.styleSheet()

    quality_window._show_receive_result(
        ReceiveResult(OperationStatus.PARTIAL, saved=1, failed=1, errors=["附件下载失败"])
    )
    assert "收件部分完成" in quality_window.message_bar.label.text()
    assert "FFF8E8" in quality_window.message_bar.styleSheet()


def test_receive_and_send_share_one_global_refresh(quality_window):
    inbox = quality_window.pages["inbox"]
    refresh_buttons = [
        button for button in inbox.findChildren(QPushButton) if button.text() == "刷新"
    ]
    assert refresh_buttons == []
    assert quality_window.inbox_refresh_button is quality_window.global_refresh_button
    assert not quality_window.global_refresh_button.icon().isNull()
    quality_window.select_page("inbox")
    assert quality_window.global_refresh_button.isVisible()
    quality_window.select_page("send")
    assert quality_window.global_refresh_button.isVisible()
    quality_window.select_page("agent")
    assert not quality_window.global_refresh_button.isVisible()


def test_file_table_removes_path_column_but_keeps_complete_value_and_real_actions(quality_window, quality_app, tmp_path, monkeypatch):
    path = tmp_path / "很长但必须完整显示的收到文件名称.txt"
    path.write_text("preview", encoding="utf-8")
    row = {
        "saved_filename": path.name,
        "saved_path": str(path),
        "path_display": str(path),
        "size_bytes": path.stat().st_size,
        "created_at": "2026-07-13 12:34:56",
        "subject": "专项整改邮件",
    }
    quality_window._populate_files(quality_window.files_table, [row], actions=True)
    assert [
        quality_window.files_table.horizontalHeaderItem(index).text()
        for index in range(quality_window.files_table.columnCount())
    ] == ["文件名", "大小", "收取时间", "操作"]
    assert quality_window.files_table.textElideMode() == Qt.TextElideMode.ElideNone
    assert quality_window.files_table.item(0, 0).text() == path.name
    assert quality_window.files_table.item(0, 0).data(Qt.ItemDataRole.UserRole) == str(path)
    assert quality_window.files_table.item(0, 2).text() == "12:34:56"
    assert "..." not in quality_window.files_table.item(0, 0).text()
    assert str(path) not in [
        quality_window.files_table.horizontalHeaderItem(index).text()
        for index in range(quality_window.files_table.columnCount())
    ]
    assert quality_window.files_table.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    assert quality_window.files_table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    action_widget = quality_window.files_table.cellWidget(0, 3)
    buttons = {button.text(): button for button in action_widget.findChildren(QPushButton)}
    assert set(buttons) == {"打开", "复制路径"}
    buttons["复制路径"].click()
    quality_app.processEvents()
    assert QApplication.clipboard().text() == str(path)
    assert buttons["复制路径"].text() == "已复制"

    opened: list[str] = []
    monkeypatch.setattr("agent_mail_bridge.ui.main_window.os.startfile", opened.append)
    quality_window.service.cfg.data_root = tmp_path
    buttons["打开"].click()
    assert opened == [str(path)]


def test_missing_received_file_does_not_execute(quality_window, tmp_path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("agent_mail_bridge.ui.main_window.os.startfile", opened.append)
    quality_window.service.cfg.data_root = tmp_path
    quality_window._open_received_file(str(tmp_path / "missing.txt"))
    assert opened == []
    assert "不存在" in quality_window.message_bar.label.text()


def test_double_click_still_routes_to_safe_preview(quality_window, monkeypatch):
    quality_window._populate_files(
        quality_window.files_table,
        [{"saved_filename": "preview.txt", "saved_path": "C:/safe/preview.txt"}],
        actions=True,
    )
    previewed: list[str] = []
    monkeypatch.setattr(quality_window, "_preview_path", previewed.append)
    quality_window._preview_table_file(0, 0)
    assert previewed == ["C:/safe/preview.txt"]


def test_receive_preference_summary_and_save_are_real(quality_window, monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr("agent_mail_bridge.ui.main_window.save_env_values", saved.update)
    quality_window.self_mail_check.setChecked(False)
    quality_window.save_receive_preferences()
    assert quality_window.service.cfg.auto_receive_only_self_mail is False
    assert saved["AUTO_RECEIVE_ONLY_SELF_MAIL"] == "false"
    assert "全部邮件" in quality_window.preference_summary_label.text()


def test_health_panel_has_five_independent_rows_and_states(quality_window):
    assert set(quality_window.health_rows) == {
        "Gmail 收件",
        "QQ SMTP",
        "Agent / MCP",
        "凭据 / OAuth",
        "SQLite / 数据目录",
    }
    checks = [
        {"name": name, "ok": index == 0, "state": "normal" if index == 0 else "partial", "message": "检查说明", "target": "gmail"}
        for index, name in enumerate(quality_window.health_rows)
    ]
    quality_window._show_health_check_result(
        ServiceResult(OperationStatus.PARTIAL, message="部分异常", details={"checks": checks, "target": "gmail"})
    )
    assert quality_window.health_rows["Gmail 收件"].state_label.text() == "正常"
    assert quality_window.health_rows["QQ SMTP"].state_label.text() == "部分异常"
    assert "最近检查" in quality_window.health_rows["SQLite / 数据目录"].checked_label.text()


def test_theme_icon_and_typography_tokens_are_formal(quality_window):
    assert quality_window.title_bar.theme_button.text() == ""
    assert not quality_window.title_bar.theme_button.icon().isNull()
    assert set(TYPOGRAPHY) == {
        "app_title", "page_title", "section_title", "card_title", "body",
        "secondary_body", "caption", "button", "table_header", "table_cell", "status",
    }
    assert all(token["weight"] in {400, 700} for token in TYPOGRAPHY.values())


def test_high_dpi_window_expands_and_keeps_full_page_scroll_fallback(quality_window):
    available = quality_window.screen().availableGeometry()
    assert quality_window.height() <= max(available.height(), quality_window.minimumHeight())
    assert quality_window.height() == min(1020, available.height())
    assert isinstance(quality_window.pages["inbox"], QScrollArea)
    assert isinstance(quality_window.right_panel, QScrollArea)
    assert quality_window.files_table.minimumHeight() == 220
    assert quality_window.logs_table.minimumHeight() == 180
    assert quality_window.logs_table.isVisible()
    assert quality_window.pages["inbox"].verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert quality_window.vertical_resize_handle.cursor().shape() == Qt.CursorShape.SizeVerCursor

    page_bar = quality_window.pages["inbox"].verticalScrollBar()
    if page_bar.maximum() > 0:
        class WheelDown:
            accepted = False

            @staticmethod
            def type():
                return QEvent.Type.Wheel

            @staticmethod
            def pixelDelta():
                return QPoint()

            @staticmethod
            def angleDelta():
                return QPoint(0, -120)

            def accept(self):
                self.accepted = True

        page_bar.setValue(0)
        wheel = WheelDown()
        assert quality_window.eventFilter(quality_window.files_table.viewport(), wheel)
        assert wheel.accepted
        assert page_bar.value() > 0


def test_inbox_has_no_duplicate_gmail_route_or_table_overlap(quality_window, quality_app):
    inbox_scroll = quality_window.pages["inbox"]
    page = inbox_scroll.widget()
    quality_app.processEvents()

    button_texts = {button.text() for button in page.findChildren(QPushButton)}
    assert "管理 Gmail 账号" not in button_texts
    assert page.minimumHeight() == 0
    assert quality_window.central_panel.minimumWidth() >= 720

    log_titles = [
        label for label in page.findChildren(QLabel) if label.text() == "最近日志"
    ]
    assert len(log_titles) == 1
    file_bottom = quality_window.files_table.mapTo(
        page, QPoint(0, quality_window.files_table.height())
    ).y()
    log_top = log_titles[0].mapTo(page, QPoint(0, 0)).y()
    assert file_bottom <= log_top
