"""Gmail API 收件模块。

职责：
1. 通过 Gmail API（HTTPS 443）读取邮件，绕过 IMAP 993 端口限制。
2. 去重：优先 RFC Message-ID（兼容 IMAP 逻辑），否则用 gmail_api:<id>。
3. 只收自发自收邮件（from==用户Gmail 且 to 含用户Gmail）。
4. 提取正文（优先 text/plain，其次 HTML 清洗）与附件。
5. 保存正文/附件到本地日期目录，写入 SQLite。
6. 返回与 IMAP 后端一致的 result dict，供 mail_receive 协调入口统一处理。

安全要求：只读 scope，不删邮件、不标记已读、不发送。
不打印 token / credentials 内容。
"""

from __future__ import annotations

import base64
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    insert_received_file,
    insert_received_message,
    log_event,
    message_id_exists,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.security import (
    check_size_ok,
    is_dangerous,
)
from agent_mail_bridge.storage import (
    build_attachment_path,
    build_body_path,
    write_bytes,
    write_text,
)
from agent_mail_bridge.utils import (
    decode_mime_header,
    fmt_datetime,
    now_local,
    sanitize_filename,
    sha256_of_bytes,
    sha256_of_file,
)

logger = get_logger("gmail_api_receive")


# ============================================================
# 公开入口
# ============================================================

