"""只读、JSON 可序列化的邮件事实查询层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_mail_bridge.database import get_connection, get_mail_package, query_mail_resources
from agent_mail_bridge.mail_common import parse_mailboxes
from agent_mail_bridge.mail_links import classify_mail_link
from agent_mail_bridge.mail_resource_access import resource_capabilities


def list_mail_messages(
    db_path,
    *,
    account_ref: str | None = None,
    mailbox_ref: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    subject_keyword: str | None = None,
    has_attachments: bool | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "newest",
) -> list[dict[str, Any]]:
    safe_limit = _limit(limit)
    safe_offset = max(0, int(offset))
    where: list[str] = []
    params: list[Any] = []
    _equal_filter(where, params, "account_ref", account_ref)
    _equal_filter(where, params, "mailbox_ref", mailbox_ref)
    _equal_filter(where, params, "archive_status", status)
    if date_from:
        where.append("COALESCE(received_at, saved_at) >= ?")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(received_at, saved_at) <= ?")
        params.append(date_to)
    if sender:
        where.append("from_email LIKE ? COLLATE NOCASE")
        params.append(f"%{sender}%")
    if recipient:
        where.append(
            "(to_emails LIKE ? COLLATE NOCASE OR cc_emails LIKE ? COLLATE NOCASE "
            "OR bcc_emails LIKE ? COLLATE NOCASE)"
        )
        params.extend([f"%{recipient}%"] * 3)
    if subject_keyword:
        where.append("subject LIKE ? COLLATE NOCASE")
        params.append(f"%{subject_keyword}%")
    if has_attachments is not None:
        where.append("attachment_count > 0" if has_attachments else "attachment_count = 0")
    clause = " WHERE " + " AND ".join(where) if where else ""
    connection = get_connection(db_path)
    direction = _sort_direction(sort)
    rows = connection.execute(
        f"SELECT * FROM mail_packages{clause} "
        f"ORDER BY COALESCE(received_at, saved_at) {direction}, id {direction} "
        "LIMIT ? OFFSET ?",
        (*params, safe_limit, safe_offset),
    ).fetchall()
    return [_package_dto(dict(row)) for row in rows]


def get_mail_message(db_path, package_id: str) -> dict[str, Any] | None:
    package = get_mail_package(db_path, package_id)
    if package is None:
        return None
    dto = _package_dto(package)
    dto["resources"] = [
        _resource_dto(resource, package_root=dto["package_root"])
        for resource in query_mail_resources(db_path, package_id)
    ]
    return dto


def list_mail_resources(db_path, package_id: str) -> list[dict[str, Any]]:
    package = get_mail_package(db_path, package_id)
    root = str((package or {}).get("package_root") or "")
    return [
        _resource_dto(row, package_root=root)
        for row in query_mail_resources(db_path, package_id)
    ]


def list_mail_threads(
    db_path,
    *,
    account_ref: str | None = None,
    mailbox_ref: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["thread_ref IS NOT NULL", "thread_ref != ''"]
    params: list[Any] = []
    _equal_filter(where, params, "account_ref", account_ref)
    _equal_filter(where, params, "mailbox_ref", mailbox_ref)
    connection = get_connection(db_path)
    rows = connection.execute(
        "SELECT account_ref, mailbox_ref, thread_ref, COUNT(*) AS message_count, "
        "MIN(COALESCE(received_at, saved_at)) AS first_at, "
        "MAX(COALESCE(received_at, saved_at)) AS last_at "
        f"FROM mail_packages WHERE {' AND '.join(where)} "
        "GROUP BY account_ref, mailbox_ref, thread_ref "
        "ORDER BY last_at DESC LIMIT ? OFFSET ?",
        (*params, _limit(limit), max(0, int(offset))),
    ).fetchall()
    return [dict(row) for row in rows]


def get_mail_thread(
    db_path,
    thread_ref: str,
    *,
    account_ref: str | None = None,
) -> dict[str, Any] | None:
    where = ["thread_ref = ?"]
    params: list[Any] = [thread_ref]
    _equal_filter(where, params, "account_ref", account_ref)
    connection = get_connection(db_path)
    rows = connection.execute(
        f"SELECT * FROM mail_packages WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(received_at, saved_at) ASC, id ASC",
        params,
    ).fetchall()
    if not rows:
        return None
    messages = [_package_dto(dict(row)) for row in rows]
    result = {
        "thread_ref": thread_ref,
        "account_ref": messages[0]["account_ref"],
        "message_count": len(messages),
        "messages": messages,
    }
    return result


def search_mail_facts(
    db_path,
    query: str,
    *,
    account_ref: str | None = None,
    mailbox_ref: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    subject: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    has_attachments: bool | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "newest",
) -> list[dict[str, Any]]:
    keyword = " ".join((query or "").split())
    where: list[str] = []
    params: list[Any] = []
    for token in keyword.split(" "):
        pattern = f"%{token}%"
        status_values = _matching_status_values(token)
        status_clause = ""
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            status_clause = (
                f" OR lower(p.archive_status) IN ({placeholders})"
                f" OR lower(p.parse_status) IN ({placeholders})"
            )
        where.append(
            "(p.subject LIKE ? COLLATE NOCASE OR p.from_email LIKE ? COLLATE NOCASE "
            "OR p.to_emails LIKE ? COLLATE NOCASE OR p.cc_emails LIKE ? COLLATE NOCASE "
            "OR p.bcc_emails LIKE ? COLLATE NOCASE "
            "OR p.search_text LIKE ? COLLATE NOCASE "
            "OR p.archive_status LIKE ? COLLATE NOCASE "
            "OR p.parse_status LIKE ? COLLATE NOCASE OR EXISTS ("
            "SELECT 1 FROM mail_resources r WHERE r.package_id = p.package_id AND ("
            "r.display_name LIKE ? COLLATE NOCASE OR r.original_name LIKE ? COLLATE NOCASE "
            "OR r.original_url LIKE ? COLLATE NOCASE OR r.status LIKE ? COLLATE NOCASE))"
            f"{status_clause})"
        )
        params.extend([pattern] * 12)
        params.extend(status_values)
        params.extend(status_values)
    if account_ref:
        where.append("p.account_ref = ?")
        params.append(account_ref)
    if mailbox_ref:
        where.append("p.mailbox_ref = ?")
        params.append(mailbox_ref)
    if date_from:
        where.append("COALESCE(p.received_at, p.saved_at) >= ?")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(p.received_at, p.saved_at) <= ?")
        params.append(date_to)
    if subject:
        where.append("p.subject LIKE ? COLLATE NOCASE")
        params.append(f"%{subject}%")
    if sender:
        where.append("p.from_email LIKE ? COLLATE NOCASE")
        params.append(f"%{sender}%")
    if recipient:
        where.append(
            "(p.to_emails LIKE ? COLLATE NOCASE OR p.cc_emails LIKE ? COLLATE NOCASE "
            "OR p.bcc_emails LIKE ? COLLATE NOCASE)"
        )
        params.extend([f"%{recipient}%"] * 3)
    if has_attachments is not None:
        where.append(
            "p.attachment_count > 0" if has_attachments else "p.attachment_count = 0"
        )
    if status:
        natural = _matching_status_values(status)
        if natural:
            placeholders = ",".join("?" for _ in natural)
            where.append(
                f"(lower(p.archive_status) IN ({placeholders}) "
                f"OR lower(p.parse_status) IN ({placeholders}))"
            )
            params.extend(natural)
            params.extend(natural)
        else:
            where.append(
                "(p.archive_status = ? COLLATE NOCASE OR p.parse_status = ? COLLATE NOCASE)"
            )
            params.extend([status, status])
    connection = get_connection(db_path)
    direction = _sort_direction(sort)
    clause = " AND ".join(where) if where else "1 = 1"
    rows = connection.execute(
        f"SELECT p.* FROM mail_packages p WHERE {clause} "
        f"ORDER BY COALESCE(p.received_at, p.saved_at) {direction}, p.id {direction} "
        "LIMIT ? OFFSET ?",
        (*params, _limit(limit), max(0, int(offset))),
    ).fetchall()
    return [_package_dto(dict(row)) for row in rows]


def _package_dto(row: dict[str, Any]) -> dict[str, Any]:
    body = str(row.get("search_text") or "")
    package_root = str(row.get("package_root") or "")
    result = {
        "package_id": str(row.get("package_id") or ""),
        "account_ref": str(row.get("account_ref") or ""),
        "mailbox_ref": str(row.get("mailbox_ref") or ""),
        "backend": str(row.get("backend") or ""),
        "message_id": str(row.get("message_id") or ""),
        "provider_message_id": str(row.get("provider_message_id") or ""),
        "thread_ref": str(row.get("thread_ref") or ""),
        "subject": str(row.get("subject") or ""),
        "from": str(row.get("from_email") or ""),
        "to": parse_mailboxes(str(row.get("to_emails") or "")),
        "cc": parse_mailboxes(str(row.get("cc_emails") or "")),
        "bcc": parse_mailboxes(str(row.get("bcc_emails") or "")),
        "sent_at": row.get("sent_at"),
        "received_at": row.get("received_at"),
        "saved_at": row.get("saved_at"),
        "body_summary": body[:800],
        "body": {
            "plain_path": row.get("body_plain_path"),
            "html_path": row.get("body_html_path"),
            "readable_path": row.get("body_readable_path"),
            "plain_absolute_path": _absolute_package_path(package_root, row.get("body_plain_path")),
            "html_absolute_path": _absolute_package_path(package_root, row.get("body_html_path")),
            "readable_absolute_path": _absolute_package_path(package_root, row.get("body_readable_path")),
            "text_sha256": row.get("body_text_sha256"),
        },
        "counts": {
            "resources": int(row.get("resource_count") or 0),
            "attachments": int(row.get("attachment_count") or 0),
            "inline_images": int(row.get("inline_image_count") or 0),
            "links": int(row.get("link_count") or 0),
            "downloads": int(row.get("downloaded_count") or 0),
        },
        "raw_eml": {
            "status": str(row.get("raw_eml_status") or ""),
            "path": row.get("raw_eml_path"),
            "sha256": row.get("raw_eml_sha256"),
        },
        "archive_status": str(row.get("archive_status") or ""),
        "parse_status": str(row.get("parse_status") or ""),
        "last_error": str(row.get("last_error") or ""),
        "package_root": package_root,
        "legacy": bool(row.get("legacy")),
    }
    return result


def _matching_status_values(token: str) -> list[str]:
    normalized = str(token or "").strip().casefold()
    if not normalized:
        return []
    labels = {
        "ready": "已归档",
        "saved": "已归档",
        "normal": "已归档",
        "partial": "部分完成",
        "failed": "处理失败",
        "needs_attention": "需要处理",
        "staging": "处理中",
    }
    return [
        value for value, label in labels.items()
        if normalized in label.casefold() or label.casefold() in normalized
    ]


def _resource_dto(
    row: dict[str, Any], *, package_root: str = ""
) -> dict[str, Any]:
    internal_type = str(row.get("resource_type") or "")
    source_type = str(row.get("source_type") or "")
    url = str(row.get("original_url") or "")
    link_kind = ""
    if internal_type == "link" and url:
        classified = classify_mail_link(
            url,
            source_type=source_type or "plain_text",
            anchor_text=str(row.get("display_name") or ""),
        )
        link_kind = str((classified or {}).get("link_type") or "webpage")
    result = {
        "resource_id": str(row.get("resource_id") or ""),
        "package_id": str(row.get("package_id") or ""),
        "category": _user_category(internal_type),
        "internal_type": internal_type,
        "source": source_type,
        "display_name": str(row.get("display_name") or ""),
        "original_name": str(row.get("original_name") or ""),
        "mime_type": str(row.get("mime_type") or ""),
        "path": row.get("local_path"),
        "absolute_path": _absolute_package_path(package_root, row.get("local_path")),
        "url": row.get("original_url"),
        "link_kind": link_kind,
        "kind_display": _resource_kind_display(internal_type, link_kind),
        "content_id": row.get("content_id"),
        "size_bytes": row.get("size_bytes"),
        "sha256": row.get("sha256"),
        "status": str(row.get("status") or ""),
        "status_display": _resource_status_display(str(row.get("status") or "")),
        "error": str(row.get("error") or ""),
    }
    result["capabilities"] = resource_capabilities(result)
    result["capability"] = result["capabilities"][0]
    result["available"] = bool(result.get("absolute_path")) and "unavailable" not in result["capabilities"]
    return result


def _user_category(internal_type: str) -> str:
    if internal_type.startswith("body_"):
        return "邮件内容"
    if internal_type == "inline_image":
        return "邮件中的图片"
    if internal_type == "attachment":
        return "附件"
    if internal_type == "downloaded_file":
        return "下载文件"
    return "链接与下载"


def _resource_kind_display(internal_type: str, link_kind: str) -> str:
    if internal_type.startswith("body_"):
        return "邮件内容"
    if internal_type == "inline_image":
        return "邮件中的图片"
    if internal_type == "attachment":
        return "附件"
    if internal_type == "downloaded_file":
        return "已下载文件"
    return {
        "downloadable_file": "可下载文件",
        "cloud_document": "云端文档",
        "image_link": "图片链接",
        "webpage": "网页链接",
    }.get(link_kind, "网页链接")


def _resource_status_display(status: str) -> str:
    return {
        "recognized": "已识别",
        "login_may_be_required": "可能需要登录",
        "downloaded": "已下载",
        "download_failed": "下载失败",
        "saved": "已保存",
        "normal": "已保存",
        "failed": "处理失败",
        "partial": "部分完成",
    }.get(status.strip().lower(), "已保存" if status else "状态未知")


def _absolute_package_path(package_root: str, value: Any) -> str:
    if not package_root or not value:
        return ""
    try:
        root = Path(package_root).resolve()
        raw = Path(str(value))
        candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        candidate.relative_to(root)
        return str(candidate)
    except (OSError, ValueError):
        return ""


def _equal_filter(
    where: list[str], params: list[Any], column: str, value: str | None
) -> None:
    if value:
        where.append(f"{column} = ?")
        params.append(value)


def _limit(value: int) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("limit 必须大于 0")
    return min(number, 500)


def _sort_direction(value: str) -> str:
    normalized = str(value or "newest").strip().lower()
    if normalized == "newest":
        return "DESC"
    if normalized == "oldest":
        return "ASC"
    raise ValueError("sort 仅支持 newest 或 oldest")
