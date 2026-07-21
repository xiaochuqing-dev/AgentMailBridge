"""v1.3.0 联系人、GUI 发件边界、回流标记和链接展示专项回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import get_outbound_message
from agent_mail_bridge.mail_archive import _contact_facts
from agent_mail_bridge.mail_common import (
    format_mail_address_header,
    normalized_mail_from_raw,
    parse_mail_address_header,
)
from agent_mail_bridge.mail_facts import get_mail_message
from agent_mail_bridge.mail_links import classify_mail_link, detect_mail_links
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mail_send import (
    OUTBOUND_ID_HEADER,
    OUTBOUND_ORIGIN_HEADER,
    normalize_manual_recipient,
    send_outbound_mail,
)
from agent_mail_bridge.receive_rules import ALL_SCANNED


RFC822_CONTACT_FIXTURE = (
    b"From: =?utf-8?B?5Lit5paH5Y+R5Lu25Lq6?= <sender@example.com>\r\n"
    b"To: Primary <primary@example.com>, =?utf-8?B?5paw55So5oi3?= <second@example.net>\r\n"
    b"Cc: Copy Person <copy@example.org>\r\n"
    b"Bcc: hidden@example.net\r\n"
    b"Reply-To: =?utf-8?B?5Zue5aSN6IGU57O75Lq6?= <reply@example.com>\r\n"
    b"Subject: =?utf-8?B?6IGU57O75Lq66Kej56CB5rWL6K+V?=\r\n"
    b"Message-ID: <contacts-v13@example.com>\r\n"
    b"Date: Mon, 20 Jul 2026 12:00:00 +0800\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    + "详细说明见 https://docs.python.org/3/library/email.html ，另一个页面是 https://example.com/report?id=123。".encode("utf-8")
)


@pytest.mark.parametrize(
    ("raw", "display", "address"),
    (
        ("=?utf-8?B?5Lit5paH?= <user@example.com>", "中文", "user@example.com"),
        ("=?utf-8?B?5Lit?= =?utf-8?B?5paH?= <user@example.com>", "中文", "user@example.com"),
        ("Plain User <plain@example.com>", "Plain User", "plain@example.com"),
        ("bare@example.com", "", "bare@example.com"),
        ("Broken Name <recover@example.com", "Broken Name", "recover@example.com"),
    ),
)
def test_address_header_parser_decodes_and_preserves_address(raw, display, address):
    parsed = parse_mail_address_header(raw)
    assert parsed
    assert parsed[0].display_name == display
    assert parsed[0].address == address
    assert parsed[0].raw_header == raw
    assert "=?" not in format_mail_address_header(raw)


def test_multiple_to_cc_bcc_reply_to_are_structured_and_raw_is_unchanged(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    mail = normalized_mail_from_raw(
        RFC822_CONTACT_FIXTURE,
        backend="gmail_api",
        backend_message_id="contacts-provider-id",
        thread_id="contacts-thread",
        uid="",
        received_at="2026-07-20 12:00:00",
        saved_date="2026-07-20",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="gmail:me/inbox",
    )
    stored = process_normalized_mail(tmp_cfg, mail)
    assert stored["status"] == "saved"
    message = get_mail_message(tmp_cfg.db_path, stored["package_id"])
    assert message is not None
    assert message["from_display"] == "中文发件人"
    assert message["from_address"] == "sender@example.com"
    assert [item["address"] for item in message["to_addresses"]] == [
        "primary@example.com", "second@example.net",
    ]
    assert message["cc_addresses"][0]["display_name"] == "Copy Person"
    assert message["bcc_addresses"][0]["address"] == "hidden@example.net"
    assert message["reply_to"][0]["display_name"] == "回复联系人"
    assert "=?utf-8?" in message["raw_headers"]["from"].lower()
    raw_path = Path(message["package_root"]) / str(message["raw_eml"]["path"])
    assert raw_path.read_bytes() == RFC822_CONTACT_FIXTURE
    manifest = json.loads((Path(message["package_root"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["contacts"]["from"][0]["address"] == "sender@example.com"
    assert manifest["metadata"]["raw_headers"]["reply_to"].startswith("=?utf-8?")


def test_decoded_name_and_normalized_address_are_searchable_and_mcp_compatible(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    tmp_cfg.mcp_mail_read_enabled = True
    mail = normalized_mail_from_raw(
        RFC822_CONTACT_FIXTURE,
        backend="gmail_api",
        backend_message_id="contacts-search",
        thread_id="contacts-thread",
        uid="",
        received_at="2026-07-20 12:00:00",
        saved_date="2026-07-20",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="gmail:me/inbox",
    )
    stored = process_normalized_mail(tmp_cfg, mail)
    service = ApplicationService(tmp_cfg)
    for query in ("中文发件人", "sender@example.com", "回复联系人"):
        result = service.search_mails(query=query, time_scope="all")
        assert result.details["result_count"] == 1
    summary = service.search_mails(query="中文发件人", time_scope="all").details["messages"][0]
    assert summary["mail_id"] == stored["package_id"]
    assert summary["from"] == "中文发件人 <sender@example.com>"
    assert summary["from_display"] == "中文发件人"
    assert summary["from_address"] == "sender@example.com"
    assert summary["to"] == ["primary@example.com", "second@example.net"]
    assert len(summary["to_addresses"]) == 2
    assert "=?" not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.parametrize(
    "value",
    ("", "not-an-email", "@example.com", "a@example.com,b@example.com", "a@example.com\r\nBcc:x@example.com"),
)
def test_gui_manual_recipient_rejects_empty_invalid_multiple_and_injection(value):
    with pytest.raises(ValueError):
        normalize_manual_recipient(value)


def test_gui_manual_send_uses_explicit_recipient_and_outbound_headers(tmp_cfg, monkeypatch):
    captured = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, message: captured.append(message),
    )
    result = send_outbound_mail(
        subject="显式外部收件人",
        body_text="仅由 GUI 用户明确发起",
        attachment_paths=[],
        links=[],
        cfg=tmp_cfg,
        recipient="outside@example.net",
        source_origin="manual_gui",
    )
    assert result["send_status"] == "sent"
    assert result["to"] == "outside@example.net"
    assert len(captured) == 1
    message = captured[0]
    assert message["To"] == "outside@example.net"
    assert message[OUTBOUND_ORIGIN_HEADER] == "outbound"
    assert message[OUTBOUND_ID_HEADER] == result["outbound_id"]
    serialized = message.as_string()
    assert tmp_cfg.qq_auth_code not in serialized
    assert tmp_cfg.gmail_app_password not in serialized
    outbound = get_outbound_message(tmp_cfg.db_path, result["outbound_id"])
    assert outbound is not None
    assert outbound["to"] == ["outside@example.net"]


def test_loop_origin_requires_matching_local_outbound_record(tmp_cfg, monkeypatch):
    monkeypatch.setattr("agent_mail_bridge.mail_send._smtp_send_with_stage", lambda *_args: None)
    sent = send_outbound_mail(
        subject="回流事实",
        body_text="body",
        attachment_paths=[],
        links=[],
        cfg=tmp_cfg,
        recipient="outside@example.net",
    )
    local = _contact_facts(
        tmp_cfg,
        from_raw=tmp_cfg.qq_email,
        to_raw=tmp_cfg.gmail_address,
        cc_raw="",
        bcc_raw="",
        reply_to_raw="",
        outbound_origin="outbound",
        outbound_id=sent["outbound_id"],
    )
    manual_same_sender = _contact_facts(
        tmp_cfg,
        from_raw=tmp_cfg.qq_email,
        to_raw=tmp_cfg.gmail_address,
        cc_raw="",
        bcc_raw="",
        reply_to_raw="",
        outbound_origin="",
        outbound_id="",
    )
    spoof_without_record = _contact_facts(
        tmp_cfg,
        from_raw=tmp_cfg.qq_email,
        to_raw=tmp_cfg.gmail_address,
        cc_raw="",
        bcc_raw="",
        reply_to_raw="",
        outbound_origin="outbound",
        outbound_id="out_not_local",
    )
    spoof_known_id_from_other_sender = _contact_facts(
        tmp_cfg,
        from_raw="attacker@example.net",
        to_raw=tmp_cfg.gmail_address,
        cc_raw="",
        bcc_raw="",
        reply_to_raw="",
        outbound_origin="outbound",
        outbound_id=sent["outbound_id"],
    )
    assert local["local_outbound"] is True
    assert manual_same_sender["local_outbound"] is False
    assert spoof_without_record["local_outbound"] is False
    assert spoof_known_id_from_other_sender["local_outbound"] is False


def test_link_detection_does_not_depend_on_prompt_words_and_handles_all_sources():
    plain = (
        "详细说明见 https://docs.python.org/3/library/email.html ，"
        "另一个页面是 https://example.com/report?id=123。"
    )
    assert all(term not in plain for term in ("链接", "URL", "下载", "地址"))
    html = (
        '<a href="https://example.com/report?id=123">view</a>'
        '<a href="https://example.com/file.pdf">完整测试报告</a>'
        '<img src="https://example.com/image.png" alt="测试图片">'
        '<img src="cid:inline-image">'
    )
    links = detect_mail_links(plain, html)
    assert {item["url"] for item in links} == {
        "https://docs.python.org/3/library/email.html",
        "https://example.com/report?id=123",
        "https://example.com/file.pdf",
        "https://example.com/image.png",
    }
    report = next(item for item in links if "report?id=123" in item["url"])
    assert report["display_name"] != "view"
    assert "example.com" in report["display_name"]
    assert all(not item["url"].startswith("cid:") for item in links)


@pytest.mark.parametrize(
    ("url", "source", "anchor", "expected"),
    (
        ("https://drive.google.com/file/d/1/view", "html_href", "view", "Google Drive 文档"),
        ("https://example.com/report.pdf", "plain_text", "", "PDF 下载"),
        ("https://example.com/image.png", "html_image", "", "图片链接"),
        ("https://example.com/report?id=1", "html_href", "report", "网页"),
    ),
)
def test_productized_link_names_include_type_and_hostname(url, source, anchor, expected):
    item = classify_mail_link(url, source_type=source, anchor_text=anchor)
    assert item is not None
    assert expected in item["display_name"]
    assert item["hostname"] in item["display_name"]
    assert item["display_name"] not in {"view", "report"}
