"""Gmail API、IMAP、手动与自动收件共用的收件规则。"""

from __future__ import annotations

import json
import hashlib
import re
from email.utils import getaddresses
from typing import Any, Iterable

from agent_mail_bridge.mail_common import is_trusted_self_mail

SELF_ONLY = "self_only"
ALL_SCANNED = "all_scanned"
CUSTOM = "custom"
VALID_MODES = {SELF_ONLY, ALL_SCANNED, CUSTOM}

_ITEM_SEPARATOR = re.compile(r"[,;，；\n\r]+")
_EMAIL_PATTERN = re.compile(
    r"^[^@\s]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_DOMAIN_PATTERN = re.compile(
    r"^@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def parse_rule_items(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """解析 JSON 数组或逗号、分号、换行分隔的规则项。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ()
        values: Iterable[Any]
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                values = parsed if isinstance(parsed, list) else [text]
            except (TypeError, ValueError, json.JSONDecodeError):
                values = _ITEM_SEPARATOR.split(text)
        else:
            values = _ITEM_SEPARATOR.split(text)
    else:
        values = raw
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def normalize_sender_rules(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    return tuple(item.casefold() for item in parse_rule_items(raw))


def normalize_subject_keywords(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    return parse_rule_items(raw)


def invalid_sender_rules(values: Iterable[str]) -> tuple[str, ...]:
    invalid = []
    for value in values:
        item = str(value).strip()
        pattern = _DOMAIN_PATTERN if item.startswith("@") else _EMAIL_PATTERN
        if not pattern.fullmatch(item):
            invalid.append(item)
    return tuple(invalid)


def validate_rule_settings(
    mode: str,
    senders: Iterable[str],
    subject_keywords: Iterable[str],
    require_attachment: bool,
) -> tuple[str, ...]:
    """返回可直接展示给用户的校验错误。"""
    errors: list[str] = []
    if mode not in VALID_MODES:
        errors.append("收件模式无效")
        return tuple(errors)
    normalized_senders = normalize_sender_rules(senders)
    invalid = invalid_sender_rules(normalized_senders)
    if invalid:
        errors.append("发件人或域名格式无效：" + "、".join(invalid))
    keywords = normalize_subject_keywords(subject_keywords)
    if mode == CUSTOM and not normalized_senders and not keywords and not require_attachment:
        errors.append("自定义规则至少需要一个有效条件")
    return tuple(errors)


def match_receive_rule(cfg: Any, mail: Any) -> tuple[bool, str]:
    """执行统一匹配：分类间 AND，同一分类内 OR。"""
    mode = str(getattr(cfg, "receive_rule_mode", "") or "").strip().lower()
    if not mode:
        mode = SELF_ONLY if getattr(cfg, "auto_receive_only_self_mail", True) else ALL_SCANNED
    if mode == SELF_ONLY:
        matched = is_trusted_self_mail(
            cfg.gmail_address, mail.from_raw, mail.to_raw, mail.cc_raw
        )
        return matched, "matched" if matched else "not_self_mail"
    if mode == ALL_SCANNED:
        return True, "matched"
    if mode != CUSTOM:
        return False, "invalid_mode"

    senders = normalize_sender_rules(getattr(cfg, "receive_rule_senders", ()))
    keywords = normalize_subject_keywords(
        getattr(cfg, "receive_rule_subject_keywords", ())
    )
    require_attachment = bool(
        getattr(cfg, "receive_rule_require_attachment", False)
    )
    if validate_rule_settings(mode, senders, keywords, require_attachment):
        return False, "invalid_custom_rule"

    if senders:
        addresses = {
            address.casefold().strip()
            for _name, address in getaddresses([str(mail.from_raw or "")])
            if address.strip()
        }
        if not any(
            any(
                address.endswith(rule) if rule.startswith("@") else address == rule
                for address in addresses
            )
            for rule in senders
        ):
            return False, "sender_not_matched"

    if keywords:
        subject = str(mail.subject or "").casefold()
        if not any(keyword.casefold() in subject for keyword in keywords):
            return False, "subject_not_matched"

    if require_attachment and not mail.attachments:
        return False, "attachment_required"
    return True, "matched"


def serialize_rule_items(values: Iterable[str]) -> str:
    """配置文件使用 JSON，避免主题关键词中的普通空格被破坏。"""
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def receive_rule_fingerprint(cfg: Any) -> str:
    """生成非敏感、稳定的规则指纹，用于历史重扫审计而非去重。"""
    payload = {
        "mode": str(getattr(cfg, "receive_rule_mode", "") or ALL_SCANNED),
        "senders": list(normalize_sender_rules(getattr(cfg, "receive_rule_senders", ()))),
        "keywords": list(
            normalize_subject_keywords(
                getattr(cfg, "receive_rule_subject_keywords", ())
            )
        ),
        "require_attachment": bool(
            getattr(cfg, "receive_rule_require_attachment", False)
        ),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
