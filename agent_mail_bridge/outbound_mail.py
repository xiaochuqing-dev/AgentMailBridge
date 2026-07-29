"""通用 Agent 新建、回复、回复全部、转发与 exact-MIME 发送。"""

from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
import os
import shutil
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from email_validator import EmailNotValidError, validate_email

from agent_mail_bridge.config import AppConfig, effective_incoming_runtime
from agent_mail_bridge.database import query_mailboxes
from agent_mail_bridge.mail_archive import archive_normalized_mail, stable_account_ref
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_send import (
    OUTBOUND_ID_HEADER,
    OUTBOUND_ORIGIN_HEADER,
    SmtpStageError,
    smtp_send_bytes_with_stage,
)
from agent_mail_bridge.mail_threading import record_thread_relations
from agent_mail_bridge.provider_foundation import (
    ProviderFoundationError,
    append_imap_message,
    sent_copy_profile,
)
from agent_mail_bridge.security import (
    assert_not_sensitive_delivery_file,
    assert_within_root,
    check_size_ok,
    is_dangerous,
)
from agent_mail_bridge.send_requests import (
    SendRequestError,
    claim_for_send,
    complete_send_request,
    create_send_request,
    get_by_idempotency,
    get_send_request,
    new_send_request_id,
    public_send_request,
    record_outbound_fact,
    record_send_mime,
)
from agent_mail_bridge.storage import atomic_copy_file
from agent_mail_bridge.utils import (
    fmt_datetime,
    now_local,
    sanitize_filename,
    sha256_of_file,
)


OPERATIONS = {"new", "reply", "reply_all", "forward"}
MAX_BODY_CHARS = 2_000_000
DEFAULT_CONFIRM_TTL_SECONDS = 3600
logger = logging.getLogger(__name__)


class OutboundMailError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def prepare_agent_send(
    cfg: AppConfig,
    *,
    client_id: str,
    send_mode: str,
    sender_account_id: str,
    sender_address: str,
    sender_display_name: str,
    idempotency_key: str,
    operation: str,
    to: Iterable[Any] = (),
    cc: Iterable[Any] = (),
    bcc: Iterable[Any] = (),
    subject: str = "",
    body_text: str = "",
    body_html: str = "",
    links: Iterable[dict[str, Any] | str] = (),
    source_message: dict[str, Any] | None = None,
    attachments: Iterable[dict[str, Any]] = (),
    confirm_ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS,
) -> tuple[dict[str, Any], bool]:
    """验证语义并创建 durable 请求；confirm 阶段绝不接触 SMTP。"""
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200 or any(ord(char) < 32 for char in key):
        raise OutboundMailError(
            "invalid_idempotency_key", "idempotency_key 必须是 1-200 字符"
        )
    existing = get_by_idempotency(cfg.db_path, client_id, key)
    if existing is not None:
        return public_send_request(existing), False
    action = str(operation or "new").strip().casefold()
    if action not in OPERATIONS:
        raise OutboundMailError("invalid_operation", "不支持的发件动作")
    if action != "new" and not source_message:
        raise OutboundMailError(
            "source_mail_required", "回复、回复全部或转发必须指定源邮件"
        )
    clean_subject = _clean_header(subject, "主题", allow_empty=True)
    clean_body_text = str(body_text or "")
    clean_body_html = str(body_html or "")
    normalized_links = _normalize_links(links)
    clean_body_text, clean_body_html = _append_links(
        clean_body_text,
        clean_body_html,
        normalized_links,
    )
    attachment_inputs = [dict(item) for item in attachments]
    if len(clean_body_text) > MAX_BODY_CHARS or len(clean_body_html) > MAX_BODY_CHARS:
        raise OutboundMailError("message_too_large", "邮件正文超过大小限制")
    sender = _validated_address(sender_address, sender_display_name)
    recipients = _resolve_recipients(
        action,
        to=to,
        cc=cc,
        bcc=bcc,
        sender_address=sender["email_address"],
        source_message=source_message,
    )
    if action in {"reply", "reply_all"}:
        clean_subject = _reply_subject(
            clean_subject or str((source_message or {}).get("subject") or "")
        )
    elif action == "forward":
        clean_subject = _forward_subject(
            clean_subject or str((source_message or {}).get("subject") or "")
        )
    if not clean_subject:
        clean_subject = "无主题"
    if (
        not clean_body_text.strip()
        and not clean_body_html.strip()
        and not attachment_inputs
    ):
        raise OutboundMailError(
            "empty_message", "正文、HTML 和附件不能同时为空"
        )

    request_id = new_send_request_id()
    try:
        attachment_rows = _snapshot_attachments(
            cfg,
            request_id=request_id,
            attachments=attachment_inputs,
        )
    except Exception:
        _remove_request_stage(cfg, request_id)
        raise
    domain = sender["email_address"].rsplit("@", 1)[-1]
    message_id = make_msgid(domain=domain)
    now = now_local()
    expires_at = (
        fmt_datetime(now + timedelta(
            seconds=max(60, min(int(confirm_ttl_seconds), 86_400))
        ))
        if send_mode == "confirm"
        else None
    )
    source_package_id = str((source_message or {}).get("package_id") or "") or None
    try:
        created, is_new = create_send_request(
            cfg.db_path,
            send_request_id=request_id,
            client_id=client_id,
            idempotency_key=key,
            operation=action,
            sender_account_id=sender_account_id,
            source_package_id=source_package_id,
            reply_to_package_id=(
                source_package_id if action in {"reply", "reply_all"} else None
            ),
            forward_from_package_id=(
                source_package_id if action == "forward" else None
            ),
            send_mode=send_mode,
            subject=clean_subject,
            body_text=clean_body_text,
            body_html=clean_body_html,
            status=(
                "pending_confirmation"
                if send_mode == "confirm"
                else "ready_to_send"
            ),
            expires_at=expires_at,
            message_id=message_id,
            in_reply_to_raw=(
                str((source_message or {}).get("message_id") or "")
                if action in {"reply", "reply_all"}
                else ""
            ),
            references_raw=(
                str((source_message or {}).get("references") or "")
                if action in {"reply", "reply_all"}
                else ""
            ),
            recipients=[
                item
                for kind in ("to", "cc", "bcc")
                for item in recipients[kind]
            ],
            attachments=attachment_rows,
        )
    except Exception:
        _remove_request_stage(cfg, request_id)
        raise
    if not is_new:
        _remove_request_stage(cfg, request_id)
    return public_send_request(created), is_new