def receive_gmail_api_messages(
    cfg: AppConfig,
    service: Any | None = None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """通过 Gmail API 收取符合条件的邮件。

    Args:
        cfg: 应用配置。
        service: 已授权的 Gmail API service；None 时自动创建。
        limit: 单次最多抓取数量，None 则使用 cfg.gmail_api_max_results。

    Returns:
        与 IMAP 后端一致的统计 dict：
            {
              "ok": True,
              "fetched": int,        list 返回的邮件数
              "saved": int,          实际保存的新邮件数
              "skipped": int,        已记录过被跳过的邮件数
              "attachments": int,    保存的附件数
              "errors": list[str],   单封邮件的错误信息
            }
    """
    from agent_mail_bridge.config import require_receive_config
    require_receive_config(cfg)

    if limit is None:
        limit = cfg.gmail_api_max_results

    result: dict[str, Any] = {
        "ok": True,
        "fetched": 0,
        "saved": 0,
        "skipped": 0,
        "attachments": 0,
        "errors": [],
    }

    gmail_addr = cfg.gmail_address.lower().strip()

    log_event(
        cfg.db_path, "INFO", "receive",
        f"开始通过 Gmail API 收取邮件（limit={limit}, query={cfg.gmail_api_query}）",
    )

    # ---- 获取 service ----
    if service is None:
        try:
            service = _build_service(cfg)
        except Exception as exc:  # noqa: BLE001
            msg = _describe_auth_error(exc)
            logger.error(msg)
            log_event(cfg.db_path, "ERROR", "receive", msg)
            result["ok"] = False
            result["errors"].append(msg)
            return result

    # ---- list 邮件 id ----
    try:
        list_resp = service.users().messages().list(
            userId="me",
            q=cfg.gmail_api_query,
            maxResults=limit,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        msg = _describe_api_error(exc)
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "receive", msg)
        result["ok"] = False
        result["errors"].append(msg)
        return result

    messages = list_resp.get("messages", []) or []
    result["fetched"] = len(messages)

    for item in messages:
        gmail_message_id = item.get("id", "")
        gmail_thread_id = item.get("threadId", "")
        try:
            _process_one(
                service, cfg, gmail_addr,
                gmail_message_id, gmail_thread_id, result,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"处理 Gmail API 邮件 id={gmail_message_id} 失败：{exc}"
            logger.exception("处理 Gmail API 邮件失败")
            logger.error(err)
            log_event(cfg.db_path, "ERROR", "receive", err)
            result["errors"].append(err)

    log_event(
        cfg.db_path,
        "SUCCESS",
        "receive",
        f"Gmail API 收取完成：扫描 {result['fetched']} 封，"
        f"新存 {result['saved']} 封，跳过 {result['skipped']} 封，"
        f"附件 {result['attachments']} 个",
    )
    return result


# ============================================================
# 内部：service 创建
# ============================================================

def _build_service(cfg: AppConfig) -> Any:
    """委托 gmail_api_auth 创建 service。"""
    from agent_mail_bridge.gmail_api_auth import get_gmail_api_service
    return get_gmail_api_service(cfg)


# ============================================================
# 内部：单封邮件处理
# ============================================================

def _process_one(
    service: Any,
    cfg: AppConfig,
    gmail_addr: str,
    gmail_message_id: str,
    gmail_thread_id: str,
    result: dict[str, Any],
) -> None:
    """抓取并处理单封 Gmail API 邮件。"""
    msg = service.users().messages().get(
        userId="me",
        id=gmail_message_id,
        format="full",
    ).execute()

    payload = msg.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])

    # ---- 去重 key：优先 RFC Message-ID，否则 gmail_api:<id> ----
    rfc_message_id = _find_header(
        headers, ("Message-ID", "Message-Id", "Message-id")
    ).strip()
    dedup_key = rfc_message_id if rfc_message_id else f"gmail_api:{gmail_message_id}"

    if message_id_exists(cfg.db_path, dedup_key):
        result["skipped"] += 1
        logger.debug("跳过已记录邮件：%s", dedup_key)
        return

    # ---- 解析发件 / 收件 / 主题 ----
    from_raw = _find_header(headers, ("From",))
    to_raw = _find_header(headers, ("To",))
    subject = decode_mime_header(_find_header(headers, ("Subject",)))

    from_email = parseaddr(from_raw)[1].lower().strip()
    to_emails = [parseaddr(x)[1].lower().strip() for x in _split_addresses(to_raw)]

    # ---- 自发自收过滤 ----
    if cfg.auto_receive_only_self_mail:
        if from_email != gmail_addr:
            logger.debug("跳过非自发自收邮件（from 不匹配）：%s", from_email)
            return
        if gmail_addr not in to_emails:
            logger.debug("跳过非自发自收邮件（to 不含本人）：%s", to_raw)
            return

    # ---- 时间 ----
    received_dt = _parse_internal_date(msg.get("internalDate"))
    received_at = fmt_datetime(received_dt)

    # ---- 正文与附件 ----
    body_text, attachments = _extract_payload(payload, cfg, service,
                                              gmail_message_id, received_dt)

    # ---- 保存正文 ----
    body_path = build_body_path(cfg, subject, received_dt)
    body_md = _compose_body_md(
        message_id=dedup_key,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        from_email=from_email,
        to_raw=to_raw,
        subject=subject,
        received_at=received_at,
        body_text=body_text,
        attachments=attachments,
    )
    write_text(body_path, body_md)
    body_sha = sha256_of_file(body_path)

    saved_date = received_dt.strftime("%Y-%m-%d")

    # ---- 写入收件主记录 ----
    insert_received_message(
        cfg.db_path,
        message_id=dedup_key,
        gmail_uid=None,
        subject=subject,
        from_email=from_email,
        to_email=to_raw,
        received_at=received_at,
        saved_date=saved_date,
        body_file_path=str(body_path),
        body_sha256=body_sha,
        has_attachments=len(attachments) > 0,
        status="saved",
        source="gmail_api",
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        backend="gmail_api",
    )

    # ---- 写入正文文件记录 ----
    insert_received_file(
        cfg.db_path,
        message_id=dedup_key,
        file_type="body",
        original_filename=subject or "无标题邮件",
        saved_filename=body_path.name,
        saved_path=str(body_path),
        sha256=body_sha,
        size_bytes=body_path.stat().st_size,
        mime_type="text/markdown",
        saved_date=saved_date,
        status="normal",
    )

    # ---- 写入附件文件记录 ----
    for att in attachments:
        insert_received_file(
            cfg.db_path,
            message_id=dedup_key,
            file_type="attachment",
            original_filename=att["original_filename"],
            saved_filename=att["saved_filename"],
            saved_path=str(att["saved_path"]),
            sha256=att["sha256"],
            size_bytes=att["size_bytes"],
            mime_type=att["mime_type"],
            saved_date=saved_date,
            status="normal",
        )
        result["attachments"] += 1

    result["saved"] += 1
    log_event(
        cfg.db_path,
        "SUCCESS",
        "receive",
        f"已保存邮件（Gmail API）：{subject or '(无标题)'} | 附件 {len(attachments)} 个",
    )
    logger.info("已保存邮件（Gmail API）：%s（附件 %d 个）", subject, len(attachments))


# ============================================================
# 内部：payload 解析（正文 + 附件）
# ============================================================

