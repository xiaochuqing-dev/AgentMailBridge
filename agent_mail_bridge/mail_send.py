"""QQ SMTP 发件模块。

职责：
1. 校验待发送文件存在、大小合理、扩展名可发送。
2. 计算 sha256，复制到 send/YYYY-MM-DD/。
3. MCP/CLI 结果固定发送到 OWNER_GMAIL；GUI 手动邮件可使用用户明确输入的收件人。
4. 发送成功后复制到 sent/YYYY-MM-DD/。
5. 写入 sent_files 表与日志。
6. 返回结构化结果。

注意：任意收件人能力只属于 GUI 手动操作，MCP submit_result 不接受 recipient。
日志中绝不打印完整 QQ 授权码。
"""

from __future__ import annotations

import mimetypes
import hashlib
import re
import socket
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent_mail_bridge.config import (
    AppConfig,
    ConfigError,
    effective_outgoing_runtime,
    require_send_config,
)
from agent_mail_bridge.database import (
    create_outbound_message,
    create_or_retry_send_attempt,
    get_outbound_by_request_id,
    insert_sent_file,
    link_sent_file_to_outbound,
    log_event,
    replace_outbound_links,
    update_outbound_message,
    upsert_outbound_resource,
    update_send_attempt,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.mail_accounts import current_send_account_id
from agent_mail_bridge.receive_rules import invalid_sender_rules
from agent_mail_bridge.security import (
    SecurityError,
    assert_within_allowed_roots,
    check_size_ok,
    is_dangerous,
)
from agent_mail_bridge.storage import (
    atomic_copy_file,
    build_send_copy_path,
    build_sent_copy_path,
    copy_file,
)
from agent_mail_bridge.utils import (
    fmt_datetime,
    now_local,
    sha256_of_file,
    split_ext,
)

logger = get_logger("mail_send")

OUTBOUND_ORIGIN_HEADER = "X-AgentMailBridge-Origin"
OUTBOUND_ID_HEADER = "X-AgentMailBridge-Outbound-ID"


def _sender_account_ref(cfg: AppConfig) -> str:
    provider = str(getattr(cfg, "runtime_provider", "") or "qq").strip().casefold()
    return f"{provider}:{_smtp_sender(cfg).casefold() or 'unconfigured'}"


def _smtp_sender(cfg: AppConfig) -> str:
    return effective_outgoing_runtime(cfg).username.strip()


def _outbound_id_for_request(request_id: str) -> str:
    return f"out_{uuid.uuid5(uuid.NAMESPACE_URL, 'agent-mail-bridge:' + request_id).hex}"


def _outbound_resource_id(outbound_id: str, order: int) -> str:
    return f"outres_{uuid.uuid5(uuid.NAMESPACE_URL, f'{outbound_id}:{order}').hex}"


def normalize_manual_recipient(value: str) -> str:
    """校验 GUI 明确收件人并阻止 CRLF/Header 注入。"""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("收件人不能为空")
    if any(char in raw for char in ("\r", "\n")) or any(
        ord(char) < 32 for char in raw
    ):
        raise ValueError("收件人包含非法控制字符")
    if "," in raw or ";" in raw or "，" in raw or "；" in raw:
        raise ValueError("当前仅支持一个明确收件人")
    if raw.startswith("@") or invalid_sender_rules((raw,)):
        raise ValueError("收件人邮箱格式无效")
    return raw


def _validate_outbound_id(value: str) -> str:
    outbound_id = str(value or "").strip()
    if not outbound_id or any(ord(char) < 33 for char in outbound_id):
        raise ValueError("outbound_id 无效")
    return outbound_id


class SmtpStageError(Exception):
    """标记 SMTP 连接、认证或发送阶段错误。"""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def send_file_to_owner_gmail(
    file_path: str | Path,
    subject: str | None = None,
    cfg: AppConfig | None = None,
) -> dict[str, Any]:
    """发送本地文件到 OWNER_GMAIL。

    Args:
        file_path: 本地文件路径。
        subject: 邮件主题，None 则自动生成。
        cfg: 应用配置，None 则重新加载。

    Returns:
        结构化结果 dict。成功示例：
            {
              "ok": True,
              "subject": "...",
              "source_path": "...",
              "send_copy_path": "...",
              "sent_copy_path": "...",
              "to": "owner@gmail.com",
              "sent_at": "2026-07-09 22:30:15",
            }
        失败示例：
            { "ok": False, "error": "SMTP authentication failed" }
    """
    if cfg is None:
        from agent_mail_bridge.config import load_config
        cfg = load_config()

    try:
        require_send_config(cfg)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "send", msg)
        return {"ok": False, "error": msg}

    source_path = Path(file_path).resolve()

    # 1. 文件存在校验
    if not source_path.exists() or not source_path.is_file():
        msg = f"文件不存在：{source_path}"
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "send", msg)
        return {"ok": False, "error": msg}

    # 2. 扩展名校验
    if is_dangerous(source_path.name):
        msg = f"危险扩展名文件，拒绝发送：{source_path.name}"
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "send", msg)
        return {"ok": False, "error": msg}

    # 3. 大小校验
    size_bytes = source_path.stat().st_size
    if not check_size_ok(size_bytes, cfg.max_send_file_bytes):
        msg = (
            f"文件超过发送大小限制({cfg.max_send_file_mb}MB)："
            f"{source_path.name} ({size_bytes/1024/1024:.2f}MB)"
        )
        logger.error(msg)
        log_event(cfg.db_path, "ERROR", "send", msg)
        return {"ok": False, "error": msg}

    # 4. 计算 sha256
    sha = sha256_of_file(source_path)

    # 5. 复制到 send 副本目录
    now = now_local()
    send_copy_path = build_send_copy_path(cfg, source_path, now)
    copy_file(source_path, send_copy_path)

    # 6. 生成主题
    stem, _ext = split_ext(source_path.name)
    if not subject:
        subject = f"Agent执行结果 - {source_path.name}"
    outbound_id = f"out_{uuid.uuid4().hex}"
    create_outbound_message(
        cfg.db_path,
        outbound_id=outbound_id,
        sender_account_ref=_sender_account_ref(cfg),
        from_account_id=current_send_account_id(cfg),
        sender_ref=_smtp_sender(cfg),
        source_origin="legacy_file_api",
        request_id=None,
        subject=subject,
        body_text="",
        to_emails=[cfg.owner_gmail],
        attachment_count=1,
        link_count=0,
        status="sending",
    )

    log_event(
        cfg.db_path, "INFO", "send",
        f"准备发送文件：{source_path.name} -> {cfg.owner_gmail}",
    )

    # 7. 构建 MIME 邮件并发送
    try:
        msg_obj = _build_email(
            cfg=cfg,
            subject=subject,
            file_path=send_copy_path,
            source_name=source_path.name,
            outbound_id=outbound_id,
        )
        _smtp_send(cfg, msg_obj)
    except smtplib.SMTPAuthenticationError as exc:
        err = f"SMTP 认证失败：{exc}。请检查 QQ_EMAIL 与 QQ_AUTH_CODE（授权码，非QQ登录密码）。"
        logger.error(err)
        log_event(cfg.db_path, "ERROR", "send", err)
        update_outbound_message(cfg.db_path, outbound_id, status="failed", error=err)
        _record_failure(cfg, source_path, send_copy_path, sha, subject, err)
        return {"ok": False, "error": err}
    except Exception as exc:  # noqa: BLE001
        err = f"发送失败：{exc}"
        logger.exception("SMTP 发送异常")
        log_event(cfg.db_path, "ERROR", "send", err)
        update_outbound_message(cfg.db_path, outbound_id, status="failed", error=err)
        _record_failure(cfg, source_path, send_copy_path, sha, subject, err)
        return {"ok": False, "error": err}

    # 8. 发送成功 -> 复制到 sent 副本目录
    sent_copy_path = build_sent_copy_path(cfg, source_path, now)
    copy_file(send_copy_path, sent_copy_path)

    sent_at = fmt_datetime(now)

    # 9. 写入 sent_files
    insert_sent_file(
        cfg.db_path,
        source_path=str(source_path),
        send_copy_path=str(send_copy_path),
        sent_copy_path=str(sent_copy_path),
        sha256=sha,
        subject=subject,
        from_email=_smtp_sender(cfg),
        to_email=cfg.owner_gmail,
        sent_at=sent_at,
        status="sent",
        error_message=None,
        original_filename=source_path.name,
        size_bytes=size_bytes,
        source_origin="legacy_file_api",
        source_sha256=sha,
        staged_sha256=sha256_of_file(send_copy_path),
        attachment_sha256=sha256_of_file(send_copy_path),
        sent_archive_sha256=sha256_of_file(sent_copy_path),
        outbound_id=outbound_id,
        from_account_id=current_send_account_id(cfg),
    )
    update_outbound_message(
        cfg.db_path, outbound_id, status="sent", sent_at=sent_at, error=None
    )

    log_event(
        cfg.db_path, "SUCCESS", "send",
        f"发送成功：{source_path.name} -> {cfg.owner_gmail}",
    )
    logger.info("发送成功：%s", source_path.name)

    return {
        "ok": True,
        "subject": subject,
        "source_path": str(source_path),
        "send_copy_path": str(send_copy_path),
        "sent_copy_path": str(sent_copy_path),
        "to": cfg.owner_gmail,
        "sent_at": sent_at,
        "outbound_id": outbound_id,
    }


