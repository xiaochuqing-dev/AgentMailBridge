"""AgentMailBridge 本机 stdio MCP 服务。"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.database import close_connection
from agent_mail_bridge.models import OperationStatus, SendResult
from agent_mail_bridge.version import __version__

SERVER_NAME = "agent-mail-bridge"
SERVER_VERSION = __version__
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
SUBMISSION_LIMIT = 5  # 单个 MCP 进程每分钟最多提交 5 次。
SUBMISSION_WINDOW_SECONDS = 60  # 频率统计窗口，单位：秒。
MAX_TITLE_LENGTH = 200  # 邮件标题上限，单位：字符。
MAX_REQUEST_ID_LENGTH = 128  # request_id 上限，单位：字符。
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


class SubmissionRateLimit:
    """限制单个 stdio 会话的高频发件调用。"""

    def __init__(self) -> None:
        self._calls: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        boundary = current - SUBMISSION_WINDOW_SECONDS
        while self._calls and self._calls[0] <= boundary:
            self._calls.popleft()
        if len(self._calls) >= SUBMISSION_LIMIT:
            return False
        self._calls.append(current)
        return True


class McpServer:
    """实现最小且完整的 MCP 生命周期与 submit_result 工具。"""

    def __init__(
        self,
        service: ApplicationService,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self.service = service
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.initialized = False
        self.rate_limit = SubmissionRateLimit()
        self.client_name = "unknown"
        self.session_id = str(uuid.uuid4())

    def serve(self) -> int:
        """逐行处理 JSON-RPC，标准输出只写协议消息。"""
        try:
            for raw_line in self.input_stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    response = self.handle_line(line)
                except Exception:  # noqa: BLE001
                    # 单条请求异常必须结构化返回，诊断不得污染 stdout。
                    response = _error_response(None, -32603, "MCP 内部错误，请查看应用日志")
                if response is not None:
                    self._write(response)
            return 0
        finally:
            close_connection()

    def handle_line(self, line: str) -> dict[str, Any] | None:
        # 接受某些 Windows 客户端在首条 UTF-8 JSON 前写入的 BOM。
        line = line.lstrip("\ufeff")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return _error_response(None, -32700, "JSON 解析失败")
        if not isinstance(message, dict):
            return _error_response(None, -32600, "请求必须是 JSON 对象")
        return self.handle_message(message)

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error_response(request_id, -32600, "无效的 JSON-RPC 请求")

        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self._initialize(request_id, message.get("params"))
        if method == "ping":
            return _success_response(request_id, {})
        if not self.initialized:
            return _error_response(request_id, -32002, "MCP 尚未完成初始化")
        if method == "tools/list":
            return _success_response(request_id, {"tools": _all_tools()})
        if method == "tools/call":
            return self._call_tool(request_id, message.get("params"))
        return _error_response(request_id, -32601, f"不支持的方法：{method}")

    def _initialize(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return _error_response(request_id, -32602, "initialize 参数无效")
        requested = params.get("protocolVersion")
        client_info = params.get("clientInfo")
        if isinstance(client_info, dict):
            self.client_name = str(client_info.get("name") or "unknown")[:120]
        protocol = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return _success_response(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "邮件读取需由用户在 GUI 中一次性启用；可搜索和分页读取本地归档，"
                    "可把资源受控准备到授权工作区。submit_result 收件人固定。"
                    "服务不能读取凭据、修改邮件或遍历任意文件系统路径。"
                ),
            },
        )

    def _call_tool(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error_response(request_id, -32602, "未知工具或调用参数无效")
        tool_name = str(params["name"])
        if tool_name not in {item["name"] for item in _all_tools()}:
            return _error_response(request_id, -32602, "未知工具或调用参数无效")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error_response(request_id, -32602, "工具 arguments 必须是对象")

        called_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        started = time.perf_counter()
        validation_error = _validate_tool_arguments(tool_name, arguments)
        if validation_error:
            if tool_name == "submit_result":
                tool_result = self._rejected_tool_result(
                    arguments, "invalid_input", validation_error
                )
            else:
                structured = {
                    "status": "invalid_input",
                    "ok": False,
                    "error_code": "invalid_input",
                    "message": validation_error,
                }
                tool_result = _tool_result(structured, is_error=True)
            self._audit_tool(
                tool_name, arguments,
                tool_result["structuredContent"], called_at, started,
            )
            return _success_response(request_id, tool_result)
        if tool_name == "submit_result" and not self.rate_limit.allow():
            tool_result = self._rejected_tool_result(
                arguments,
                "rate_limited",
                "一分钟内最多提交 5 次，请稍后重试",
            )
            self._audit_tool(
                tool_name, arguments, tool_result["structuredContent"], called_at, started
            )
            return _success_response(request_id, tool_result)

        result = self._dispatch_tool(tool_name, arguments)
        structured = (
            _send_result_payload(result)
            if tool_name == "submit_result"
            else _service_result_payload(result)
        )
        structured = _redact_payload(structured, self.service)
        is_error = not bool(structured.get("ok"))
        if tool_name == "submit_result" and structured.get("status") in {
            "success", "duplicate", "sent_archive_failed"
        }:
            is_error = False
        tool_result = _tool_result(structured, is_error=is_error)
        self._audit_tool(tool_name, arguments, structured, called_at, started)
        return _success_response(request_id, tool_result)

    def _dispatch_tool(self, tool_name: str, arguments: dict[str, Any]):
        if tool_name == "submit_result":
            return self.service.submit_result(
                arguments["file_path"],
                title=arguments.get("title"),
                request_id=arguments.get("request_id"),
            )
        if tool_name == "search_mails":
            return self.service.search_mails(**arguments)
        if tool_name == "get_mail":
            return self.service.get_mail(
                _mail_identifier(arguments),
                offset=arguments.get("offset", 0),
                max_chars=arguments.get("max_chars", 20_000),
            )
        if tool_name == "read_mail_resource":
            options = {
                key: arguments[key]
                for key in ("mode", "offset", "max_chars", "row_offset", "max_rows")
                if key in arguments
            }
            return self.service.read_mail_resource(
                _mail_identifier(arguments), arguments["resource_id"], **options
            )
        if tool_name == "prepare_mail_resources":
            options = {
                key: arguments[key]
                for key in ("target_workspace", "target_subdir", "overwrite_policy")
                if key in arguments
            }
            return self.service.prepare_mail_resources(
                _mail_identifier(arguments), arguments["resource_ids"], **options
            )
        if tool_name == "list_agent_workspaces":
            result = self.service.list_agent_workspaces()
            result.details = {
                "workspaces": result.details.get("workspace_details", []),
                "takes_effect": result.details.get("takes_effect"),
            }
            return result
        if tool_name == "get_mail_sync_status":
            return self.service.get_mail_sync_status()
        raise ValueError(f"未知工具：{tool_name}")

    def _audit_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        structured: dict[str, Any],
        called_at: str,
        started: float,
    ) -> None:
        try:
            target, source_path, prepared_path = _audit_target(
                tool_name, arguments, structured
            )
            self.service.record_mcp_audit(
                call_id=str(uuid.uuid4()),
                called_at=called_at,
                completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                tool_name=tool_name,
                operation_type=_operation_type(tool_name),
                client_name=self.client_name,
                session_id=self.session_id,
                request_id=str(arguments.get("request_id") or "") or None,
                query_summary=_query_summary(tool_name, arguments),
                mail_id=_mail_identifier(arguments, required=False),
                resource_id=str(arguments.get("resource_id") or "") or None,
                result_count=int(structured.get("result_count") or structured.get("prepared_count") or 0),
                target_summary=target,
                source_path=source_path,
                prepared_path=prepared_path,
                status=str(structured.get("status") or "failed"),
                error_code=str(structured.get("error_code") or "") or None,
                duration_ms=int((time.perf_counter() - started) * 1000),
                bytes_returned=int(
                    structured.get("bytes_returned")
                    or len(json.dumps(structured, ensure_ascii=False, default=str).encode("utf-8"))
                ),
                cached=bool(structured.get("cached")),
                ensure_fresh=bool(arguments.get("ensure_fresh")),
                sync_triggered=bool(structured.get("sync_triggered")),
                details=_audit_details(tool_name, structured),
            )
        except Exception:  # noqa: BLE001
            # 审计失败不能污染 MCP stdout，也不能把邮件正文写入诊断。
            return

    def _rejected_tool_result(
        self,
        arguments: dict[str, Any],
        error_code: str,
        message: str,
    ) -> dict[str, Any]:
        stable_request_id, call_id = self.service.record_mcp_rejection(
            file_path=str(arguments.get("file_path", "")),
            title=arguments.get("title") if isinstance(arguments.get("title"), str) else None,
            request_id=(
                arguments.get("request_id")
                if isinstance(arguments.get("request_id"), str)
                else None
            ),
            error_code=error_code,
            message=message,
        )
        structured = {
            "status": error_code,
            "operation_status": "failed",
            "ok": False,
            "request_id": stable_request_id,
            "error_code": error_code,
            "message": message,
            "mcp_call_id": call_id,
        }
        return _tool_result(_redact_payload(structured, self.service), is_error=True)

    def _write(self, response: dict[str, Any]) -> None:
        text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        self.output_stream.write(text + "\n")
        self.output_stream.flush()


def _validate_submit_arguments(arguments: dict[str, Any]) -> str | None:
    allowed = {"file_path", "title", "request_id"}
    extra = sorted(set(arguments) - allowed)
    if extra:
        return "不支持的参数：" + ", ".join(extra)
    file_path = arguments.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return "file_path 必须是非空字符串"
    title = arguments.get("title")
    if title is not None and (
        not isinstance(title, str) or len(title.strip()) > MAX_TITLE_LENGTH
    ):
        return "title 必须是 200 个字符以内的字符串"
    request_id = arguments.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > MAX_REQUEST_ID_LENGTH
        or _REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        return "request_id 仅允许 1-128 位字母、数字、点、下划线、冒号和连字符"
    return None


def _validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> str | None:
    if tool_name == "submit_result":
        return _validate_submit_arguments(arguments)
    allowed: dict[str, set[str]] = {
        "search_mails": {
            "query", "time_scope", "recent_days", "date_from", "date_to",
            "subject", "sender", "recipient", "has_attachments", "status",
            "sort", "limit", "offset", "ensure_fresh", "allow_cached",
            "account_ref", "mailbox_ref",
        },
        "get_mail": {"mail_id", "package_id", "offset", "max_chars"},
        "read_mail_resource": {
            "mail_id", "package_id", "resource_id", "offset", "max_chars",
            "mode", "row_offset", "max_rows",
        },
        "prepare_mail_resources": {
            "mail_id", "package_id", "resource_ids", "target_workspace",
            "target_subdir", "overwrite_policy",
        },
        "list_agent_workspaces": set(),
        "get_mail_sync_status": set(),
    }
    extra = sorted(set(arguments) - allowed[tool_name])
    if extra:
        return "不支持的参数：" + ", ".join(extra)
    if tool_name in {"get_mail", "read_mail_resource", "prepare_mail_resources"}:
        identifiers = [arguments.get("mail_id"), arguments.get("package_id")]
        if sum(isinstance(value, str) and bool(value.strip()) for value in identifiers) != 1:
            return "mail_id 与 package_id 必须且只能提供一个非空字符串"
    if tool_name == "read_mail_resource":
        if not isinstance(arguments.get("resource_id"), str) or not arguments["resource_id"].strip():
            return "resource_id 必须是非空字符串"
    if tool_name == "prepare_mail_resources":
        values = arguments.get("resource_ids")
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            return "resource_ids 必须是非空字符串数组"
    boolean_fields = {"ensure_fresh", "allow_cached", "has_attachments"}
    for field in boolean_fields & set(arguments):
        if not isinstance(arguments[field], bool):
            return f"{field} 必须是布尔值"
    integer_fields = {"recent_days", "limit", "offset", "max_chars", "row_offset", "max_rows"}
    for field in integer_fields & set(arguments):
        if isinstance(arguments[field], bool) or not isinstance(arguments[field], int):
            return f"{field} 必须是整数"
    return None


def _all_tools() -> list[dict[str, Any]]:
    return [
        _submit_result_tool(),
        _search_mails_tool(),
        _get_mail_tool(),
        _read_mail_resource_tool(),
        _prepare_mail_resources_tool(),
        _list_agent_workspaces_tool(),
        _get_mail_sync_status_tool(),
    ]


def _submit_result_tool() -> dict[str, Any]:
    return {
        "name": "submit_result",
        "title": "提交 Agent 结果",
        "description": "验证允许目录内的结果文件，由产品原子 staging、校验 Hash 后发送到固定 Gmail 收件人。",
        "annotations": _tool_annotations(
            read_only=False, idempotent=False, open_world=True
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "本地结果文件绝对路径"},
                "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
                "request_id": {
                    "type": "string",
                    "maxLength": MAX_REQUEST_ID_LENGTH,
                    "pattern": _REQUEST_ID_PATTERN.pattern,
                },
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "ok": {"type": "boolean"},
                "request_id": {"type": "string"},
                "error_code": {"type": ["string", "null"]},
                "message": {"type": "string"},
                "filename": {"type": "string"},
                "size_bytes": {"type": "integer"},
                "source_sha256": {"type": "string"},
                "staged_sha256": {"type": "string"},
                "attachment_pre_smtp_sha256": {"type": "string"},
                "sent_archive_sha256": {"type": "string"},
            },
            "required": ["status", "ok", "request_id", "message"],
        },
    }


def _search_mails_tool() -> dict[str, Any]:
    return {
        "name": "search_mails",
        "title": "搜索本地邮件",
        "description": (
            "按最新、今天、昨天、最近若干天或日期范围搜索已归档邮件。query 使用多词 AND，"
            "覆盖主题、发件人、收件人、抄送、完整可读正文、附件/图片名、链接文字、域名、URL 和自然状态。"
            "可用 latest + newest + limit=1 读取最新邮件；ensure_fresh=true 会在需要时受控同步。"
        ),
        "annotations": _tool_annotations(
            read_only=True, idempotent=True, open_world=False
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "time_scope": {"type": "string", "enum": ["latest", "today", "yesterday", "recent_days", "date_range", "all"], "default": "all"},
                "recent_days": {"type": "integer", "minimum": 1, "maximum": 3650},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "subject": {"type": "string"},
                "sender": {"type": "string"},
                "recipient": {"type": "string"},
                "has_attachments": {"type": "boolean"},
                "status": {"type": "string"},
                "sort": {"type": "string", "enum": ["newest", "oldest"], "default": "newest"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "ensure_fresh": {"type": "boolean", "default": False},
                "allow_cached": {"type": "boolean", "default": True},
                "account_ref": {"type": "string"},
                "mailbox_ref": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "outputSchema": _generic_output_schema(),
    }


def _mail_identifier_properties() -> dict[str, Any]:
    return {
        "mail_id": {"type": "string", "description": "search_mails 返回的稳定邮件标识"},
        "package_id": {"type": "string", "description": "mail_id 的兼容别名"},
    }


def _get_mail_tool() -> dict[str, Any]:
    return {
        "name": "get_mail",
        "title": "读取邮件正文与资源清单",
        "description": "按 mail_id/package_id 返回邮件元数据、有界完整可读正文、会话、附件、图片、链接、下载资源和 raw.eml 描述。长正文使用 offset/max_chars 分页。",
        "annotations": _tool_annotations(
            read_only=True, idempotent=True, open_world=False
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_mail_identifier_properties(),
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 20000},
            },
            "anyOf": [{"required": ["mail_id"]}, {"required": ["package_id"]}],
            "additionalProperties": False,
        },
        "outputSchema": _generic_output_schema(),
    }


def _read_mail_resource_tool() -> dict[str, Any]:
    return {
        "name": "read_mail_resource",
        "title": "读取邮件资源",
        "description": (
            "严格验证资源属于指定邮件。text/preview 分段读取 TXT、Markdown、代码、JSON、YAML、XML、TOML、INI、LOG、SQL、HTML 等文本；"
            "csv_preview 流式返回 CSV/TSV 列名和行范围；preview 返回图片、PDF、Office、ZIP、EXE 等二进制描述；raw 仅分段读取真实 raw.eml。"
        ),
        "annotations": _tool_annotations(
            read_only=True, idempotent=True, open_world=False
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_mail_identifier_properties(),
                "resource_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["text", "preview", "csv_preview", "raw"], "default": "preview"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 12000},
                "row_offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["resource_id"],
            "anyOf": [{"required": ["mail_id"]}, {"required": ["package_id"]}],
            "additionalProperties": False,
        },
        "outputSchema": _generic_output_schema(),
    }


def _prepare_mail_resources_tool() -> dict[str, Any]:
    return {
        "name": "prepare_mail_resources",
        "title": "准备邮件资源到工作区",
        "description": "由 AgentMailBridge 把指定邮件资源原子复制到授权工作区的 .agentmailbridge/mail/<mail-id>/，保留文件名并校验复制前后大小和 SHA-256；不执行、不解压。",
        "annotations": _tool_annotations(
            read_only=False, idempotent=False, open_world=False
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_mail_identifier_properties(),
                "resource_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100, "uniqueItems": True},
                "target_workspace": {"type": "string", "description": "list_agent_workspaces 返回的 workspace_id 或完整路径"},
                "target_subdir": {"type": "string", "description": "邮件目录内的可选安全相对子目录"},
                "overwrite_policy": {"type": "string", "enum": ["rename", "error", "overwrite"], "default": "rename"},
            },
            "required": ["resource_ids"],
            "anyOf": [{"required": ["mail_id"]}, {"required": ["package_id"]}],
            "additionalProperties": False,
        },
        "outputSchema": _generic_output_schema(),
    }


def _list_agent_workspaces_tool() -> dict[str, Any]:
    return {
        "name": "list_agent_workspaces",
        "title": "列出 Agent 工作区",
        "description": "列出用户在 GUI 中明确授权的工作区标识、完整显示路径、可用状态和默认状态，不返回秘密或无关目录。",
        "annotations": _tool_annotations(
            read_only=True, idempotent=True, open_world=False
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": _generic_output_schema(),
    }


def _get_mail_sync_status_tool() -> dict[str, Any]:
    return {
        "name": "get_mail_sync_status",
        "title": "查询邮件同步状态",
        "description": "返回后台自动收件状态、上次检查/成功、最近结果、下次检查、跨进程同步状态、重试数、本地数据年龄和 freshness，不读取邮件正文。",
        "annotations": _tool_annotations(
            read_only=True, idempotent=True, open_world=False
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": _generic_output_schema(),
    }


def _generic_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "error_code": {"type": ["string", "null"]},
            "message": {"type": "string"},
        },
        "required": ["status", "ok", "message"],
        "additionalProperties": True,
    }


def _tool_annotations(
    *, read_only: bool, idempotent: bool, open_world: bool
) -> dict[str, bool]:
    """向支持审批策略的 MCP 客户端声明真实副作用边界。"""
    return {
        "readOnlyHint": read_only,
        "destructiveHint": False,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def _send_result_payload(result: SendResult) -> dict[str, Any]:
    raw = result.to_dict()
    raw["operation_status"] = raw["status"]
    if result.send_status == "sent":
        raw["status"] = "success"
    elif result.send_status in {"duplicate", "sent_archive_failed"}:
        raw["status"] = result.send_status
    elif result.error_code in {
        "configuration_error",
        "file_not_found",
        "path_not_allowed",
        "file_type_not_allowed",
        "file_too_large",
    }:
        raw["status"] = result.error_code
    else:
        raw["status"] = "failed"
    return raw


def _service_result_payload(result) -> dict[str, Any]:
    raw = result.to_dict()
    details = raw.pop("details", {})
    core = {
        "status": raw.get("status", "failed"),
        "ok": bool(raw.get("ok")),
        "error_code": raw.get("error_code"),
        "message": str(raw.get("message") or ""),
    }
    payload = dict(core)
    if isinstance(details, dict):
        payload.update(details)
    # 资源 DTO 也有自身 status，不能覆盖工具调用的稳定顶层状态。
    payload.update(core)
    return payload


def _mail_identifier(arguments: dict[str, Any], *, required: bool = True) -> str | None:
    value = arguments.get("mail_id") or arguments.get("package_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if required:
        raise ValueError("缺少 mail_id/package_id")
    return None


def _operation_type(tool_name: str) -> str:
    return {
        "search_mails": "search",
        "get_mail": "read_mail",
        "read_mail_resource": "read_resource",
        "prepare_mail_resources": "prepare",
        "list_agent_workspaces": "workspace",
        "get_mail_sync_status": "sync_status",
        "submit_result": "send",
    }[tool_name]


def _query_summary(tool_name: str, arguments: dict[str, Any]) -> str | None:
    if tool_name != "search_mails":
        return None
    fields = []
    for key in (
        "time_scope", "recent_days", "date_from", "date_to", "query", "subject",
        "sender", "recipient", "has_attachments", "status", "sort", "limit", "offset",
    ):
        value = arguments.get(key)
        if value not in {None, ""}:
            fields.append(f"{key}={value}")
    return "; ".join(fields)[:500]


def _audit_target(
    tool_name: str, arguments: dict[str, Any], structured: dict[str, Any]
) -> tuple[str | None, str | None, str | None]:
    source_path = prepared_path = None
    if tool_name == "search_mails":
        query = str(arguments.get("query") or "").strip()
        scope = str(arguments.get("time_scope") or "all")
        target = f"{scope} 邮件" + (f"，包含“{query[:80]}”" if query else "")
    elif tool_name == "get_mail":
        mail = structured.get("mail") or {}
        target = f"邮件：{mail.get('subject') or _mail_identifier(arguments, required=False) or '未知'}"
    elif tool_name == "read_mail_resource":
        resource = structured.get("resource") or structured
        target = f"资源：{resource.get('display_name') or arguments.get('resource_id')}"
        source_path = str(resource.get("local_path") or "") or None
    elif tool_name == "prepare_mail_resources":
        target = f"工作区：{Path(str(structured.get('workspace_path') or arguments.get('target_workspace') or '待选择')).name}"
        prepared = structured.get("prepared") or []
        if prepared:
            source_path = str(prepared[0].get("source_path") or "") or None
            prepared_path = str(prepared[0].get("prepared_path") or "") or None
    elif tool_name == "list_agent_workspaces":
        target = "Agent 工作区"
    elif tool_name == "get_mail_sync_status":
        target = "邮件同步状态"
    else:
        source_path = str(arguments.get("file_path") or "") or None
        target = f"文件：{Path(source_path).name}" if source_path else "发送结果"
        prepared_path = str(structured.get("send_copy_path") or "") or None
    return target, source_path, prepared_path


def _audit_details(tool_name: str, structured: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "prepare_mail_resources":
        return {
            "hashes": [
                {
                    "resource_id": item.get("resource_id"),
                    "filename": item.get("filename"),
                    "size_bytes": item.get("size_bytes"),
                    "sha256": item.get("sha256"),
                }
                for item in (structured.get("prepared") or [])[:100]
            ]
        }
    if tool_name == "submit_result":
        return {
            "filename": structured.get("filename"),
            "size_bytes": structured.get("size_bytes"),
            "source_sha256": structured.get("source_sha256"),
            "staged_sha256": structured.get("staged_sha256"),
            "sent_archive_sha256": structured.get("sent_archive_sha256"),
        }
    return {}


def _redact_payload(value: Any, service: ApplicationService) -> Any:
    secrets = (
        service.cfg.gmail_app_password,
        service.cfg.qq_auth_code,
    )
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[已脱敏]")
        return result
    if isinstance(value, dict):
        return {key: _redact_payload(item, service) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, service) for item in value]
    return value


def _tool_result(structured: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }


def _success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(
    request_id: Any, code: int, message: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> None:
    for stream, errors in (
        (sys.stdin, "strict"),
        (sys.stdout, "strict"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            options: dict[str, Any] = {"encoding": "utf-8", "errors": errors}
            if stream is not sys.stdin:
                options["write_through"] = True
            reconfigure(**options)
    service = ApplicationService(load_config())
    raise SystemExit(McpServer(service).serve())


if __name__ == "__main__":
    main()
