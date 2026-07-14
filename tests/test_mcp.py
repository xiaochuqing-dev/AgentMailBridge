"""MCP 受控提交入口与审计测试。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    insert_mcp_call,
    query_recent_events,
    update_mcp_call,
)
from agent_mail_bridge.models import OperationStatus
from agent_mail_bridge.mcp_server import McpServer, SubmissionRateLimit


def test_submit_result_reuses_send_idempotency(tmp_cfg, monkeypatch):
    source = tmp_cfg.data_root_path / "result.md"
    source.write_text("result", encoding="utf-8")
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda cfg, message: smtp_calls.append(message),
    )
    service = ApplicationService(tmp_cfg)

    first = service.submit_result(
        source,
        title="Agent 结果",
        request_id="mcp-idempotent-001",
    )
    second = service.submit_result(
        source,
        title="Agent 结果",
        request_id="mcp-idempotent-001",
    )
    calls = service.get_mcp_history().details["calls"]

    assert first.status == OperationStatus.SUCCESS
    assert second.status == OperationStatus.DUPLICATE
    assert len(smtp_calls) == 1
    assert [item["status"] for item in calls[:2]] == ["duplicate", "sent"]
    assert query_recent_events(tmp_cfg.db_path, 1)[0]["level"] == "SUCCESS"


def test_submit_result_rejects_outside_path(tmp_cfg, tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    smtp = monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda cfg, message: None,
    )
    del smtp

    result = ApplicationService(tmp_cfg).submit_result(
        outside,
        request_id="mcp-outside-001",
    )

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "path_not_allowed"


def test_submit_result_distinguishes_missing_file_and_config(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    missing = service.submit_result(
        tmp_cfg.data_root_path / "missing.md",
        request_id="mcp-missing-001",
    )
    source = tmp_cfg.data_root_path / "result.md"
    source.write_text("result", encoding="utf-8")
    tmp_cfg.qq_auth_code = ""
    no_config = service.submit_result(
        source,
        request_id="mcp-config-001",
    )

    assert missing.error_code == "file_not_found"
    assert no_config.error_code == "configuration_error"


def test_mcp_history_hides_legacy_outside_path(tmp_cfg, tmp_path):
    outside = tmp_path / "legacy-result.md"
    call_id = insert_mcp_call(
        tmp_cfg.db_path,
        request_id="mcp-legacy-001",
        file_path=str(outside),
        title=None,
    )
    update_mcp_call(tmp_cfg.db_path, call_id, status="failed")

    row = ApplicationService(tmp_cfg).get_mcp_history().details["calls"][0]

    assert row["file_path"] == ""
    assert row["file_path_status"] == "unsafe_path"


def test_mcp_lifecycle_and_tool_schema(tmp_cfg):
    server = McpServer(ApplicationService(tmp_cfg))
    before_init = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    initialized = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        }
    )
    notification = server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    tools = server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    )

    assert before_init["error"]["code"] == -32002
    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert notification is None
    tool = tools["result"]["tools"][0]
    assert tool["name"] == "submit_result"
    assert set(tool["inputSchema"]["properties"]) == {
        "file_path",
        "title",
        "request_id",
    }
    assert tool["inputSchema"]["additionalProperties"] is False


def test_mcp_tool_call_success_and_duplicate(tmp_cfg, monkeypatch):
    source = tmp_cfg.data_root_path / "mcp-tool-result.md"
    source.write_text("result", encoding="utf-8")
    smtp_calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda cfg, message: smtp_calls.append(message),
    )
    server = McpServer(ApplicationService(tmp_cfg))
    server.initialized = True
    arguments = {
        "file_path": str(source),
        "title": "结果",
        "request_id": "mcp-tool-001",
    }

    first = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "submit_result", "arguments": arguments},
        }
    )
    second = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "submit_result", "arguments": arguments},
        }
    )

    assert first["result"]["structuredContent"]["status"] == "success"
    assert first["result"]["isError"] is False
    assert second["result"]["structuredContent"]["status"] == "duplicate"
    assert second["result"]["isError"] is False
    assert len(smtp_calls) == 1


def test_mcp_rejects_recipient_argument_and_audits(tmp_cfg):
    server = McpServer(ApplicationService(tmp_cfg))
    server.initialized = True
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "submit_result",
                "arguments": {
                    "file_path": str(tmp_cfg.data_root_path / "result.md"),
                    "recipient": "other@example.com",
                },
            },
        }
    )

    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["status"] == "invalid_input"
    calls = ApplicationService(tmp_cfg).get_mcp_history().details["calls"]
    assert calls[0]["status"] == "invalid_input"


def test_mcp_rate_limit_has_sixty_second_window():
    limiter = SubmissionRateLimit()
    assert all(limiter.allow(float(second)) for second in range(5))
    assert limiter.allow(5.0) is False
    assert limiter.allow(61.0) is True


def test_mcp_stdio_subprocess_outputs_only_json(tmp_path):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    env = os.environ.copy()
    env["AGENT_MAIL_BRIDGE_DISABLE_DOTENV"] = "1"
    env["DATA_ROOT"] = str(tmp_path / "data")
    completed = subprocess.run(
        [sys.executable, "-m", "agent_mail_bridge.mcp_server"],
        input="".join(json.dumps(item) + "\n" for item in messages),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=10,  # 协议握手最多等待 10 秒。
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]

    assert len(responses) == 2
    assert responses[0]["result"]["serverInfo"]["name"] == "agent-mail-bridge"
    assert responses[1]["result"]["tools"][0]["name"] == "submit_result"
    assert completed.stderr == ""
