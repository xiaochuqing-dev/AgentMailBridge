"""Gmail API 收件模块。

职责：
1. 通过 Gmail API（HTTPS 443）读取邮件，绕过 IMAP 993 端口限制。
2. 去重：优先 RFC Message-ID（兼容 IMAP 逻辑），否则用 gmail_api:<id>。
3. 由统一收件规则决定候选邮件是否归档，默认接收当前扫描范围内全部邮件。
4. 提取正文（优先 text/plain，其次 HTML 清洗）与附件。
5. 保存正文/附件到本地日期目录，写入 SQLite。
6. 返回与 IMAP 后端一致的 result dict，供 mail_receive 协调入口统一处理。

安全要求：只读 scope，不删邮件、不标记已读、不发送。
不打印 token / credentials 内容。
"""

from __future__ import annotations

import base64
import email
import re
from datetime import datetime, timedelta
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
    automatic: bool = False,
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
        page_size = cfg.gmail_api_max_results
        scan_cap = cfg.receive_scan_cap
    else:
        page_size = limit
        scan_cap = limit

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
        log_event(
            cfg.db_path, "INFO", "receive",
            f"开始通过 Gmail API 收取邮件（page_size={page_size}, lookback={cfg.receive_lookback_minutes}m）",
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
            result["global_error"] = True
            result["errors"].append(msg)
            return result

    # ---- list 邮件 id ----
    try:
        messages: list[dict[str, Any]] = []
        page_token: str | None = None
        query = _query_with_lookback(cfg)
        while len(messages) < scan_cap:
            request: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": min(page_size, scan_cap - len(messages)),
            }
            if page_token:
                request["pageToken"] = page_token
            list_resp = service.users().messages().list(**request).execute()
            messages.extend(list_resp.get("messages", []) or [])
            page_token = list_resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:  # noqa: BLE001
        msg = _describe_api_error(exc)
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "receive", msg)
        result["ok"] = False
        result["global_error"] = True
        result["errors"].append(msg)
        return result

    messages = messages[:scan_cap]
    known_ids = {str(item.get("id", "")) for item in messages}
    for retry in query_due_receive_retries(
        cfg.db_path, "gmail_api", limit=min(100, scan_cap)
    ):
        resource_id = str(retry.get("resource_id") or "")
        if resource_id and resource_id not in known_ids:
            messages.append({"id": resource_id, "retry": True})
            known_ids.add(resource_id)
    result["fetched"] = len(messages)

    for item in messages:
        gmail_message_id = item.get("id", "")
        gmail_thread_id = item.get("threadId", "")
        if not receive_retry_is_due(
            cfg.db_path, "gmail_api", gmail_message_id
        ):
            result["skipped"] += 1
            result["retry_deferred"] += 1
            continue
        try:
            _process_one_unified(
                service, cfg, gmail_message_id, gmail_thread_id, result,
            )
            clear_receive_retry(cfg.db_path, "gmail_api", gmail_message_id)
        except Exception as exc:  # noqa: BLE001
            err = f"处理 Gmail API 邮件 id={gmail_message_id} 失败：{exc}"
            logger.warning("处理 Gmail API 邮件失败", exc_info=True)
            logger.warning(err)
            log_event(cfg.db_path, "WARNING", "receive", err)
            result["errors"].append(err)
            result["failed"] += 1
            record_receive_failure(
                cfg.db_path,
                backend="gmail_api",
                resource_id=gmail_message_id,
                message_id=str(getattr(exc, "message_id", gmail_message_id)),
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
            f"Gmail API 收取完成：扫描 {result['fetched']} 封，"
            f"新存 {result['saved']} 封，跳过 {result['skipped']} 封，"
            f"附件 {result['attachments']} 个",
        )
    return result


