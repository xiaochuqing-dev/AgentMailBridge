"""历史、受管文件与统一收件规则专项回归。"""

from __future__ import annotations

import os
from email.message import EmailMessage
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    get_connection,
    insert_mcp_call,
    insert_received_file,
    insert_received_message,
    insert_sent_file,
)
from agent_mail_bridge.mail_common import AttachmentData, NormalizedMail
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.managed_files import localize_status
from agent_mail_bridge.receive_rules import (
    ALL_SCANNED,
    CUSTOM,
    SELF_ONLY,
    match_receive_rule,
    normalize_sender_rules,
    normalize_subject_keywords,
    validate_rule_settings,
)
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font


def _mail(
    *,
    backend: str = "imap",
    message_id: str = "<rule@test>",
    sender: str = "boss@example.com",
    subject: str = "Project report",
    attachment: bool = True,
) -> NormalizedMail:
    attachments = (
        [AttachmentData("report.txt", b"data", "text/plain", "allowed")]
        if attachment
        else []
    )
    raw_message = EmailMessage()
    raw_message["From"] = f'"Sender" <{sender}>'
    raw_message["To"] = "test@gmail.com"
    raw_message["Subject"] = subject
    if message_id:
        raw_message["Message-ID"] = message_id
    raw_message.set_content("body")
    if attachment:
        raw_message.add_attachment(
            b"data", maintype="text", subtype="plain", filename="report.txt"
        )
    return NormalizedMail(
        backend=backend,
        message_id=message_id,
        backend_message_id="api-id" if backend == "gmail_api" else "",
        thread_id="thread",
        uid="1",
        from_raw=f'"Sender" <{sender}>',
        to_raw="test@gmail.com",
        cc_raw="",
        subject=subject,
        received_at="2026-07-13 09:08:07",
        saved_date="2026-07-13",
        body_text="body",
        attachments=attachments,
        raw_bytes=raw_message.as_bytes(),
        body_plain="body",
        mailbox_ref="gmail:me/inbox" if backend == "gmail_api" else "imap:INBOX",
    )


def test_legacy_boolean_maps_to_new_modes(tmp_path):
    assert AppConfig(
        data_root=tmp_path / "self", auto_receive_only_self_mail=True
    ).receive_rule_mode == SELF_ONLY
    assert AppConfig(
        data_root=tmp_path / "all", auto_receive_only_self_mail=False
    ).receive_rule_mode == ALL_SCANNED


def test_custom_rule_normalization_validation_and_and_or_semantics(tmp_cfg):
    tmp_cfg.receive_rule_mode = CUSTOM
    tmp_cfg.receive_rule_senders = normalize_sender_rules(
        [" Boss@Example.com ", "@example.org", "boss@example.com"]
    )
    tmp_cfg.receive_rule_subject_keywords = normalize_subject_keywords(
        [" Report ", "project", "REPORT", ""]
    )
    tmp_cfg.receive_rule_require_attachment = True
    assert tmp_cfg.receive_rule_senders == ("boss@example.com", "@example.org")
    assert tmp_cfg.receive_rule_subject_keywords == ("Report", "project")

    assert match_receive_rule(
        tmp_cfg, _mail(sender="other@example.org", subject="PROJECT update")
    )[0]
    assert not match_receive_rule(
        tmp_cfg, _mail(sender="other@outside.net", subject="project")
    )[0]
    assert not match_receive_rule(
        tmp_cfg, _mail(sender="boss@example.com", subject="unrelated")
    )[0]
    assert not match_receive_rule(
        tmp_cfg, _mail(sender="boss@example.com", subject="report", attachment=False)
    )[0]

    assert validate_rule_settings(CUSTOM, (), (), False) == (
        "自定义规则至少需要一个有效条件",
    )
    assert "格式无效" in validate_rule_settings(
        CUSTOM, ("bad-address",), (), False
    )[0]


@pytest.mark.parametrize("backend", ["imap", "gmail_api"])
def test_api_and_imap_share_the_same_business_rule(tmp_cfg, backend):
    tmp_cfg.receive_rule_mode = CUSTOM
    tmp_cfg.receive_rule_senders = ("@example.com",)
    tmp_cfg.receive_rule_subject_keywords = ("report",)
    tmp_cfg.receive_rule_require_attachment = True
    result = process_normalized_mail(
        tmp_cfg,
        _mail(backend=backend, message_id=f"<{backend}@test>"),
    )
    assert result["status"] == "saved"


