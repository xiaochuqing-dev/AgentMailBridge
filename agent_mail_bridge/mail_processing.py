"""统一的邮件校验、去重、保存和数据库登记流程。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import store_received_message_atomically
from agent_mail_bridge.mail_common import (
    NormalizedMail,
    fallback_dedup_key,
    normalize_message_id,
)
from agent_mail_bridge.receive_rules import match_receive_rule
from agent_mail_bridge.security import assert_within_root
from agent_mail_bridge.utils import sanitize_filename, sha256_of_bytes, split_ext


def process_normalized_mail(cfg: AppConfig, mail: NormalizedMail) -> dict[str, Any]:
    """处理两个后端共同的业务规则并返回结构化单封结果。"""
    matched, reason = match_receive_rule(cfg, mail)
    if not matched:
        return {"status": "skipped", "reason": reason, "saved_files": []}

    message_id = normalize_message_id(mail.message_id)
    if not message_id:
        message_id = fallback_dedup_key(
            from_header=mail.from_raw,
            to_header=mail.to_raw,
            cc_header=mail.cc_raw,
            subject=mail.subject,
            received_at=mail.received_at,
            body_text=mail.body_text,
            attachments=mail.attachments,
        )

    message = {
        "message_id": message_id,
        "gmail_uid": mail.uid or None,
        "subject": mail.subject,
        "from_email": mail.from_raw,
        "to_email": ", ".join(filter(None, [mail.to_raw, mail.cc_raw])),
        "received_at": mail.received_at,
        "saved_date": mail.saved_date,
        "has_attachments": bool(mail.attachments),
        "source": mail.backend,
        "gmail_message_id": mail.backend_message_id or None,
        "gmail_thread_id": mail.thread_id or None,
        "backend": mail.backend,
    }

    def write_files() -> list[dict[str, Any]]:
        return _write_deterministic_files(cfg, mail, message_id)

    inserted, files = store_received_message_atomically(cfg.db_path, message, write_files)
    if not inserted:
        return {"status": "duplicate", "message_id": message_id, "saved_files": []}
    return {
        "status": "saved",
        "message_id": message_id,
        "saved_files": [item["saved_path"] for item in files],
        "attachments": sum(item["file_type"] == "attachment" for item in files),
    }


def _write_deterministic_files(
    cfg: AppConfig, mail: NormalizedMail, message_id: str
) -> list[dict[str, Any]]:
    """按去重键生成稳定路径，失败重试不会产生第二套文件。"""
    time_prefix = _time_prefix(mail.received_at)
    identity = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:10]
    day_dir = cfg.received_dir / mail.saved_date
    attachment_dir = day_dir / "attachments"
    day_dir.mkdir(parents=True, exist_ok=True)
    attachment_dir.mkdir(parents=True, exist_ok=True)

    subject = sanitize_filename(mail.subject or "无标题邮件", max_len=60)
    body_path = day_dir / f"{time_prefix}_{subject}_{identity}.md"
    assert_within_root(body_path, cfg.data_root_path)
    body_content = _compose_body(mail, message_id)
    body_bytes = body_content.encode("utf-8")
    body_path.write_bytes(body_bytes)
    files: list[dict[str, Any]] = [{
        "file_type": "body",
        "original_filename": mail.subject or "无标题邮件",
        "saved_filename": body_path.name,
        "saved_path": str(body_path),
        "sha256": sha256_of_bytes(body_bytes),
        "size_bytes": len(body_bytes),
        "mime_type": "text/markdown",
        "status": "normal",
    }]

    for index, attachment in enumerate(mail.attachments, 1):
        safe_name = sanitize_filename(attachment.filename or "未命名附件", max_len=80)
        stem, ext = split_ext(safe_name)
        content_hash = sha256_of_bytes(attachment.content)
        filename = (
            f"{time_prefix}_{sanitize_filename(stem, max_len=50)}_"
            f"{index}_{content_hash[:8]}{ext}"
        )
        path = attachment_dir / filename
        assert_within_root(path, cfg.data_root_path)
        path.write_bytes(attachment.content)
        files.append({
            "file_type": "attachment",
            "original_filename": safe_name,
            "saved_filename": filename,
            "saved_path": str(path),
            "sha256": content_hash,
            "size_bytes": len(attachment.content),
            "mime_type": attachment.mime_type,
            "status": attachment.security_status,
        })
    return files


def _time_prefix(received_at: str) -> str:
    """从标准时间文本提取 HH-MM-SS，异常时使用固定占位。"""
    try:
        return received_at.split(" ", 1)[1].replace(":", "-")[:8]
    except (IndexError, AttributeError):
        return "00-00-00"


def _compose_body(mail: NormalizedMail, message_id: str) -> str:
    """生成两个后端一致的正文文件。"""
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
        "---",
        "",
        mail.body_text.strip() or "(本邮件无正文)",
    ]
    if mail.attachments:
        lines.extend(["", "附件："])
        lines.extend(
            f"{item.filename}，安全状态：{item.security_status}"
            for item in mail.attachments
        )
    return "\n".join(lines) + "\n"
