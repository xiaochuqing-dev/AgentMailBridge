"""收件与发件列表共用的紧凑摘要。"""

from __future__ import annotations

import re


MAIL_LIST_PREVIEW_CHARS = 36
MAIL_LIST_TOOLTIP_CHARS = 600
MAIL_LIST_ROW_HEIGHT = 74

_MARKDOWN_DECORATION_RE = re.compile(
    r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s+|[-*+]\s+|\d+[.)]\s+)"
)
_MARKDOWN_INLINE_RE = re.compile(r"(?<!\\)(?:\*\*|__|~~|`)")
_WHITESPACE_RE = re.compile(r"\s+")


def compact_readable_text(value: str | None) -> str:
    """只清理展示噪音，不修改邮件正文事实。"""
    text = str(value or "").replace("\u200b", "")
    text = _MARKDOWN_DECORATION_RE.sub("", text)
    text = _MARKDOWN_INLINE_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def truncate_preview(value: str | None, limit: int = MAIL_LIST_PREVIEW_CHARS) -> str:
    text = compact_readable_text(value)
    safe_limit = max(1, int(limit))
    if len(text) <= safe_limit:
        return text
    return text[:safe_limit].rstrip() + "…"


def resource_count_parts(
    *,
    attachment_count: int = 0,
    inline_image_count: int = 0,
    link_count: int = 0,
    downloaded_count: int = 0,
) -> list[str]:
    parts: list[str] = []
    if int(attachment_count or 0) > 0:
        parts.append(f"{int(attachment_count)} 个附件")
    if int(inline_image_count or 0) > 0:
        parts.append(f"{int(inline_image_count)} 张邮件图片")
    if int(link_count or 0) > 0:
        parts.append(f"{int(link_count)} 个链接")
    if int(downloaded_count or 0) > 0:
        parts.append(f"{int(downloaded_count)} 个已下载文件")
    return parts


def build_mail_list_summary(
    body: str | None,
    *,
    attachment_count: int = 0,
    inline_image_count: int = 0,
    link_count: int = 0,
    downloaded_count: int = 0,
    archive_status: str = "",
    parse_status: str = "",
    max_chars: int = MAIL_LIST_PREVIEW_CHARS,
) -> str:
    preview = truncate_preview(body, max_chars)
    facts = resource_count_parts(
        attachment_count=attachment_count,
        inline_image_count=inline_image_count,
        link_count=link_count,
        downloaded_count=downloaded_count,
    )
    # 资源事实放在首行，避免长正文在固定行高内把附件/图片/链接裁到不可见。
    parts = [" · ".join(facts)] if facts else []
    if preview:
        parts.append(preview)
    if not parts:
        status = (parse_status or archive_status).strip().lower()
        parts.append("邮件内容待处理" if status in {"partial", "failed", "needs_attention"} else "无正文和资源")
    return "\n".join(parts)


def build_outbound_list_summary(
    body: str | None,
    *,
    attachment_count: int = 0,
    link_count: int = 0,
    source_origin: str = "",
    max_chars: int = MAIL_LIST_PREVIEW_CHARS,
) -> str:
    preview = truncate_preview(body, max_chars)
    facts = resource_count_parts(
        attachment_count=attachment_count,
        link_count=link_count,
    )
    parts = [" · ".join(facts)] if facts else []
    if preview:
        parts.append(preview)
    if not parts:
        parts.append("旧发送记录" if source_origin == "legacy_sent_file" else "无正文和资源")
    return "\n".join(parts)


def build_mail_list_tooltip(
    *,
    subject: str,
    sender: str = "",
    body: str = "",
    attachment_count: int = 0,
    inline_image_count: int = 0,
    link_count: int = 0,
    downloaded_count: int = 0,
) -> str:
    body_preview = truncate_preview(body, MAIL_LIST_TOOLTIP_CHARS)
    facts = " · ".join(resource_count_parts(
        attachment_count=attachment_count,
        inline_image_count=inline_image_count,
        link_count=link_count,
        downloaded_count=downloaded_count,
    )) or "无附件、邮件图片、链接或下载文件"
    lines = [f"主题：{subject or '无主题邮件'}"]
    if sender:
        lines.append(f"发件人：{sender}")
    if body_preview:
        lines.append(f"正文预览：{body_preview}")
    lines.extend((f"资源：{facts}", "双击查看完整邮件。"))
    return "\n".join(lines)
