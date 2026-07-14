"""AgentMailBridge 本机 stdio MCP 服务。"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import deque
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
            return _success_response(request_id, {"tools": [_submit_result_tool()]})
        if method == "tools/call":
            return self._call_tool(request_id, message.get("params"))
        return _error_response(request_id, -32601, f"不支持的方法：{method}")

    def _initialize(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return _error_response(request_id, -32602, "initialize 参数无效")
        requested = params.get("protocolVersion")
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
                    "只能提交允许目录内的本地结果文件；收件人固定，"
                    "不能读取或修改邮箱凭据与配置。"
                ),
            },
        )

    def _call_tool(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or params.get("name") != "submit_result":
            return _error_response(request_id, -32602, "未知工具或调用参数无效")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error_response(request_id, -32602, "工具 arguments 必须是对象")

        validation_error = _validate_arguments(arguments)
        if validation_error:
            return _success_response(
                request_id,
                self._rejected_tool_result(arguments, "invalid_input", validation_error),
            )
        if not self.rate_limit.allow():
            return _success_response(
                request_id,
                self._rejected_tool_result(
                    arguments,
                    "rate_limited",
                    "一分钟内最多提交 5 次，请稍后重试",
                ),
            )

        result = self.service.submit_result(
            arguments["file_path"],
            title=arguments.get("title"),
            request_id=arguments.get("request_id"),
        )
        structured = _send_result_payload(result)
        structured = _redact_payload(structured, self.service)
        is_error = structured["status"] not in {
            "success",
            "duplicate",
            "sent_archive_failed",
        }
        return _success_response(
            request_id,
            _tool_result(structured, is_error=is_error),
        )

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


def _validate_arguments(arguments: dict[str, Any]) -> str | None:
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


def _submit_result_tool() -> dict[str, Any]:
    return {
        "name": "submit_result",
        "title": "提交 Agent 结果",
        "description": "验证允许目录内的结果文件，由产品原子 staging、校验 Hash 后发送到固定 Gmail 收件人。",
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
