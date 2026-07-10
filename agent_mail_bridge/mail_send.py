"""QQ SMTP 发件模块。

职责：
1. 校验待发送文件存在、大小合理、扩展名可发送。
2. 计算 sha256，复制到 send/YYYY-MM-DD/。
3. 使用 QQ SMTP 以 QQ 邮箱身份发送到 OWNER_GMAIL（收件人固定）。
4. 发送成功后复制到 sent/YYYY-MM-DD/。
5. 写入 sent_files 表与日志。
6. 返回结构化结果。

注意：收件人固定为 OWNER_GMAIL，不允许任意传 to。
日志中绝不打印完整 QQ 授权码。
"""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig, ConfigError, require_send_config
from agent_mail_bridge.database import (
    create_or_retry_send_attempt,
    insert_sent_file,
    log_event,
    update_send_attempt,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.security import (
    SecurityError,
    assert_within_allowed_roots,
    check_size_ok,
    is_dangerous,
)
from agent_mail_bridge.storage import (
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
        )
        _smtp_send(cfg, msg_obj)
    except smtplib.SMTPAuthenticationError as exc:
        err = f"SMTP 认证失败：{exc}。请检查 QQ_EMAIL 与 QQ_AUTH_CODE（授权码，非QQ登录密码）。"
        logger.error(err)
        log_event(cfg.db_path, "ERROR", "send", err)
        _record_failure(cfg, source_path, send_copy_path, sha, subject, err)
        return {"ok": False, "error": err}
    except Exception as exc:  # noqa: BLE001
        err = f"发送失败：{exc}"
        logger.exception("SMTP 发送异常")
        log_event(cfg.db_path, "ERROR", "send", err)
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
        from_email=cfg.qq_email,
        to_email=cfg.owner_gmail,
        sent_at=sent_at,
        status="sent",
        error_message=None,
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
    }


# ============================================================
# 内部：构建邮件
# ============================================================

def _build_email(
    *,
    cfg: AppConfig,
    subject: str,
    file_path: Path,
    source_name: str,
) -> EmailMessage:
    """构建 MIME 邮件。

    - .md / .txt：内容同时作为正文，附件也保留。
    - 其他类型：正文写简单说明，文件作为附件。
    """
    msg = EmailMessage()
    msg["From"] = cfg.qq_email
    msg["To"] = cfg.owner_gmail
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

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
            f"发件身份：{cfg.qq_email}\n"
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
    """使用 QQ SMTP SSL 发送邮件。"""
    ctx = ssl.create_default_context()
    # 连接超时直接传给底层 SMTP_SSL，单位为秒。
    with smtplib.SMTP_SSL(
        cfg.qq_smtp_host,
        cfg.qq_smtp_port,
        timeout=cfg.qq_smtp_connect_timeout,
        context=ctx,
    ) as server:
        server.login(cfg.qq_email, cfg.qq_auth_code)
        server.send_message(msg)
    logger.info("SMTP 发送完成")


def _smtp_send_with_stage(cfg: AppConfig, msg: EmailMessage) -> None:
    """分阶段执行 SMTP，便于 GUI 给出准确错误。"""
    context = ssl.create_default_context()
    try:
        server = smtplib.SMTP_SSL(
            cfg.qq_smtp_host, cfg.qq_smtp_port,
            timeout=cfg.qq_smtp_connect_timeout, context=context,
        )
    except Exception as exc:  # noqa: BLE001
        raise SmtpStageError("connect", f"QQ SMTP 连接失败：{exc}") from exc
    try:
        try:
            server.login(cfg.qq_email, cfg.qq_auth_code)
        except Exception as exc:  # noqa: BLE001
            raise SmtpStageError("auth", f"QQ SMTP 认证失败：{exc}") from exc
        try:
            server.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            raise SmtpStageError("send", f"QQ SMTP 发送失败：{exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_file_with_request(
    file_path: str | Path,
    *,
    request_id: str,
    subject: str | None,
    cfg: AppConfig,
) -> dict[str, Any]:
    """按 request_id 幂等发送，并准确区分 SMTP 与归档状态。"""
    try:
        require_send_config(cfg)
    except ConfigError as exc:
        return _send_error_result(request_id, "configuration_error", str(exc))
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

    sha = sha256_of_file(source_path)
    actual_subject = subject or f"Agent执行结果 - {source_path.name}"
    attempt_state, previous = create_or_retry_send_attempt(
        cfg.db_path,
        request_id=request_id,
        source_path=str(source_path),
        sha256=sha,
        subject=actual_subject,
        from_email=cfg.qq_email,
        to_email=cfg.owner_gmail,
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
        }

    now = now_local()
    try:
        send_copy_path = build_send_copy_path(cfg, source_path, now)
        copy_file(source_path, send_copy_path)
    except Exception as exc:  # noqa: BLE001
        error = f"创建 send 副本失败：{exc}"
        update_send_attempt(
            cfg.db_path, request_id, status="failed", error_message=error
        )
        return _send_error_result(request_id, "file_copy_failed", error)

    try:
        message = _build_email(
            cfg=cfg,
            subject=actual_subject,
            file_path=send_copy_path,
            source_name=source_path.name,
        )
        _smtp_send_with_stage(cfg, message)
    except SmtpStageError as exc:
        error = str(exc)
        update_send_attempt(
            cfg.db_path, request_id, status="failed",
            send_copy_path=str(send_copy_path), error_message=error,
        )
        return _send_error_result(request_id, f"smtp_{exc.stage}_failed", error)

    sent_at = fmt_datetime(now_local())
    update_send_attempt(
        cfg.db_path, request_id, status="sent",
        send_copy_path=str(send_copy_path), sent_at=sent_at,
    )
    try:
        sent_copy_path = build_sent_copy_path(cfg, source_path, now)
        copy_file(send_copy_path, sent_copy_path)
    except Exception as exc:  # noqa: BLE001
        error = f"SMTP 已发送，但本地 sent 归档失败：{exc}"
        update_send_attempt(
            cfg.db_path, request_id, status="sent_archive_failed",
            sent_at=sent_at, error_message=error,
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
        }

    update_send_attempt(
        cfg.db_path, request_id, status="sent",
        sent_copy_path=str(sent_copy_path), sent_at=sent_at,
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
            "file_copy_failed",
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
        from_email=cfg.qq_email,
        to_email=cfg.owner_gmail,
        sent_at=sent_at,
        status="failed",
        error_message=err,
    )