def test_custom_rule_never_saves_when_no_condition(tmp_cfg):
    tmp_cfg.receive_rule_mode = CUSTOM
    tmp_cfg.receive_rule_senders = ()
    tmp_cfg.receive_rule_subject_keywords = ()
    tmp_cfg.receive_rule_require_attachment = False
    result = process_normalized_mail(tmp_cfg, _mail())
    assert result == {
        "status": "skipped",
        "reason": "invalid_custom_rule",
        "saved_files": [],
    }


def _insert_received(
    cfg: AppConfig,
    *,
    message_id: str,
    path: Path,
    size_bytes: int | None,
    file_type: str = "body",
) -> None:
    insert_received_message(
        cfg.db_path,
        message_id=message_id,
        gmail_uid="1",
        subject="真实文件主题",
        from_email="sender@example.com",
        to_email=cfg.gmail_address,
        received_at="2026-07-13 10:11:12",
        saved_date="2026-07-13",
        body_file_path=str(path),
        body_sha256="hash",
        has_attachments=file_type == "attachment",
        status="saved",
        source="gmail_api",
        backend="gmail_api",
    )
    insert_received_file(
        cfg.db_path,
        message_id=message_id,
        file_type=file_type,
        original_filename=path.name,
        saved_filename=path.name,
        saved_path=str(path),
        sha256="hash",
        size_bytes=0 if size_bytes is None else size_bytes,
        mime_type="text/plain",
        saved_date="2026-07-13",
        status="normal",
    )
    if size_bytes is None:
        get_connection(cfg.db_path).execute(
            "UPDATE received_files SET size_bytes = NULL WHERE message_id = ?",
            (message_id,),
        )


def test_managed_files_use_received_files_and_distinguish_sizes(tmp_cfg):
    zero = tmp_cfg.received_dir / "zero.txt"
    zero.write_bytes(b"")
    body = tmp_cfg.received_dir / "body.txt"
    body.write_bytes(b"12345")
    missing = tmp_cfg.received_dir / "missing.txt"
    _insert_received(tmp_cfg, message_id="<zero@test>", path=zero, size_bytes=0)
    _insert_received(tmp_cfg, message_id="<body@test>", path=body, size_bytes=5)
    _insert_received(tmp_cfg, message_id="<missing@test>", path=missing, size_bytes=99)

    rows = ApplicationService(tmp_cfg).get_managed_files().details["files"]
    by_name = {row["display_name"]: row for row in rows}
    assert by_name["zero.txt"]["size_bytes"] == 0
    assert by_name["zero.txt"]["size_known"] is True
    assert by_name["body.txt"]["size_bytes"] == 5
    assert by_name["body.txt"]["subject"] == "真实文件主题"
    assert by_name["missing.txt"]["exists"] is False
    assert by_name["missing.txt"]["status_display"] == "文件已不存在"


def test_old_sent_record_uses_safe_stat_and_mcp_is_deduplicated(tmp_cfg):
    archive = tmp_cfg.sent_dir / "archive.txt"
    archive.write_bytes(b"1234567")
    insert_sent_file(
        tmp_cfg.db_path,
        request_id="req-shared",
        source_path=str(archive),
        send_copy_path=None,
        sent_copy_path=str(archive),
        sha256="hash",
        subject="Agent result",
        from_email="test@qq.com",
        to_email="test@gmail.com",
        sent_at="2026-07-13 10:00:00",
        status="sent",
    )
    insert_mcp_call(
        tmp_cfg.db_path,
        request_id="req-shared",
        file_path=str(archive),
        title="Agent result",
        status="sent",
    )
    rows = ApplicationService(tmp_cfg).get_managed_files().details["files"]
    matching = [row for row in rows if row.get("request_id") == "req-shared"]
    assert len(matching) == 1
    assert matching[0]["size_bytes"] == 7
    assert matching[0]["category"] == "Agent 结果"


