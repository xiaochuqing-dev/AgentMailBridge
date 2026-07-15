"""Gmail 收件协调模块（支持 IMAP / Gmail API 双后端）。

职责：
1. 根据 GMAIL_RECEIVE_BACKEND 选择收件后端：
   - imap      -> 通过 IMAP 993 收件（需应用专用密码）
   - gmail_api -> 通过 Gmail API over HTTPS 443 收件（需 OAuth）
   - auto      -> 优先 gmail_api（已配置 credentials.json），否则回退 imap
2. 保持 IMAP 后端原有逻辑不变（保留、不破坏）。
3. Gmail API 后端走 HTTPS 443，绕过 IMAP 993 端口限制。
4. 两个后端返回结构一致的统计 dict，供 CLI 统一展示。

注意：日志中绝不打印完整应用专用密码 / token 内容。
"""

from __future__ import annotations

import email
import imaplib
import mimetypes
import re
from datetime import timedelta
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    clear_receive_retry,
    count_receive_retries,
    insert_received_file,
    insert_received_message,
    log_event,
    message_id_exists,
    query_due_receive_retries,
    receive_retry_is_due,
    record_receive_failure,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.mail_common import (
    AttachmentData,
    NormalizedMail,
    attachment_security_status,
    normalized_mail_from_raw,
)
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.security import (
    is_attachment_allowed,
    is_dangerous,
    check_size_ok,
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

logger = get_logger("mail_receive")


class MailArchivePartialError(RuntimeError):
    """邮件事实已保留，但仍有资源需要有限重试。"""

    def __init__(self, message: str, *, message_id: str, package_id: str):
        super().__init__(message)
        self.message_id = message_id
        self.package_id = package_id


# ============================================================
# 公开入口（后端协调）
# ============================================================

def receive_mails(
    cfg: AppConfig,
    *,
    limit: int | None = None,
    unseen_only: bool | None = None,
    mark_seen: bool | None = None,
) -> dict[str, Any]:
    """收取 Gmail 中符合条件的邮件（根据后端配置分发）。

    Args:
        cfg: 应用配置。
        limit: 单次最多抓取数量。
            IMAP: None 用 cfg.max_fetch_limit。
            Gmail API: None 用 cfg.gmail_api_max_results。
        unseen_only: 仅 IMAP 有效（只收未读）。
        mark_seen: 仅 IMAP 有效（收取后标记已读）；Gmail API 只读不标记。

    Returns:
        统计结果 dict（两后端结构一致）：
            {
              "ok": True,
              "fetched": int,        扫描的邮件数
              "saved": int,          实际保存的新邮件数
              "skipped": int,        已记录过被跳过的邮件数
              "attachments": int,    保存的附件数
              "errors": list[str],   单封邮件的错误信息
              "backend": str,        实际使用的后端（imap / gmail_api）
            }
    """
    from agent_mail_bridge.config import _effective_receive_backend
    backend = _effective_receive_backend(cfg)
    logger.info("收件后端：%s（配置=%s）", backend, cfg.gmail_receive_backend)

    if backend == "gmail_api":
        result = receive_gmail_api(cfg, limit=limit)
    else:
        result = _receive_via_imap(
            cfg, limit=limit, unseen_only=unseen_only, mark_seen=mark_seen,
        )
    result["backend"] = backend
    return result


def receive_gmail_api(
    cfg: AppConfig, *, limit: int | None = None
) -> dict[str, Any]:
    """Gmail API 后端收件（委托 gmail_api_receive 模块）。"""
    from agent_mail_bridge.gmail_api_receive import receive_gmail_api_messages
    return receive_gmail_api_messages(cfg, service=None, limit=limit)


# ============================================================
# IMAP 后端（原有逻辑，完整保留）
# ============================================================

def _receive_via_imap(
    cfg: AppConfig,
    *,
    limit: int | None = None,
    unseen_only: bool | None = None,
    mark_seen: bool | None = None,
) -> dict[str, Any]:
    """收取 Gmail Inbox 中符合条件的邮件（IMAP 后端）。

    Args:
        cfg: 应用配置。
        limit: 单次最多抓取数量，None 则使用 cfg.max_fetch_limit。
        unseen_only: 是否只收未读，None 则使用 cfg.receive_unseen_only。
        mark_seen: 收取后是否标记已读，None 则使用 cfg.receive_mark_seen。

    Returns:
        统计结果 dict：
            {
              "ok": True,
              "fetched": int,        扫描的邮件数
              "saved": int,          实际保存的新邮件数
              "skipped": int,        已记录过被跳过的邮件数
              "attachments": int,    保存的附件数
              "errors": list[str],   单封邮件的错误信息
            }
    """
    from agent_mail_bridge.config import require_receive_config
    require_receive_config(cfg)

    scan_cap = limit if limit is not None else max(
        cfg.max_fetch_limit, cfg.receive_scan_cap
    )
    if unseen_only is None:
        unseen_only = cfg.receive_unseen_only
    if mark_seen is None:
        mark_seen = cfg.receive_mark_seen

    result: dict[str, Any] = {
        "ok": True,
        "fetched": 0,
        "saved": 0,
        "skipped": 0,
        "attachments": 0,
        "accepted": 0,
        "duplicates": 0,
        "failed": 0,
        "saved_files": [],
        "errors": [],
        "global_error": False,
        "retry_deferred": 0,
    }

    gmail_addr = cfg.gmail_address.lower().strip()

    log_event(cfg.db_path, "INFO", "receive", f"开始收取邮件（limit={limit}）")

    try:
        conn = _connect_imap(cfg)
    except Exception as exc:  # noqa: BLE001
        # 区分网络错误与认证错误，给出用户可读提示
        from agent_mail_bridge.network import (
            GmailAuthError,
            NetworkConfigError,
            NetworkConnectError,
        )
        if isinstance(exc, GmailAuthError):
            msg = (f"Gmail 认证失败：{exc}。"
                   "请检查 GMAIL_ADDRESS 与 GMAIL_APP_PASSWORD（应用专用密码，非普通密码）。")
        elif isinstance(exc, NetworkConfigError):
            msg = f"Gmail 网络配置错误：{exc}"
        elif isinstance(exc, NetworkConnectError):
            mode = cfg.gmail_network_mode
            msg = (f"Gmail IMAP 连接失败（模式={mode}）：{exc}。"
                   "可运行 `python -m agent_mail_bridge diagnose-gmail` 排查。")
        else:
            msg = (f"IMAP 连接失败：{exc}。"
                   "请检查 GMAIL_ADDRESS 与 GMAIL_APP_PASSWORD（应用专用密码，非普通密码）。")
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "receive", msg)
        result["ok"] = False
        result["global_error"] = True
        result["errors"].append(msg)
        return result

    try:
        conn.select("INBOX")

        # 搜索条件
        cutoff = now_local() - timedelta(minutes=max(1, cfg.receive_lookback_minutes))
        criteria: list[str] = []
        if unseen_only:
            criteria.append("UNSEEN")
        criteria.extend(("SINCE", _imap_date(cutoff)))
        typ, data = conn.uid("search", None, *criteria)
        if typ != "OK":
            msg = f"IMAP 搜索失败：{typ}"
            logger.error(msg)
            log_event(cfg.db_path, "ERROR", "receive", msg)
            result["ok"] = False
            result["global_error"] = True
            result["errors"].append(msg)
            return result

        uids = [u for u in data[0].split() if u]
        # 限制扫描数量；从最新的开始（UID 通常递增，倒序取最近 limit 封）
        uids = uids[-scan_cap:] if scan_cap and scan_cap > 0 else uids
        known_uids = {uid.decode(errors="replace") for uid in uids}
        for retry in query_due_receive_retries(
            cfg.db_path, "imap", limit=min(100, scan_cap)
        ):
            resource_id = str(retry.get("resource_id") or "")
            if resource_id and resource_id not in known_uids:
                uids.append(resource_id.encode("ascii", errors="ignore"))
                known_uids.add(resource_id)
        result["fetched"] = len(uids)

        for uid in uids:
            resource_id = uid.decode(errors="replace")
            if not receive_retry_is_due(cfg.db_path, "imap", resource_id):
                result["skipped"] += 1
                result["retry_deferred"] += 1
                continue
            try:
                _process_one_unified(conn, uid, cfg, mark_seen, result)
                clear_receive_retry(cfg.db_path, "imap", resource_id)
            except Exception as exc:  # noqa: BLE001
                err = f"处理邮件 uid={resource_id} 失败：{exc}"
                logger.warning("处理邮件失败", exc_info=True)
                logger.warning(err)
                log_event(cfg.db_path, "WARNING", "receive", err)
                result["errors"].append(err)
                result["failed"] += 1
                record_receive_failure(
                    cfg.db_path,
                    backend="imap",
                    resource_id=resource_id,
                    message_id=str(getattr(exc, "message_id", resource_id)),
                    error=str(exc),
                )

        retry_counts = count_receive_retries(cfg.db_path)
        result["pending_retries"] = retry_counts["pending"]
        result["needs_attention"] = retry_counts["needs_attention"]

        log_event(
            cfg.db_path,
            "WARNING" if result["failed"] else "SUCCESS",
            "receive",
            f"收取完成：扫描 {result['fetched']} 封，新存 {result['saved']} 封，"
            f"跳过 {result['skipped']} 封，附件 {result['attachments']} 个",
        )
        return result
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ============================================================
# 内部：连接
# ============================================================

