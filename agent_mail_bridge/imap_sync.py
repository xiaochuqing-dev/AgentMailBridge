"""Provider-neutral IMAP 增量同步。

只实现稳定的 polling + mailbox 级 UID checkpoint。IDLE、CONDSTORE 与
QRESYNC 保留为后续能力，避免在 Provider 真实兼容性验证前扩大协议面。
"""

from __future__ import annotations

import json
import ssl
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from agent_mail_bridge.config import AppConfig, effective_incoming_runtime
from agent_mail_bridge.database import (
    clear_receive_retry,
    count_receive_retries,
    get_auto_receive_state,
    query_due_receive_retries,
    receive_retry_is_due,
    record_receive_failure,
    save_auto_receive_state,
    upsert_mailboxes,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.mail_accounts import current_receive_account_id
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.provider_foundation import _mailbox_role
from agent_mail_bridge.utils import fmt_datetime, now_local


logger = get_logger("imap_sync")
DEFAULT_BATCH_SIZE = 25


class ImapSyncError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def receive_imap_account(
    cfg: AppConfig,
    *,
    limit: int | None = None,
    mark_seen: bool | None = None,
    automatic: bool = False,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """按 UID 增量同步一个账号的 INBOX，并复用统一 Mail Package 流程。"""
    incoming = effective_incoming_runtime(cfg)
    account_id = current_receive_account_id(cfg)
    mailbox = incoming.mailbox or "INBOX"
    scan_cap = max(1, int(limit or cfg.receive_scan_cap or cfg.max_fetch_limit))
    result = _empty_result()
    client: Any | None = None
    try:
        client = _connect(incoming, client_factory)
        _refresh_mailboxes(cfg, account_id, client)
        selected = client.select_folder(mailbox, readonly=not bool(mark_seen))
        uidvalidity = _response_int(selected, b"UIDVALIDITY")
        uidnext = _response_int(selected, b"UIDNEXT")
        highestmodseq = _response_int(selected, b"HIGHESTMODSEQ")
        checkpoint = _load_mailbox_checkpoint(cfg, account_id, mailbox)
        previous_validity = int(checkpoint.get("uidvalidity") or 0)
        last_uid = int(checkpoint.get("last_uid") or 0)
        validity_changed = bool(
            previous_validity and uidvalidity and previous_validity != uidvalidity
        )
        if validity_changed:
            last_uid = 0
            checkpoint["uidvalidity_reset_count"] = (
                int(checkpoint.get("uidvalidity_reset_count") or 0) + 1
            )
            result["uidvalidity_changed"] = True

        overlap_start = (
            max(1, last_uid - max(0, incoming.uid_overlap) + 1)
            if last_uid
            else 1
        )
        searched = _search_uids(client, overlap_start)
        overlap_uids = [uid for uid in searched if uid <= last_uid]
        new_uids = [uid for uid in searched if uid > last_uid]
        recent_overlap = (
            overlap_uids[-incoming.uid_overlap :]
            if incoming.uid_overlap
            else []
        )
        candidates = recent_overlap + new_uids[:scan_cap]
        candidates = _append_due_retry_uids(
            cfg, account_id, mailbox, candidates, scan_cap
        )
        result["fetched"] = len(candidates)

        successful_new_uids: list[int] = []
        for batch in _chunks(candidates, DEFAULT_BATCH_SIZE):
            fetched = _fetch_with_isolation(client, batch)
            for uid in batch:
                resource_id = _retry_resource_id(mailbox, uid)
                if not receive_retry_is_due(
                    cfg.db_path, "imap", resource_id, account_id=account_id
                ):
                    result["skipped"] += 1
                    result["retry_deferred"] += 1
                    continue
                raw_or_error = fetched.get(uid)
                if isinstance(raw_or_error, Exception):
                    _record_message_failure(
                        cfg, account_id, resource_id, uid, raw_or_error, result
                    )
                    continue
                if not isinstance(raw_or_error, bytes):
                    _record_message_failure(
                        cfg,
                        account_id,
                        resource_id,
                        uid,
                        ImapSyncError(
                            "imap_fetch_missing", "服务器未返回该 UID 的 RFC822 原文"
                        ),
                        result,
                    )
                    continue
                try:
                    single = _process_raw(
                        cfg,
                        raw_or_error,
                        uid=uid,
                        mailbox=mailbox,
                        result=result,
                        apply_receive_rule=True,
                    )
                    if single.get("status") == "partial":
                        raise ImapSyncError(
                            "mail_archive_partial",
                            str(single.get("error") or "邮件归档部分完成"),
                        )
                    if mark_seen:
                        _mark_seen(client, uid)
                    clear_receive_retry(
                        cfg.db_path,
                        "imap",
                        resource_id,
                        account_id=account_id,
                    )
                    if uid > last_uid:
                        successful_new_uids.append(uid)
                except Exception as exc:  # noqa: BLE001
                    _record_message_failure(
                        cfg, account_id, resource_id, uid, exc, result
                    )

        new_last_uid = max([last_uid, *successful_new_uids])
        checkpoint.update(
            {
                "uidvalidity": uidvalidity,
                "uidnext": uidnext,
                "highestmodseq": highestmodseq,
                "last_uid": new_last_uid,
                "strategy": "polling_uid_checkpoint",
            }
        )
        _save_mailbox_checkpoint(cfg, account_id, mailbox, checkpoint)
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_connection_error(exc)
        result.update(ok=False, global_error=True, error_code=code)
        result["errors"].append(message)
    finally:
        _logout(client)

    retries = count_receive_retries(cfg.db_path, account_id=account_id)
    result["pending_retries"] = retries["pending"]
    result["needs_attention"] = retries["needs_attention"]
    if not automatic and result["errors"]:
        logger.warning("IMAP 同步完成但存在错误：%s", result["errors"][0])
    return result


def rescan_imap_account(
    cfg: AppConfig,
    *,
    date_from: datetime,
    date_to: datetime,
    apply_receive_rule: bool,
    cancel_check: Callable[[], bool] | None,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    page_size: int,
    scan_cap: int,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """显式历史补扫；不推进普通增量 checkpoint。"""
    incoming = effective_incoming_runtime(cfg)
    account_id = current_receive_account_id(cfg)
    mailbox = incoming.mailbox or "INBOX"
    result = _empty_result()
    result.update(
        matched=0,
        rule_skipped=0,
        cancelled=False,
        truncated=False,
    )
    client: Any | None = None
    try:
        client = _connect(incoming, client_factory)
        client.select_folder(mailbox, readonly=True)
        uids = sorted(
            int(uid)
            for uid in client.search(
                [
                    "SINCE",
                    date_from.date(),
                    "BEFORE",
                    date_to.date() + timedelta(days=1),
                ]
            )
        )
        safe_cap = max(1, min(int(scan_cap), 10_000))
        if len(uids) > safe_cap:
            uids = uids[-safe_cap:]
            result["truncated"] = True
        safe_page = max(1, min(int(page_size), 500))
        for page in _chunks(uids, safe_page):
            if cancel_check and cancel_check():
                result["cancelled"] = True
                break
            fetched = _fetch_with_isolation(client, page)
            for uid in page:
                if cancel_check and cancel_check():
                    result["cancelled"] = True
                    break
                result["fetched"] += 1
                raw_or_error = fetched.get(uid)
                if not isinstance(raw_or_error, bytes):
                    error = (
                        raw_or_error
                        if isinstance(raw_or_error, Exception)
                        else ImapSyncError(
                            "imap_fetch_missing",
                            "服务器未返回该 UID 的 RFC822 原文",
                        )
                    )
                    _record_message_failure(
                        cfg,
                        account_id,
                        _retry_resource_id(mailbox, uid),
                        uid,
                        error,
                        result,
                    )
                    continue
                try:
                    single = _process_raw(
                        cfg,
                        raw_or_error,
                        uid=uid,
                        mailbox=mailbox,
                        result=result,
                        apply_receive_rule=apply_receive_rule,
                        date_from=date_from,
                        date_to=date_to,
                    )
                    status = str(single.get("status") or "")
                    if status == "skipped":
                        result["rule_skipped"] += 1
                    elif status in {"saved", "partial", "duplicate"}:
                        result["matched"] += 1
                except Exception as exc:  # noqa: BLE001
                    _record_message_failure(
                        cfg,
                        account_id,
                        _retry_resource_id(mailbox, uid),
                        uid,
                        exc,
                        result,
                    )
                if progress_callback:
                    try:
                        progress_callback(dict(result))
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "IMAP 历史补扫进度回调失败", exc_info=True
                        )
            if result["cancelled"]:
                break
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_connection_error(exc)
        result.update(ok=False, global_error=True, error_code=code)
        result["errors"].append(message)
    finally:
        _logout(client)
    retries = count_receive_retries(cfg.db_path, account_id=account_id)
    result["pending_retries"] = retries["pending"]
    result["needs_attention"] = retries["needs_attention"]
    return result


def _connect(incoming: Any, client_factory: Callable[..., Any] | None) -> Any:
    if not incoming.host or not incoming.username or not incoming.secret:
        raise ImapSyncError("imap_auth_required", "IMAP 账号、服务器或凭据缺失")
    if incoming.security not in {"ssl", "starttls"}:
        raise ImapSyncError(
            "imap_insecure_transport", "IMAP 只允许 SSL/TLS 或 STARTTLS"
        )
    if client_factory is None:
        from imapclient import IMAPClient

        client_factory = IMAPClient
    use_ssl = incoming.security == "ssl"
    client = client_factory(
        incoming.host,
        port=incoming.port,
        ssl=use_ssl,
        timeout=incoming.connect_timeout,
        use_uid=True,
    )
    try:
        if not use_ssl:
            client.starttls(ssl_context=ssl.create_default_context())
        client.login(incoming.username, incoming.secret)
    except Exception:
        _logout(client)
        raise
    return client


def _refresh_mailboxes(
    cfg: AppConfig, account_id: str, client: Any
) -> None:
    try:
        discovered = []
        for flags, delimiter, name in client.list_folders():
            discovered.append(
                {
                    "external_ref": str(name),
                    "display_name": str(name),
                    "delimiter": (
                        delimiter.decode("ascii", errors="ignore")
                        if isinstance(delimiter, bytes)
                        else str(delimiter or "")
                    ),
                    "flags": [
                        item.decode("ascii", errors="ignore")
                        if isinstance(item, bytes)
                        else str(item)
                        for item in flags
                    ],
                    "mailbox_role": _mailbox_role(flags, str(name)),
                }
            )
        upsert_mailboxes(cfg.db_path, account_id, discovered)
    except Exception:  # noqa: BLE001
        logger.warning("IMAP 目录刷新失败，本次继续同步已配置目录", exc_info=True)


def _search_uids(client: Any, lower_bound: int) -> list[int]:
    values = client.search(["UID", f"{max(1, lower_bound)}:*"])
    return sorted({int(value) for value in values if int(value) >= lower_bound})


def _fetch_with_isolation(
    client: Any, uids: Iterable[int]
) -> dict[int, bytes | Exception | None]:
    batch = list(uids)
    if not batch:
        return {}
    try:
        fetched = client.fetch(batch, [b"BODY.PEEK[]"])
        result = {
            uid: _extract_raw(fetched.get(uid) or fetched.get(str(uid)))
            for uid in batch
        }
        for uid, raw in tuple(result.items()):
            if raw is not None:
                continue
            try:
                single = client.fetch([uid], [b"BODY.PEEK[]"])
                result[uid] = _extract_raw(
                    single.get(uid) or single.get(str(uid))
                )
            except Exception as exc:  # noqa: BLE001
                result[uid] = exc
        return result
    except Exception:
        result: dict[int, bytes | Exception | None] = {}
        for uid in batch:
            try:
                fetched = client.fetch([uid], [b"BODY.PEEK[]"])
                result[uid] = _extract_raw(
                    fetched.get(uid) or fetched.get(str(uid))
                )
            except Exception as exc:  # noqa: BLE001
                result[uid] = exc
        return result


def _extract_raw(item: Any) -> bytes | None:
    if isinstance(item, bytes):
        return item
    if not isinstance(item, dict):
        return None
    for key in (
        b"BODY[]",
        b"BODY.PEEK[]",
        b"RFC822",
        "BODY[]",
        "BODY.PEEK[]",
        "RFC822",
    ):
        value = item.get(key)
        if isinstance(value, bytes):
            return value
    return next(
        (value for value in item.values() if isinstance(value, bytes)),
        None,
    )


def _process_raw(
    cfg: AppConfig,
    raw: bytes,
    *,
    uid: int,
    mailbox: str,
    result: dict[str, Any],
    apply_receive_rule: bool,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    received_at = _message_datetime(raw)
    normalized = normalized_mail_from_raw(
        raw,
        backend="imap",
        backend_message_id="",
        thread_id="",
        uid=str(uid),
        received_at=fmt_datetime(received_at),
        saved_date=received_at.strftime("%Y-%m-%d"),
        max_attachment_bytes=cfg.max_attachment_bytes,
        mailbox_ref=mailbox,
    )
    if date_from is not None or date_to is not None:
        normalized_at = datetime.fromisoformat(normalized.received_at)
        if (
            date_from is not None
            and normalized_at < date_from
            or date_to is not None
            and normalized_at > date_to
        ):
            result["skipped"] += 1
            return {"status": "out_of_range"}
    single = process_normalized_mail(
        cfg, normalized, apply_receive_rule=apply_receive_rule
    )
    status = str(single.get("status") or "")
    if status in {"saved", "partial"}:
        result["accepted"] += 1
        result["saved"] += 1
        result["attachments"] += int(single.get("attachments") or 0)
        result["saved_files"].extend(single.get("saved_files") or [])
    elif status == "duplicate":
        result["duplicates"] += 1
        result["skipped"] += 1
    else:
        result["skipped"] += 1
    return single


def _message_datetime(raw: bytes) -> datetime:
    try:
        parsed = parsedate_to_datetime(
            str(message_from_bytes(raw).get("Date") or "")
        )
        if parsed is None:
            return now_local()
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return now_local()


def _record_message_failure(
    cfg: AppConfig,
    account_id: str,
    resource_id: str,
    uid: int,
    exc: Exception,
    result: dict[str, Any],
) -> None:
    message = f"处理 IMAP 邮件 uid={uid} 失败：{type(exc).__name__}"
    result["failed"] += 1
    result["errors"].append(message)
    record_receive_failure(
        cfg.db_path,
        backend="imap",
        resource_id=resource_id,
        message_id=str(uid),
        error=str(exc),
        account_id=account_id,
    )


def _append_due_retry_uids(
    cfg: AppConfig,
    account_id: str,
    mailbox: str,
    candidates: list[int],
    scan_cap: int,
) -> list[int]:
    result = list(dict.fromkeys(candidates))
    for retry in query_due_receive_retries(
        cfg.db_path,
        "imap",
        limit=min(100, max(1, scan_cap)),
        account_id=account_id,
    ):
        parsed = _parse_retry_resource_id(
            str(retry.get("resource_id") or ""), mailbox
        )
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


def _retry_resource_id(mailbox: str, uid: int) -> str:
    return f"{mailbox}:{uid}"


def _parse_retry_resource_id(value: str, mailbox: str) -> int | None:
    prefix, separator, uid_text = value.rpartition(":")
    if separator and prefix == mailbox and uid_text.isdigit():
        return int(uid_text)
    if value.isdigit():  # v1.4.1 旧 IMAP retry 兼容
        return int(value)
    return None


def _load_mailbox_checkpoint(
    cfg: AppConfig, account_id: str, mailbox: str
) -> dict[str, Any]:
    raw = get_auto_receive_state(
        cfg.db_path, account_id=account_id
    ).get("checkpoint")
    try:
        data = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    mailboxes = data.get("mailboxes")
    if not isinstance(mailboxes, dict):
        return {}
    item = mailboxes.get(mailbox)
    return dict(item) if isinstance(item, dict) else {}


def _save_mailbox_checkpoint(
    cfg: AppConfig,
    account_id: str,
    mailbox: str,
    checkpoint: dict[str, Any],
) -> None:
    state = get_auto_receive_state(cfg.db_path, account_id=account_id)
    try:
        data = json.loads(str(state.get("checkpoint") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    mailboxes = data.setdefault("mailboxes", {})
    if not isinstance(mailboxes, dict):
        mailboxes = {}
        data["mailboxes"] = mailboxes
    mailboxes[mailbox] = checkpoint
    save_auto_receive_state(
        cfg.db_path,
        account_id=account_id,
        checkpoint=json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _response_int(response: Any, key: bytes) -> int:
    if not isinstance(response, dict):
        return 0
    value = response.get(key)
    if value is None:
        value = response.get(key.decode("ascii"))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mark_seen(client: Any, uid: int) -> None:
    if hasattr(client, "add_flags"):
        client.add_flags([uid], [b"\\Seen"], silent=True)


def _classify_connection_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ImapSyncError):
        return exc.error_code, str(exc)
    text = str(exc).casefold()
    if "auth" in text or "login" in text or "password" in text:
        return "imap_auth_failed", "IMAP 认证失败，请检查账号授权码或应用专用密码"
    if "ssl" in text or "tls" in text or "certificate" in text:
        return "imap_tls_failed", "IMAP TLS 连接失败"
    if "timeout" in text or "timed out" in text:
        return "imap_timeout", "IMAP 连接超时"
    if "bye" in text or "disconnect" in text or "reset" in text:
        return "imap_disconnected", "IMAP 连接已断开"
    return "imap_connection_failed", f"IMAP 连接失败：{type(exc).__name__}"


def _logout(client: Any | None) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        pass


def _chunks(values: Iterable[int], size: int) -> Iterable[list[int]]:
    items = list(values)
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + max(1, size)]


def _empty_result() -> dict[str, Any]:
    return {
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
        "uidvalidity_changed": False,
    }