@pytest.fixture(scope="module")
def specialty_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def specialty_window(specialty_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    specialty_app.processEvents()
    yield window
    window.request_quit()
    specialty_app.processEvents()


def test_account_cards_keep_complete_title_email_and_status(specialty_window):
    long_email = "complete.account.address@example-domain.com"
    specialty_window.gmail_card.email_label.setText(long_email)
    assert specialty_window.gmail_card.title_label.text() == "Gmail（收件）"
    assert specialty_window.qq_card.title_label.text() == "QQ（发件）"
    assert specialty_window.gmail_card.email_label.text() == long_email
    assert specialty_window.gmail_card.email_label.wordWrap()
    assert specialty_window.gmail_card.status_tag.text() in {"已配置", "未配置"}
    for card in (specialty_window.gmail_card, specialty_window.qq_card):
        for child in (card.title_label, card.email_label, card.status_tag):
            assert card.rect().contains(child.geometry().center())


def test_today_file_table_has_four_columns_time_and_centered_actions(specialty_window):
    path = specialty_window.service.cfg.received_dir / "long-file-name.txt"
    path.write_text("content", encoding="utf-8")
    specialty_window._populate_files(
        specialty_window.files_table,
        [{
            "saved_filename": path.name,
            "saved_path": str(path),
            "size_bytes": path.stat().st_size,
            "exists_now": True,
            "created_at": "2026-07-13 17:31:46",
        }],
        actions=True,
    )
    assert [
        specialty_window.files_table.horizontalHeaderItem(index).text()
        for index in range(specialty_window.files_table.columnCount())
    ] == ["文件名", "大小", "收取时间", "操作"]
    assert specialty_window.files_table.item(0, 2).text() == "17:31:46"
    assert specialty_window.files_table.item(0, 0).data(
        Qt.ItemDataRole.UserRole
    ) == str(path)
    action = specialty_window.files_table.cellWidget(0, 3)
    buttons = action.findChildren(QPushButton)
    assert {button.text() for button in buttons} == {"打开", "复制路径"}
    assert len({button.height() for button in buttons}) == 1
    assert action.layout().alignment() & Qt.AlignmentFlag.AlignVCenter


def test_rule_save_failure_keeps_previous_effective_config(
    specialty_window, monkeypatch
):
    cfg = specialty_window.service.cfg
    before = (
        cfg.receive_rule_mode,
        cfg.receive_rule_senders,
        cfg.receive_rule_subject_keywords,
        cfg.receive_rule_require_attachment,
    )

    def fail_save(_values):
        raise OSError("disk full")

    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.save_env_values", fail_save
    )
    assert not specialty_window.save_receive_preferences(
        CUSTOM, ("@example.com",), ("report",), True
    )
    assert (
        cfg.receive_rule_mode,
        cfg.receive_rule_senders,
        cfg.receive_rule_subject_keywords,
        cfg.receive_rule_require_attachment,
    ) == before


def test_manual_and_automatic_receive_call_the_same_service_operation(
    specialty_window, monkeypatch
):
    operations = []
    calls = []
    monkeypatch.setattr(
        specialty_window.service,
        "receive",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        specialty_window,
        "_run_task",
        lambda _title, operation, _callback, **_kwargs: operations.append(operation),
    )
    specialty_window.receive()
    specialty_window.auto_switch.setChecked(True)
    specialty_window._automatic_receive()
    assert len(operations) == 2
    operations[0]()
    operations[1]()
    assert calls == [{}, {"automatic": True}]


def test_history_main_table_is_productized_and_detail_is_structured(
    specialty_window, monkeypatch
):
    specialty_window.history_rows = {
        "received": [{
            "subject": "收件主题",
            "message_id": "message-1",
            "status": "saved",
            "created_at": "2026-07-13 11:00:00",
            "body_file_path": "C:/safe/body.md",
            "source": "gmail_api",
            "backend": "gmail_api",
        }],
        "sent": [{
            "original_filename": "report.docx",
            "request_id": "req-private",
            "status": "sent",
            "created_at": "2026-07-13 12:00:00",
            "sent_copy_path": "C:/safe/report.docx",
        }],
    }
    specialty_window.mcp_rows = [{
        "title": "Agent result",
        "request_id": "req-agent",
        "status": "partial",
        "created_at": "2026-07-13 13:00:00",
        "file_path": "",
    }]
    specialty_window._populate_history()
    assert [
        specialty_window.history_table.horizontalHeaderItem(index).text()
        for index in range(specialty_window.history_table.columnCount())
    ] == ["类型", "摘要", "时间", "状态", "操作"]
    visible = [
        specialty_window.history_table.item(row, column).text()
        for row in range(specialty_window.history_table.rowCount())
        for column in range(4)
    ]
    assert "req-private" not in visible
    assert "已保存" in visible and "已发送" in visible and "部分完成" in visible
    assert specialty_window.history_table.cellWidget(0, 4) is not None
    no_file_buttons = {
        button.text(): button
        for button in specialty_window.history_table.cellWidget(0, 4).findChildren(QPushButton)
    }
    assert not no_file_buttons["关联文件"].isEnabled()

    captured = {}
    monkeypatch.setattr(
        specialty_window,
        "_show_structured_detail",
        lambda title, fields: captured.update(title=title, fields=dict(fields)),
    )
    specialty_window._show_history_detail(0, 0)
    assert captured["title"] == "历史记录详情"
    assert {"类型", "摘要", "完整时间", "状态", "原始状态", "request_id", "关联文件", "完整路径", "错误详情", "source", "backend"} <= set(captured["fields"])


