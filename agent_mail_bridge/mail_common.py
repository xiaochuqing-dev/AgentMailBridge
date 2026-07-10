"""IMAP 与 Gmail API 共用的邮件规则。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email.utils import getaddresses

from agent_mail_bridge.security import is_attachment_allowed, is_dangerous


@dataclass
class AttachmentData:
    """尚未落盘的附件。"""

    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"
    security_status: str = "unknown_type"


@dataclass
class NormalizedMail:
    """后端读取后交给统一处理流程的邮件。"""

    backend: str
    message_id: str
    backend_message_id: str
    thread_id: str
    uid: str
    from_raw: str
    to_raw: str
    cc_raw: str
    subject: str
    received_at: str
    saved_date: str
    body_text: str
    attachments: list[AttachmentData] = field(default_factory=list)


def parse_mailboxes(*header_values: str | None) -> list[str]:
    """解析显示名、多收件人和异常空值，统一返回小写地址。"""
    values = [value for value in header_values if value]
    result: list[str] = []
    for _display_name, address in getaddresses(values):
        normalized = address.strip().lower()
        if normalized and "@" in normalized and normalized not in result:
            result.append(normalized)
    return result


def canonical_gmail_address(address: str) -> str:
    """归一化 Gmail 大小写、加号地址、点号和 googlemail 别名。"""
    value = address.strip().lower()
    if "@" not in value:
        return value
    local, domain = value.rsplit("@", 1)
    if domain not in {"gmail.com", "googlemail.com"}:
        return value
    local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@gmail.com"


def is_trusted_self_mail(
    owner_address: str,
    from_header: str | None,
    to_header: str | None,
    cc_header: str | None = None,
) -> bool:
    """判断邮件是否由绑定 Gmail 发出并投递给同一 Gmail。"""
    owner = canonical_gmail_address(owner_address)
    senders = {canonical_gmail_address(item) for item in parse_mailboxes(from_header)}
    recipients = {
        canonical_gmail_address(item)
        for item in parse_mailboxes(to_header, cc_header)
    }
    return owner in senders and owner in recipients


def normalize_message_id(message_id: str | None) -> str:
    """统一 Message-ID 的空格、尖括号和大小写。"""
    value = re.sub(r"\s+", "", message_id or "").strip("<>").lower()
    return f"<{value}>" if value else ""


def fallback_dedup_key(
    *,
    from_header: str,
    to_header: str,
    cc_header: str,
    subject: str,
    received_at: str,
    body_text: str,
    attachments: list[AttachmentData],
) -> str:
    """缺少 RFC Message-ID 时按稳定语义生成跨后端去重键。"""
    attachment_parts = sorted(
        f"{item.filename.lower()}:{hashlib.sha256(item.content).hexdigest()}"
        for item in attachments
    )
    fields = [
        ",".join(sorted(parse_mailboxes(from_header))),
        ",".join(sorted(parse_mailboxes(to_header, cc_header))),
        subject.strip(),
        received_at.strip()[:10],
        body_text.replace("\r\n", "\n").strip(),
        "|".join(attachment_parts),
    ]
    digest = hashlib.sha256("\n".join(fields).encode("utf-8")).hexdigest()
    return f"<generated-{digest[:32]}@agent-mail-bridge.local>"


def attachment_security_status(filename: str) -> str:
    """给附件标记安全状态，不执行也不自动打开。"""
    if is_dangerous(filename):
        return "dangerous"
    if is_attachment_allowed(filename):
        return "allowed"
    return "unknown_type"