def execute_agent_send(
    cfg: AppConfig,
    *,
    runtime_cfg: AppConfig,
    send_request_id: str,
    sender_address: str,
    sender_display_name: str,
    authorize: Callable[[dict[str, Any]], None],
    confirmed_by: str | None = None,
) -> dict[str, Any]:
    """取得唯一发送权、复核权限和附件、发送 exact bytes、归档事实。"""
    request, claimed = claim_for_send(
        cfg.db_path, send_request_id, confirmed_by=confirmed_by
    )
    if not claimed:
        return public_send_request(request)
    try:
        authorize(request)
        _verify_request_attachments(cfg, request)
        raw_bytes, envelope_recipients = _build_request_mime(
            request,
            sender_address=sender_address,
            sender_display_name=sender_display_name,
        )
        raw_path = _request_root(cfg, send_request_id) / "raw.eml"
        _atomic_write(raw_path, raw_bytes)
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
        record_send_mime(
            cfg.db_path,
            send_request_id,
            raw_eml_path=str(raw_path),
            raw_eml_sha256=raw_sha,
        )
    except Exception as exc:
        failed = complete_send_request(
            cfg.db_path,
            send_request_id,
            status="failed",
            error_code=getattr(exc, "code", "send_validation_failed"),
            error_message=str(exc),
        )
        return public_send_request(failed)

    try:
        smtp_send_bytes_with_stage(
            runtime_cfg,
            raw_bytes,
            from_addr=sender_address,
            to_addrs=envelope_recipients,
        )
    except SmtpStageError as exc:
        status = "delivery_unknown" if exc.delivery_unknown else "failed"
        failed = complete_send_request(
            cfg.db_path,
            send_request_id,
            status=status,
            delivery_status=status,
            error_code=(
                "delivery_unknown"
                if exc.delivery_unknown
                else f"smtp_{exc.stage}_failed"
            ),
            error_message=str(exc),
        )
        return public_send_request(failed)

    accepted_at = now_local()
    sent_at = fmt_datetime(accepted_at)
    sent_copy = _append_provider_sent_copy(
        runtime_cfg,
        account_id=str(request["sender_account_id"]),
        raw_bytes=raw_bytes,
        sent_at=accepted_at,
    )
    provider_result = (
        "accepted_sent_copy_appended"
        if sent_copy["status"] == "appended"
        else "accepted_sent_copy_failed"
        if sent_copy["status"] == "failed"
        else "accepted"
    )
    outbound_id = _outbound_id(send_request_id)
    grouped = _group_recipients(request)
    account_ref = stable_account_ref(runtime_cfg)
    record_outbound_fact(
        cfg.db_path,
        outbound_id=outbound_id,
        request=request,
        sender_ref=sender_address,
        account_ref=account_ref,
        to_emails=grouped["to"],
        cc_emails=grouped["cc"],
        bcc_emails=grouped["bcc"],
        raw_eml_sha256=raw_sha,
        package_id=None,
        status="sent_archive_pending",
        sent_at=sent_at,
    )

    package_id: str | None = None
    try:
        normalized = normalized_mail_from_raw(
            raw_bytes,
            backend="smtp",
            backend_message_id=outbound_id,
            thread_id="",
            uid="",
            received_at=sent_at,
            saved_date=sent_at[:10],
            max_attachment_bytes=max(
                cfg.max_attachment_bytes, cfg.max_send_file_bytes
            ),
            mailbox_ref=_local_sent_mailbox_ref(cfg, request),
            direction="outbound",
        )
        normalized.bcc_raw = ", ".join(grouped["bcc"])
        normalized.reply_to_package_id = str(
            request.get("reply_to_package_id") or ""
        )
        normalized.forward_from_package_id = str(
            request.get("forward_from_package_id") or ""
        )
        archived = archive_normalized_mail(
            runtime_cfg, normalized, str(request["message_id"])
        )
        package_id = archived.package_id
        if archived.status not in {"ready", "duplicate"}:
            raise OutboundMailError(
                "sent_archive_failed",
                archived.error or "发件事实归档未完整完成",
            )
        record_thread_relations(
            cfg.db_path,
            account_id=str(request["sender_account_id"]),
            package_id=package_id,
            in_reply_to_raw=normalized.in_reply_to_raw,
            references_raw=normalized.references_raw,
            reply_to_package_id=(
                str(request.get("reply_to_package_id") or "") or None
            ),
            forward_from_package_id=(
                str(request.get("forward_from_package_id") or "") or None
            ),
        )
    except Exception as exc:
        record_outbound_fact(
            cfg.db_path,
            outbound_id=outbound_id,
            request=request,
            sender_ref=sender_address,
            account_ref=account_ref,
            to_emails=grouped["to"],
            cc_emails=grouped["cc"],
            bcc_emails=grouped["bcc"],
            raw_eml_sha256=raw_sha,
            package_id=package_id,
            status="sent_archive_failed",
            sent_at=sent_at,
            error=str(exc),
        )
        result = complete_send_request(
            cfg.db_path,
            send_request_id,
            status="sent_archive_failed",
            delivery_status="sent",
            provider_result=provider_result,
            error_code="sent_archive_failed",
            error_message="邮件已发送，但正式归档需要恢复",
            outbound_id=outbound_id,
            package_id=package_id,
        )
        return public_send_request(result)

    record_outbound_fact(
        cfg.db_path,
        outbound_id=outbound_id,
        request=request,
        sender_ref=sender_address,
        account_ref=account_ref,
        to_emails=grouped["to"],
        cc_emails=grouped["cc"],
        bcc_emails=grouped["bcc"],
        raw_eml_sha256=raw_sha,
        package_id=package_id,
        status="sent",
        sent_at=sent_at,
    )
    result = complete_send_request(
        cfg.db_path,
        send_request_id,
        status="sent",
        delivery_status="sent",
        provider_result=provider_result,
        outbound_id=outbound_id,
        package_id=package_id,
    )
    return public_send_request(result)


