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
import uuid
from datetime import datetime, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

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
    record_receive_rule_evaluation,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.mail_common import (
    AttachmentData,
    NormalizedMail,
    attachment_security_status,
    normalized_mail_from_raw,
)
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mail_archive import stable_account_ref
from agent_mail_bridge.receive_rules import receive_rule_fingerprint
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
    automatic: bool = False,
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
    log_backend = logger.debug if automatic else logger.info
    log_backend("收件后端：%s（配置=%s）", backend, cfg.gmail_receive_backend)

    if backend == "gmail_api":
        result = receive_gmail_api(cfg, limit=limit, automatic=automatic)
    else:
        result = _receive_via_imap(
            cfg, limit=limit, unseen_only=unseen_only, mark_seen=mark_seen,
            automatic=automatic,
        )
    result["backend"] = backend
    return result


def receive_gmail_api(
    cfg: AppConfig, *, limit: int | None = None, automatic: bool = False
) -> dict[str, Any]:
    """Gmail API 后端收件（委托 gmail_api_receive 模块）。"""
    from agent_mail_bridge.gmail_api_receive import receive_gmail_api_messages
    return receive_gmail_api_messages(
        cfg, service=None, limit=limit, automatic=automatic
    )


def historical_rescan_mails(
    cfg: AppConfig,
    *,
    date_from: datetime,
    date_to: datetime,
    apply_receive_rule: bool = True,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    scan_id: str | None = None,
    page_size: int = 100,
    scan_cap: int = 5000,
) -> dict[str, Any]:
    """按用户指定范围重扫历史邮件，完全独立于普通增量 lookback。"""
    from agent_mail_bridge.config import _effective_receive_backend

    backend = _effective_receive_backend(cfg)
    stable_scan_id = scan_id or f"scan_{uuid.uuid4().hex}"
    if backend == "gmail_api":
        from agent_mail_bridge.gmail_api_receive import rescan_gmail_api_messages

        result = rescan_gmail_api_messages(
            cfg,
            date_from=date_from,
            date_to=date_to,
            apply_receive_rule=apply_receive_rule,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            scan_id=stable_scan_id,
            page_size=page_size,
            scan_cap=scan_cap,
        )
    else:
        result = _rescan_via_imap(
            cfg,
            date_from=date_from,
            date_to=date_to,
            apply_receive_rule=apply_receive_rule,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            scan_id=stable_scan_id,
            page_size=page_size,
            scan_cap=scan_cap,
        )
    result["backend"] = backend
    result["scan_id"] = stable_scan_id
    return result


# ============================================================
# IMAP 后端（原有逻辑，完整保留）
# ============================================================

