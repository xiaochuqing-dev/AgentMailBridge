"""v1.1.0 摘要、事实搜索、中文编码和技术日志长期运行收口。"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import default
from unittest.mock import MagicMock

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    app_event_overview,
    clear_all_app_events,
    configure_app_event_retention,
    get_connection,
    log_event,
    prune_app_events,
    query_app_events,
    query_recent_events,
    store_mail_archive_atomically,
)
from agent_mail_bridge.gmail_api_receive import receive_gmail_api_messages
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_summaries import (
    MAIL_LIST_PREVIEW_CHARS,
    build_mail_list_summary,
    build_outbound_list_summary,
    compact_readable_text,
)
from agent_mail_bridge.ui.theme import build_stylesheet
from agent_mail_bridge.utils import decode_mime_header


def test_shared_summaries_are_bounded_and_always_show_resource_facts():
    body = "# 标题\n\n**很长正文** " + "中文与 Emoji🙂 " * 30
    summary = build_mail_list_summary(
        body,
        attachment_count=7,
        inline_image_count=1,
        link_count=3,
        downloaded_count=2,
    )
    assert "#" not in summary and "**" not in summary
    assert "7 个附件" in summary
    assert "1 张邮件图片" in summary
    assert "3 个链接" in summary
    assert "2 个已下载文件" in summary
    assert "…" in summary
    assert summary.splitlines()[0].startswith("7 个附件")
    assert len(summary.splitlines()[-1]) <= MAIL_LIST_PREVIEW_CHARS + 1
    assert body.startswith("# 标题")

    outbound = build_outbound_list_summary(
        "发件正文 " * 80, attachment_count=3, link_count=2,
        source_origin="agent_mcp",
    )
    assert "3 个附件" in outbound and "2 个链接" in outbound
    assert outbound.splitlines()[0] == "3 个附件 · 2 个链接"
    assert "request_id" not in outbound
    assert compact_readable_text("a\n\n b\t c") == "a b c"


def test_dark_table_hover_has_explicit_dark_and_mail_row_overrides():
    stylesheet = build_stylesheet("dark")
    assert "QTableWidget::item:hover { background: #2A2E3E" in stylesheet
    assert "QTableWidget#mailRecordTable::item:hover" in stylesheet
    assert "selection-background-color: transparent" in stylesheet


def test_log_retention_choices_fall_back_to_supported_defaults():
    cfg = AppConfig(
        normal_log_retention_days=15,
        warning_error_log_retention_days=45,
        app_event_max_count=1234,
    )
    assert cfg.normal_log_retention_days == 30
    assert cfg.warning_error_log_retention_days == 90
    assert cfg.app_event_max_count == 10_000


def _store_search_package(tmp_cfg) -> None:
    now = "2026-07-16 10:00:00"
    package = {
        "package_id": "pkg-search-v110",
        "account_ref": "gmail:owner@example.com",
        "mailbox_ref": "inbox",
        "backend": "gmail_api",
        "message_id": "<search-v110@example.com>",
        "provider_message_id": "gmail-1",
        "thread_ref": "gmail:thread-1",
        "subject": "季度总结与部署方案",
        "from_email": "张三 <Sender@Example.com>",
        "to_emails": "owner@example.com, teammate@example.com",
        "cc_emails": "review@example.net",
        "bcc_emails": "hidden@example.org",
        "received_at": now,
        "saved_at": now,
        "package_root": str(tmp_cfg.received_dir / "mail" / "pkg-search-v110"),
        "raw_eml_status": "available",
        "search_text": "这里是完整可读正文，包含发布口令蓝鲸计划和部署方案。",
        "resource_count": 3,
        "attachment_count": 1,
        "inline_image_count": 1,
        "link_count": 1,
        "downloaded_count": 0,
        "archive_status": "needs_attention",
        "parse_status": "partial",
    }
    resources = [
        {
            "resource_id": "res-attachment",
            "resource_type": "attachment",
            "source_type": "mime_attachment",
            "display_name": "超长中文专项报告 终稿.xlsx",
            "original_name": "超长中文专项报告 终稿.xlsx",
            "status": "saved",
            "sort_order": 1,
        },
        {
            "resource_id": "res-image",
            "resource_type": "inline_image",
            "source_type": "mime_inline",
            "display_name": "中文流程图.png",
            "original_name": "中文流程图.png",
            "content_id": "diagram-1",
            "status": "saved",
            "sort_order": 2,
        },
        {
            "resource_id": "res-link",
            "resource_type": "link",
            "source_type": "html_anchor",
            "display_name": "项目说明页面",
            "original_url": "https://docs.example.cn/releases/path?参数=蓝鲸",
            "status": "detected",
            "sort_order": 3,
        },
    ]
    store_mail_archive_atomically(tmp_cfg.db_path, package, resources, [])


def test_mail_facts_search_covers_recipients_body_resources_links_status_and_dedup(tmp_cfg):
    _store_search_package(tmp_cfg)
    service = ApplicationService(tmp_cfg)
    for query in (
        "季度总结",
        "张三",
        "sender@example",
        "teammate@",
        "review@example",
        "hidden@example",
        "蓝鲸计划",
        "中文专项报告",
        "中文流程图",
        "中文",
        "项目说明页面",
        "docs.example.cn",
        "releases/path?参数",
        "需要处理",
        "部分完成",
        "蓝鲸 部署",
    ):
        rows = service.search_mail_facts(query).details["messages"]
        assert [row["package_id"] for row in rows] == ["pkg-search-v110"], query


def _raw_text_message(payload: bytes, charset: str | None) -> bytes:
    charset_part = f"; charset={charset}" if charset else ""
    encoded = base64.b64encode(payload)
    return (
        "Subject: =?UTF-8?B?5Lit5paH5Li76aKY?=\r\n"
        "From: sender@example.com\r\n"
        "To: owner@example.com\r\n"
        "Message-ID: <encoding@example.com>\r\n"
        f"Content-Type: text/plain{charset_part}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
    ).encode("ascii") + encoded + b"\r\n"


def _normalize(raw: bytes):
    return normalized_mail_from_raw(
        raw,
        backend="imap",
        backend_message_id="1",
        thread_id="",
        uid="1",
        received_at="2026-07-16 10:00:00",
        saved_date="2026-07-16",
        max_attachment_bytes=1024 * 1024,
        mailbox_ref="inbox",
    )


def test_chinese_decode_matrix_and_rfc2231_filename():
    folded_mixed = (
        "AgentMailBridge v1.1.0 "
        "=?utf-8?b?55yf5a6e5omL5Yqo5Y+R5Lu2?=\r\n V110-marker"
    )
    assert decode_mime_header(folded_mixed) == (
        "AgentMailBridge v1.1.0 真实手动发件 V110-marker"
    )
    cases = (
        ("简体中文 UTF-8 🙂", "utf-8", "utf-8"),
        ("简体中文 GBK", "gbk", "gbk"),
        ("简体中文 GB2312", "gb2312", "gb2312"),
        ("繁體中文 Big5", "big5", "big5"),
    )
    for text, codec, declared in cases:
        mail = _normalize(_raw_text_message(text.encode(codec), declared))
        assert mail.subject == "中文主题"
        assert mail.body_text == text
        assert "?" * 4 not in mail.body_text and "�" not in mail.body_text

    no_charset = _normalize(_raw_text_message("无声明中文".encode("gbk"), None))
    no_charset_big5_text = "繁體中文沒有字符集聲明"
    no_charset_big5 = _normalize(
        _raw_text_message(no_charset_big5_text.encode("big5"), None)
    )
    wrong_charset = _normalize(
        _raw_text_message("错误声明仍是中文🙂".encode("utf-8"), "iso-8859-1")
    )
    wrong_charset_gbk = _normalize(
        _raw_text_message("错误声明仍可读".encode("gbk"), "iso-8859-1")
    )
    assert no_charset.body_text == "无声明中文"
    assert no_charset_big5.body_text == no_charset_big5_text
    assert wrong_charset.body_text == "错误声明仍是中文🙂"
    assert wrong_charset_gbk.body_text == "错误声明仍可读"

    message = EmailMessage(policy=default)
    message["Subject"] = "附件名"
    message["From"] = "sender@example.com"
    message["To"] = "owner@example.com"
    message["Message-ID"] = "<filename@example.com>"
    message.set_content("HTML 与附件名测试🙂")
    message.add_attachment(
        b"content",
        maintype="application",
        subtype="octet-stream",
        filename="中文 附件 长文件名.txt",
    )
    parsed = _normalize(message.as_bytes())
    assert parsed.attachments[0].filename == "中文 附件 长文件名.txt"


def test_automatic_no_change_does_not_write_permanent_events(tmp_cfg):
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": []}

    automatic = receive_gmail_api_messages(
        tmp_cfg, service, limit=10, automatic=True
    )
    assert automatic["saved"] == 0
    assert query_recent_events(tmp_cfg.db_path, 20) == []

    manual = receive_gmail_api_messages(
        tmp_cfg, service, limit=10, automatic=False
    )
    assert manual["saved"] == 0
    assert len(query_recent_events(tmp_cfg.db_path, 20)) == 2


def test_one_day_minute_poll_simulation_stays_quiet(tmp_cfg):
    """方案 B：批量模拟最密集的一分钟轮询，覆盖完整 24 小时。"""
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    for _minute in range(24 * 60):
        result = receive_gmail_api_messages(
            tmp_cfg, service, limit=10, automatic=True
        )
        assert result["saved"] == 0 and result["failed"] == 0
    assert query_recent_events(tmp_cfg.db_path, 20) == []


def test_app_event_hard_cap_shrinks_immediately_to_target(tmp_cfg):
    configure_app_event_retention(tmp_cfg.db_path, max_count=100)
    for index in range(101):
        log_event(tmp_cfg.db_path, "INFO", "system", f"event-{index}")
    connection = get_connection(tmp_cfg.db_path)
    count = 101
    deadline = time.monotonic() + 2
    while count != 80 and time.monotonic() < deadline:
        time.sleep(0.01)
        count = int(
            connection.execute("SELECT COUNT(*) FROM app_events").fetchone()[0]
        )
    assert count == 80


def test_log_filters_paging_and_redacted_export(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    log_event(
        tmp_cfg.db_path,
        "ERROR",
        "receive",
        "owner@example.com C:\\Private\\mail.eml token=super-secret 网络失败",
    )
    log_event(tmp_cfg.db_path, "SUCCESS", "send", "发送成功")

    page = service.query_logs(
        level="ERROR", category="收件", search="网络 失败", limit=1
    )
    assert page.ok and page.details["total"] == 1
    assert page.details["events"][0]["category"] == "收件"

    destination = tmp_path / "filtered.csv"
    exported = service.export_filtered_logs(
        destination, level="ERROR", category="收件", search="网络"
    )
    assert exported.ok and exported.details["count"] == 1
    text = destination.read_text(encoding="utf-8-sig")
    assert "o***@example.com" in text
    assert "super-secret" not in text
    assert "C:\\Private" not in text
    assert "[本地路径已隐藏]" in text


def test_app_event_retention_filters_prunes_and_never_touches_business_tables(tmp_cfg):
    _store_search_package(tmp_cfg)
    connection = get_connection(tmp_cfg.db_path)
    before_business = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "mail_packages", "mail_resources", "outbound_messages", "sent_files",
            "mcp_calls", "receive_retries",
        )
    }
    now = datetime(2026, 7, 16, 12, 0, 0)
    events = [
        ("INFO", "system", "普通旧日志", now - timedelta(days=31)),
        ("ERROR", "system", "错误旧日志", now - timedelta(days=91)),
        ("ERROR", "system", "仍需保留的错误", now - timedelta(days=60)),
        ("INFO", "receive", "开始收取邮件（limit=None）", now - timedelta(days=1)),
        ("SUCCESS", "receive", "检查完成，暂无新邮件", now - timedelta(days=1)),
    ]
    connection.executemany(
        "INSERT INTO app_events(level, event_type, message, created_at) VALUES (?, ?, ?, ?)",
        [(level, kind, message, moment.strftime("%Y-%m-%d %H:%M:%S")) for level, kind, message, moment in events],
    )
    connection.commit()

    filtered = query_app_events(tmp_cfg.db_path, include_daily_checks=False)
    assert filtered["total"] == 3
    overview = app_event_overview(tmp_cfg.db_path, normal_days=30, error_days=90)
    assert overview["daily_checks"] == 2 and overview["expired"] == 2
    result = prune_app_events(
        tmp_cfg.db_path, normal_days=30, error_days=90, max_count=100, now=now
    )
    assert result["deleted_by_age"] == 2
    assert result["after"] == 3

    connection.executemany(
        "INSERT INTO app_events(level, event_type, message, created_at) VALUES ('INFO', 'system', ?, ?)",
        [(f"current-{index}", now.strftime("%Y-%m-%d %H:%M:%S")) for index in range(105)],
    )
    connection.commit()
    capped = prune_app_events(
        tmp_cfg.db_path, normal_days=30, error_days=90, max_count=100, now=now
    )
    assert capped["after"] == 80
    assert capped["deleted_by_count"] == 28

    after_business = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in before_business
    }
    assert after_business == before_business
    assert clear_all_app_events(tmp_cfg.db_path) == 80
    assert {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in before_business
    } == before_business
