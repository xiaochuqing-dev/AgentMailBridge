"""v1.2.0 Agent 邮件读取、资源准备、同步与统一审计回归。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    get_connection,
    query_recent_mcp_audit_events,
    save_auto_receive_state,
)
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mcp_server import McpServer
from agent_mail_bridge.models import OperationStatus, ReceiveResult
from agent_mail_bridge.process_lock import ProcessLock
from agent_mail_bridge.receive_rules import ALL_SCANNED
from agent_mail_bridge.utils import sha256_of_file


def _archive_mail(
    cfg,
    *,
    message_id: str,
    subject: str,
    body: str,
    received_at: str,
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> str:
    cfg.receive_rule_mode = ALL_SCANNED
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "owner@example.com"
    message["Cc"] = "copy@example.com"
    message["Message-ID"] = message_id
    message["Subject"] = subject
    message.set_content(body)
    for filename, mime, content in attachments or []:
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid=message_id,
        received_at=received_at,
        saved_date=received_at[:10],
        max_attachment_bytes=cfg.max_attachment_bytes,
        mailbox_ref="imap:INBOX",
    )
    return process_normalized_mail(cfg, normalized)["package_id"]


def test_read_access_is_opt_in_persistent_and_submit_policy_is_independent(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    assert service.search_mails(query="x").error_code == "read_access_disabled"
    env_path = tmp_path / "settings.env"
    enabled = service.set_mcp_mail_read_access(True, env_path=env_path)
    assert enabled.ok
    assert "MCP_MAIL_READ_ENABLED=\"true\"" in env_path.read_text(encoding="utf-8")
    assert service.get_mcp_mail_read_access().details["submit_result_available"] is True
    assert service.set_mcp_mail_read_access(False, env_path=env_path).ok
    assert service.search_mails(query="x").error_code == "read_access_disabled"


def test_search_time_scopes_filters_paging_and_mail_body_paging(tmp_cfg):
    tmp_cfg.mcp_mail_read_enabled = True
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    old_id = _archive_mail(
        tmp_cfg,
        message_id="<old-v12@test>",
        subject="旧方案",
        body="历史正文",
        received_at=f"{yesterday} 08:00:00",
    )
    new_id = _archive_mail(
        tmp_cfg,
        message_id="<new-v12@test>",
        subject="AgentMailBridge 提示词",
        body="完整中文正文-" * 100,
        received_at=f"{today} 09:00:00",
        attachments=[("开发提示词.md", "text/markdown", "附件中文内容".encode("utf-8"))],
    )
    service = ApplicationService(tmp_cfg)
    latest = service.search_mails(time_scope="latest").details
    assert latest["messages"][0]["mail_id"] == new_id
    assert len(latest["messages"]) == 1
    assert len(service.search_mails(time_scope="today").details["messages"]) == 1
    assert service.search_mails(time_scope="yesterday").details["messages"][0]["mail_id"] == old_id
    assert service.search_mails(query="开发提示词", has_attachments=True).details["result_count"] == 1
    assert service.search_mails(sender="sender@example.com", recipient="copy@example.com").details["result_count"] == 2
    first = service.get_mail(new_id, max_chars=60).details["mail"]
    assert first["body"]["has_more"] is True
    second = service.get_mail(new_id, offset=first["body"]["next_offset"], max_chars=60).details["mail"]
    assert second["body"]["offset"] == 60
    assert first["raw_eml"]["available"] is True
    assert first["resources"][0]["capability"] in {
        "directly_readable", "structured_preview", "visual_file", "document_file", "binary_file", "link"
    }


def test_text_csv_binary_image_and_raw_resource_modes(tmp_cfg):
    tmp_cfg.mcp_mail_read_enabled = True
    today = datetime.now().strftime("%Y-%m-%d")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (3).to_bytes(4, "big") + (2).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    package_id = _archive_mail(
        tmp_cfg,
        message_id="<resources-v12@test>",
        subject="资源读取",
        body="正文",
        received_at=f"{today} 10:00:00",
        attachments=[
            ("中文.txt", "text/plain", "简体中文 GBK".encode("gbk")),
            ("数据.csv", "text/csv", '姓名,备注\n张三,"第一行\n第二行"\n李四,正常\n'.encode("utf-8")),
            ("图片.png", "image/png", png),
            ("文档.pdf", "application/pdf", b"%PDF-1.4\n%test"),
            ("程序.exe", "application/octet-stream", b"MZ\x00\x01binary"),
        ],
    )
    service = ApplicationService(tmp_cfg)
    mail = service.get_mail(package_id).details["mail"]
    by_name = {item["display_name"]: item for item in mail["resources"]}
    text = service.read_mail_resource(
        package_id, by_name["中文.txt"]["resource_id"], mode="text", max_chars=100
    ).details
    assert text["content"] == "简体中文 GBK"
    assert text["encoding"] in {"gb18030", "gbk"}
    csv_result = service.read_mail_resource(
        package_id, by_name["数据.csv"]["resource_id"], mode="csv_preview", max_rows=5
    ).details
    assert csv_result["columns"] == ["姓名", "备注"]
    assert csv_result["rows"][0] == ["张三", "第一行\n第二行"]
    image = service.read_mail_resource(
        package_id, by_name["图片.png"]["resource_id"], mode="preview"
    ).details
    assert (image["format"], image["width"], image["height"]) == ("PNG", 3, 2)
    assert service.read_mail_resource(
        package_id, by_name["文档.pdf"]["resource_id"], mode="preview"
    ).details["capability"] == "document_file"
    binary = service.read_mail_resource(
        package_id, by_name["程序.exe"]["resource_id"], mode="text"
    )
    assert binary.error_code == "binary_resource"
    raw = service.read_mail_resource(package_id, "raw.eml", mode="raw", max_chars=100)
    assert raw.ok and raw.details["resource_id"] == "raw.eml" and raw.details["has_more"] is True


def test_resource_membership_traversal_and_prepare_hash_closure(tmp_cfg, tmp_path):
    tmp_cfg.mcp_mail_read_enabled = True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tmp_cfg.allowed_send_roots = [workspace]
    today = datetime.now().strftime("%Y-%m-%d")
    package_id = _archive_mail(
        tmp_cfg,
        message_id="<prepare-v12@test>",
        subject="准备资源",
        body="正文",
        received_at=f"{today} 11:00:00",
        attachments=[("结果.md", "text/markdown", b"prepared content")],
    )
    service = ApplicationService(tmp_cfg)
    mail = service.get_mail(package_id).details["mail"]
    attachment = next(item for item in mail["resources"] if item["display_name"] == "结果.md")
    prepared = service.prepare_mail_resources(package_id, [attachment["resource_id"]])
    assert prepared.ok
    copied = Path(prepared.details["prepared"][0]["prepared_path"])
    assert copied.is_file()
    assert sha256_of_file(copied) == attachment["sha256"]
    assert Path(prepared.details["note_path"]).is_file()
    renamed = service.prepare_mail_resources(package_id, [attachment["resource_id"]])
    assert Path(renamed.details["prepared"][0]["prepared_path"]).name == "结果 (2).md"
    assert service.read_mail_resource(package_id, "not-this-mail", mode="text").error_code == "resource_not_found"

    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE mail_resources SET local_path='../outside.txt' WHERE resource_id=?",
        (attachment["resource_id"],),
    )
    connection.commit()
    assert service.get_mail(package_id).error_code == "path_not_allowed"
    assert service.read_mail_resource(
        package_id, attachment["resource_id"], mode="text"
    ).error_code == "path_not_allowed"


def test_agent_access_rejects_package_root_and_workspace_link_escape(tmp_cfg, tmp_path):
    tmp_cfg.mcp_mail_read_enabled = True
    today = datetime.now().strftime("%Y-%m-%d")
    package_id = _archive_mail(
        tmp_cfg,
        message_id="<path-boundary-v12@test>",
        subject="路径边界",
        body="正文",
        received_at=f"{today} 11:30:00",
        attachments=[("边界.txt", "text/plain", b"boundary")],
    )
    service = ApplicationService(tmp_cfg)
    mail = service.get_mail(package_id).details["mail"]
    attachment = next(item for item in mail["resources"] if item["display_name"] == "边界.txt")

    workspace = tmp_path / "linked-workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / ".agentmailbridge"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if created.returncode:
            pytest.skip(f"当前环境不能创建目录联接：{created.stderr.strip()}")
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"当前环境不能创建目录符号链接：{exc}")
    tmp_cfg.allowed_send_roots = [workspace]
    escaped = service.prepare_mail_resources(package_id, [attachment["resource_id"]])
    assert escaped.error_code == "path_not_allowed"
    assert not (outside / "mail").exists()

    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE mail_packages SET package_root=? WHERE package_id=?",
        (str(tmp_path / "outside-package"), package_id),
    )
    connection.commit()
    assert service.get_mail(package_id).error_code == "path_not_allowed"
    assert service.read_mail_resource(
        package_id, attachment["resource_id"], mode="text"
    ).error_code == "path_not_allowed"


def test_sync_freshness_and_cross_process_lock(tmp_cfg, monkeypatch):
    tmp_cfg.mcp_mail_read_enabled = True
    service = ApplicationService(tmp_cfg)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_auto_receive_state(tmp_cfg.db_path, last_success_at=now_text, last_check_at=now_text)
    assert service.get_mail_sync_status().details["freshness"] == "fresh"
    monkeypatch.setattr(service, "receive", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("新鲜缓存不应同步")))
    assert service.search_mails(ensure_fresh=True).ok

    stale = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    save_auto_receive_state(tmp_cfg.db_path, last_success_at=stale, last_check_at=stale)
    monkeypatch.setattr(
        service,
        "receive",
        lambda **_kwargs: ReceiveResult(OperationStatus.NO_CHANGES, message="暂无新邮件"),
    )
    refreshed = service.search_mails(ensure_fresh=True)
    assert refreshed.ok and refreshed.details["sync_triggered"] is True
    assert refreshed.details["cached"] is False

    second = ApplicationService(tmp_cfg)
    lock = ProcessLock(tmp_cfg.data_root_path / ".locks" / "receive.lock")
    assert lock.acquire()
    try:
        busy = second.receive(wait_for_process_lock=0.05)
        assert busy.error_code == "sync_in_progress"
    finally:
        lock.release()


def test_process_lock_is_released_when_owner_process_exits(tmp_path):
    lock_path = tmp_path / "receive.lock"
    code = (
        "import sys,time; "
        "from agent_mail_bridge.process_lock import ProcessLock; "
        "lock=ProcessLock(sys.argv[1]); "
        "assert lock.acquire(); print('ready', flush=True); time.sleep(60)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert owner.stdout is not None and owner.stdout.readline().strip() == "ready"
        contender = ProcessLock(lock_path)
        assert contender.acquire(timeout=0.05) is False
        owner.kill()
        owner.wait(timeout=10)
        assert contender.acquire(timeout=2.0) is True
        contender.release()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


def test_mcp_tools_structured_output_and_audit_omit_content(tmp_cfg):
    today = datetime.now().strftime("%Y-%m-%d")
    tmp_cfg.mcp_mail_read_enabled = True
    package_id = _archive_mail(
        tmp_cfg,
        message_id="<mcp-v12@test>",
        subject="MCP 中文读取",
        body="不得进入审计的私密正文标记-XYZ",
        received_at=f"{today} 12:00:00",
    )
    server = McpServer(ApplicationService(tmp_cfg))
    server.initialized = True
    listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = [item["name"] for item in listed["result"]["tools"]]
    assert names == [
        "submit_result", "search_mails", "get_mail", "read_mail_resource",
        "prepare_mail_resources", "list_agent_workspaces", "get_mail_sync_status",
    ]
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_mail", "arguments": {"mail_id": package_id, "max_chars": 100}},
        }
    )
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["mail"]["mail_id"] == package_id
    audit = query_recent_mcp_audit_events(tmp_cfg.db_path, 10)
    assert audit[0]["tool_name"] == "get_mail"
    assert "私密正文标记-XYZ" not in json.dumps(audit, ensure_ascii=False)


def test_mcp_read_disabled_returns_stable_error_but_sync_status_is_available(tmp_cfg):
    server = McpServer(ApplicationService(tmp_cfg))
    server.initialized = True
    denied = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_mails", "arguments": {"time_scope": "today"}},
        }
    )
    assert denied["result"]["structuredContent"]["error_code"] == "read_access_disabled"
    status = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_mail_sync_status", "arguments": {}},
        }
    )
    assert status["result"]["isError"] is False


def test_real_stdio_protocol_reads_and_prepares_archived_resource(tmp_cfg, tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    package_id = _archive_mail(
        tmp_cfg,
        message_id="<stdio-v12@test>",
        subject="stdio 中文邮件",
        body="stdio 完整正文",
        received_at=f"{today} 13:00:00",
        attachments=[("协议附件.md", "text/markdown", "协议附件内容".encode("utf-8"))],
    )
    service = ApplicationService(tmp_cfg)
    resource_id = next(
        item["resource_id"]
        for item in service.get_mail_message(package_id).details["message"]["resources"]
        if item["display_name"] == "协议附件.md"
    )
    workspace = tmp_path / "stdio-workspace"
    workspace.mkdir()
    messages = [
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "independent-protocol-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_mails", "arguments": {"query": "stdio 中文", "time_scope": "today"}},
        },
        {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "get_mail", "arguments": {"mail_id": package_id}},
        },
        {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "read_mail_resource", "arguments": {"mail_id": package_id, "resource_id": resource_id, "mode": "text"}},
        },
        {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "prepare_mail_resources", "arguments": {"mail_id": package_id, "resource_ids": [resource_id]}},
        },
        {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "get_mail_sync_status", "arguments": {}},
        },
    ]
    env = os.environ.copy()
    env.update(
        {
            "AGENT_MAIL_BRIDGE_DISABLE_DOTENV": "1",
            "AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE": "1",
            "DATA_ROOT": str(tmp_cfg.data_root_path),
            "MCP_MAIL_READ_ENABLED": "true",
            "ALLOWED_SEND_ROOTS": str(workspace),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "agent_mail_bridge.mcp_server"],
        input="".join(json.dumps(item, ensure_ascii=False) + "\n" for item in messages),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=20,
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert completed.stderr == ""
    assert len(responses) == 7
    assert len(responses[1]["result"]["tools"]) == 7
    tools = {item["name"]: item for item in responses[1]["result"]["tools"]}
    assert tools["search_mails"]["annotations"]["readOnlyHint"] is True
    assert tools["read_mail_resource"]["annotations"]["openWorldHint"] is False
    assert tools["prepare_mail_resources"]["annotations"]["readOnlyHint"] is False
    assert tools["submit_result"]["annotations"]["openWorldHint"] is True
    assert responses[2]["result"]["structuredContent"]["result_count"] == 1
    assert responses[3]["result"]["structuredContent"]["mail"]["body"]["content"] == "stdio 完整正文"
    assert responses[4]["result"]["structuredContent"]["content"] == "协议附件内容"
    assert responses[4]["result"]["structuredContent"]["status"] == "success"
    prepared = responses[5]["result"]["structuredContent"]["prepared"][0]
    assert Path(prepared["prepared_path"]).is_file()
    assert sha256_of_file(Path(prepared["prepared_path"])) == prepared["sha256"]
    assert responses[6]["result"]["isError"] is False
