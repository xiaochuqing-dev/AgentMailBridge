"""v1.0.0 信息架构、真实页面与滚动交互收口测试。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ServiceResult
from agent_mail_bridge.runtime_paths import get_runtime_paths
from agent_mail_bridge.ui.account_management import AccountTypeDialog
from agent_mail_bridge.ui.main_window import BridgeWindow


@pytest.fixture(scope="module")
def v1_qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def v1_window(v1_qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    v1_qt_app.processEvents()
    yield window
    window.request_quit()
    v1_qt_app.processEvents()


def _all_text(widget) -> str:
    children = widget.findChildren(QLabel) + widget.findChildren(QPushButton)
    return "\n".join(child.text() for child in children)


def test_final_primary_navigation_is_unique(v1_window):
    assert [button.text() for button in v1_window.tab_buttons.values()] == ["收件", "发件"]
    assert [button.text() for button in v1_window.nav_buttons.values()] == [
        "Agent / MCP", "历史记录", "文件与数据", "设置", "关于",
    ]
    assert "advanced" not in v1_window.tab_buttons
    assert "agent" in v1_window.nav_buttons


def test_secondary_pages_keep_parent_navigation_context(v1_window):
    v1_window.select_page("advanced")
    assert v1_window.nav_buttons["settings"].isChecked()
    v1_window.select_page("maintenance")
    assert v1_window.nav_buttons["files_data"].isChecked()
    v1_window.select_page("agent")
    assert v1_window.nav_buttons["agent"].isChecked()
    assert not any(button.isChecked() for button in v1_window.tab_buttons.values())


def test_add_account_is_a_future_extension_demo(v1_qt_app):
    dialog = AccountTypeDialog()
    dialog.show()
    v1_qt_app.processEvents()
    text = _all_text(dialog)
    assert "未来邮箱扩展入口" in text
    assert "暂不开放新增第二个同类型账号" in text
    assert "通过左侧账号卡片管理" in text
    assert not hasattr(dialog, "selected_type")
    dialog.close()


def test_user_provided_provider_assets_are_packaged_and_readable():
    branding = get_runtime_paths().resource_root / "branding"
    gmail = QImage(str(branding / "gmail.svg"))
    qq = QImage(str(branding / "qqmail.webp"))
    assert not gmail.isNull() and gmail.width() == 192
    assert not qq.isNull() and qq.width() == 80


def test_settings_is_primary_and_advanced_has_no_duplicate_account_or_mcp(v1_window):
    settings_text = _all_text(v1_window.pages["settings"])
    advanced_text = _all_text(v1_window.pages["advanced"])
    assert "高级设置" in settings_text
    assert "QQ SMTP 授权码" not in advanced_text
    assert "Gmail IMAP 应用专用密码" not in advanced_text
    assert "Agent 接口配置" not in advanced_text
    assert "设置 > 高级设置" in advanced_text


def test_send_page_keeps_record_management_without_duplicate_agent_route(v1_window):
    text = _all_text(v1_window.pages["send"])
    assert "Agent 发件 / MCP" not in text
    assert "管理记录" in text
    assert "复制 MCP 配置" in _all_text(v1_window.pages["agent"])
    v1_window.open_send_history()
    assert v1_window.page_stack.currentWidget() is v1_window.pages["history"]
    assert v1_window.history_type_filter.currentText() == "发件"


def test_history_combines_receive_send_and_agent_business_records(v1_window):
    v1_window.history_rows = {
        "received": [{"subject": "收件", "status": "saved", "created_at": "2026-07-12T08:00:00"}],
        "sent": [{"original_filename": "发送.txt", "status": "sent", "sent_at": "2026-07-12T09:00:00"}],
    }
    v1_window.mcp_rows = [{"file_path": "result.md", "request_id": "req-1", "status": "success", "created_at": "2026-07-12T10:00:00"}]
    v1_window.history_type_filter.setCurrentText("全部类型")
    v1_window.history_time_filter.setCurrentText("全部时间")
    v1_window._populate_history()
    types = {v1_window.history_table.item(row, 0).text() for row in range(v1_window.history_table.rowCount())}
    assert types == {"收件", "发件", "Agent / MCP"}


def test_files_and_data_page_has_real_files_overview_and_maintenance(v1_window):
    text = _all_text(v1_window.pages["files_data"])
    assert "收件文件" in text
    assert "已发送归档" in text
    assert "Agent 结果" in text
    assert "数据维护与备份" in text
    assert v1_window.managed_files_table.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_inbox_logs_and_sent_records_use_internal_scroll_without_pagination(v1_window):
    for table in (v1_window.files_table, v1_window.logs_table, v1_window.sent_table):
        assert table.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    visible_text = _all_text(v1_window.pages["inbox"]) + _all_text(v1_window.pages["send"])
    assert "每页" not in visible_text
    assert "页码" not in visible_text


def test_log_management_supports_search_level_time_export_and_folder(v1_window):
    text = _all_text(v1_window.pages["logs"])
    assert "日志管理" in text
    assert "导出脱敏诊断信息" in text
    assert "打开日志目录" in text
    assert v1_window.log_search.placeholderText() == "搜索事件或消息"


def test_one_click_health_check_aggregates_required_surfaces(v1_window, monkeypatch):
    ok = ServiceResult(OperationStatus.SUCCESS, message="正常")
    monkeypatch.setattr(v1_window.service, "diagnose_imap", lambda: ok)
    monkeypatch.setattr(v1_window.service, "diagnose_gmail_api", lambda: ok)
    monkeypatch.setattr(v1_window.service, "diagnose_qq_smtp", lambda: ok)
    monkeypatch.setattr(
        v1_window.service,
        "get_maintenance_status",
        lambda: ServiceResult(OperationStatus.SUCCESS, details={"integrity_check": "ok"}),
    )
    result = v1_window._collect_all_connection_diagnostics()
    names = {item["name"] for item in result.details["checks"]}
    assert names == {"Gmail 收件", "QQ SMTP", "Agent / MCP", "凭据 / OAuth", "SQLite / 数据目录"}


def test_mcp_panel_preserves_fixed_recipient_and_path_boundary(v1_window):
    text = _all_text(v1_window.pages["agent"])
    assert "允许目录" in text
    assert "不能指定收件人" in text
    assert "邮件读取" in text
    assert "MCP 自检" in text


def test_about_page_is_not_empty(v1_window):
    text = _all_text(v1_window.pages["about"])
    assert "AgentMailBridge" in text
    assert "本地优先" in text
    assert "LICENSE" in text
    assert "第三方说明" in text