def _receive_via_imap(
    cfg: AppConfig,
    *,
    limit: int | None = None,
    unseen_only: bool | None = None,
    mark_seen: bool | None = None,
    automatic: bool = False,
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

    if not automatic:
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

        if not automatic or result["saved"] or result["failed"]:
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


def _rescan_via_imap(
    cfg: AppConfig,
    *,
    date_from: datetime,
    date_to: datetime,
    apply_receive_rule: bool,
    cancel_check: Callable[[], bool] | None,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    scan_id: str,
    page_size: int,
    scan_cap: int,
) -> dict[str, Any]:
    """IMAP 历史补扫使用 UID SEARCH + BODY.PEEK，不误标已读。"""
    from agent_mail_bridge.config import require_receive_config

    require_receive_config(cfg)
    safe_page_size = max(1, min(int(page_size), 500))
    safe_scan_cap = max(1, min(int(scan_cap), 10_000))
    result: dict[str, Any] = {
        "ok": True,
        "fetched": 0,
        "saved": 0,
        "skipped": 0,
        "rule_skipped": 0,
        "matched": 0,
        "attachments": 0,
        "accepted": 0,
        "duplicates": 0,
        "failed": 0,
        "saved_files": [],
        "errors": [],
        "global_error": False,
        "cancelled": False,
        "truncated": False,
        "scan_id": scan_id,
    }
    log_event(
        cfg.db_path,
        "INFO",
        "receive_history",
        f"历史补扫开始：backend=imap，scan_id={scan_id}",
    )
    try:
        conn = _connect_imap(cfg)
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, global_error=True)
        result["errors"].append(f"IMAP 历史补扫连接失败：{type(exc).__name__}")
        return result
    fingerprint = receive_rule_fingerprint(cfg) if apply_receive_rule else "all_scanned_override"
    account_ref = stable_account_ref(cfg)
    try:
        conn.select("INBOX")
        end_exclusive = date_to.date() + timedelta(days=1)
        typ, data = conn.uid(
            "search", None,
            "SINCE", _imap_date(date_from),
            "BEFORE", _imap_date(end_exclusive),
        )
        if typ != "OK":
            result.update(ok=False, global_error=True)
            result["errors"].append(f"IMAP 历史搜索失败：{typ}")
            return result
        all_uids = [uid for uid in (data[0] or b"").split() if uid]
        if len(all_uids) > safe_scan_cap:
            all_uids = all_uids[-safe_scan_cap:]
            result["truncated"] = True
        for page_start in range(0, len(all_uids), safe_page_size):
            for uid in all_uids[page_start:page_start + safe_page_size]:
                if cancel_check and cancel_check():
                    result["cancelled"] = True
                    break
                provider_id = uid.decode("ascii", errors="replace")
                result["fetched"] += 1
                try:
                    single = _process_one_unified(
                        conn,
                        uid,
                        cfg,
                        False,
                        result,
                        apply_receive_rule=apply_receive_rule,
                        date_from=date_from,
                        date_to=date_to,
                    )
                    status = str(single.get("status") or "")
                    if status == "skipped":
                        result["rule_skipped"] += 1
                        evaluation_result = "rule_skipped"
                    elif status == "duplicate":
                        result["matched"] += 1
                        evaluation_result = "duplicate"
                    elif status == "out_of_range":
                        evaluation_result = "out_of_range"
                    else:
                        result["matched"] += 1
                        evaluation_result = status or "saved"
                    record_receive_rule_evaluation(
                        cfg.db_path,
                        account_ref=account_ref,
                        backend="imap",
                        provider_message_id=provider_id,
                        message_id=str(single.get("message_id") or "") or None,
                        result=evaluation_result,
                        reason=str(single.get("reason") or "matched"),
                        rule_fingerprint=fingerprint,
                        scan_id=scan_id,
                    )
                    clear_receive_retry(cfg.db_path, "imap", provider_id)
                except Exception as exc:  # noqa: BLE001
                    is_partial = isinstance(exc, MailArchivePartialError)
                    if is_partial:
                        result["matched"] += 1
                    result["failed"] += 1
                    result["errors"].append(
                        f"IMAP 历史邮件处理失败：{type(exc).__name__}"
                    )
                    record_receive_failure(
                        cfg.db_path,
                        backend="imap",
                        resource_id=provider_id,
                        message_id=str(getattr(exc, "message_id", provider_id)),
                        error=str(exc),
                    )
                    record_receive_rule_evaluation(
                        cfg.db_path,
                        account_ref=account_ref,
                        backend="imap",
                        provider_message_id=provider_id,
                        message_id=str(getattr(exc, "message_id", "")) or None,
                        result="partial" if is_partial else "failed",
                        reason=type(exc).__name__,
                        rule_fingerprint=fingerprint,
                        scan_id=scan_id,
                    )
                if progress_callback:
                    try:
                        progress_callback(dict(result))
                    except Exception:  # noqa: BLE001 - 进度展示失败不能中断邮件归档
                        logger.warning("历史补扫进度回调失败", exc_info=True)
            if result["cancelled"]:
                break
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    retry_counts = count_receive_retries(cfg.db_path)
    result.update(
        pending_retries=retry_counts["pending"],
        needs_attention=retry_counts["needs_attention"],
    )
    log_event(
        cfg.db_path,
        "WARNING" if result["failed"] or result["truncated"] else "SUCCESS",
        "receive_history",
        f"历史补扫完成：scan_id={scan_id}，扫描 {result['fetched']}，"
        f"新增 {result['saved']}，重复 {result['duplicates']}，"
        f"规则跳过 {result['rule_skipped']}，失败 {result['failed']}",
    )
    return result


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

def _process_one_unified(
    conn: imaplib.IMAP4_SSL,
    uid: bytes,
    cfg: AppConfig,
    mark_seen: bool,
    result: dict[str, Any],
    *,
    apply_receive_rule: bool = True,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
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
    if date_from is not None or date_to is not None:
        normalized_at = datetime.fromisoformat(normalized.received_at)
        if (
            date_from is not None and normalized_at < date_from
        ) or (
            date_to is not None and normalized_at > date_to
        ):
            return {
                "status": "out_of_range",
                "reason": "provider_date_overlap",
                "message_id": normalized.message_id,
                "saved_files": [],
            }
    single = process_normalized_mail(
        cfg, normalized, apply_receive_rule=apply_receive_rule
    )
    single.setdefault("message_id", normalized.message_id)
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
    return single


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