def _extract_payload(
    payload: dict[str, Any],
    cfg: AppConfig,
    service: Any,
    gmail_message_id: str,
    dt,
) -> tuple[str, list[dict[str, Any]]]:
    """递归遍历 payload parts，提取正文文本与附件。

    Returns:
        (body_text, attachments)
    """
    body_text = ""
    html_text = ""
    attachments: list[dict[str, Any]] = []

    # 用列表包装可变状态，避免闭包内 nonlocal 顺序问题
    state = {"body": "", "html": ""}

    def walk(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body") or {}
        parts = part.get("parts") or []

        # 附件：有文件名且有 attachmentId
        if filename and body.get("attachmentId"):
            _handle_api_attachment(
                service, cfg, dt, gmail_message_id, filename,
                body, mime_type, attachments,
            )
            return

        # 纯文本正文（只取第一个 text/plain）
        if mime_type == "text/plain" and body.get("data") is not None:
            text = _decode_base64url_text(body["data"])
            if text and not state["body"]:
                state["body"] = text
            return

        # HTML 正文（只取第一个 text/html）
        if mime_type == "text/html" and body.get("data") is not None:
            html = _decode_base64url_text(body["data"])
            if html and not state["html"]:
                state["html"] = html
            return

        # 递归子 parts
        for sub in parts:
            walk(sub)

    walk(payload)

    body_text = state["body"]
    html_text = state["html"]

    # 正文优先 text/plain，否则清洗 HTML
    if not body_text and html_text:
        body_text = _html_to_text(html_text)
    if not body_text and not attachments:
        body_text = "(本邮件无正文)"

    return body_text, attachments


def _handle_api_attachment(
    service: Any,
    cfg: AppConfig,
    dt,
    gmail_message_id: str,
    filename: str,
    body: dict[str, Any],
    mime_type: str,
    attachments: list[dict[str, Any]],
) -> None:
    """下载并保存单个 Gmail API 附件。"""
    attachment_id = body.get("attachmentId")
    if not attachment_id:
        return

    original_filename = decode_mime_header(filename) if filename else "未命名附件"
    original_filename = sanitize_filename(original_filename, max_len=120) or "未命名附件"

    try:
        att_resp = service.users().messages().attachments().get(
            userId="me",
            messageId=gmail_message_id,
            id=attachment_id,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载附件失败：%s（%s）", original_filename, exc)
        log_event(
            cfg.db_path, "WARNING", "receive",
            f"附件下载失败：{original_filename}（{exc}）",
        )
        return

    data_b64 = att_resp.get("data")
    if not data_b64:
        return
    payload_bytes = decode_base64url(data_b64)
    size_bytes = len(payload_bytes)

    if is_dangerous(original_filename):
        logger.warning("附件含危险扩展名，仅保存不执行：%s", original_filename)
        log_event(
            cfg.db_path, "WARNING", "receive",
            f"危险扩展名附件：{original_filename}（仅保存，不执行）",
        )

    if not check_size_ok(size_bytes, cfg.max_attachment_bytes):
        logger.warning(
            "附件超过大小限制(%dMB)，跳过：%s (%.2fMB)",
            cfg.max_attachment_mb, original_filename, size_bytes / 1024 / 1024,
        )
        log_event(
            cfg.db_path, "WARNING", "receive",
            f"附件超限跳过：{original_filename} "
            f"({size_bytes/1024/1024:.2f}MB > {cfg.max_attachment_mb}MB)",
        )
        return

    saved_path = build_attachment_path(cfg, original_filename, dt)
    write_bytes(saved_path, payload_bytes)
    sha = sha256_of_bytes(payload_bytes)

    attachments.append({
        "original_filename": original_filename,
        "saved_filename": saved_path.name,
        "saved_path": str(saved_path),
        "sha256": sha,
        "size_bytes": size_bytes,
        "mime_type": mime_type or "application/octet-stream",
        "skipped": False,
    })
    logger.info("已保存附件：%s", saved_path.name)


# ============================================================
# 内部：base64url / header / 时间
# ============================================================

def decode_base64url(data: str) -> bytes:
    """解码 Gmail API 的 base64url 编码（自动补 padding）。"""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _decode_base64url_text(data: str) -> str:
    """解码 base64url 并尝试按常见编码转为文本。"""
    raw = decode_base64url(data)
    for cs in ("utf-8", "gbk", "gb2312", "big5", "latin-1"):
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _headers_to_dict(headers: list[dict[str, str]]) -> dict[str, str]:
    """把 Gmail API headers 列表转为 dict（key 小写）。"""
    return {h.get("name", "").lower(): h.get("value", "")
            for h in headers}


def _find_header(headers: dict[str, str], names: tuple[str, ...]) -> str:
    """不区分大小写查找 header 值。headers 的 key 已小写化。"""
    for name in names:
        key = name.lower()
        if key in headers:
            return headers[key]
    return ""


def _split_addresses(header_value: str) -> list[str]:
    """把 To/Cc 头按逗号拆分为多个地址段。"""
    if not header_value:
        return []
    return [x.strip() for x in header_value.split(",") if x.strip()]


def _parse_internal_date(internal_date) -> "Any":
    """把 Gmail API 的 internalDate（毫秒时间戳）转为本地 datetime。"""
    from datetime import datetime
    try:
        ts_ms = int(internal_date)
    except (TypeError, ValueError):
        return now_local()
    return datetime.fromtimestamp(ts_ms / 1000.0)


# ============================================================
# 内部：HTML 清洗（与 IMAP 后端逻辑一致）
# ============================================================

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


def _html_to_text(html: str) -> str:
    """简易 HTML 清洗为可读文本（与 IMAP 后端保持一致）。"""
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = _TAG_RE.sub("", text)
    for ent, ch in _HTML_ENTITIES.items():
        text = text.replace(ent, ch)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


# ============================================================
# 内部：正文 Markdown 组装（兼容 IMAP frontmatter，新增 Gmail API 字段）
# ============================================================

def _compose_body_md(
    *,
    message_id: str,
    gmail_message_id: str,
    gmail_thread_id: str,
    from_email: str,
    to_raw: str,
    subject: str,
    received_at: str,
    body_text: str,
    attachments: list[dict[str, Any]],
) -> str:
    """组装正文 Markdown，顶部附加元信息（兼容现有 IMAP 格式 + Gmail API 字段）。"""
    saved_at = fmt_datetime(now_local())
    meta_lines = [
        "---",
        "source: gmail_api",
        f'gmail_message_id: "{gmail_message_id}"',
        f'gmail_thread_id: "{gmail_thread_id}"',
        f'message_id: "{message_id}"',
        f'from: "{from_email}"',
        f'to: "{to_raw}"',
        f'subject: "{subject}"',
        f'received_at: "{received_at}"',
        f'saved_at: "{saved_at}"',
        "---",
        "",
    ]

    body = body_text
    if not body.strip() and attachments:
        body = "(本邮件无正文，仅含附件。附件列表如下：)\n\n"
        for att in attachments:
            body += f"- {att['original_filename']} ({att['size_bytes']} bytes, {att['mime_type']})\n"

    if attachments:
        body += "\n\n---\n附件：\n"
        for att in attachments:
            body += f"- {att['saved_filename']}\n"

    return "\n".join(meta_lines) + body + "\n"


# ============================================================
# 内部：错误文案
# ============================================================

def _describe_auth_error(exc: Exception) -> str:
    """授权类错误的可读文案（不输出 token 内容）。"""
    from agent_mail_bridge.gmail_api_auth import (
        CredentialsNotFoundError,
        TokenScopeMismatchError,
    )
    if isinstance(exc, CredentialsNotFoundError):
        return (
            "找不到 Gmail API credentials.json。\n"
            "请确认已从 Google Cloud Console 下载 OAuth Desktop Client JSON，\n"
            "并放到 GMAIL_API_CREDENTIALS_PATH 指定位置。"
        )
    if isinstance(exc, TokenScopeMismatchError):
        return (
            "Gmail API token 无效或权限不匹配。\n"
            "请删除 token.json 后重新运行：\n"
            "python -m agent_mail_bridge gmail-api-auth"
        )
    return (
        f"Gmail API 授权失败：{exc}\n"
        "可运行 `python -m agent_mail_bridge gmail-api-auth` 重新授权。"
    )


def _describe_api_error(exc: Exception) -> str:
    """API 调用类错误的可读文案。"""
    low = str(exc).lower()
    if "access" in low and "denied" in low or "forbidden" in low:
        return (
            "Gmail API 权限不足。\n"
            "当前推荐 scope:\n"
            "https://www.googleapis.com/auth/gmail.readonly\n"
            "如果你修改过 scopes，请删除 token.json 后重新授权。"
        )
    if "not found" in low and "api" in low:
        return (
            "Gmail API 调用失败，可能是 Google Cloud 项目未启用 Gmail API。\n"
            "请到 Google Cloud Console 启用 Gmail API。"
        )
    if "invalid_grant" in low:
        return (
            "Gmail API 授权已失效（token 被撤销或过期无法刷新）。\n"
            "请删除 token.json 后重新运行：\n"
            "python -m agent_mail_bridge gmail-api-auth"
        )
    return f"Gmail API 调用失败：{exc}"