def _connect_imap(cfg: AppConfig) -> imaplib.IMAP4_SSL:
    """连接 Gmail IMAP 并登录。

    连接（TCP/TLS）由网络适配层 create_gmail_imap_client 负责，
    登录认证失败由 login_imap_client 包装为 GmailAuthError。
    邮件业务逻辑不变。
    """
    from agent_mail_bridge.config import require_gmail_network_config
    from agent_mail_bridge.network import (
        create_gmail_imap_client,
        login_imap_client,
    )

    require_gmail_network_config(cfg)
    conn = create_gmail_imap_client(cfg)
    login_imap_client(conn, cfg.gmail_address, cfg.gmail_app_password)
    logger.info("IMAP 登录成功：%s", cfg.gmail_address)
    return conn


# ============================================================
# 内部：单封邮件处理
# ============================================================

def _process_one(
    conn: imaplib.IMAP4_SSL,
    uid: bytes,
    cfg: AppConfig,
    gmail_addr: str,
    mark_seen: bool,
    result: dict[str, Any],
) -> None:
    """抓取并处理单封邮件。"""
    # 抓取邮件原始内容
    fetch_typ, fetch_data = conn.fetch(uid, "(RFC822)")
    if fetch_typ != "OK" or not fetch_data or not fetch_data[0]:
        logger.warning("无法抓取邮件 uid=%s", uid)
        return

    raw = fetch_data[0][1]
    msg = email.message_from_bytes(raw)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # 无 Message-ID 时用部分原始内容生成稳定 id，避免重复
        message_id = f"<generated-{sha256_of_bytes(raw)[:16]}@local>"

    # 去重
    if message_id_exists(cfg.db_path, message_id):
        result["skipped"] += 1
        logger.debug("跳过已记录邮件：%s", message_id)
        return

    # 解析发件 / 收件
    from_email = _extract_email_address(msg.get("From", ""))
    to_raw = msg.get("To", "")
    to_emails = _extract_email_addresses(to_raw)
    subject = decode_mime_header(msg.get("Subject", ""))

    # 只收自发自收邮件（from == gmail 且 to 含 gmail）
    if cfg.auto_receive_only_self_mail:
        if from_email.lower() != gmail_addr:
            logger.debug("跳过非自发自收邮件（from 不匹配）：%s", from_email)
            return
        if gmail_addr not in [e.lower() for e in to_emails]:
            logger.debug("跳过非自发自收邮件（to 不含本人）：%s", to_raw)
            return

    # 解析时间
    received_dt = now_local()
    received_at = fmt_datetime(received_dt)

    # 提取正文与附件
    body_text, attachments = _parse_parts(msg, cfg, received_dt)

    # 保存正文
    body_path = build_body_path(cfg, subject, received_dt)
    body_md = _compose_body_md(
        message_id=message_id,
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

    # 写入收件主记录
    insert_received_message(
        cfg.db_path,
        message_id=message_id,
        gmail_uid=uid.decode(errors="replace"),
        subject=subject,
        from_email=from_email,
        to_email=to_raw,
        received_at=received_at,
        saved_date=saved_date,
        body_file_path=str(body_path),
        body_sha256=body_sha,
        has_attachments=len(attachments) > 0,
        status="saved",
        source="gmail",
        backend="imap",
    )

    # 写入正文文件记录
    insert_received_file(
        cfg.db_path,
        message_id=message_id,
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

    # 写入附件文件记录
    for att in attachments:
        insert_received_file(
            cfg.db_path,
            message_id=message_id,
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

    # 可选标记已读
    if mark_seen:
        try:
            conn.store(uid, "+FLAGS", "\\Seen")
        except Exception:
            logger.warning("标记已读失败 uid=%s", uid)

    result["saved"] += 1
    log_event(
        cfg.db_path,
        "SUCCESS",
        "receive",
        f"已保存邮件：{subject or '(无标题)'} | 附件 {len(attachments)} 个",
    )
    logger.info("已保存邮件：%s（附件 %d 个）", subject, len(attachments))


def _process_one_unified(
    conn: imaplib.IMAP4_SSL,
    uid: bytes,
    cfg: AppConfig,
    mark_seen: bool,
    result: dict[str, Any],
) -> None:
    """把 IMAP 原始邮件转换后交给统一业务流程。"""
    fetch_typ, fetch_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
    if fetch_typ != "OK" or not fetch_data or not fetch_data[0]:
        raise RuntimeError("IMAP 未返回邮件原文")
    raw = fetch_data[0][1]
    msg = email.message_from_bytes(raw)
    received_dt = _message_datetime(msg)
    normalized = normalized_mail_from_raw(
        bytes(raw),
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid=uid.decode(errors="replace"),
        received_at=fmt_datetime(received_dt),
        saved_date=received_dt.strftime("%Y-%m-%d"),
        max_attachment_bytes=cfg.max_attachment_bytes,
        mailbox_ref="imap:INBOX",
    )
    single = process_normalized_mail(cfg, normalized)
    status = single["status"]
    if status in {"saved", "partial"}:
        result["accepted"] += 1
        result["saved"] += 1
        result["attachments"] += single.get("attachments", 0)
        result["saved_files"].extend(single.get("saved_files", []))
        if status == "partial":
            raise MailArchivePartialError(
                single.get("error") or "邮件归档部分完成",
                message_id=single["message_id"], package_id=single["package_id"],
            )
    elif status == "duplicate":
        result["duplicates"] += 1
        result["skipped"] += 1
    else:
        result["skipped"] += 1
    if mark_seen and status in {"saved", "duplicate"}:
        conn.uid("store", uid, "+FLAGS", "\\Seen")


def _imap_date(value) -> str:
    """生成不受 Windows 当前区域设置影响的 IMAP SINCE 日期。"""
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return f"{value.day:02d}-{months[value.month - 1]}-{value.year:04d}"


def _message_datetime(msg: Message):
    """优先使用邮件 Date，解析失败时使用本地当前时间。"""
    try:
        parsed = parsedate_to_datetime(msg.get("Date", ""))
        if parsed is None:
            return now_local()
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return now_local()


def _extract_message_content(
    msg: Message, cfg: AppConfig
) -> tuple[str, list[AttachmentData]]:
    """提取 MIME 正文和附件，不在后端适配阶段落盘。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentData] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        filename = decode_mime_header(part.get_filename())
        disposition = (part.get_content_disposition() or "").lower()
        payload = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        if filename or disposition in {"attachment", "inline"} and content_type not in {
            "text/plain", "text/html"
        }:
            safe_name = sanitize_filename(filename or "未命名附件", max_len=120)
            if check_size_ok(len(payload), cfg.max_attachment_bytes):
                attachments.append(AttachmentData(
                    filename=safe_name,
                    content=payload,
                    mime_type=content_type,
                    security_status=attachment_security_status(safe_name),
                ))
            continue
        text = _decode_payload_text(part)
        if content_type == "text/plain" and text:
            plain_parts.append(text)
        elif content_type == "text/html" and text:
            html_parts.append(text)
    body = "\n\n".join(plain_parts).strip()
    if not body:
        body = _html_to_text("\n".join(html_parts))
    return body, attachments


# ============================================================
# 内部：邮件解析
# ============================================================

def _parse_parts(msg: Message, cfg: AppConfig, dt) -> tuple[str, list[dict[str, Any]]]:
    """遍历邮件各部分，提取正文文本与附件。

    Returns:
        (body_text, attachments)
        attachments 中每项含：
            original_filename, saved_filename, saved_path,
            sha256, size_bytes, mime_type, skipped(bool), skip_reason
    """
    body_text = ""
    html_text = ""
    attachments: list[dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = part.get_content_disposition() or ""
            # 跳过多重嵌套的 multipart 容器本身
            if part.is_multipart():
                continue

            if disposition.lower() == "attachment" or part.get_filename():
                _handle_attachment(part, cfg, dt, attachments)
            elif ctype == "text/plain":
                body_text = _decode_payload_text(part) or body_text
            elif ctype == "text/html":
                html_text = _decode_payload_text(part) or html_text
    else:
        # 非多部分邮件
        ctype = msg.get_content_type()
        if msg.get_filename():
            _handle_attachment(msg, cfg, dt, attachments)
        elif ctype == "text/plain":
            body_text = _decode_payload_text(msg)
        elif ctype == "text/html":
            html_text = _decode_payload_text(msg)

    # 正文优先 text/plain，否则清洗 HTML
    if not body_text and html_text:
        body_text = _html_to_text(html_text)
    if not body_text and not attachments:
        body_text = "(本邮件无正文)"

    return body_text, attachments


def _handle_attachment(
    part: Message, cfg: AppConfig, dt, attachments: list[dict[str, Any]]
) -> None:
    """处理单个附件部分。"""
    raw_filename = part.get_filename()
    original_filename = decode_mime_header(raw_filename) if raw_filename else "未命名附件"
    original_filename = sanitize_filename(original_filename, max_len=120) or "未命名附件"

    payload = part.get_payload(decode=True)
    if payload is None:
        return

    size_bytes = len(payload)
    mime_type = part.get_content_type() or "application/octet-stream"

    # 危险扩展名：记录 warning，但仍保存（不执行不解压）
    if is_dangerous(original_filename):
        logger.warning("附件含危险扩展名，仅保存不执行：%s", original_filename)
        log_event(
            cfg.db_path,
            "WARNING",
            "receive",
            f"危险扩展名附件：{original_filename}（仅保存，不执行）",
        )

    # 大小超限：跳过并记录
    if not check_size_ok(size_bytes, cfg.max_attachment_bytes):
        logger.warning(
            "附件超过大小限制(%dMB)，跳过：%s (%.2fMB)",
            cfg.max_attachment_mb,
            original_filename,
            size_bytes / 1024 / 1024,
        )
        log_event(
            cfg.db_path,
            "WARNING",
            "receive",
            f"附件超限跳过：{original_filename} ({size_bytes/1024/1024:.2f}MB > {cfg.max_attachment_mb}MB)",
        )
        return

    # 保存附件
    saved_path = build_attachment_path(cfg, original_filename, dt)
    write_bytes(saved_path, payload)
    sha = sha256_of_bytes(payload)

    attachments.append({
        "original_filename": original_filename,
        "saved_filename": saved_path.name,
        "saved_path": str(saved_path),
        "sha256": sha,
        "size_bytes": size_bytes,
        "mime_type": mime_type,
        "skipped": False,
    })
    logger.info("已保存附件：%s", saved_path.name)


def _decode_payload_text(part: Message) -> str:
    """解码文本部分，处理 charset。"""
    payload = part.get_payload(decode=True)
    if payload is None:
        # 纯文本未编码
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""

    charset = part.get_content_charset()
    candidates = []
    if charset:
        candidates.append(charset)
    candidates.extend(["utf-8", "gbk", "gb2312", "big5", "latin-1"])

    for cs in candidates:
        try:
            return payload.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    # 全部失败，用替换字符
    return payload.decode("utf-8", errors="replace")


# 简易 HTML -> 文本：去标签 + 转义还原
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


def _html_to_text(html: str) -> str:
    """简易 HTML 清洗为可读文本。"""
    if not html:
        return ""
    # 块级标签转换行
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    # 去除所有标签
    text = _TAG_RE.sub("", text)
    # 还原常见实体
    for ent, ch in _HTML_ENTITIES.items():
        text = text.replace(ent, ch)
    # 数字实体
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    # 折叠空白
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


# ============================================================
# 内部：地址解析
# ============================================================

_ADDR_RE = re.compile(r"<([^>]+)>")


def _extract_email_address(header_value: str) -> str:
    """从 From 头提取第一个 email 地址。"""
    if not header_value:
        return ""
    m = _ADDR_RE.search(header_value)
    if m:
        return m.group(1).strip()
    # 无尖括号，可能直接是地址
    return header_value.strip().split(",")[0].strip()


def _extract_email_addresses(header_value: str) -> list[str]:
    """从 To/Cc 头提取所有 email 地址。"""
    if not header_value:
        return []
    addrs: list[str] = []
    # 先按尖括号提取
    for m in _ADDR_RE.finditer(header_value):
        addrs.append(m.group(1).strip())
    if not addrs:
        # 无尖括号，按逗号分割
        for chunk in header_value.split(","):
            chunk = chunk.strip()
            if chunk and "@" in chunk:
                addrs.append(chunk)
    return addrs


# ============================================================
# 内部：正文 Markdown 组装
# ============================================================

def _compose_body_md(
    *,
    message_id: str,
    from_email: str,
    to_raw: str,
    subject: str,
    received_at: str,
    body_text: str,
    attachments: list[dict[str, Any]],
) -> str:
    """组装正文 Markdown，顶部附加元信息。

    若正文为空但有附件，则生成说明 .md 列出附件信息。
    """
    saved_at = fmt_datetime(now_local())
    meta_lines = [
        "---",
        "source: gmail",
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
