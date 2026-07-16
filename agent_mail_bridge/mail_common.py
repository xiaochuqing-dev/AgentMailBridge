"""IMAP 与 Gmail API 共用的邮件规则。"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from html.parser import HTMLParser
from email.utils import getaddresses

from agent_mail_bridge.security import is_attachment_allowed, is_dangerous
from agent_mail_bridge.utils import decode_mime_header


@dataclass
class AttachmentData:
    """尚未落盘的附件。"""

    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"
    security_status: str = "unknown_type"
    content_id: str = ""
    disposition: str = "attachment"
    is_inline: bool = False
    part_id: str = ""
    error: str = ""


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
    raw_bytes: bytes = b""
    body_plain: str = ""
    body_html: str = ""
    bcc_raw: str = ""
    sent_at: str = ""
    references_raw: str = ""
    in_reply_to_raw: str = ""
    mailbox_ref: str = ""


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


def normalized_mail_from_raw(
    raw_bytes: bytes,
    *,
    backend: str,
    backend_message_id: str,
    thread_id: str,
    uid: str,
    received_at: str,
    saved_date: str,
    max_attachment_bytes: int,
    mailbox_ref: str,
) -> NormalizedMail:
    """把真实 RFC822 bytes 归一化；两个收件后端共用此入口。"""
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise ValueError("邮件原文为空")
    message = message_from_bytes(raw_bytes)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentData] = []
    leaf_index = 0
    leaves = message.walk() if message.is_multipart() else [message]
    for part in leaves:
        if part.is_multipart():
            continue
        leaf_index += 1
        content_type = part.get_content_type() or "application/octet-stream"
        disposition = (part.get_content_disposition() or "").lower()
        raw_filename = part.get_filename()
        original_filename = decode_mime_header(raw_filename) if raw_filename else ""
        content_id = str(part.get("Content-ID", "")).strip().strip("<>")
        payload = part.get_payload(decode=True)
        payload_bytes = payload if isinstance(payload, bytes) else b""
        is_inline = bool(
            content_type.startswith("image/")
            and (disposition == "inline" or bool(content_id))
        )
        is_file = bool(original_filename) or disposition == "attachment" or is_inline
        if is_file:
            display_name = original_filename
            if not display_name:
                extension = mimetypes.guess_extension(content_type) or ".bin"
                display_name = f"inline-{leaf_index}{extension}"
            error = ""
            status = attachment_security_status(display_name)
            if len(payload_bytes) > max_attachment_bytes:
                error = f"资源超过大小限制：{len(payload_bytes)} bytes"
                status = "failed"
            attachments.append(
                AttachmentData(
                    filename=display_name,
                    content=b"" if error else payload_bytes,
                    mime_type=content_type,
                    security_status=status,
                    content_id=content_id,
                    disposition=disposition or ("inline" if is_inline else "attachment"),
                    is_inline=is_inline,
                    part_id=str(leaf_index),
                    error=error,
                )
            )
            continue
        text = _decode_message_text(part)
        if content_type == "text/plain" and text:
            plain_parts.append(text)
        elif content_type == "text/html" and text:
            html_parts.append(text)

    body_plain = "\n\n".join(plain_parts).strip()
    body_html = "\n\n".join(html_parts).strip()
    readable = body_plain or safe_html_to_text(body_html)
    return NormalizedMail(
        backend=backend,
        message_id=str(message.get("Message-ID", "")),
        backend_message_id=backend_message_id,
        thread_id=thread_id,
        uid=uid,
        from_raw=", ".join(message.get_all("From", [])),
        to_raw=", ".join(message.get_all("To", [])),
        cc_raw=", ".join(message.get_all("Cc", [])),
        subject=decode_mime_header(message.get("Subject", "")),
        received_at=received_at,
        saved_date=saved_date,
        body_text=readable,
        attachments=attachments,
        raw_bytes=raw_bytes,
        body_plain=body_plain,
        body_html=body_html,
        bcc_raw=", ".join(message.get_all("Bcc", [])),
        sent_at=received_at,
        references_raw=str(message.get("References", "")),
        in_reply_to_raw=str(message.get("In-Reply-To", "")),
        mailbox_ref=mailbox_ref,
    )


def derive_thread_ref(mail: NormalizedMail, message_id: str) -> str:
    """优先使用 provider thread；IMAP 仅按明确 RFC 引用建立会话。"""
    if mail.thread_id:
        return f"gmail:{mail.thread_id}"
    references = re.findall(r"<[^<>]+>", mail.references_raw or "")
    if references:
        root = normalize_message_id(references[0])
        if root:
            return f"rfc:{root}"
    in_reply_to = normalize_message_id(mail.in_reply_to_raw)
    if in_reply_to:
        return f"rfc:{in_reply_to}"
    return f"rfc:{message_id}" if message_id else ""


def safe_html_to_text(html: str) -> str:
    """离线生成可读正文，忽略脚本、样式和远程资源。"""
    if not html:
        return ""
    parser = _ReadableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, TypeError):
        return ""
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode_message_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        value = part.get_payload()
        return value if isinstance(value, str) else ""
    return decode_text_bytes(payload, declared_charset=part.get_content_charset())


def decode_text_bytes(payload: bytes, *, declared_charset: str | None = None) -> str:
    """在声明缺失或错误时，从常见中文编码中选择最可信的严格解码结果。"""
    if not payload:
        return ""
    declared = str(declared_charset or "").strip().lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8", "gb2312": "gb18030", "gbk": "gb18030",
        "cp936": "gb18030", "big-5": "big5", "cp950": "big5",
    }
    declared = aliases.get(declared, declared)
    trusted_declared = declared if declared in {
        "utf-8", "utf-16", "utf-16le", "utf-16be", "gb18030", "big5"
    } else ""
    candidates: list[str] = []
    for charset in (
        trusted_declared,
        "utf-8-sig" if payload.startswith(b"\xef\xbb\xbf") else "utf-8",
        "gb18030",
        "big5",
        declared,
        "latin-1",
    ):
        if charset and charset not in candidates:
            candidates.append(charset)

    decoded: list[tuple[float, int, str]] = []
    for index, charset in enumerate(candidates):
        try:
            value = payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
        score = _decoded_text_score(value) - index * 0.01
        if charset in {"utf-8", "utf-8-sig"}:
            # 严格 UTF-8 成功是强信号，可覆盖错误的单字节 charset 声明。
            score += 50.0
        if trusted_declared and charset == trusted_declared:
            score += 2.0
        decoded.append((score, -index, value))
    if decoded:
        return max(decoded, key=lambda item: (item[0], item[1]))[2]
    return payload.decode("utf-8", errors="replace")


def _decoded_text_score(value: str) -> float:
    cjk = sum(
        1 for char in value
        if "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
    )
    controls = sum(1 for char in value if ord(char) < 32 and char not in "\r\n\t")
    c1_controls = sum(1 for char in value if 0x7F <= ord(char) <= 0x9F)
    replacement = value.count("\ufffd")
    mojibake = sum(value.count(marker) for marker in ("Ã", "Â", "ä", "å", "æ", "ç", "é"))
    printable = sum(1 for char in value if char.isprintable() or char in "\r\n\t")
    return (
        cjk * 3.0
        + printable / max(1, len(value))
        - controls * 8.0
        - c1_controls * 8.0
        - replacement * 20.0
        - mojibake * 1.5
    )


class _ReadableHTMLParser(HTMLParser):
    _BLOCKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
    _IGNORED = {"script", "style", "head", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.casefold()
        if lowered in self._IGNORED:
            self._ignored_depth += 1
        elif not self._ignored_depth and lowered in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and lowered in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)