def test_files_table_has_no_path_column_and_size_semantics(specialty_window):
    specialty_window.managed_file_rows = [
        {
            "id": "received:1",
            "category": "收件文件",
            "source": "Gmail API",
            "display_name": "zero.txt",
            "path": "C:/safe/zero.txt",
            "size_bytes": 0,
            "size_known": True,
            "time": "2026-07-13 10:00:00",
            "status": "normal",
            "status_display": "已保存",
            "exists": True,
            "file_type": "attachment",
            "mime_type": "text/plain",
            "request_id": "",
            "sha256": "hash",
        },
        {
            "id": "sent:2",
            "category": "已发送归档",
            "source": "手动发件",
            "display_name": "unknown.bin",
            "path": "C:/safe/unknown.bin",
            "size_bytes": None,
            "size_known": False,
            "time": "2026-07-13 09:00:00",
            "status": "sent",
            "status_display": "已发送",
            "exists": True,
        },
        {
            "id": "sent:3",
            "category": "已发送归档",
            "source": "手动发件",
            "display_name": "missing.bin",
            "path": "C:/safe/missing.bin",
            "size_bytes": 0,
            "size_known": True,
            "time": "2026-07-13 08:00:00",
            "status": "missing",
            "status_display": "文件已不存在",
            "exists": False,
        },
    ]
    specialty_window._filter_managed_files()
    assert [
        specialty_window.managed_files_table.horizontalHeaderItem(index).text()
        for index in range(specialty_window.managed_files_table.columnCount())
    ] == ["类型", "来源", "文件名", "大小", "时间", "状态", "操作"]
    sizes = [
        specialty_window.managed_files_table.item(row, 3).text()
        for row in range(3)
    ]
    assert sizes == ["0 B", "—", "文件已不存在"]
    assert all(
        specialty_window.managed_files_table.cellWidget(row, 6) is not None
        for row in range(3)
    )
    assert set(specialty_window.data_overview_values) == {
        "database", "database_size", "received", "sent", "agent", "backups"
    }


def test_managed_preview_open_copy_and_detail_use_real_path(
    specialty_window, specialty_app, tmp_path, monkeypatch
):
    path = specialty_window.service.cfg.data_root_path / "managed.txt"
    path.write_text("preview", encoding="utf-8")
    row = {
        "display_name": path.name,
        "path": str(path),
        "category": "收件文件",
        "source": "Gmail API",
        "size_bytes": path.stat().st_size,
        "size_known": True,
        "time": "2026-07-13 10:00:00",
        "status": "normal",
        "status_display": "已保存",
        "exists": True,
        "file_type": "attachment",
        "mime_type": "text/plain",
        "request_id": "req-1",
        "sha256": "hash",
    }
    specialty_window.managed_file_rows = [row]
    specialty_window._filter_managed_files()
    specialty_window.managed_files_table.selectRow(0)
    previewed = []
    opened = []
    captured = {}
    monkeypatch.setattr(specialty_window, "_preview_path", previewed.append)
    monkeypatch.setattr("agent_mail_bridge.ui.main_window.os.startfile", opened.append)
    monkeypatch.setattr(
        specialty_window,
        "_show_structured_detail",
        lambda title, fields: captured.update(title=title, fields=dict(fields)),
    )
    specialty_window._preview_managed_file(0, 0)
    specialty_window.open_selected_managed_file()
    specialty_window.copy_selected_managed_file_path()
    specialty_app.processEvents()
    specialty_window.show_selected_managed_file_detail()
    assert previewed == [str(path)]
    assert opened == [str(path)]
    assert QApplication.clipboard().text() == str(path)
    assert captured["fields"]["完整路径"] == str(path)
    assert captured["fields"]["request_id"] == "req-1"


def test_data_overview_reads_real_maintenance_values(specialty_window):
    result = specialty_window._collect_refresh_result()
    assert result.ok
    specialty_window._apply_refresh_result(result)
    assert specialty_window.data_overview_values["database"].text() == "正常"
    assert specialty_window.data_overview_values["database_size"].text() != "—"


def test_status_localization_is_stable():
    assert localize_status("saved") == "已保存"
    assert localize_status("sent") == "已发送"
    assert localize_status("failed") == "失败"
    assert localize_status("duplicate") == "重复"
    assert localize_status("partial") == "部分完成"
    assert localize_status("attempt_created") == "处理中"
    assert localize_status("unmapped") == "其他"