def send_outbound_mail(
    *,
    subject: str | None,
    body_text: str,
    attachment_paths: list[str | Path],
    links: list[dict[str, Any] | str],
    cfg: AppConfig,
    recipient: str | None = None,
    source_origin: str = "manual_gui",
    outbound_id: str | None = None,
) -> dict[str, Any]:
    """发送一封带一个正文、N 个附件和显式链接的 MIME 邮件。"""
    try:
        require_send_config(cfg, require_owner=recipient is None)
        target_recipient = normalize_manual_recipient(
            cfg.owner_gmail if recipient is None else recipient
        )
    except (ConfigError, ValueError) as exc:
        return _send_error_result("", "configuration_error", str(exc))

    normalized_links: list[dict[str, str]] = []
    seen_links: set[str] = set()
    try:
        for raw in links:
            if isinstance(raw, str):
                url = raw
                display_text = ""
            else:
                url = str(raw.get("url") or "")
                display_text = str(raw.get("display_text") or "")
            normalized = _normalize_outbound_url(url)
            key = normalized.casefold()
            if key in seen_links:
                continue
            seen_links.add(key)
            normalized_links.append({"url": normalized, "display_text": display_text.strip()})
    except ValueError as exc:
        return _send_error_result("", "invalid_link", str(exc))

    sources: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_size = 0
    for raw_path in attachment_paths:
        try:
            source = Path(raw_path).resolve(strict=True)
        except OSError:
            return _send_error_result("", "file_not_found", f"附件不存在：{Path(raw_path).name}")
        if not source.is_file():
            return _send_error_result("", "file_not_found", f"附件不是普通文件：{source.name}")
        key = str(source).casefold()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if is_dangerous(source.name):
            return _send_error_result("", "file_type_not_allowed", f"危险扩展名文件禁止发送：{source.name}")
        size_bytes = source.stat().st_size
        if not check_size_ok(size_bytes, cfg.max_send_file_bytes):
            return _send_error_result("", "file_too_large", f"附件超过发送大小限制：{source.name}")
        total_size += size_bytes
        if total_size > cfg.max_send_file_bytes:
            return _send_error_result("", "total_size_too_large", "附件总大小超过发送限制")
        sources.append({
            "path": source,
            "display_name": source.name,
            "size_bytes": size_bytes,
            "sha256": sha256_of_file(source),
            "mime_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        })

    clean_body = str(body_text or "")
    requested_subject = (subject or "").strip()
    if not requested_subject and not clean_body.strip() and not sources and not normalized_links:
        return _send_error_result("", "empty_message", "邮件主题、正文、附件和链接不能同时为空")
    actual_subject = requested_subject
    if not actual_subject:
        actual_subject = (
            f"邮件附件 - {sources[0]['display_name']}"
            if sources else "来自 AgentMailBridge 的邮件"
        )

    try:
        mail_id = _validate_outbound_id(outbound_id or f"out_{uuid.uuid4().hex}")
    except ValueError as exc:
        return _send_error_result("", "invalid_outbound_id", str(exc))
    create_outbound_message(
        cfg.db_path,
        outbound_id=mail_id,
        sender_account_ref=_sender_account_ref(cfg),
        from_account_id=current_send_account_id(cfg),
        sender_ref=_smtp_sender(cfg),
        source_origin=source_origin,
        request_id=None,
        subject=actual_subject,
        body_text=clean_body,
        to_emails=[target_recipient],
        attachment_count=len(sources),
        link_count=len(normalized_links),
        status="sending",
    )
    replace_outbound_links(cfg.db_path, mail_id, normalized_links)

    now = now_local()
    day = now.strftime("%Y-%m-%d")
    staged_resources: list[dict[str, Any]] = []
    try:
        for index, source in enumerate(sources, 1):
            resource_id = _outbound_resource_id(mail_id, index)
            original = Path(source["path"])
            # 完整展示名已在数据库审计；内部受控路径使用短 Hash 名，避免
            # 深层用户数据目录叠加长 Unicode 文件名后触发 Win32 MAX_PATH。
            safe_name = f"{str(source['sha256'])[:16]}{original.suffix.lower()}"
            staged_path = (
                cfg.send_dir / "outbound" / day / mail_id / "attachments"
                / f"{index:03d}_{safe_name}"
            )
            atomic_copy_file(original, staged_path)
            staged_size = staged_path.stat().st_size
            staged_sha = sha256_of_file(staged_path)
            if staged_size != source["size_bytes"] or staged_sha != source["sha256"]:
                raise OSError(f"附件受控副本校验不一致：{source['display_name']}")
            item = {
                **source,
                "resource_id": resource_id,
                "staged_path": staged_path,
                "staged_sha256": staged_sha,
                "sort_order": index,
            }
            staged_resources.append(item)
            upsert_outbound_resource(
                cfg.db_path,
                resource_id=resource_id,
                outbound_id=mail_id,
                display_name=str(source["display_name"]),
                mime_type=str(source["mime_type"]),
                source_path=str(original),
                staged_path=str(staged_path),
                sent_archive_path=None,
                size_bytes=int(source["size_bytes"]),
                sha256=str(source["sha256"]),
                staged_sha256=staged_sha,
                sent_archive_sha256=None,
                status="staged",
                error=None,
                sort_order=index,
            )
    except Exception as exc:  # noqa: BLE001
        error = f"创建附件受控副本失败：{exc}"
        update_outbound_message(cfg.db_path, mail_id, status="failed", error=error)
        return {
            **_send_error_result("", "staging_failed", error),
            "outbound_id": mail_id,
            "subject": actual_subject,
        }

    try:
        message = _build_outbound_email(
            cfg=cfg,
            subject=actual_subject,
            body_text=clean_body,
            resources=staged_resources,
            links=normalized_links,
            recipient=target_recipient,
            outbound_id=mail_id,
        )
        _smtp_send_with_stage(cfg, message)
    except Exception as exc:  # noqa: BLE001
        stage = exc.stage if isinstance(exc, SmtpStageError) else "send"
        error = str(exc) if isinstance(exc, SmtpStageError) else f"构建或发送邮件失败：{exc}"
        update_outbound_message(cfg.db_path, mail_id, status="failed", error=error)
        for item in staged_resources:
            upsert_outbound_resource(
                cfg.db_path,
                resource_id=item["resource_id"], outbound_id=mail_id,
                display_name=item["display_name"], mime_type=item["mime_type"],
                source_path=str(item["path"]), staged_path=str(item["staged_path"]),
                sent_archive_path=None, size_bytes=item["size_bytes"],
                sha256=item["sha256"], staged_sha256=item["staged_sha256"],
                sent_archive_sha256=None, status="failed", error=error,
                sort_order=item["sort_order"],
            )
            insert_sent_file(
                cfg.db_path,
                source_path=str(item["path"]), send_copy_path=str(item["staged_path"]),
                sent_copy_path=None, sha256=item["sha256"], subject=actual_subject,
                from_email=_smtp_sender(cfg), to_email=target_recipient, sent_at=None,
                status="failed", error_message=error,
                original_filename=item["display_name"], size_bytes=item["size_bytes"],
                source_origin=source_origin, source_sha256=item["sha256"],
                staged_sha256=item["staged_sha256"],
                attachment_sha256=item["staged_sha256"],
                outbound_id=mail_id, outbound_resource_id=item["resource_id"],
                from_account_id=current_send_account_id(cfg),
            )
        return {
            **_send_error_result("", f"smtp_{stage}_failed", error),
            "outbound_id": mail_id,
            "subject": actual_subject,
        }

    sent_at = fmt_datetime(now_local())
    archive_errors: list[str] = []
    for item in staged_resources:
        archive_path = (
            cfg.sent_dir / "outbound" / day / mail_id / "attachments"
            / Path(item["staged_path"]).name
        )
        archive_sha = ""
        resource_status = "sent"
        resource_error: str | None = None
        try:
            atomic_copy_file(item["staged_path"], archive_path)
            archive_sha = sha256_of_file(archive_path)
            if archive_path.stat().st_size != item["size_bytes"] or archive_sha != item["staged_sha256"]:
                raise OSError("发送归档与 SMTP 附件来源校验不一致")
        except Exception as exc:  # noqa: BLE001
            resource_status = "sent_archive_failed"
            resource_error = str(exc)
            archive_errors.append(f"{item['display_name']}：{exc}")
            archive_path = Path()
        upsert_outbound_resource(
            cfg.db_path,
            resource_id=item["resource_id"], outbound_id=mail_id,
            display_name=item["display_name"], mime_type=item["mime_type"],
            source_path=str(item["path"]), staged_path=str(item["staged_path"]),
            sent_archive_path=str(archive_path) if str(archive_path) not in {"", "."} else None,
            size_bytes=item["size_bytes"], sha256=item["sha256"],
            staged_sha256=item["staged_sha256"],
            sent_archive_sha256=archive_sha or None,
            status=resource_status, error=resource_error,
            sort_order=item["sort_order"],
        )
        insert_sent_file(
            cfg.db_path,
            source_path=str(item["path"]), send_copy_path=str(item["staged_path"]),
            sent_copy_path=str(archive_path) if str(archive_path) not in {"", "."} else None,
            sha256=item["sha256"], subject=actual_subject,
            from_email=_smtp_sender(cfg), to_email=target_recipient, sent_at=sent_at,
            status=resource_status, error_message=resource_error,
            original_filename=item["display_name"], size_bytes=item["size_bytes"],
            source_origin=source_origin, source_sha256=item["sha256"],
            staged_sha256=item["staged_sha256"],
            attachment_sha256=item["staged_sha256"],
            sent_archive_sha256=archive_sha or None,
            outbound_id=mail_id, outbound_resource_id=item["resource_id"],
            from_account_id=current_send_account_id(cfg),
        )

    final_status = "partial" if archive_errors else "sent"
    error_text = "；".join(archive_errors) if archive_errors else None
    update_outbound_message(
        cfg.db_path, mail_id, status=final_status, sent_at=sent_at, error=error_text
    )
    log_event(
        cfg.db_path,
        "WARNING" if archive_errors else "SUCCESS",
        "send",
        f"邮件发送完成：{len(staged_resources)} 个附件，{len(normalized_links)} 个链接",
    )
    return {
        "ok": True,
        "status": "partial" if archive_errors else "success",
        "send_status": "sent_archive_failed" if archive_errors else "sent",
        "outbound_id": mail_id,
        "subject": actual_subject,
        "body_text": clean_body,
        "to": target_recipient,
        "sent_at": sent_at,
        "attachment_count": len(staged_resources),
        "link_count": len(normalized_links),
        "error": error_text or "发送完成",
    }


def _normalize_outbound_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("链接格式无效") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("链接必须是完整的 HTTP 或 HTTPS 地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("链接端口无效") from exc
    host = parsed.hostname.rstrip(".").casefold()
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def _build_outbound_email(
    *,
    cfg: AppConfig,
    subject: str,
    body_text: str,
    resources: list[dict[str, Any]],
    links: list[dict[str, str]],
    recipient: str,
    outbound_id: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = _smtp_sender(cfg)
    message["To"] = normalize_manual_recipient(recipient)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message[OUTBOUND_ORIGIN_HEADER] = "outbound"
    message[OUTBOUND_ID_HEADER] = _validate_outbound_id(outbound_id)
    sections: list[str] = []
    if body_text:
        sections.append(body_text.rstrip())
    if links:
        link_lines = ["相关链接："]
        for item in links:
            label = item.get("display_text", "").strip()
            link_lines.append(
                f"- {label}：{item['url']}" if label else f"- {item['url']}"
            )
        sections.append("\n".join(link_lines))
    message.set_content("\n\n".join(sections))
    for item in resources:
        staged = Path(item["staged_path"])
        data = staged.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != int(item["size_bytes"]) or digest != item["staged_sha256"]:
            raise SmtpStageError("send", f"附件发送前校验失败：{item['display_name']}")
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        maintype, subtype = mime_type.split("/", 1)
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=str(item["display_name"]),
        )
    return message


# ============================================================
# 内部：构建邮件
# ============================================================

def _build_email(
    *,
    cfg: AppConfig,
    subject: str,
    file_path: Path,
    source_name: str,
    outbound_id: str | None = None,
) -> EmailMessage:
    """构建 MIME 邮件。

    - .md / .txt：内容同时作为正文，附件也保留。
    - 其他类型：正文写简单说明，文件作为附件。
    """
    msg = EmailMessage()
    msg["From"] = _smtp_sender(cfg)
    msg["To"] = cfg.owner_gmail
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg[OUTBOUND_ORIGIN_HEADER] = "outbound"
    msg[OUTBOUND_ID_HEADER] = _validate_outbound_id(
        outbound_id or f"out_{uuid.uuid4().hex}"
    )

    stem, ext = split_ext(source_name)

    if ext in (".md", ".txt"):
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        msg.set_content(content)
    else:
        msg.set_content(
            f"这是 Agent Mail Bridge 发送的文件：{source_name}。\n"
            f"请查看附件。\n\n"
            f"发件身份：{_smtp_sender(cfg)}\n"
            f"接收邮箱：{cfg.owner_gmail}\n"
        )

    # 添加附件（无论正文类型，附件都保留）
    mime_type, _ = mimetypes.guess_type(source_name)
    if mime_type is None:
        maintype, subtype = "application", "octet-stream"
    else:
        maintype, subtype = mime_type.split("/", 1)

    with open(file_path, "rb") as f:
        file_data = f.read()
    msg.add_attachment(
        file_data,
        maintype=maintype,
        subtype=subtype,
        filename=source_name,
    )
    return msg


# ============================================================
# 内部：SMTP 发送
# ============================================================

def _smtp_send(cfg: AppConfig, msg: EmailMessage) -> None:
    """使用账号级 Provider-neutral SMTP 发送邮件。"""
    _smtp_send_with_stage(cfg, msg)


def _smtp_send_with_stage(cfg: AppConfig, msg: EmailMessage) -> None:
    """分阶段执行 SMTP，便于 GUI 给出准确错误。"""
    outgoing = effective_outgoing_runtime(cfg)
    if outgoing.security not in {"ssl", "starttls"}:
        raise SmtpStageError("tls", "SMTP 只允许 SSL/TLS 或 STARTTLS")
    context = ssl.create_default_context()
    try:
        if outgoing.security == "ssl":
            server = smtplib.SMTP_SSL(
                outgoing.host,
                outgoing.port,
                timeout=outgoing.connect_timeout,
                context=context,
            )
        else:
            server = smtplib.SMTP(
                outgoing.host,
                outgoing.port,
                timeout=outgoing.connect_timeout,
            )
    except Exception as exc:  # noqa: BLE001
        stage = _classify_smtp_error(exc, default_stage="connect")
        raise SmtpStageError(stage, _smtp_error_message(stage)) from exc
    try:
        try:
            server.ehlo()
            if outgoing.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
        except Exception as exc:  # noqa: BLE001
            stage = _classify_smtp_error(exc, default_stage="tls")
            raise SmtpStageError(stage, _smtp_error_message(stage)) from exc
        try:
            server.login(outgoing.username, outgoing.secret)
        except Exception as exc:  # noqa: BLE001
            stage = _classify_smtp_error(exc, default_stage="auth")
            raise SmtpStageError(stage, _smtp_error_message(stage)) from exc
        try:
            server.send_message(
                msg,
                from_addr=outgoing.username,
                to_addrs=[str(msg.get("To") or "")],
            )
        except Exception as exc:  # noqa: BLE001
            stage = _classify_smtp_error(exc, default_stage="send")
            raise SmtpStageError(stage, _smtp_error_message(stage)) from exc
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass
    logger.info("SMTP 发送完成")


def _classify_smtp_error(exc: Exception, *, default_stage: str) -> str:
    if isinstance(exc, (ssl.SSLError, smtplib.SMTPNotSupportedError)):
        return "tls"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(
        exc,
        (
            smtplib.SMTPServerDisconnected,
            ConnectionAbortedError,
            ConnectionResetError,
        ),
    ):
        return "disconnected"
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror)):
        return "server_unavailable"
    codes = _smtp_response_codes(exc)
    has_temporary = any(400 <= code < 500 for code in codes)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "temporary" if has_temporary else "auth"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "temporary" if has_temporary else "recipient_rejected"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "temporary" if has_temporary else "sender_rejected"
    if 421 in codes:
        return "server_unavailable"
    if has_temporary:
        return "temporary"
    if 552 in codes or _smtp_has_enhanced_status(exc, "5.3.4"):
        return "message_too_large"
    if any(500 <= code < 600 for code in codes):
        return "permanent"
    return default_stage


def _smtp_response_codes(exc: Exception) -> tuple[int, ...]:
    values: list[Any] = [getattr(exc, "smtp_code", 0)]
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        values.extend(
            response[0]
            for response in exc.recipients.values()
            if isinstance(response, tuple) and response
        )
    result: list[int] = []
    for value in values:
        try:
            code = int(value or 0)
        except (TypeError, ValueError):
            continue
        if code:
            result.append(code)
    return tuple(result)


def _smtp_has_enhanced_status(exc: Exception, status: str) -> bool:
    values: list[Any] = [getattr(exc, "smtp_error", b"")]
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        values.extend(
            response[1]
            for response in exc.recipients.values()
            if isinstance(response, tuple) and len(response) > 1
        )
    pattern = re.compile(rf"(?<!\d){re.escape(status)}(?!\d)")
    return any(
        pattern.search(
            value.decode("ascii", errors="ignore")
            if isinstance(value, bytes)
            else str(value)
        )
        for value in values
    )


def _smtp_error_message(stage: str) -> str:
    return {
        "auth": "SMTP 认证失败，请检查账号授权码或应用专用密码",
        "recipient_rejected": "SMTP 服务器拒绝收件人",
        "sender_rejected": "SMTP 服务器拒绝发件身份",
        "tls": "SMTP TLS 协商失败",
        "timeout": "SMTP 连接超时",
        "disconnected": "SMTP 连接意外断开，请稍后重试",
        "message_too_large": "SMTP 服务器拒绝超大邮件",
        "server_unavailable": "SMTP 服务器暂时不可用",
        "temporary": "SMTP 服务器返回临时错误，请稍后重试",
        "permanent": "SMTP 服务器永久拒绝本次发送，请检查邮件内容和账号策略",
        "connect": "SMTP 连接失败",
        "send": "SMTP 发送失败",
    }.get(stage, "SMTP 操作失败")


def send_file_with_request(
    file_path: str | Path,
    *,
    request_id: str,
    subject: str | None,
    cfg: AppConfig,
    attachment_name: str | None = None,
    source_origin: str = "controlled",
    source_sha256: str | None = None,
    staged_sha256: str | None = None,
    original_source_path: str | Path | None = None,
) -> dict[str, Any]:
    """按 request_id 幂等发送，并准确区分 SMTP 与归档状态。"""
    try:
        source_path = assert_within_allowed_roots(
            Path(file_path), cfg.effective_allowed_send_roots
        )
    except SecurityError as exc:
        return _send_error_result(request_id, "path_not_allowed", str(exc))

    if not source_path.exists() or not source_path.is_file():
        return _send_error_result(request_id, "file_not_found", "待发送文件不存在")
    if is_dangerous(source_path.name):
        return _send_error_result(request_id, "file_type_not_allowed", "危险扩展名文件禁止发送")
    size_bytes = source_path.stat().st_size
    if not check_size_ok(size_bytes, cfg.max_send_file_bytes):
        return _send_error_result(request_id, "file_too_large", "文件超过发送大小限制")
    try:
        require_send_config(cfg)
    except ConfigError as exc:
        return _send_error_result(request_id, "configuration_error", str(exc))

    sha = sha256_of_file(source_path)
    source_sha = source_sha256 or sha
    staged_sha = staged_sha256 or sha
    if sha != staged_sha:
        return _send_error_result(
            request_id, "staged_hash_mismatch", "受控 staging 文件 Hash 已变化，已阻止发送"
        )
    confirmed_name = attachment_name or source_path.name
    actual_subject = subject or f"Agent执行结果 - {confirmed_name}"
    attempt_state, previous = create_or_retry_send_attempt(
        cfg.db_path,
        request_id=request_id,
        source_path=str(source_path),
        sha256=sha,
        subject=actual_subject,
        from_email=_smtp_sender(cfg),
        to_email=cfg.owner_gmail,
        original_filename=confirmed_name,
        size_bytes=size_bytes,
        source_origin=source_origin,
        source_sha256=source_sha,
        staged_sha256=staged_sha,
        from_account_id=current_send_account_id(cfg),
    )
    existing_outbound = get_outbound_by_request_id(cfg.db_path, request_id)
    outbound_id = str(
        (existing_outbound or {}).get("outbound_id") or _outbound_id_for_request(request_id)
    )
    outbound_resource_id = _outbound_resource_id(outbound_id, 1)
    outbound_origin = "agent_mcp" if source_origin in {"mcp_staged", "controlled"} else source_origin
    default_body = f"Agent 已通过 AgentMailBridge 交付文件：{confirmed_name}。"
    initial_outbound_status = (
        "sent" if str(previous.get("status") or "") == "sent"
        else "partial" if str(previous.get("status") or "") == "sent_archive_failed"
        else "sending"
    )
    create_outbound_message(
        cfg.db_path,
        outbound_id=outbound_id,
        sender_account_ref=_sender_account_ref(cfg),
        from_account_id=current_send_account_id(cfg),
        sender_ref=_smtp_sender(cfg),
        source_origin=outbound_origin,
        request_id=request_id,
        subject=actual_subject,
        body_text=default_body,
        to_emails=[cfg.owner_gmail],
        attachment_count=1,
        link_count=0,
        status=initial_outbound_status,
    )
    if attempt_state != "duplicate":
        update_outbound_message(cfg.db_path, outbound_id, status="sending", error=None)
    upsert_outbound_resource(
        cfg.db_path,
        resource_id=outbound_resource_id,
        outbound_id=outbound_id,
        display_name=confirmed_name,
        mime_type=mimetypes.guess_type(confirmed_name)[0] or "application/octet-stream",
        source_path=str(Path(original_source_path).resolve()) if original_source_path else str(source_path),
        staged_path=str(source_path),
        sent_archive_path=previous.get("sent_copy_path"),
        size_bytes=size_bytes,
        sha256=source_sha,
        staged_sha256=staged_sha,
        sent_archive_sha256=previous.get("sent_archive_sha256"),
        status=initial_outbound_status if attempt_state == "duplicate" else "staged",
        error=previous.get("error_message") if attempt_state == "duplicate" else None,
        sort_order=1,
    )
    link_sent_file_to_outbound(
        cfg.db_path,
        outbound_id=outbound_id,
        resource_id=outbound_resource_id,
        request_id=request_id,
    )
    if attempt_state == "duplicate":
        return {
            "ok": False,
            "status": "duplicate",
            "send_status": "duplicate",
            "request_id": request_id,
            "error_code": "duplicate_request",
            "error": "相同发送请求已执行或正在执行，未重复发信",
            "previous_status": previous.get("status"),
            "source_path": previous.get("source_path", ""),
            "send_copy_path": previous.get("send_copy_path", ""),
            "sent_copy_path": previous.get("sent_copy_path", ""),
            "subject": previous.get("subject", actual_subject),
            "to": previous.get("to_email", cfg.owner_gmail),
            "sent_at": previous.get("sent_at", ""),
            "filename": previous.get("original_filename") or confirmed_name,
            "size_bytes": int(previous.get("size_bytes") or size_bytes),
            "source_sha256": previous.get("source_sha256") or previous.get("sha256") or source_sha,
            "staged_sha256": previous.get("staged_sha256") or previous.get("sha256") or staged_sha,
            "attachment_pre_smtp_sha256": previous.get("attachment_sha256") or "",
            "sent_archive_sha256": previous.get("sent_archive_sha256") or "",
            "outbound_id": outbound_id,
        }

    now = now_local()
    try:
        send_copy_path = build_send_copy_path(cfg, source_path, now)
        copy_file(source_path, send_copy_path)
        attachment_sha = sha256_of_file(send_copy_path)
        if send_copy_path.stat().st_size != size_bytes or attachment_sha != staged_sha:
            raise OSError("send 副本与受控 staging 文件校验不一致")
    except Exception as exc:  # noqa: BLE001
        error = f"创建 send 副本失败：{exc}"
        update_send_attempt(
            cfg.db_path, request_id, status="failed", error_message=error
        )
        update_outbound_message(cfg.db_path, outbound_id, status="failed", error=error)
        upsert_outbound_resource(
            cfg.db_path,
            resource_id=outbound_resource_id, outbound_id=outbound_id,
            display_name=confirmed_name,
            mime_type=mimetypes.guess_type(confirmed_name)[0] or "application/octet-stream",
            source_path=str(Path(original_source_path).resolve()) if original_source_path else str(source_path),
            staged_path=str(source_path), sent_archive_path=None,
            size_bytes=size_bytes, sha256=source_sha, staged_sha256=staged_sha,
            sent_archive_sha256=None, status="failed", error=error, sort_order=1,
        )
        return {
            **_send_error_result(request_id, "file_copy_failed", error),
            "outbound_id": outbound_id,
        }

    try:
        message = _build_email(
            cfg=cfg,
            subject=actual_subject,
            file_path=send_copy_path,
            source_name=confirmed_name,
            outbound_id=outbound_id,
        )
        _smtp_send_with_stage(cfg, message)
    except SmtpStageError as exc:
        error = str(exc)
        update_send_attempt(
            cfg.db_path, request_id, status="failed",
            send_copy_path=str(send_copy_path), error_message=error,
            attachment_sha256=attachment_sha,
        )
        update_outbound_message(cfg.db_path, outbound_id, status="failed", error=error)
        upsert_outbound_resource(
            cfg.db_path,
            resource_id=outbound_resource_id, outbound_id=outbound_id,
            display_name=confirmed_name,
            mime_type=mimetypes.guess_type(confirmed_name)[0] or "application/octet-stream",
            source_path=str(Path(original_source_path).resolve()) if original_source_path else str(source_path),
            staged_path=str(send_copy_path), sent_archive_path=None,
            size_bytes=size_bytes, sha256=source_sha, staged_sha256=attachment_sha,
            sent_archive_sha256=None, status="failed", error=error, sort_order=1,
        )
        return {
            **_send_error_result(request_id, f"smtp_{exc.stage}_failed", error),
            "outbound_id": outbound_id,
        }

    sent_at = fmt_datetime(now_local())
    update_send_attempt(
        cfg.db_path, request_id, status="sent",
        send_copy_path=str(send_copy_path), sent_at=sent_at,
        attachment_sha256=attachment_sha,
    )
    try:
        sent_copy_path = build_sent_copy_path(cfg, source_path, now)
        copy_file(send_copy_path, sent_copy_path)
        archive_sha = sha256_of_file(sent_copy_path)
        if sent_copy_path.stat().st_size != size_bytes or archive_sha != attachment_sha:
            raise OSError("sent 归档与 SMTP 附件来源校验不一致")
    except Exception as exc:  # noqa: BLE001
        error = f"SMTP 已发送，但本地 sent 归档失败：{exc}"
        update_send_attempt(
            cfg.db_path, request_id, status="sent_archive_failed",
            sent_at=sent_at, error_message=error,
            attachment_sha256=attachment_sha,
        )
        update_outbound_message(
            cfg.db_path, outbound_id, status="partial", sent_at=sent_at, error=error
        )
        upsert_outbound_resource(
            cfg.db_path,
            resource_id=outbound_resource_id, outbound_id=outbound_id,
            display_name=confirmed_name,
            mime_type=mimetypes.guess_type(confirmed_name)[0] or "application/octet-stream",
            source_path=str(Path(original_source_path).resolve()) if original_source_path else str(source_path),
            staged_path=str(send_copy_path), sent_archive_path=None,
            size_bytes=size_bytes, sha256=source_sha, staged_sha256=attachment_sha,
            sent_archive_sha256=None, status="sent_archive_failed", error=error,
            sort_order=1,
        )
        return {
            "ok": True,
            "status": "partial",
            "send_status": "sent_archive_failed",
            "request_id": request_id,
            "error_code": "sent_archive_failed",
            "error": error,
            "source_path": str(source_path),
            "send_copy_path": str(send_copy_path),
            "sent_copy_path": "",
            "subject": actual_subject,
            "to": cfg.owner_gmail,
            "sent_at": sent_at,
            "filename": confirmed_name,
            "size_bytes": size_bytes,
            "source_sha256": source_sha,
            "staged_sha256": staged_sha,
            "attachment_pre_smtp_sha256": attachment_sha,
            "sent_archive_sha256": "",
            "outbound_id": outbound_id,
        }

    update_send_attempt(
        cfg.db_path, request_id, status="sent",
        sent_copy_path=str(sent_copy_path), sent_at=sent_at,
        attachment_sha256=attachment_sha,
        sent_archive_sha256=archive_sha,
    )
    update_outbound_message(
        cfg.db_path, outbound_id, status="sent", sent_at=sent_at, error=None
    )
    upsert_outbound_resource(
        cfg.db_path,
        resource_id=outbound_resource_id, outbound_id=outbound_id,
        display_name=confirmed_name,
        mime_type=mimetypes.guess_type(confirmed_name)[0] or "application/octet-stream",
        source_path=str(Path(original_source_path).resolve()) if original_source_path else str(source_path),
        staged_path=str(send_copy_path), sent_archive_path=str(sent_copy_path),
        size_bytes=size_bytes, sha256=source_sha, staged_sha256=attachment_sha,
        sent_archive_sha256=archive_sha, status="sent", error=None, sort_order=1,
    )
    return {
        "ok": True,
        "status": "success",
        "send_status": "sent",
        "request_id": request_id,
        "source_path": str(source_path),
        "send_copy_path": str(send_copy_path),
        "sent_copy_path": str(sent_copy_path),
        "subject": actual_subject,
        "to": cfg.owner_gmail,
        "sent_at": sent_at,
        "filename": confirmed_name,
        "size_bytes": size_bytes,
        "source_sha256": source_sha,
        "staged_sha256": staged_sha,
        "attachment_pre_smtp_sha256": attachment_sha,
        "sent_archive_sha256": archive_sha,
        "outbound_id": outbound_id,
    }


def _send_error_result(request_id: str, error_code: str, message: str) -> dict[str, Any]:
    """构造未发送或发送失败结果。"""
    send_status = (
        "not_sent"
        if error_code in {
            "configuration_error",
            "path_not_allowed",
            "file_not_found",
            "file_type_not_allowed",
            "file_too_large",
            "total_size_too_large",
            "file_copy_failed",
            "staged_hash_mismatch",
            "invalid_link",
            "empty_message",
            "staging_failed",
        }
        else "failed"
    )
    return {
        "ok": False,
        "status": "failed",
        "send_status": send_status,
        "request_id": request_id,
        "error_code": error_code,
        "error": message,
    }


def _record_failure(
    cfg: AppConfig,
    source_path: Path,
    send_copy_path: Path,
    sha: str,
    subject: str,
    err: str,
) -> None:
    """发送失败时记录到 sent_files（status=failed）。"""
    sent_at = fmt_datetime(now_local())
    insert_sent_file(
        cfg.db_path,
        source_path=str(source_path),
        send_copy_path=str(send_copy_path),
        sent_copy_path=None,
        sha256=sha,
        subject=subject,
        from_email=_smtp_sender(cfg),
        to_email=cfg.owner_gmail,
        sent_at=sent_at,
        status="failed",
        error_message=err,
        from_account_id=current_send_account_id(cfg),
    )
