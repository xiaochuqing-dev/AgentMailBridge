"""统一邮件归档：一封邮件一个 package，所有事实和文件都归属 package_id。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    get_mail_package_by_identity,
    query_mail_resources,
    query_legacy_messages_for_backfill,
    query_received_files_by_message,
    query_trusted_domains,
    save_migration_metadata,
    store_mail_archive_atomically,
)
from agent_mail_bridge.mail_common import (
    NormalizedMail,
    canonical_gmail_address,
    derive_thread_ref,
)
from agent_mail_bridge.mail_links import detect_mail_links
from agent_mail_bridge.security import SecurityError, assert_within_root
from agent_mail_bridge.trusted_downloads import download_trusted_url, is_host_trusted
from agent_mail_bridge.utils import sanitize_filename, sha256_of_bytes, sha256_of_file, split_ext


MANIFEST_SCHEMA_VERSION = 1
LEGACY_MIGRATION_KEY = "unified_mail_archive_v1"

_locks_guard = threading.Lock()
_package_locks: dict[str, threading.Lock] = {}


@dataclass
class ArchiveResult:
    status: str
    package_id: str
    saved_files: list[str]
    attachments: int
    error: str = ""


def stable_account_ref(cfg: AppConfig) -> str:
    address = canonical_gmail_address(cfg.gmail_address)
    return f"gmail:{address}" if address else "gmail:legacy-unknown"


def stable_package_id(account_ref: str, message_id: str) -> str:
    digest = hashlib.sha256(f"{account_ref}\n{message_id.casefold()}".encode("utf-8")).hexdigest()
    return f"pkg_{digest[:24]}"


def archive_normalized_mail(
    cfg: AppConfig, mail: NormalizedMail, message_id: str
) -> ArchiveResult:
    """保存一封已匹配收件规则的邮件；partial 重试复用同一目录。"""
    account_ref = stable_account_ref(cfg)
    package_id = stable_package_id(account_ref, message_id)
    lock = _lock_for(package_id)
    with lock:
        existing = get_mail_package_by_identity(cfg.db_path, account_ref, message_id)
        if existing and existing.get("archive_status") in {"ready", "legacy"}:
            existing_root = Path(str(existing.get("package_root") or ""))
            raw_ok = (
                existing.get("raw_eml_status") != "available"
                or bool(existing.get("raw_eml_path") and (existing_root / str(existing["raw_eml_path"])).is_file())
            )
            if existing_root.is_dir() and (existing_root / "manifest.json").is_file() and raw_ok:
                return ArchiveResult("duplicate", str(existing["package_id"]), [], 0)
        package_root = (
            Path(str(existing["package_root"]))
            if existing and existing.get("package_root")
            else _package_root(cfg, package_id, mail.subject, mail.saved_date)
        )
        if not mail.raw_bytes:
            raise ValueError("正式接收邮件缺少真实 RFC822 原文")
        existing_resources = {
            item["resource_id"]: item
            for item in query_mail_resources(cfg.db_path, package_id)
        } if existing else {}
        assert_within_root(package_root, cfg.data_root_path)
        is_new = not package_root.exists()
        work_root = (
            cfg.received_dir / "mail" / ".staging" / f"{package_id}.tmp"
            if is_new else package_root
        )
        assert_within_root(work_root, cfg.data_root_path)
        work_root.mkdir(parents=True, exist_ok=True)
        for name in ("body", "inline", "attachments", "downloads"):
            (work_root / name).mkdir(parents=True, exist_ok=True)

        resources: list[dict[str, Any]] = []
        errors: list[str] = []
        raw_hash = sha256_of_bytes(mail.raw_bytes)
        _write_atomic(work_root / "raw.eml", mail.raw_bytes)
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sort_order = 0

        body_paths: dict[str, str | None] = {
            "body_plain_path": None,
            "body_html_path": None,
            "body_readable_path": None,
        }
        body_specs = []
        if mail.body_plain:
            body_specs.append(("body_plain", "body/body.txt", mail.body_plain, "text/plain"))
        if mail.body_html:
            body_specs.append(("body_html", "body/body.html", mail.body_html, "text/html"))
        readable_document = _readable_document(mail, message_id)
        body_specs.append(("body_readable", "body/body.md", readable_document, "text/markdown"))
        for resource_type, relative_path, content, mime_type in body_specs:
            sort_order += 1
            resource = _text_resource(
                package_id, resource_type, relative_path, content, mime_type, sort_order
            )
            try:
                _write_atomic(work_root / relative_path, content.encode("utf-8"))
            except OSError as exc:
                resource.update(status="failed", error=str(exc), local_path=None, sha256=None)
                errors.append(f"正文资源保存失败：{resource_type}")
            else:
                key = {
                    "body_plain": "body_plain_path",
                    "body_html": "body_html_path",
                    "body_readable": "body_readable_path",
                }[resource_type]
                body_paths[key] = relative_path
            resources.append(resource)

        used_names: dict[str, int] = {}
        for index, attachment in enumerate(mail.attachments, 1):
            sort_order += 1
            resource_type = "inline_image" if attachment.is_inline else "attachment"
            folder = "inline" if attachment.is_inline else "attachments"
            max_name_len = max(
                8, 230 - len(str((work_root / folder).resolve())) - 1
            )
            safe_name = _unique_resource_name(
                attachment.filename, used_names, max_len=max_name_len
            )
            relative_path = f"{folder}/{safe_name}"
            resource_id = _resource_id(
                package_id, resource_type, attachment.part_id or str(index), attachment.content_id
            )
            resource: dict[str, Any] = {
                "resource_id": resource_id,
                "resource_type": resource_type,
                "source_type": "mime_inline" if attachment.is_inline else "mime_attachment",
                "display_name": attachment.filename or safe_name,
                "original_name": attachment.filename or "",
                "mime_type": attachment.mime_type,
                "local_path": relative_path,
                "original_url": None,
                "content_id": attachment.content_id or None,
                "size_bytes": len(attachment.content) if not attachment.error else None,
                "sha256": sha256_of_bytes(attachment.content) if attachment.content and not attachment.error else None,
                "status": attachment.security_status,
                "error": attachment.error or None,
                "sort_order": sort_order,
            }
            if attachment.error:
                resource["status"] = "failed"
                resource["local_path"] = None
                errors.append(f"邮件资源处理失败：{attachment.filename}")
            else:
                try:
                    _write_atomic(work_root / relative_path, attachment.content)
                except OSError as exc:
                    resource.update(status="failed", error=str(exc), local_path=None)
                    errors.append(f"邮件资源保存失败：{attachment.filename}")
            resources.append(resource)

        links = detect_mail_links(mail.body_plain or mail.body_text, mail.body_html)
        link_resources: list[dict[str, Any]] = []
        for index, link in enumerate(links, 1):
            sort_order += 1
            resource = {
                "resource_id": _resource_id(package_id, "link", link["url"]),
                "resource_type": "link",
                "source_type": link["source_type"],
                "display_name": link["display_name"],
                "original_name": None,
                "mime_type": None,
                "local_path": None,
                "original_url": link["url"],
                "content_id": None,
                "size_bytes": None,
                "sha256": None,
                "status": link["status"],
                "error": None,
                "sort_order": sort_order,
                "link_type": link["link_type"],
                "hostname": link["hostname"],
            }
            resources.append(resource)
            link_resources.append(resource)

        trusted = query_trusted_domains(cfg.db_path)
        for link_resource in link_resources:
            if link_resource["link_type"] != "downloadable_file":
                continue
            if not is_host_trusted(link_resource["hostname"], trusted):
                continue
            downloaded_resource_id = _resource_id(
                package_id, "downloaded_file", str(link_resource["original_url"])
            )
            existing_download = existing_resources.get(downloaded_resource_id)
            if existing_download and existing_download.get("local_path"):
                existing_path = package_root / str(existing_download["local_path"])
                try:
                    if (
                        existing_path.is_file()
                        and existing_download.get("sha256")
                        and sha256_of_file(existing_path) == existing_download["sha256"]
                    ):
                        sort_order += 1
                        preserved = dict(existing_download)
                        preserved["sort_order"] = sort_order
                        preserved.pop("id", None)
                        preserved.pop("package_id", None)
                        preserved.pop("created_at", None)
                        preserved.pop("updated_at", None)
                        resources.append(preserved)
                        link_resource["status"] = "downloaded"
                        continue
                except OSError:
                    pass
            try:
                downloaded = download_trusted_url(
                    str(link_resource["original_url"]),
                    work_root / "downloads",
                    max_bytes=cfg.trusted_download_max_bytes,
                    timeout_seconds=cfg.trusted_download_timeout_seconds,
                    max_redirects=cfg.trusted_download_max_redirects,
                )
            except Exception as exc:  # noqa: BLE001
                link_resource["status"] = "download_failed"
                link_resource["error"] = str(exc)
                errors.append(f"可信链接下载失败：{link_resource['hostname']}")
                continue
            sort_order += 1
            absolute_download = Path(downloaded["saved_path"])
            relative_download = absolute_download.relative_to(work_root).as_posix()
            link_resource["status"] = "downloaded"
            resources.append({
                "resource_id": downloaded_resource_id,
                "resource_type": "downloaded_file",
                "source_type": "trusted_link",
                "display_name": downloaded["original_filename"],
                "original_name": downloaded["original_filename"],
                "mime_type": downloaded["mime_type"],
                "local_path": relative_download,
                "original_url": link_resource["original_url"],
                "content_id": None,
                "size_bytes": downloaded["size_bytes"],
                "sha256": downloaded["sha256"],
                "status": "downloaded",
                "error": None,
                "sort_order": sort_order,
            })

        attachment_count = sum(item["resource_type"] == "attachment" for item in resources)
        inline_count = sum(item["resource_type"] == "inline_image" for item in resources)
        link_count = sum(item["resource_type"] == "link" for item in resources)
        downloaded_count = sum(item["resource_type"] == "downloaded_file" for item in resources)
        archive_status = "partial" if errors else "ready"
        thread_ref = derive_thread_ref(mail, message_id)
        package = {
            "package_id": package_id,
            "account_ref": account_ref,
            "mailbox_ref": mail.mailbox_ref or _default_mailbox_ref(mail.backend),
            "backend": mail.backend,
            "message_id": message_id,
            "provider_message_id": mail.backend_message_id or None,
            "thread_ref": thread_ref,
            "gmail_thread_id": mail.thread_id or None,
            "gmail_uid": mail.uid or None,
            "subject": mail.subject,
            "from_email": mail.from_raw,
            "to_emails": mail.to_raw,
            "cc_emails": mail.cc_raw,
            "bcc_emails": mail.bcc_raw,
            "sent_at": mail.sent_at or mail.received_at,
            "received_at": mail.received_at,
            "saved_at": saved_at,
            "saved_date": mail.saved_date,
            "package_root": str(package_root),
            "raw_eml_path": "raw.eml",
            "raw_eml_sha256": raw_hash,
            "raw_eml_status": "available",
            **body_paths,
            "body_text_sha256": sha256_of_bytes((mail.body_text or "").encode("utf-8")),
            "search_text": mail.body_text or "",
            "resource_count": len(resources),
            "attachment_count": attachment_count,
            "inline_image_count": inline_count,
            "link_count": link_count,
            "downloaded_count": downloaded_count,
            "archive_status": archive_status,
            "parse_status": "partial" if errors else "parsed",
            "last_error": "; ".join(errors)[:2000] or None,
            "legacy": False,
        }
        manifest = _manifest(package, resources, errors)
        _write_manifest(work_root, manifest)
        if is_new:
            package_root.parent.mkdir(parents=True, exist_ok=True)
            if package_root.exists():
                shutil.rmtree(work_root, ignore_errors=True)
            else:
                os.replace(work_root, package_root)

        compatibility_files = _compatibility_files(package_root, resources)
        store_mail_archive_atomically(cfg.db_path, package, resources, compatibility_files)
        saved_files = [item["saved_path"] for item in compatibility_files]
        return ArchiveResult(
            archive_status,
            package_id,
            saved_files,
            attachment_count,
            "; ".join(errors),
        )


def backfill_legacy_mail_packages(cfg: AppConfig) -> dict[str, Any]:
    """幂等迁移旧邮件；复制数据、不删除旧文件、不伪造 raw.eml。"""
    rows = query_legacy_messages_for_backfill(cfg.db_path)
    if not rows:
        return {"status": "no_changes", "migrated": 0, "failed": 0}
    save_migration_metadata(
        cfg.db_path, LEGACY_MIGRATION_KEY,
        schema_version=MANIFEST_SCHEMA_VERSION, status="running",
        details={"pending": len(rows)},
    )
    migrated = 0
    failed = 0
    for row in rows:
        try:
            _backfill_one_legacy(cfg, row)
            migrated += 1
        except Exception:  # noqa: BLE001
            failed += 1
    status = "partial" if failed else "completed"
    details = {"migrated": migrated, "failed": failed, "total": len(rows)}
    save_migration_metadata(
        cfg.db_path, LEGACY_MIGRATION_KEY,
        schema_version=MANIFEST_SCHEMA_VERSION, status=status, details=details,
    )
    return {"status": status, **details}


def _backfill_one_legacy(cfg: AppConfig, row: dict[str, Any]) -> None:
    message_id = str(row.get("message_id") or f"legacy:{row['id']}")
    account_ref = stable_account_ref(cfg)
    package_id = stable_package_id(account_ref, message_id)
    package_root = _package_root(
        cfg, package_id, str(row.get("subject") or "旧邮件"),
        str(row.get("saved_date") or "1970-01-01"),
    )
    assert_within_root(package_root, cfg.data_root_path)
    work_root = package_root if package_root.exists() else (
        cfg.received_dir / "mail" / ".staging" / f"{package_id}.tmp"
    )
    work_root.mkdir(parents=True, exist_ok=True)
    for name in ("body", "inline", "attachments", "downloads"):
        (work_root / name).mkdir(parents=True, exist_ok=True)
    files = query_received_files_by_message(cfg.db_path, message_id)
    if not files and row.get("body_file_path"):
        files = [{
            "id": None, "file_type": "body", "original_filename": row.get("subject") or "旧邮件",
            "saved_filename": Path(str(row["body_file_path"])).name,
            "saved_path": row["body_file_path"], "sha256": row.get("body_sha256"),
            "size_bytes": None, "mime_type": "text/markdown", "status": "normal",
        }]
    resources: list[dict[str, Any]] = []
    compatibility: list[dict[str, Any]] = []
    errors: list[str] = []
    used_names: dict[str, int] = {}
    body_relative: str | None = None
    for index, item in enumerate(files, 1):
        is_body = str(item.get("file_type")) == "body"
        resource_type = "body_readable" if is_body else "attachment"
        folder = "body" if is_body else "attachments"
        original_name = str(item.get("original_filename") or item.get("saved_filename") or "旧文件")
        max_name_len = max(
            8, 230 - len(str((work_root / folder).resolve())) - 1
        )
        safe_name = (
            "body.md" if is_body
            else _unique_resource_name(original_name, used_names, max_len=max_name_len)
        )
        relative_path = f"{folder}/{safe_name}"
        resource_id = _resource_id(package_id, resource_type, str(item.get("id") or index))
        resource = {
            "resource_id": resource_id,
            "resource_type": resource_type,
            "source_type": "legacy_received_file",
            "display_name": original_name,
            "original_name": original_name,
            "mime_type": item.get("mime_type"),
            "local_path": relative_path,
            "original_url": None,
            "content_id": None,
            "size_bytes": item.get("size_bytes"),
            "sha256": item.get("sha256"),
            "status": "legacy_available",
            "error": None,
            "sort_order": index,
        }
        source = Path(str(item.get("saved_path") or ""))
        try:
            assert_within_root(source, cfg.data_root_path)
            if not source.is_file():
                raise FileNotFoundError("旧文件不存在")
            _copy_atomic(source, work_root / relative_path)
            actual_hash = sha256_of_file(work_root / relative_path)
            resource["sha256"] = actual_hash
            resource["size_bytes"] = (work_root / relative_path).stat().st_size
        except (OSError, SecurityError) as exc:
            resource.update(status="failed", error=str(exc), local_path=None)
            errors.append(f"旧文件迁移失败：{original_name}")
        if is_body and resource.get("local_path"):
            body_relative = relative_path
        resources.append(resource)
        if item.get("id"):
            compatibility.append({
                "legacy_file_id": item["id"], "resource_id": resource_id,
                "file_type": "body" if is_body else "attachment",
            })

    saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    package = {
        "package_id": package_id,
        "account_ref": account_ref,
        "mailbox_ref": _default_mailbox_ref(str(row.get("backend") or "imap")),
        "backend": str(row.get("backend") or row.get("source") or "legacy"),
        "message_id": message_id,
        "provider_message_id": row.get("gmail_message_id"),
        "thread_ref": f"gmail:{row['gmail_thread_id']}" if row.get("gmail_thread_id") else f"rfc:{message_id}",
        "gmail_thread_id": row.get("gmail_thread_id"),
        "gmail_uid": row.get("gmail_uid"),
        "subject": str(row.get("subject") or ""),
        "from_email": str(row.get("from_email") or ""),
        "to_emails": str(row.get("to_email") or ""),
        "cc_emails": "", "bcc_emails": "",
        "sent_at": row.get("received_at"), "received_at": row.get("received_at"),
        "saved_at": saved_at, "saved_date": str(row.get("saved_date") or ""),
        "package_root": str(package_root),
        "raw_eml_path": None, "raw_eml_sha256": None,
        "raw_eml_status": "legacy_missing",
        "body_plain_path": None, "body_html_path": None,
        "body_readable_path": body_relative,
        "body_text_sha256": row.get("body_sha256"), "search_text": "",
        "resource_count": len(resources),
        "attachment_count": sum(item["resource_type"] == "attachment" for item in resources),
        "inline_image_count": 0, "link_count": 0, "downloaded_count": 0,
        "archive_status": "partial" if errors else "legacy",
        "parse_status": "legacy_limited",
        "last_error": "; ".join(errors)[:2000] or None,
        "legacy": True,
    }
    _write_manifest(work_root, _manifest(package, resources, errors, legacy=True))
    if work_root != package_root:
        package_root.parent.mkdir(parents=True, exist_ok=True)
        if not package_root.exists():
            os.replace(work_root, package_root)
    store_mail_archive_atomically(cfg.db_path, package, resources, compatibility)


def _package_root(
    cfg: AppConfig, package_id: str, subject: str, saved_date: str
) -> Path:
    try:
        parsed = datetime.strptime(saved_date[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        parsed = datetime(1970, 1, 1)
    base = cfg.received_dir / "mail" / f"{parsed.year:04d}" / f"{parsed.month:02d}" / f"{parsed.day:02d}"
    slug = sanitize_filename(subject or "无标题邮件", max_len=48)
    available = max(0, 150 - len(str(base.resolve())) - len(package_id) - 2)
    slug = slug[:available].rstrip(" ._") if available else ""
    return base / (f"{package_id}_{slug}" if slug else package_id)


def _default_mailbox_ref(backend: str) -> str:
    return "gmail:me/inbox" if backend == "gmail_api" else "imap:INBOX"


def _resource_id(package_id: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join((package_id, *parts)).encode("utf-8")).hexdigest()
    return f"res_{digest[:28]}"


def _unique_resource_name(
    original_name: str, used: dict[str, int], *, max_len: int = 80
) -> str:
    raw_name = (original_name or "未命名附件").replace("\\", "/").rsplit("/", 1)[-1]
    stem, extension = split_ext(raw_name)
    safe_extension = (
        "." + re.sub(r"[^A-Za-z0-9_-]", "", extension.lstrip("."))[:15].casefold()
        if extension else ""
    )
    stem_limit = max(1, max_len - len(safe_extension) - 4)
    safe_stem = sanitize_filename(stem or "未命名附件", max_len=stem_limit)
    base = f"{safe_stem}{safe_extension}"
    key = base.casefold()
    used[key] = used.get(key, 0) + 1
    if used[key] == 1:
        return base
    suffix = f"__{used[key]}"
    duplicate_stem = safe_stem[: max(1, max_len - len(safe_extension) - len(suffix))]
    return f"{duplicate_stem}{suffix}{safe_extension}"


def _text_resource(
    package_id: str,
    resource_type: str,
    relative_path: str,
    content: str,
    mime_type: str,
    sort_order: int,
) -> dict[str, Any]:
    data = content.encode("utf-8")
    display = {
        "body_plain": "纯文本正文",
        "body_html": "HTML 正文",
        "body_readable": "可读正文",
    }[resource_type]
    return {
        "resource_id": _resource_id(package_id, resource_type),
        "resource_type": resource_type,
        "source_type": "mime_body" if resource_type != "body_readable" else "derived_readable",
        "display_name": display,
        "original_name": None,
        "mime_type": mime_type,
        "local_path": relative_path,
        "original_url": None,
        "content_id": None,
        "size_bytes": len(data),
        "sha256": sha256_of_bytes(data),
        "status": "saved",
        "error": None,
        "sort_order": sort_order,
    }


def _readable_document(mail: NormalizedMail, message_id: str) -> str:
    lines = [
        "---",
        f"source: {mail.backend}",
        f'gmail_message_id: "{mail.backend_message_id}"',
        f'gmail_thread_id: "{mail.thread_id}"',
        f'message_id: "{message_id}"',
        f'from: "{mail.from_raw}"',
        f'to: "{mail.to_raw}"',
        f'cc: "{mail.cc_raw}"',
        f'subject: "{mail.subject}"',
        f'received_at: "{mail.received_at}"',
        "---", "", mail.body_text.strip() or "(本邮件无正文)", "",
    ]
    return "\n".join(lines)


def _compatibility_files(
    package_root: Path, resources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for resource in resources:
        resource_type = resource["resource_type"]
        if resource_type not in {"body_readable", "attachment"} or not resource.get("local_path"):
            continue
        path = package_root / str(resource["local_path"])
        result.append({
            "resource_id": resource["resource_id"],
            "file_type": "body" if resource_type == "body_readable" else "attachment",
            "original_filename": resource.get("original_name") or resource["display_name"],
            "saved_filename": path.name,
            "saved_path": str(path),
            "sha256": resource.get("sha256"),
            "size_bytes": resource.get("size_bytes"),
            "mime_type": resource.get("mime_type"),
            "status": resource.get("status") or "normal",
        })
    return result


def _manifest(
    package: dict[str, Any],
    resources: list[dict[str, Any]],
    errors: list[str],
    *,
    legacy: bool = False,
) -> dict[str, Any]:
    manifest_resources = []
    for resource in resources:
        manifest_resources.append({
            "resource_id": resource["resource_id"],
            "user_category": _user_category(resource["resource_type"]),
            "internal_type": resource["resource_type"],
            "source": resource["source_type"],
            "display_name": resource["display_name"],
            "original_name": resource.get("original_name"),
            "path": resource.get("local_path"),
            "url": resource.get("original_url"),
            "content_id": resource.get("content_id"),
            "mime_type": resource.get("mime_type"),
            "size_bytes": resource.get("size_bytes"),
            "sha256": resource.get("sha256"),
            "status": resource["status"],
            "error": resource.get("error"),
        })
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "package_id": package["package_id"],
        "account_ref": package["account_ref"],
        "mailbox_ref": package["mailbox_ref"],
        "message_id": package["message_id"],
        "thread_ref": package.get("thread_ref"),
        "metadata": {
            "backend": package["backend"],
            "provider_message_id": package.get("provider_message_id"),
            "subject": package.get("subject", ""),
            "from": package.get("from_email", ""),
            "to": package.get("to_emails", ""),
            "cc": package.get("cc_emails", ""),
            "bcc": package.get("bcc_emails", ""),
            "sent_at": package.get("sent_at"),
            "received_at": package.get("received_at"),
            "saved_at": package.get("saved_at"),
        },
        "raw_eml": {
            "status": package["raw_eml_status"],
            "path": package.get("raw_eml_path"),
            "sha256": package.get("raw_eml_sha256"),
            "note": "原始邮件未保存于旧版本" if legacy else None,
        },
        "body": {
            "plain_path": package.get("body_plain_path"),
            "html_path": package.get("body_html_path"),
            "readable_path": package.get("body_readable_path"),
            "text_sha256": package.get("body_text_sha256"),
        },
        "resources": manifest_resources,
        "counts": {
            "resources": package["resource_count"],
            "attachments": package["attachment_count"],
            "inline_images": package["inline_image_count"],
            "links": package["link_count"],
            "downloads": package["downloaded_count"],
        },
        "parse_status": package["parse_status"],
        "archive_status": package["archive_status"],
        "errors": errors,
        "created_at": package.get("saved_at"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _user_category(resource_type: str) -> str:
    if resource_type.startswith("body_"):
        return "邮件内容"
    if resource_type == "inline_image":
        return "邮件中的图片"
    if resource_type == "attachment":
        return "附件"
    return "链接与下载"


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _write_atomic(root / "manifest.json", data)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.stat().st_size == len(data) and sha256_of_file(path) == sha256_of_bytes(data):
                return
        except OSError:
            pass
    temporary = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _lock_for(package_id: str) -> threading.Lock:
    with _locks_guard:
        return _package_locks.setdefault(package_id, threading.Lock())
