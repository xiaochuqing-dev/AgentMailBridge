"""v1.3.0 邮件详情布局、资源分区、零字节与数据库迁移回归。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QPushButton

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import close_connection, init_db
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.receive_rules import ALL_SCANNED
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font
from agent_mail_bridge.utils import sha256_of_bytes, sha256_of_file


@pytest.fixture(scope="module")
def v13_gui_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def v13_gui_window(v13_gui_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    v13_gui_app.processEvents()
    yield window
    window.request_quit()
    v13_gui_app.processEvents()


def _section_names(window: BridgeWindow) -> list[str]:
    result = []
    layout = window.mail_detail_resource_layout
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is not None and widget.property("resourceSection"):
            result.append(str(widget.property("resourceSection")))
    return result


def test_gui_recipient_defaults_to_owner_but_is_editable(v13_gui_window):
    assert v13_gui_window.recipient_edit.text() == v13_gui_window.service.cfg.owner_gmail
    assert v13_gui_window.recipient_edit.isReadOnly() is False
    v13_gui_window.recipient_edit.setText("outside@example.net")
    v13_gui_window._mark_recipient_edited("outside@example.net")
    v13_gui_window._apply_config_to_controls({"config": {}})
    assert v13_gui_window.recipient_edit.text() == "outside@example.net"
    assert "MCP" in v13_gui_window.recipient_edit.toolTip()


def test_inbox_sender_uses_decoded_human_readable_value(v13_gui_window):
    v13_gui_window._populate_inbox_messages(
        [{
            "package_id": "decoded-sender",
            "subject": "中文发件人显示",
            "from": "测试发件人 <sender@example.com>",
            "body_summary": "正文",
            "received_at": "2026-07-21 10:00:00",
            "archive_status": "ready",
            "counts": {},
        }]
    )
    sender = v13_gui_window.inbox_table.item(0, 1).text()
    assert sender == "测试发件人 <sender@example.com>"
    assert "=?" not in sender


def test_mail_detail_uses_vertical_splitter_with_readable_body_height(v13_gui_window, v13_gui_app):
    splitter = v13_gui_window.mail_detail_splitter
    assert splitter.orientation() == Qt.Orientation.Vertical
    assert splitter.childrenCollapsible() is False
    assert v13_gui_window.mail_detail_body.minimumHeight() >= 240
    assert v13_gui_window.mail_detail_body.minimumHeight() <= 280
    splitter.setSizes([420, 180])
    v13_gui_app.processEvents()
    v13_gui_window._remember_mail_detail_splitter()
    remembered = list(v13_gui_window._mail_detail_splitter_sizes)
    assert len(remembered) == 2 and min(remembered) > 0
    splitter.setSizes(remembered)
    assert splitter.sizes()[0] >= 270


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1320, 660), (1500, 850), (1680, 1000)])
def test_mail_detail_layout_survives_theme_and_logical_dpi_sizes(
    v13_gui_window, v13_gui_app, theme, size
):
    v13_gui_window.apply_theme(theme)
    v13_gui_window.resize(*size)
    v13_gui_app.processEvents()
    assert v13_gui_window.mail_detail_splitter.width() > 0
    assert v13_gui_window.mail_detail_body.height() >= 240
    assert v13_gui_window.mail_detail_resource_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_resource_sections_are_created_only_when_nonempty(v13_gui_window):
    v13_gui_window._populate_mail_detail_resources(
        [{"internal_type": "body_plain", "display_name": "正文"}]
    )
    assert _section_names(v13_gui_window) == []

    v13_gui_window._populate_mail_detail_resources(
        [{"internal_type": "attachment", "display_name": "附件.txt"}]
    )
    assert _section_names(v13_gui_window) == ["附件"]

    mixed = [
        {"internal_type": "inline_image", "display_name": "cid.png", "mime_type": "image/png"},
        *[
            {"internal_type": "attachment", "display_name": f"附件-{index}.dat", "size_bytes": index}
            for index in range(9)
        ],
        *[
            {
                "internal_type": "link",
                "display_name": f"网页 · example.com · page-{index}",
                "url": f"https://example.com/page-{index}",
                "hostname": "example.com",
            }
            for index in range(5)
        ],
    ]
    v13_gui_window._populate_mail_detail_resources(mixed)
    assert _section_names(v13_gui_window) == ["邮件中的图片", "附件", "链接与下载"]
    assert all(
        label.text() != "邮件内容与附件"
        for label in v13_gui_window.mail_detail_page.findChildren(QLabel)
    )


def test_long_filename_tooltip_wrap_zero_size_and_link_copy_action(v13_gui_window):
    long_name = "非常长的中文附件文件名" * 12 + ".dat"
    attachment_card = v13_gui_window._mail_resource_card(
        {
            "internal_type": "attachment",
            "display_name": long_name,
            "kind_display": "附件",
            "status_display": "已保存",
            "size_bytes": 0,
            "mime_type": "application/octet-stream",
        }
    )
    title = next(label for label in attachment_card.findChildren(QLabel) if label.text() == long_name)
    assert title.wordWrap() is True
    assert title.toolTip() == long_name
    assert any("0" in label.text() for label in attachment_card.findChildren(QLabel))

    link_card = v13_gui_window._mail_resource_card(
        {
            "internal_type": "link",
            "display_name": "网页 · example.com · report",
            "kind_display": "网页链接",
            "status_display": "已识别",
            "hostname": "example.com",
            "url": "https://example.com/report?id=123",
        }
    )
    assert {button.text() for button in link_card.findChildren(QPushButton)} == {
        "复制 URL", "打开链接",
    }
    assert next(
        label for label in link_card.findChildren(QLabel) if "网页 · example.com" in label.text()
    ).toolTip() == "https://example.com/report?id=123"


def test_history_rescan_dialog_has_all_ranges_rule_toggle_and_cancel(
    v13_gui_window, monkeypatch
):
    monkeypatch.setattr("agent_mail_bridge.ui.main_window.QDialog.exec", lambda _dialog: 0)
    v13_gui_window.open_history_rescan_dialog()
    dialog = v13_gui_window._history_rescan_dialog
    assert dialog is not None
    combo = dialog.findChild(QComboBox, "historyRescanRange")
    assert [combo.itemText(index) for index in range(combo.count())] == [
        "最近 24 小时", "最近 7 天", "最近 30 天", "自定义日期范围",
    ]
    assert dialog.findChild(QCheckBox, "historyRescanApplyRule").isChecked() is True
    assert dialog.findChild(QPushButton, "historyRescanStart").text() == "开始补扫"
    assert dialog.findChild(QPushButton, "historyRescanCancel").text() == "关闭"


def test_zero_byte_attachment_keeps_hash_prepares_and_displays(tmp_cfg, tmp_path):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    tmp_cfg.mcp_mail_read_enabled = True
    workspace = tmp_path / "authorized-workspace"
    workspace.mkdir()
    tmp_cfg.allowed_send_roots = [workspace]
    message = EmailMessage()
    message["From"] = "outside@example.com"
    message["To"] = tmp_cfg.gmail_address
    message["Subject"] = "零字节附件"
    message["Message-ID"] = "<zero-byte-v13@example.com>"
    message["Date"] = format_datetime(datetime(2026, 7, 20, 12, 0).astimezone())
    message.set_content("body")
    message.add_attachment(
        b"", maintype="application", subtype="octet-stream", filename="空附件.dat"
    )
    raw = message.as_bytes()
    normalized = normalized_mail_from_raw(
        raw,
        backend="gmail_api",
        backend_message_id="zero-byte-provider",
        thread_id="zero-byte-thread",
        uid="",
        received_at="2026-07-20 12:00:00",
        saved_date="2026-07-20",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="gmail:me/inbox",
    )
    stored = process_normalized_mail(tmp_cfg, normalized)
    service = ApplicationService(tmp_cfg)
    mail = service.get_mail(stored["package_id"]).details["mail"]
    attachment = next(item for item in mail["resources"] if item["internal_type"] == "attachment")
    assert attachment["size_bytes"] == 0
    assert attachment["sha256"] == sha256_of_bytes(b"")
    prepared = service.prepare_mail_resources(stored["package_id"], [attachment["resource_id"]])
    assert prepared.ok
    target = Path(prepared.details["prepared"][0]["prepared_path"])
    assert target.stat().st_size == 0
    assert sha256_of_file(target) == attachment["sha256"]


OLD_MAIL_PACKAGES_SQL = """
CREATE TABLE mail_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL UNIQUE,
    account_ref TEXT NOT NULL,
    mailbox_ref TEXT NOT NULL,
    backend TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_ref TEXT,
    received_at TEXT,
    archive_status TEXT,
    package_root TEXT
)
"""


def test_v121_database_migrates_idempotently_without_losing_mail(tmp_path):
    path = tmp_path / "v121.db"
    connection = sqlite3.connect(path)
    connection.execute(OLD_MAIL_PACKAGES_SQL)
    connection.execute(
        "INSERT INTO mail_packages "
        "(package_id, account_ref, mailbox_ref, backend, message_id, thread_ref, received_at, archive_status, package_root) "
        "VALUES ('pkg-old', 'gmail:test', 'gmail:inbox', 'gmail_api', '<old@test>', 'thread-old', "
        "'2026-07-20 12:00:00', 'ready', 'old-root')"
    )
    connection.commit()
    connection.close()
    init_db(path)
    init_db(path)
    close_connection()
    check = sqlite3.connect(path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(mail_packages)")}
    assert {
        "contacts_json", "from_raw_header", "reply_to_raw_header",
        "outbound_origin", "outbound_id", "local_outbound",
    } <= columns
    assert check.execute("SELECT message_id FROM mail_packages WHERE package_id='pkg-old'").fetchone()[0] == "<old@test>"
    assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    check.close()


def test_database_migration_failure_rolls_back_all_v13_schema_changes(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    connection = sqlite3.connect(path)
    connection.execute(OLD_MAIL_PACKAGES_SQL)
    connection.commit()
    connection.close()

    def fail_after_probe(connection):
        connection.execute("ALTER TABLE mail_packages ADD COLUMN migration_probe TEXT")
        raise sqlite3.OperationalError("forced migration failure")

    monkeypatch.setattr("agent_mail_bridge.database._migrate_mail_packages_v13", fail_after_probe)
    with pytest.raises(sqlite3.OperationalError, match="forced migration failure"):
        init_db(path)
    close_connection()
    check = sqlite3.connect(path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(mail_packages)")}
    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "migration_probe" not in columns
    assert "receive_rule_evaluations" not in tables
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
