"""只读、JSON 可序列化的邮件事实查询层。"""

from __future__ import annotations

from typing import Any

from agent_mail_bridge.database import get_connection, get_mail_package, query_mail_resources
from agent_mail_bridge.mail_common import parse_mailboxes


def list_mail_messages(
    db_path,
    *,
    account_ref: str | None = None,
    mailbox_ref: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sender: str | None = None,
    subject_keyword: str | None = None,
    has_attachments: bool | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    safe_limit = _limit(limit)
    safe_offset = max(0, int(offset))
    where: list[str] = []
    params: list[Any] = []
    _equal_filter(where, params, "account_ref", account_ref)
    _equal_filter(where, params, "mailbox_ref", mailbox_ref)
    _equal_filter(where, params, "archive_status", status)
    if date_from:
        where.append("received_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("received_at <= ?")
        params.append(date_to)
    if sender:
        where.append("from_email LIKE ? COLLATE NOCASE")
        params.append(f"%{sender}%")
    if subject_keyword:
        where.append("subject LIKE ? COLLATE NOCASE")
        params.append(f"%{subject_keyword}%")
    if has_attachments is not None:
        where.append("attachment_count > 0" if has_attachments else "attachment_count = 0")
    clause = " WHERE " + " AND ".join(where) if where else ""
    connection = get_connection(db_path)
    rows = connection.execute(
        f"SELECT * FROM mail_packages{clause} "
        "ORDER BY COALESCE(received_at, saved_at) DESC, id DESC LIMIT ? OFFSET ?",
        (*params, safe_limit, safe_offset),
    ).fetchall()
    return [_package_dto(dict(row)) for row in rows]


def get_mail_message(db_path, package_id: str) -> dict[str, Any] | None:
    package = get_mail_package(db_path, package_id)
    if package is None:
        return None
    dto = _package_dto(package)
    dto["resources"] = [
        _resource_dto(resource) for resource in query_mail_resources(db_path, package_id)
    ]
    return dto


def list_mail_resources(db_path, package_id: str) -> list[dict[str, Any]]:
    return [_resource_dto(row) for row in query_mail_resources(db_path, package_id)]


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
    return {
        "thread_ref": thread_ref,
        "account_ref": messages[0]["account_ref"],
        "message_count": len(messages),
        "messages": messages,
    }


def search_mail_facts(
    db_path,
    query: str,
    *,
    account_ref: str | None = None,
    mailbox_ref: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    keyword = (query or "").strip()
    if not keyword:
        return []
    pattern = f"%{keyword}%"
    where = [
        "(p.subject LIKE ? COLLATE NOCASE OR p.from_email LIKE ? COLLATE NOCASE "
        "OR p.to_emails LIKE ? COLLATE NOCASE OR p.cc_emails LIKE ? COLLATE NOCASE "
        "OR p.search_text LIKE ? COLLATE NOCASE OR EXISTS ("
        "SELECT 1 FROM mail_resources r WHERE r.package_id = p.package_id AND ("
        "r.display_name LIKE ? COLLATE NOCASE OR r.original_name LIKE ? COLLATE NOCASE "
        "OR r.original_url LIKE ? COLLATE NOCASE)))"
    ]
    params: list[Any] = [pattern] * 8
    if account_ref:
        where.append("p.account_ref = ?")
        params.append(account_ref)
    if mailbox_ref:
        where.append("p.mailbox_ref = ?")
        params.append(mailbox_ref)
    connection = get_connection(db_path)
    rows = connection.execute(
        f"SELECT p.* FROM mail_packages p WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(p.received_at, p.saved_at) DESC, p.id DESC LIMIT ? OFFSET ?",
        (*params, _limit(limit), max(0, int(offset))),
    ).fetchall()
    return [_package_dto(dict(row)) for row in rows]


def _package_dto(row: dict[str, Any]) -> dict[str, Any]:
    body = str(row.get("search_text") or "")
    return {
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
        "body_summary": body[:500],
        "body": {
            "plain_path": row.get("body_plain_path"),
            "html_path": row.get("body_html_path"),
            "readable_path": row.get("body_readable_path"),
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
        "package_root": str(row.get("package_root") or ""),
        "legacy": bool(row.get("legacy")),
    }


def _resource_dto(row: dict[str, Any]) -> dict[str, Any]:
    internal_type = str(row.get("resource_type") or "")
    return {
        "resource_id": str(row.get("resource_id") or ""),
        "package_id": str(row.get("package_id") or ""),
        "category": _user_category(internal_type),
        "internal_type": internal_type,
        "source": str(row.get("source_type") or ""),
        "display_name": str(row.get("display_name") or ""),
        "original_name": str(row.get("original_name") or ""),
        "mime_type": str(row.get("mime_type") or ""),
        "path": row.get("local_path"),
        "url": row.get("original_url"),
        "content_id": row.get("content_id"),
        "size_bytes": row.get("size_bytes"),
        "sha256": row.get("sha256"),
        "status": str(row.get("status") or ""),
        "error": str(row.get("error") or ""),
    }


def _user_category(internal_type: str) -> str:
    if internal_type.startswith("body_"):
        return "邮件内容"
    if internal_type == "inline_image":
        return "邮件中的图片"
    if internal_type == "attachment":
        return "附件"
    return "链接与下载"


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
