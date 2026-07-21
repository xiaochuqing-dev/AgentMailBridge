"""Gmail API 收件测试（使用 mock service，不发起真实 API 请求）。

覆盖：
- messages.list 返回空列表
- messages.list 返回多个消息
- messages.get 返回 text/plain body
- messages.get 返回 multipart body
- messages.get 返回附件
- RFC Message-ID 存在时用 RFC Message-ID 去重
- RFC Message-ID 不存在时用 gmail_api:<id> 去重
- From/To 自发自收过滤
- 重复运行不重复保存
"""

from __future__ import annotations

import base64
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    close_connection,
    init_db,
    message_id_exists,
    query_received_files_by_message,
    query_received_messages_by_date,
)
from agent_mail_bridge.gmail_api_receive import receive_gmail_api_messages
from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.storage import ensure_data_dirs


def _b64url(data: bytes) -> str:
    """编码为 base64url（去 padding，符合 Gmail API）。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_service(messages_list_resp=None, messages_get_map=None):
    """构造一个 mock Gmail API service。

    messages_list_resp: list() 的返回 dict（含 messages 列表）。
    messages_get_map: {message_id: message_dict} get() 的返回。
    """
    service = MagicMock()
    users = service.users.return_value
    msgs = users.messages.return_value

    if messages_list_resp is None:
        messages_list_resp = {"messages": []}
    msgs.list.return_value.execute.return_value = messages_list_resp

    get_map = messages_get_map or {}

    def _get_execute():
        # 返回一个 MagicMock，其 execute 返回对应 message
        # 由于 .get(id=...).execute() 链式调用，需记录 id
        return MagicMock()

    # 简化：让 .get(...).execute() 根据调用的 id 返回
    call_state = {"last_id": None}

    def _get(id=None, **kwargs):
        call_state["last_id"] = id
        m = get_map.get(id, {})
        result = MagicMock()
        result.execute.return_value = m
        return result

    msgs.get.side_effect = _get

    # attachments
    atts = msgs.attachments.return_value
    atts.get.return_value.execute.return_value = {}

    return service


def _make_message(
    *,
    gmail_id="m1",
    thread_id="t1",
    rfc_message_id="<abc@gmail.com>",
    from_header="user@gmail.com",
    to_header="user@gmail.com",
    subject="测试邮件",
    body_text="正文内容",
    html_text=None,
    attachments=None,
    internal_date_ms=1720000000000,
):
    """构造一个 Gmail API message dict。"""
    raw_message = EmailMessage()
    raw_message["From"] = from_header
    raw_message["To"] = to_header
    raw_message["Subject"] = subject
    raw_message["Date"] = format_datetime(
        datetime.fromtimestamp(internal_date_ms / 1000.0).astimezone()
    )
    if rfc_message_id:
        raw_message["Message-ID"] = rfc_message_id
    if html_text and not body_text:
        raw_message.set_content(html_text, subtype="html")
    else:
        raw_message.set_content(body_text or "")
    for att in attachments or []:
        maintype, subtype = att.get("mime", "application/octet-stream").split("/", 1)
        raw_message.add_attachment(
            b"attachment-data", maintype=maintype, subtype=subtype,
            filename=att["filename"],
        )
    headers = [
        {"name": "From", "value": from_header},
        {"name": "To", "value": to_header},
        {"name": "Subject", "value": subject},
    ]
    if rfc_message_id:
        headers.append({"name": "Message-ID", "value": rfc_message_id})

    payload: dict = {"headers": headers}

    if attachments:
        parts = []
        # 正文部分
        parts.append({
            "mimeType": "text/plain",
            "body": {"data": _b64url(body_text.encode("utf-8"))},
        })
        # 附件部分
        for att in attachments:
            parts.append({
                "mimeType": att.get("mime", "application/octet-stream"),
                "filename": att["filename"],
                "body": {"attachmentId": att["attachment_id"]},
            })
        payload["mimeType"] = "multipart/mixed"
        payload["parts"] = parts
    elif html_text and not body_text:
        payload["mimeType"] = "text/html"
        payload["body"] = {"data": _b64url(html_text.encode("utf-8"))}
    elif body_text:
        payload["mimeType"] = "text/plain"
        payload["body"] = {"data": _b64url(body_text.encode("utf-8"))}

    return {
        "id": gmail_id,
        "threadId": thread_id,
        "internalDate": str(internal_date_ms),
        "payload": payload,
        "raw": _b64url(raw_message.as_bytes()),
    }


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> AppConfig:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        '{"installed":{"client_id":"1234567890-fake.apps.googleusercontent.com",'
        '"client_secret":"fake-client-secret-for-tests-only",'
        '"auth_uri":"https://accounts.google.com/o/oauth2/auth",'
        '"token_uri":"https://oauth2.googleapis.com/token",'
        '"redirect_uris":["http://localhost"]}}',
        encoding="utf-8",
    )
    cfg = AppConfig(
        gmail_address="user@gmail.com",
        gmail_app_password="",
        qq_email="test@qq.com",
        qq_auth_code="code",
        owner_gmail="user@gmail.com",
        data_root=tmp_path / "data",
        gmail_receive_backend="gmail_api",
        gmail_api_credentials_path=credentials_path,
        gmail_api_token_path=tmp_path / "token.json",
        gmail_api_max_results=20,
        gmail_api_query="in:inbox",
        gmail_api_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    ensure_data_dirs(cfg)
    init_db(cfg.db_path)
    yield cfg
    close_connection()


class TestEmptyList:
    def test_empty_list(self, tmp_cfg):
        service = _make_service(messages_list_resp={"messages": []})
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["ok"] is True
        assert result["fetched"] == 0
        assert result["saved"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []


class TestSingleMessage:
    def test_text_plain_body(self, tmp_cfg):
        msg = _make_message(gmail_id="m1", body_text="Hello World")
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["ok"] is True
        assert result["fetched"] == 1
        assert result["saved"] == 1
        assert result["skipped"] == 0

        # 验证数据库写入
        rows = query_received_messages_by_date(tmp_cfg.db_path, _saved_date(msg))
        assert len(rows) == 1
        assert rows[0]["source"] == "gmail_api"
        assert rows[0]["gmail_message_id"] == "m1"
        assert rows[0]["gmail_thread_id"] == "t1"
        assert rows[0]["backend"] == "gmail_api"
        facts = ApplicationService(tmp_cfg).list_mail_messages().details["messages"][0]
        assert (Path(facts["package_root"]) / "raw.eml").read_bytes() == decode_raw(msg["raw"])
        assert any(call.kwargs.get("format") == "raw" for call in service.users().messages().get.call_args_list)

    def test_dedup_rfc_message_id(self, tmp_cfg):
        msg = _make_message(gmail_id="m1", rfc_message_id="<abc@gmail.com>")
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        # 第一次保存
        r1 = receive_gmail_api_messages(tmp_cfg, service=service)
        assert r1["saved"] == 1
        # 第二次应跳过
        r2 = receive_gmail_api_messages(tmp_cfg, service=service)
        assert r2["saved"] == 0
        assert r2["skipped"] == 1

    def test_dedup_gmail_api_id_when_no_rfc(self, tmp_cfg):
        msg = _make_message(gmail_id="m1", rfc_message_id="")
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        r1 = receive_gmail_api_messages(tmp_cfg, service=service)
        assert r1["saved"] == 1
        # 无 RFC Message-ID 时仍生成稳定跨后端语义键。
        rows = query_received_messages_by_date(tmp_cfg.db_path, _saved_date(msg))
        assert rows and rows[0]["message_id"].startswith("<generated-")
        r2 = receive_gmail_api_messages(tmp_cfg, service=service)
        assert r2["saved"] == 0
        assert r2["skipped"] == 1


class TestMultipleMessages:
    def test_multiple_messages(self, tmp_cfg):
        msgs = {
            "m1": _make_message(gmail_id="m1", rfc_message_id="<a@gmail.com>",
                                subject="邮件A"),
            "m2": _make_message(gmail_id="m2", rfc_message_id="<b@gmail.com>",
                                subject="邮件B"),
        }
        service = _make_service(
            messages_list_resp={"messages": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t2"},
            ]},
            messages_get_map=msgs,
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["fetched"] == 2
        assert result["saved"] == 2
        assert result["skipped"] == 0


class TestMultipart:
    def test_multipart_body(self, tmp_cfg):
        msg = _make_message(
            gmail_id="m1",
            body_text="纯文本部分",
            rfc_message_id="<multi@gmail.com>",
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["saved"] == 1
        # 验证正文文件包含文本
        rows = query_received_messages_by_date(
            tmp_cfg.db_path, _saved_date(msg)
        )
        body_path = Path(rows[0]["body_file_path"])
        content = body_path.read_text(encoding="utf-8")
        assert "纯文本部分" in content
        assert "source: gmail_api" in content

    def test_html_only_body(self, tmp_cfg):
        msg = _make_message(
            gmail_id="m1",
            body_text="",
            html_text="<p>HTML正文</p>",
            rfc_message_id="<html@gmail.com>",
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["saved"] == 1
        rows = query_received_messages_by_date(
            tmp_cfg.db_path, _saved_date(msg)
        )
        body_path = Path(rows[0]["body_file_path"])
        content = body_path.read_text(encoding="utf-8")
        assert "HTML正文" in content  # HTML 被清洗为文本


class TestAttachments:
    def test_attachment_saved(self, tmp_cfg):
        msg = _make_message(
            gmail_id="m1",
            rfc_message_id="<att@gmail.com>",
            body_text="见附件",
            attachments=[{
                "filename": "报告.pdf",
                "attachment_id": "aid1",
                "mime": "application/pdf",
            }],
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        # 让 attachments.get 返回数据
        att_data = b"%PDF-1.4 fake"
        service.users().messages().attachments().get.return_value.execute.return_value = {
            "data": _b64url(att_data),
            "size": len(att_data),
        }
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["saved"] == 1
        assert result["attachments"] == 1
        # 验证附件文件记录
        files = query_received_files_by_message(tmp_cfg.db_path, "<att@gmail.com>")
        att_files = [f for f in files if f["file_type"] == "attachment"]
        assert len(att_files) == 1
        assert att_files[0]["original_filename"] == "报告.pdf"
        # 验证文件实际存在
        assert Path(att_files[0]["saved_path"]).exists()


class TestSelfMailFilter:
    def test_skip_non_self_from(self, tmp_cfg):
        # from 不是用户自己 -> 跳过
        tmp_cfg.receive_rule_mode = "self_only"
        msg = _make_message(
            gmail_id="m1",
            from_header="other@gmail.com",
            to_header="user@gmail.com",
            rfc_message_id="<x@gmail.com>",
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["fetched"] == 1
        assert result["saved"] == 0  # 被过滤

    def test_skip_non_self_to(self, tmp_cfg):
        # to 不含用户自己 -> 跳过
        tmp_cfg.receive_rule_mode = "self_only"
        msg = _make_message(
            gmail_id="m1",
            from_header="user@gmail.com",
            to_header="other@gmail.com",
            rfc_message_id="<y@gmail.com>",
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["saved"] == 0

    def test_self_mail_with_display_name(self, tmp_cfg):
        # From 含昵称 "Name" <user@gmail.com>，应正确提取
        tmp_cfg.receive_rule_mode = "self_only"
        msg = _make_message(
            gmail_id="m1",
            from_header='"我的名字" <user@gmail.com>',
            to_header="user@gmail.com",
            rfc_message_id="<z@gmail.com>",
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        result = receive_gmail_api_messages(tmp_cfg, service=service)
        assert result["saved"] == 1


class TestFrontmatter:
    def test_frontmatter_contains_gmail_fields(self, tmp_cfg):
        msg = _make_message(
            gmail_id="m1",
            thread_id="t1",
            rfc_message_id="<fm@gmail.com>",
            subject="Frontmatter测试",
            body_text="内容",
        )
        service = _make_service(
            messages_list_resp={"messages": [{"id": "m1", "threadId": "t1"}]},
            messages_get_map={"m1": msg},
        )
        receive_gmail_api_messages(tmp_cfg, service=service)
        rows = query_received_messages_by_date(
            tmp_cfg.db_path, _saved_date(msg)
        )
        body_path = Path(rows[0]["body_file_path"])
        content = body_path.read_text(encoding="utf-8")
        assert "source: gmail_api" in content
        assert 'gmail_message_id: "m1"' in content
        assert 'gmail_thread_id: "t1"' in content
        assert 'message_id: "<fm@gmail.com>"' in content


def _saved_date(msg: dict) -> str:
    """从 message 的 internalDate 推算 saved_date (YYYY-MM-DD)。"""
    from datetime import datetime
    ts = int(msg.get("internalDate", "0"))
    return datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d")


def decode_raw(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
