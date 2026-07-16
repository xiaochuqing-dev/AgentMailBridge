"""邮件级 GUI、完整发件和 Agent 交付体验回归。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, SendResult, ServiceResult
from agent_mail_bridge.ui.main_window import BridgeWindow, SendFileSelection
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font


@pytest.fixture(scope="module")
def mail_gui_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def mail_gui_window(mail_gui_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    mail_gui_app.processEvents()
    yield window
    window.request_quit()
    mail_gui_app.processEvents()


def _wait(window: BridgeWindow, app: QApplication) -> None:
    deadline = time.monotonic() + 3
    while window.task_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _page_text(widget) -> str:
    return "\n".join(
        child.text() for child in widget.findChildren(QPushButton) if child.text()
    )


def test_inbox_is_one_row_per_mail_even_with_seven_attachments(mail_gui_window):
    long_subject = "超长中文主题与 English mixed content " * 5
    mail_gui_window.mail_rows = [{
        "package_id": "mail-one",
        "subject": long_subject,
        "from": "sender@example.com",
        "body_summary": "正文摘要",
        "received_at": "2026-07-16 10:11:12",
        "archive_status": "ready",
        "counts": {"resources": 9, "attachments": 7, "inline_images": 1, "links": 0},
        "legacy": False,
    }]
    mail_gui_window._populate_inbox_messages(mail_gui_window.mail_rows)

    assert mail_gui_window.inbox_table.rowCount() == 1
    assert [
        mail_gui_window.inbox_table.horizontalHeaderItem(index).text()
        for index in range(mail_gui_window.inbox_table.columnCount())
    ] == ["主题", "发件人", "内容", "收取时间", "状态", "操作"]
    assert mail_gui_window.inbox_table.textElideMode() == Qt.TextElideMode.ElideNone
    assert mail_gui_window.inbox_table.item(0, 0).text() == long_subject
    assert mail_gui_window.inbox_table.rowHeight(0) > 58
    assert mail_gui_window.inbox_table.rowHeight(0) == 74
    assert "7 个附件" in mail_gui_window.inbox_table.item(0, 2).text()
    assert "1 张邮件图片" in mail_gui_window.inbox_table.item(0, 2).text()
    assert mail_gui_window.inbox_table.objectName() == "mailRecordTable"
    assert mail_gui_window.inbox_table.selectionMode() == mail_gui_window.inbox_table.SelectionMode.NoSelection
    assert mail_gui_window.inbox_table.cellWidget(0, 5).text() == "查看邮件"


def test_mail_detail_shows_body_attachment_link_and_natural_terms(
    mail_gui_window, monkeypatch
):
    body = mail_gui_window.service.cfg.data_root_path / "body.txt"
    attachment = mail_gui_window.service.cfg.data_root_path / "长文件名附件.txt"
    body.write_text("完整邮件正文", encoding="utf-8")
    attachment.write_text("attachment", encoding="utf-8")
    details = {
        "package_id": "mail-detail",
        "subject": "详情主题",
        "from": "sender@example.com",
        "to": ["owner@example.com"],
        "received_at": "2026-07-16 10:00:00",
        "backend": "gmail_api",
        "thread_ref": "thread-1",
        "package_root": str(mail_gui_window.service.cfg.data_root_path),
        "body": {"plain_absolute_path": str(body)},
        "counts": {"attachments": 1, "inline_images": 0, "links": 1, "downloads": 0},
        "resources": [
            {
                "internal_type": "attachment",
                "display_name": attachment.name,
                "kind_display": "附件",
                "status_display": "已保存",
                "absolute_path": str(attachment),
            },
            {
                "internal_type": "link",
                "display_name": "说明页面",
                "kind_display": "网页链接",
                "status_display": "已识别",
                "url": "https://example.com/page",
            },
        ],
    }
    monkeypatch.setattr(
        mail_gui_window.service,
        "get_mail_message",
        lambda _package_id: ServiceResult(
            OperationStatus.SUCCESS, details={"message": details}
        ),
    )

    mail_gui_window.show_mail_detail("mail-detail", "inbox")

    assert mail_gui_window.page_stack.currentWidget() is mail_gui_window.mail_detail_page
    assert mail_gui_window.mail_detail_subject.text() == "详情主题"
    assert mail_gui_window.mail_detail_body.toPlainText() == "完整邮件正文"
    assert "package_id" not in mail_gui_window.mail_detail_meta.text()
    assert "resource_type" not in mail_gui_window.mail_detail_meta.text()
    buttons = {
        button.text()
        for button in mail_gui_window.mail_detail_resource_widget.findChildren(QPushButton)
    }
    assert {"安全预览", "打开", "打开链接"} <= buttons


def test_history_deduplicates_agent_audit_from_agent_outbound(mail_gui_window):
    mail_gui_window.history_rows = {
        "received": [{
            "package_id": "mail-1", "subject": "收到主题", "archive_status": "ready",
            "received_at": "2026-07-16 10:00:00",
        }],
        "sent": [{
            "outbound_id": "out-1", "subject": "交付主题", "source_origin": "agent_mcp",
            "request_id": "stable-1", "status": "sent", "sent_at": "2026-07-16 11:00:00",
        }],
    }
    mail_gui_window.mcp_rows = [{
        "request_id": "stable-1", "status": "success", "created_at": "2026-07-16 11:00:01"
    }]
    mail_gui_window.history_type_filter.setCurrentText("全部类型")
    mail_gui_window.history_time_filter.setCurrentText("全部时间")
    mail_gui_window._populate_history()

    assert mail_gui_window.history_table.rowCount() == 2
    assert {
        mail_gui_window.history_table.item(row, 0).text()
        for row in range(mail_gui_window.history_table.rowCount())
    } == {"收到邮件", "Agent 发送"}


def test_files_data_exposes_associated_mail_without_path_column(mail_gui_window):
    mail_gui_window.managed_file_rows = [{
        "category": "收件文件",
        "display_type": "附件",
        "source": "Gmail API",
        "mail_subject": "所属邮件主题",
        "display_name": "完整长文件名.txt",
        "path": "",
        "size_bytes": 12,
        "size_known": True,
        "time": "2026-07-16 12:00:00",
        "status": "saved",
        "status_display": "已保存",
        "exists": False,
        "package_id": "mail-1",
    }]
    mail_gui_window._filter_managed_files()

    headers = [
        mail_gui_window.managed_files_table.horizontalHeaderItem(index).text()
        for index in range(mail_gui_window.managed_files_table.columnCount())
    ]
    assert headers == ["类型", "来源", "所属邮件", "文件名", "大小", "时间", "状态", "操作"]
    assert "路径" not in headers
    assert mail_gui_window.managed_files_table.item(0, 2).text() == "所属邮件主题"
    assert {
        button.text()
        for button in mail_gui_window.managed_files_table.cellWidget(0, 7).findChildren(QPushButton)
    } == {"预览", "打开", "邮件"}


def test_composer_sends_body_three_attachments_and_two_links_as_one_call(
    mail_gui_window, mail_gui_app, monkeypatch
):
    for index in range(3):
        path = mail_gui_window.service.cfg.data_root_path / f"附件-{index}.txt"
        path.write_text(f"content-{index}", encoding="utf-8")
        mail_gui_window._append_send_selection(SendFileSelection.capture(path))
    mail_gui_window.subject_edit.setText("完整发件")
    mail_gui_window.send_body_edit.setPlainText("邮件正文")
    mail_gui_window.send_links = [
        {"url": "https://example.com/one", "display_text": ""},
        {"url": "https://example.com/two", "display_text": ""},
    ]
    mail_gui_window._populate_send_links()
    mail_gui_window._update_send_action_state()
    captured = []

    def fake_send(**kwargs):
        captured.append(kwargs)
        return SendResult(
            OperationStatus.SUCCESS,
            send_status="sent",
            outbound_id="out-one",
            attachment_count=3,
            link_count=2,
            message="发送完成",
        )

    monkeypatch.setattr(mail_gui_window.service, "send_user_selected_mail", fake_send)
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    mail_gui_window.send_composed_mail()
    _wait(mail_gui_window, mail_gui_app)

    assert len(captured) == 1
    assert captured[0]["body_text"] == "邮件正文"
    assert len(captured[0]["attachment_paths"]) == 3
    assert len(captured[0]["links"]) == 2
    assert mail_gui_window.send_selections == []
    assert mail_gui_window.send_links == []


def test_agent_instruction_has_full_safe_delivery_contract_and_copy(mail_gui_window):
    mail_gui_window.agent_delivery_title_edit.clear()
    instruction = mail_gui_window.agent_delivery_instruction.toPlainText()
    assert "自行识别最终交付文件" in instruction
    assert "不要要求我再次提供文件路径" in instruction
    assert "submit_result" in instruction
    assert "稳定 request_id" in instruction
    assert "不自行使用 PowerShell、Copy-Item" in instruction
    assert "path_not_allowed" in instruction
    assert "transport closed" in instruction
    assert "duplicate" in instruction
    assert "C:\\Users\\" not in instruction

    mail_gui_window.agent_delivery_title_edit.setText("自定义交付主题")
    mail_gui_window.copy_agent_delivery_instruction()
    assert "自定义交付主题" in QApplication.clipboard().text()
    assert mail_gui_window.message_bar.label.text() == "交付指令已复制"


def test_recent_sent_table_uses_stretch_columns_and_no_small_fixed_height(mail_gui_window):
    header = mail_gui_window.sent_table.horizontalHeader()
    assert header.sectionResizeMode(0).name == "Stretch"
    assert header.sectionResizeMode(1).name == "Stretch"
    assert mail_gui_window.sent_table.maximumHeight() >= 16_777_215
    assert "Agent 发件 / MCP" in _page_text(mail_gui_window.pages["send"])


def test_received_and_sent_tables_use_compact_unified_rows(mail_gui_window):
    long_body = "这是需要在详情中完整查看的长正文🙂" * 80
    mail_gui_window._populate_inbox_messages([{
        "package_id": "compact-inbox",
        "subject": "紧凑收件主题",
        "from": "sender@example.com",
        "body_summary": long_body,
        "received_at": "2026-07-16 10:00:00",
        "archive_status": "ready",
        "counts": {"attachments": 3, "inline_images": 2, "links": 4, "downloads": 1},
    }])
    mail_gui_window.history_rows["sent"] = [{
        "outbound_id": "compact-outbound",
        "subject": "紧凑发件主题",
        "body_text": long_body,
        "attachment_count": 3,
        "link_count": 2,
        "source_origin": "manual_gui",
        "status": "sent",
        "sent_at": "2026-07-16 11:00:00",
    }]
    mail_gui_window._populate_sent_history()

    assert mail_gui_window.inbox_table.rowHeight(0) == 74
    assert mail_gui_window.sent_table.rowHeight(0) == 74
    assert len(mail_gui_window.inbox_table.item(0, 2).text()) < len(long_body)
    assert "3 个附件" in mail_gui_window.inbox_table.item(0, 2).text()
    assert "2 个链接" in mail_gui_window.sent_table.item(0, 1).text()
    assert mail_gui_window.sent_table.objectName() == "mailRecordTable"
    assert "双击查看完整邮件" in mail_gui_window.inbox_table.item(0, 0).toolTip()


def test_mail_fact_search_is_debounced_and_sent_row_double_click_still_opens(
    mail_gui_window, monkeypatch
):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        mail_gui_window.service,
        "search_mail_facts",
        lambda query, **filters: (
            captured.append((query, filters))
            or ServiceResult(OperationStatus.SUCCESS, details={"messages": []})
        ),
    )
    mail_gui_window.inbox_search.setText("中文附件名")
    assert mail_gui_window.inbox_search_timer.interval() == 250
    assert mail_gui_window.inbox_search_timer.isActive()
    mail_gui_window._filter_inbox()
    assert captured[0][0] == "中文附件名"
    assert captured[0][1]["date_from"] == time.strftime("%Y-%m-%d")

    mail_gui_window.history_rows["sent"] = [{
        "outbound_id": "double-click-outbound",
        "subject": "双击发送记录",
        "body_text": "正文",
        "source_origin": "manual_gui",
        "status": "sent",
    }]
    mail_gui_window._populate_sent_history()
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mail_gui_window,
        "show_outbound_detail",
        lambda outbound_id, page: opened.append((outbound_id, page)),
    )
    mail_gui_window._open_sent_record(0, 1)
    assert opened == [("double-click-outbound", "send")]


def test_log_management_exposes_overview_filters_retention_and_safe_cleanup(mail_gui_window):
    page = mail_gui_window.pages["logs"]
    buttons = {button.text() for button in page.findChildren(QPushButton)}
    assert {
        "导出当前筛选日志",
        "导出脱敏诊断信息（完整）",
        "立即清理过期日志",
        "清除日常检查",
        "清空全部技术日志",
    } <= buttons
    assert mail_gui_window.log_type_filter.count() == 7
    assert not mail_gui_window.log_daily_check.isChecked()
    assert mail_gui_window.log_page_size == 150
    assert mail_gui_window.log_normal_retention.currentData() == 30
    assert mail_gui_window.log_error_retention.currentData() == 90
    assert mail_gui_window.log_max_count.currentData() == 10_000
    assert "当前技术事件" in mail_gui_window.log_overview_label.text()