def _append_provider_sent_copy(
    cfg: AppConfig,
    *,
    account_id: str,
    raw_bytes: bytes,
    sent_at: datetime,
) -> dict[str, Any]:
    """仅为不会自动保存 Sent 的 Provider 追加 exact MIME 副本。"""
    mode, fallback_mailbox = sent_copy_profile(
        str(getattr(cfg, "runtime_provider", "") or "")
    )
    if mode != "imap_append":
        return {"status": "provider_managed"}
    if os.getenv("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE") == "1":
        return {"status": "skipped_test_environment"}
    sent_mailboxes = [
        row
        for row in query_mailboxes(
            cfg.db_path, account_id=account_id, enabled_only=True
        )
        if str(row.get("mailbox_role") or "") == "sent"
        and not str(row.get("external_ref") or "").startswith("local:sent:")
        and str(row.get("discovery_status") or "active") != "missing"
    ]
    mailbox = (
        str(sent_mailboxes[0].get("external_ref") or "").strip()
        if sent_mailboxes
        else fallback_mailbox
    )
    incoming = effective_incoming_runtime(cfg)
    settings = {
        "profile_id": str(getattr(cfg, "runtime_provider", "") or ""),
        "imap_host": incoming.host,
        "imap_port": incoming.port,
        "imap_security": incoming.security,
        "connect_timeout": incoming.connect_timeout,
        "imap_id_enabled": incoming.imap_id_enabled,
    }
    try:
        return append_imap_message(
            settings=settings,
            username=incoming.username,
            secret=incoming.secret,
            folder=mailbox,
            raw_bytes=raw_bytes,
            msg_time=sent_at,
        )
    except ProviderFoundationError as exc:
        logger.warning(
            "SMTP 已接受邮件，但 Provider Sent 保存失败：%s",
            exc.error_code,
        )
        return {"status": "failed", "error_code": exc.error_code}


