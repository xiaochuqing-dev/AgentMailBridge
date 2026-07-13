"""统一受管文件查询，文件页面不得从业务历史拼装文件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    query_all_received_files_with_messages,
    query_recent_mcp_calls,
    query_recent_sent_files,
)
from agent_mail_bridge.security import SecurityError, assert_within_allowed_roots


STATUS_LABELS = {
    "saved": "已保存",
    "normal": "已保存",
    "ok": "已保存",
    "sent": "已发送",
    "success": "成功",
    "accepted": "成功",
    "failed": "失败",
    "error": "失败",
    "rejected": "失败",
    "duplicate": "重复",
    "duplicated": "重复",
    "partial": "部分完成",
    "attempt_created": "处理中",
    "missing": "文件已不存在",
    "modified": "文件已修改",
    "renamed": "文件已改名",
    "unsafe_path": "路径不可用",
    "allowed": "已保存",
    "dangerous": "危险类型",
    "unknown_type": "未知类型",
    "sent_archive_failed": "发送成功，归档失败",
}


def localize_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return STATUS_LABELS.get(raw, "其他")


def get_managed_files(cfg: AppConfig, limit: int = 500) -> list[dict[str, Any]]:
    """返回收件、发送归档和 Agent 结果的统一安全 DTO。"""
    rows: list[dict[str, Any]] = []
    received_roots = [cfg.data_root_path]
    allowed_roots = cfg.effective_allowed_send_roots

    for record in query_all_received_files_with_messages(cfg.db_path):
        item = _base_item(
            record,
            identifier=f"received:{record.get('id')}",
            category="收件文件",
            source=_received_source(record),
            display_name=str(
                record.get("saved_filename")
                or record.get("original_filename")
                or "未命名文件"
            ),
            path_value=record.get("saved_path"),
            size_value=record.get("size_bytes"),
            time_value=record.get("created_at") or record.get("saved_date"),
            roots=received_roots,
        )
        item.update({
            "file_type": str(record.get("file_type") or ""),
            "mime_type": str(record.get("mime_type") or ""),
            "request_id": "",
            "message_id": str(record.get("message_id") or ""),
            "subject": str(record.get("subject") or ""),
            "sha256": str(record.get("sha256") or ""),
        })
        rows.append(item)

    sent_request_ids: set[str] = set()
    seen_paths: set[str] = {
        _path_key(item.get("path")) for item in rows if item.get("path")
    }
    for record in query_recent_sent_files(cfg.db_path, max(limit, 500)):
        request_id = str(record.get("request_id") or "")
        if request_id:
            sent_request_ids.add(request_id.casefold())
        path_value = record.get("sent_copy_path") or record.get("send_copy_path")
        if not path_value:
            continue
        category = (
            "已发送归档"
            if record.get("source_origin") == "manual_gui"
            else "Agent 结果"
        )
        source = "手动发件" if category == "已发送归档" else "Agent / MCP"
        item = _base_item(
            record,
            identifier=f"sent:{record.get('id')}",
            category=category,
            source=source,
            display_name=str(
                record.get("original_filename") or Path(str(path_value)).name
            ),
            path_value=path_value,
            size_value=record.get("size_bytes"),
            time_value=record.get("sent_at") or record.get("created_at"),
            roots=allowed_roots,
        )
        item.update({
            "file_type": "sent_archive",
            "mime_type": "",
            "request_id": request_id,
            "message_id": "",
            "subject": str(record.get("subject") or ""),
            "sha256": str(record.get("sha256") or ""),
        })
        key = _path_key(item.get("path"))
        if key and key in seen_paths:
            continue
        if key:
            seen_paths.add(key)
        rows.append(item)

    for record in query_recent_mcp_calls(cfg.db_path, max(limit, 500)):
        request_id = str(record.get("request_id") or "")
        if request_id and request_id.casefold() in sent_request_ids:
            continue
        path_value = record.get("file_path")
        if not path_value:
            continue
        key = _path_key(path_value)
        if key and key in seen_paths:
            continue
        item = _base_item(
            record,
            identifier=f"mcp:{record.get('id')}",
            category="Agent 结果",
            source="Agent / MCP",
            display_name=Path(str(path_value)).name,
            path_value=path_value,
            size_value=None,
            time_value=record.get("created_at"),
            roots=allowed_roots,
        )
        item.update({
            "file_type": "agent_source",
            "mime_type": "",
            "request_id": request_id,
            "message_id": "",
            "subject": str(record.get("title") or ""),
            "sha256": "",
        })
        if key:
            seen_paths.add(key)
        rows.append(item)

    rows.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    return rows[: max(1, limit)]


def _base_item(
    record: dict[str, Any],
    *,
    identifier: str,
    category: str,
    source: str,
    display_name: str,
    path_value: Any,
    size_value: Any,
    time_value: Any,
    roots: list[Path],
) -> dict[str, Any]:
    raw_path = str(path_value or "")
    safe_path = ""
    exists = False
    if raw_path:
        try:
            path = Path(raw_path)
            assert_within_allowed_roots(path, roots)
            safe_path = str(path)
            exists = path.is_file()
        except (OSError, SecurityError):
            safe_path = ""

    size_bytes: int | None
    try:
        size_bytes = int(size_value) if size_value is not None else None
    except (TypeError, ValueError):
        size_bytes = None
    if size_bytes is None and safe_path and exists:
        try:
            size_bytes = Path(safe_path).stat().st_size
        except OSError:
            size_bytes = None
    raw_status = str(record.get("status") or "")
    status_display = (
        "文件已不存在"
        if safe_path and not exists
        else localize_status(raw_status)
    )
    return {
        "id": identifier,
        "category": category,
        "source": source,
        "display_name": display_name,
        "path": safe_path,
        "size_bytes": size_bytes,
        "size_known": size_bytes is not None,
        "time": str(time_value or ""),
        "status": raw_status,
        "status_display": status_display,
        "exists": exists,
    }


def _received_source(record: dict[str, Any]) -> str:
    backend = str(record.get("message_backend") or record.get("message_source") or "")
    return "Gmail API" if backend == "gmail_api" else "Gmail IMAP" if backend == "imap" else "Gmail"


def _path_key(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(Path(str(value)).resolve()).casefold()
    except OSError:
        return str(value).casefold()