def rescan_gmail_api_messages(
    cfg: AppConfig,
    *,
    date_from: datetime,
    date_to: datetime,
    service: Any | None = None,
    apply_receive_rule: bool = True,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    scan_id: str,
    page_size: int = 100,
    scan_cap: int = 5000,
) -> dict[str, Any]:
    """分页重扫明确历史范围，不复用普通 lookback，也不修改 Gmail 状态。"""
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
        f"历史补扫开始：backend=gmail_api，scan_id={scan_id}",
    )
    if service is None:
        try:
            service = _build_service(cfg)
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, global_error=True)
            result["errors"].append(_describe_auth_error(exc))
            return result
    query = _historical_query(cfg, date_from, date_to)
    page_token: str | None = None
    fingerprint = receive_rule_fingerprint(cfg) if apply_receive_rule else "all_scanned_override"
    account_ref = stable_account_ref(cfg)
    while result["fetched"] < safe_scan_cap:
        if cancel_check and cancel_check():
            result["cancelled"] = True
            break
        request: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": min(safe_page_size, safe_scan_cap - result["fetched"]),
        }
        if page_token:
            request["pageToken"] = page_token
        try:
            page = service.users().messages().list(**request).execute()
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, global_error=True)
            result["errors"].append(_describe_api_error(exc))
            break
        messages = page.get("messages", []) or []
        for item in messages:
            if cancel_check and cancel_check():
                result["cancelled"] = True
                break
            provider_id = str(item.get("id") or "")
            thread_id = str(item.get("threadId") or "")
            result["fetched"] += 1
            try:
                single = _process_one_unified(
                    service,
                    cfg,
                    provider_id,
                    thread_id,
                    result,
                    apply_receive_rule=apply_receive_rule,
                )
                status = str(single.get("status") or "")
                if status == "skipped":
                    result["rule_skipped"] += 1
                    evaluation_result = "rule_skipped"
                elif status == "duplicate":
                    result["matched"] += 1
                    evaluation_result = "duplicate"
                else:
                    result["matched"] += 1
                    evaluation_result = status or "saved"
                record_receive_rule_evaluation(
                    cfg.db_path,
                    account_ref=account_ref,
                    backend="gmail_api",
                    provider_message_id=provider_id,
                    message_id=str(single.get("message_id") or "") or None,
                    result=evaluation_result,
                    reason=str(single.get("reason") or "matched"),
                    rule_fingerprint=fingerprint,
                    scan_id=scan_id,
                )
                clear_receive_retry(cfg.db_path, "gmail_api", provider_id)
            except Exception as exc:  # noqa: BLE001
                is_partial = isinstance(exc, Exception) and type(exc).__name__ == "MailArchivePartialError"
                if is_partial:
                    result["matched"] += 1
                result["failed"] += 1
                result["errors"].append(f"历史邮件处理失败：{type(exc).__name__}")
                record_receive_failure(
                    cfg.db_path,
                    backend="gmail_api",
                    resource_id=provider_id,
                    message_id=str(getattr(exc, "message_id", provider_id)),
                    error=str(exc),
                )
                record_receive_rule_evaluation(
                    cfg.db_path,
                    account_ref=account_ref,
                    backend="gmail_api",
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
            if result["fetched"] >= safe_scan_cap:
                break
        if result["cancelled"]:
            break
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    if page_token and result["fetched"] >= safe_scan_cap:
        result["truncated"] = True
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


def _query_with_lookback(cfg: AppConfig) -> str:
    """每轮重叠回看并依靠 Message-ID/唯一约束去重，优先防漏。"""
    cutoff = now_local() - timedelta(minutes=max(1, cfg.receive_lookback_minutes))
    base = cfg.gmail_api_query.strip()
    return f"{base} after:{int(cutoff.timestamp())}".strip()


def _historical_query(cfg: AppConfig, date_from: datetime, date_to: datetime) -> str:
    base = cfg.gmail_api_query.strip()
    # Gmail after/before 使用 epoch；结束时间加一秒形成用户可理解的闭区间。
    return (
        f"{base} after:{int(date_from.timestamp()) - 1} "
        f"before:{int(date_to.timestamp()) + 1}"
    ).strip()


# ============================================================
# 内部：service 创建
# ============================================================

def _build_service(cfg: AppConfig) -> Any:
    """委托 gmail_api_auth 创建 service。"""
    from agent_mail_bridge.gmail_api_auth import get_gmail_api_service
    # 普通收件不得突然打开浏览器，授权必须由显式授权操作触发。
    return get_gmail_api_service(cfg, interactive=False)


# ============================================================
# 内部：单封邮件处理
# ============================================================

def _process_one_unified(
    service: Any,
    cfg: AppConfig,
    gmail_message_id: str,
    gmail_thread_id: str,
    result: dict[str, Any],
    *,
    apply_receive_rule: bool = True,
) -> dict[str, Any]:
    """读取 Gmail 真实 raw bytes，再交给统一 RFC822 归档流程。"""
    msg = service.users().messages().get(
        userId="me", id=gmail_message_id, format="raw"
    ).execute()
    encoded_raw = msg.get("raw")
    if not encoded_raw:
        raise RuntimeError("Gmail API 未返回 raw 邮件原文")
    raw_bytes = decode_base64url(str(encoded_raw))
    parsed_message = email.message_from_bytes(raw_bytes)
    received_dt = _raw_message_datetime(parsed_message, msg.get("internalDate"))
    normalized = normalized_mail_from_raw(
        raw_bytes,
        backend="gmail_api",
        backend_message_id=gmail_message_id,
        thread_id=str(msg.get("threadId") or gmail_thread_id),
        uid="",
        received_at=fmt_datetime(received_dt),
        saved_date=received_dt.strftime("%Y-%m-%d"),
        max_attachment_bytes=cfg.max_attachment_bytes,
        mailbox_ref="gmail:me/inbox",
    )
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
            from agent_mail_bridge.mail_receive import MailArchivePartialError
            raise MailArchivePartialError(
                single.get("error") or "邮件归档部分完成",
                message_id=single["message_id"], package_id=single["package_id"],
            )
    elif status == "duplicate":
        result["duplicates"] += 1
        result["skipped"] += 1
    else:
        result["skipped"] += 1
    return single


def _api_message_datetime(headers: dict[str, str], internal_date: Any):
    """优先使用标准 Date 头，确保两个后端得到相同时间。"""
    raw_date = _find_header(headers, ("Date",))
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed is not None:
                if parsed.tzinfo is not None:
                    return parsed.astimezone().replace(tzinfo=None)
                return parsed
        except (TypeError, ValueError, OverflowError):
            pass
    return _parse_internal_date(internal_date)


def _raw_message_datetime(message, internal_date: Any):
    """raw 模式优先采用 RFC Date，失败时使用 provider internalDate。"""
    raw_date = message.get("Date", "")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed is not None:
                if parsed.tzinfo is not None:
                    return parsed.astimezone().replace(tzinfo=None)
                return parsed
        except (TypeError, ValueError, OverflowError):
            pass
    return _parse_internal_date(internal_date)


def _extract_payload_unified(
    payload: dict[str, Any],
    cfg: AppConfig,
    service: Any,
    gmail_message_id: str,
) -> tuple[str, list[AttachmentData]]:
    """递归提取正文和内联/普通附件，附件失败时整封标记失败。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentData] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "application/octet-stream")
        filename = decode_mime_header(part.get("filename", ""))
        body = part.get("body") or {}
        if filename:
            safe_name = sanitize_filename(filename, max_len=120)
            if body.get("attachmentId"):
                response = service.users().messages().attachments().get(
                    userId="me", messageId=gmail_message_id,
                    id=body["attachmentId"],
                ).execute()
                encoded = response.get("data", "")
            else:
                encoded = body.get("data", "")
            if not encoded:
                raise RuntimeError(f"附件下载失败：{safe_name}")
            content = decode_base64url(encoded)
            if check_size_ok(len(content), cfg.max_attachment_bytes):
                attachments.append(AttachmentData(
                    filename=safe_name,
                    content=content,
                    mime_type=mime_type,
                    security_status=attachment_security_status(safe_name),
                ))
            return
        encoded = body.get("data")
        if mime_type == "text/plain" and encoded is not None:
            text = _decode_base64url_text(encoded)
            if text:
                plain_parts.append(text)
        elif mime_type == "text/html" and encoded is not None:
            text = _decode_base64url_text(encoded)
            if text:
                html_parts.append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    body = "\n\n".join(plain_parts).strip()
    if not body:
        body = _html_to_text("\n".join(html_parts))
    return body, attachments


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