def _resolve_recipients(
    operation: str,
    *,
    to: Iterable[Any],
    cc: Iterable[Any],
    bcc: Iterable[Any],
    sender_address: str,
    source_message: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    explicit = {
        "to": _validated_addresses(to, "to"),
        "cc": _validated_addresses(cc, "cc"),
        "bcc": _validated_addresses(bcc, "bcc"),
    }
    if operation in {"new", "forward"}:
        result = explicit
    else:
        source = source_message or {}
        reply_target = (
            list(source.get("reply_to") or [])
            or list(source.get("from_addresses") or [])
        )
        result = {
            "to": _validated_addresses(reply_target, "to"),
            "cc": [],
            "bcc": [],
        }
        if operation == "reply_all":
            result["to"].extend(
                _validated_addresses(source.get("to_addresses") or [], "to")
            )
            result["cc"].extend(
                _validated_addresses(source.get("cc_addresses") or [], "cc")
            )
        if any(explicit.values()):
            result = explicit
    own = sender_address.casefold()
    seen: set[str] = set()
    for kind in ("to", "cc", "bcc"):
        deduped: list[dict[str, Any]] = []
        for item in result[kind]:
            address = str(item["email_address"]).casefold()
            if address == own or address in seen:
                continue
            seen.add(address)
            deduped.append({**item, "recipient_type": kind})
        result[kind] = deduped
    if not result["to"] and not result["cc"] and not result["bcc"]:
        raise OutboundMailError("recipient_required", "邮件至少需要一个收件人")
    return result


def _validated_addresses(
    values: Iterable[Any], recipient_type: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in values or ():
        if isinstance(raw, dict):
            display_name = str(
                raw.get("display_name") or raw.get("name") or ""
            )
            address_value = str(
                raw.get("email_address") or raw.get("address") or ""
            )
            parsed = [(display_name, address_value)]
        else:
            text = str(raw or "")
            if "\r" in text or "\n" in text:
                raise OutboundMailError(
                    "header_injection", "收件人字段包含非法换行"
                )
            parsed = getaddresses([text])
        for display_name, address in parsed:
            item = _validated_address(address, display_name)
            item["recipient_type"] = recipient_type
            result.append(item)
    return result


def _validated_address(address: str, display_name: str = "") -> dict[str, str]:
    raw = str(address or "").strip()
    if "\r" in raw or "\n" in raw:
        raise OutboundMailError("header_injection", "邮箱地址包含非法换行")
    try:
        info = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        raise OutboundMailError("invalid_recipient", "邮箱地址格式无效") from exc
    display = _clean_header(display_name, "显示名", allow_empty=True)
    return {
        "display_name": display,
        "email_address": info.normalized,
    }


def _snapshot_attachments(
    cfg: AppConfig,
    *,
    request_id: str,
    attachments: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    root = _request_root(cfg, request_id) / "attachments"
    rows: list[dict[str, Any]] = []
    total_size = 0
    used_names: set[str] = set()
    for index, item in enumerate(attachments, 1):
        source = Path(str(item.get("path") or ""))
        try:
            source = source.resolve(strict=True)
        except OSError as exc:
            raise OutboundMailError(
                "attachment_not_found", "附件不存在"
            ) from exc
        if not source.is_file() or source.is_symlink() or (
            hasattr(source, "is_junction") and source.is_junction()
        ):
            raise OutboundMailError(
                "attachment_not_file", "附件不是普通文件"
            )
        assert_not_sensitive_delivery_file(source)
        if is_dangerous(source.name):
            raise OutboundMailError(
                "file_type_not_allowed", "危险扩展名文件禁止发送"
            )
        size = source.stat().st_size
        if not check_size_ok(size, cfg.max_send_file_bytes):
            raise OutboundMailError("file_too_large", "附件超过发送大小限制")
        total_size += size
        if total_size > cfg.max_send_file_bytes:
            raise OutboundMailError(
                "total_size_too_large", "附件总大小超过发送限制"
            )
        digest = sha256_of_file(source)
        display_name = _clean_header(
            str(item.get("display_name") or source.name),
            "附件名",
            allow_empty=False,
        )
        safe = _unique_name(display_name, used_names)
        target = root / f"{index:03d}_{safe}"
        atomic_copy_file(source, target)
        if target.stat().st_size != size or sha256_of_file(target) != digest:
            raise OutboundMailError(
                "attachment_hash_mismatch", "附件受控快照校验失败"
            )
        rows.append(
            {
                "attachment_id": f"att_{uuid.uuid4().hex}",
                "source_kind": str(item.get("source_kind") or "local"),
                "source_package_id": item.get("source_package_id"),
                "source_resource_id": item.get("source_resource_id"),
                "source_path": str(source),
                "snapshot_path": str(target),
                "display_name": display_name,
                "mime_type": str(
                    item.get("mime_type")
                    or mimetypes.guess_type(display_name)[0]
                    or "application/octet-stream"
                ),
                "size_bytes": size,
                "sha256": digest,
                "status": "ready",
                "sort_order": index,
            }
        )
    return rows


def _verify_request_attachments(
    cfg: AppConfig, request: dict[str, Any]
) -> None:
    total = 0
    for item in request.get("attachments") or []:
        path = Path(str(item.get("snapshot_path") or "")).resolve(strict=True)
        assert_within_root(path, _request_root(cfg, str(request["send_request_id"])))
        if not path.is_file() or path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise OutboundMailError(
                "attachment_not_file", "附件快照不可用"
            )
        size = path.stat().st_size
        digest = sha256_of_file(path)
        if (
            size != int(
                item.get("size_bytes")
                if item.get("size_bytes") is not None
                else -1
            )
            or digest.casefold() != str(item.get("sha256") or "").casefold()
        ):
            raise OutboundMailError(
                "attachment_hash_mismatch", "发送前附件大小或 Hash 已变化"
            )
        total += size
    if total > cfg.max_send_file_bytes:
        raise OutboundMailError(
            "total_size_too_large", "附件总大小超过发送限制"
        )


def _build_request_mime(
    request: dict[str, Any],
    *,
    sender_address: str,
    sender_display_name: str,
) -> tuple[bytes, list[str]]:
    grouped = _group_recipient_items(request)
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr(
        (sender_display_name, sender_address), charset="utf-8"
    )
    if grouped["to"]:
        message["To"] = ", ".join(_format_recipient(item) for item in grouped["to"])
    if grouped["cc"]:
        message["Cc"] = ", ".join(_format_recipient(item) for item in grouped["cc"])
    message["Subject"] = str(request["subject"])
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = str(request["message_id"])
    message[OUTBOUND_ORIGIN_HEADER] = "outbound"
    message[OUTBOUND_ID_HEADER] = _outbound_id(
        str(request["send_request_id"])
    )
    if request.get("reply_to_package_id"):
        source_message_id = str(request.get("in_reply_to_raw") or "")
        if source_message_id:
            message["In-Reply-To"] = source_message_id
            message["References"] = _source_references(request, source_message_id)
    body_text = str(request.get("body_text") or "")
    body_html = str(request.get("body_html") or "")
    message.set_content(body_text or _html_fallback(body_html))
    if body_html:
        message.add_alternative(body_html, subtype="html")
    for item in request.get("attachments") or []:
        path = Path(str(item["snapshot_path"]))
        data = path.read_bytes()
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        maintype, subtype = (
            mime_type.split("/", 1)
            if "/" in mime_type
            else ("application", "octet-stream")
        )
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=str(item["display_name"]),
        )
    raw = message.as_bytes(policy=SMTP)
    envelope = [
        str(item["email_address"])
        for kind in ("to", "cc", "bcc")
        for item in grouped[kind]
    ]
    return raw, envelope


def _group_recipient_items(
    request: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"to": [], "cc": [], "bcc": []}
    for item in request.get("recipients") or []:
        kind = str(item.get("recipient_type") or "")
        if kind in result:
            result[kind].append(dict(item))
    return result


def _group_recipients(request: dict[str, Any]) -> dict[str, list[str]]:
    items = _group_recipient_items(request)
    return {
        key: [str(item["email_address"]) for item in value]
        for key, value in items.items()
    }


def _format_recipient(item: dict[str, Any]) -> str:
    return formataddr(
        (
            str(item.get("display_name") or ""),
            str(item["email_address"]),
        ),
        charset="utf-8",
    )


def _request_root(cfg: AppConfig, request_id: str) -> Path:
    root = cfg.send_dir / "agent_requests" / str(request_id)
    assert_within_root(root, cfg.data_root_path)
    return root


def _remove_request_stage(cfg: AppConfig, request_id: str) -> None:
    root = _request_root(cfg, request_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".amb-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _unique_name(value: str, used: set[str]) -> str:
    path = Path(value.replace("\\", "/").rsplit("/", 1)[-1])
    stem = sanitize_filename(path.stem or "attachment", max_len=80)
    suffix = path.suffix[:20]
    candidate = f"{stem}{suffix}"
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _clean_header(value: str, label: str, *, allow_empty: bool) -> str:
    clean = " ".join(str(value or "").split())
    if ("\r" in str(value or "") or "\n" in str(value or "")):
        raise OutboundMailError("header_injection", f"{label}包含非法换行")
    if not clean and not allow_empty:
        raise OutboundMailError("invalid_header", f"{label}不能为空")
    return clean[:998]


def _reply_subject(value: str) -> str:
    clean = value.strip()
    return clean if clean.casefold().startswith("re:") else f"Re: {clean}"


def _forward_subject(value: str) -> str:
    clean = value.strip()
    return clean if clean.casefold().startswith(("fwd:", "fw:")) else f"Fwd: {clean}"


def _outbound_id(request_id: str) -> str:
    return "out_" + hashlib.sha256(
        str(request_id).encode("utf-8")
    ).hexdigest()[:32]


def _source_references(
    request: dict[str, Any], source_message_id: str
) -> str:
    existing = str(request.get("references_raw") or "").strip()
    values = existing.split()
    if source_message_id not in values:
        values.append(source_message_id)
    return " ".join(values[-50:])


def _html_fallback(value: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", value).strip()


def _normalize_links(
    values: Iterable[dict[str, Any] | str],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values or ():
        if isinstance(raw, str):
            url = raw
            display_text = ""
        elif isinstance(raw, dict):
            url = str(raw.get("url") or "")
            display_text = _clean_header(
                str(raw.get("display_text") or ""),
                "链接文字",
                allow_empty=True,
            )
        else:
            raise OutboundMailError(
                "invalid_link", "链接必须是字符串或链接对象"
            )
        try:
            parsed = urlsplit(str(url or "").strip())
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.hostname
            ):
                raise ValueError
            port = parsed.port
        except ValueError as exc:
            raise OutboundMailError(
                "invalid_link",
                "链接必须是完整的 HTTP 或 HTTPS 地址",
            ) from exc
        host = parsed.hostname.rstrip(".").casefold()
        netloc = host if port is None else f"{host}:{port}"
        normalized = urlunsplit(
            (
                parsed.scheme.casefold(),
                netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {"url": normalized, "display_text": display_text}
        )
    return result


def _append_links(
    body_text: str,
    body_html: str,
    links: list[dict[str, str]],
) -> tuple[str, str]:
    if not links:
        return body_text, body_html
    lines = ["相关链接："]
    for item in links:
        label = item["display_text"]
        lines.append(
            f"- {label}：{item['url']}"
            if label
            else f"- {item['url']}"
        )
    text = "\n\n".join(
        part for part in (body_text.rstrip(), "\n".join(lines)) if part
    )
    if not body_html:
        return text, body_html
    items = "".join(
        "<li><a href=\"{url}\">{label}</a></li>".format(
            url=html.escape(item["url"], quote=True),
            label=html.escape(
                item["display_text"] or item["url"]
            ),
        )
        for item in links
    )
    return text, f"{body_html.rstrip()}<p>相关链接：</p><ul>{items}</ul>"


def _local_sent_mailbox_ref(
    _cfg: AppConfig, request: dict[str, Any]
) -> str:
    return f"local:sent:{str(request['sender_account_id'])}"
