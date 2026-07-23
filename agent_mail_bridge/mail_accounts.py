"""统一邮箱账号模型与稳定身份工具。

本模块只描述账号事实，不执行网络认证、收件或发件。现有 Gmail 与 QQ
实现继续由原业务模块承担，Provider Adapter 负责声明它们的能力边界。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_mail_bridge.config import AppConfig


@dataclass(frozen=True)
class MailAccount:
    """本地一等邮箱账号实体；不包含密码、Token 或 Client Secret。"""

    account_id: str
    provider: str
    email_address: str
    display_name: str
    auth_type: str
    receive_enabled: bool
    send_enabled: bool
    enabled: bool = True
    data_namespace: str = ""
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    provider_settings: dict[str, Any] = field(default_factory=dict)
    source: str = "legacy_config"

    def to_record(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "email_address": normalize_email_address(self.email_address),
            "display_name": self.display_name.strip(),
            "auth_type": self.auth_type,
            "receive_enabled": 1 if self.receive_enabled else 0,
            "send_enabled": 1 if self.send_enabled else 0,
            "enabled": 1 if self.enabled else 0,
            "data_namespace": self.data_namespace or self.account_id,
            "capabilities_json": json.dumps(
                sorted(set(self.capabilities)), ensure_ascii=False, separators=(",", ":")
            ),
            "provider_settings_json": json.dumps(
                self.provider_settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "source": self.source,
        }


def normalize_email_address(value: str | None) -> str:
    return str(value or "").strip().casefold()


def stable_account_id(provider: str, email_address: str) -> str:
    """按 provider + 地址生成跨重启稳定且不泄露邮箱明文的账号 ID。"""
    normalized_provider = str(provider or "generic").strip().casefold() or "generic"
    normalized_email = normalize_email_address(email_address) or "legacy-unknown"
    digest = hashlib.sha256(
        f"agentmailbridge-account-v1\n{normalized_provider}\n{normalized_email}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"acct_{digest[:24]}"


def stable_mailbox_id(account_id: str, external_ref: str) -> str:
    normalized_ref = str(external_ref or "INBOX").strip().casefold() or "inbox"
    digest = hashlib.sha256(
        f"agentmailbridge-mailbox-v1\n{account_id}\n{normalized_ref}".encode("utf-8")
    ).hexdigest()
    return f"mbx_{digest[:24]}"


def provider_and_address_from_legacy_ref(value: str | None) -> tuple[str, str]:
    """解析 v1.3 account_ref；未知格式保留为 generic 账号。"""
    raw = str(value or "").strip()
    if ":" in raw:
        provider, address = raw.split(":", 1)
        provider = provider.strip().casefold() or "generic"
        return provider, normalize_email_address(address)
    return "generic", normalize_email_address(raw)


def legacy_accounts_from_config(cfg: AppConfig) -> list[MailAccount]:
    """把 v1.3 的 Gmail/QQ 配置映射为正式账号，不读取或持久化秘密。"""
    from agent_mail_bridge.config import _effective_receive_backend

    accounts: list[MailAccount] = []
    gmail_address = normalize_email_address(cfg.gmail_address)
    if gmail_address:
        backend = _effective_receive_backend(cfg)
        auth_type = "oauth2" if backend == "gmail_api" else "app_password"
        capabilities = (
            "receive",
            "archive",
            "mail_facts",
            "gmail_api" if backend == "gmail_api" else "imap",
        )
        account_id = stable_account_id("gmail", gmail_address)
        accounts.append(
            MailAccount(
                account_id=account_id,
                provider="gmail",
                email_address=gmail_address,
                display_name="Gmail",
                auth_type=auth_type,
                receive_enabled=True,
                send_enabled=False,
                data_namespace=account_id,
                capabilities=capabilities,
                provider_settings={"receive_backend": backend},
            )
        )
    qq_address = normalize_email_address(cfg.qq_email)
    if qq_address:
        account_id = stable_account_id("qq", qq_address)
        accounts.append(
            MailAccount(
                account_id=account_id,
                provider="qq",
                email_address=qq_address,
                display_name="QQ 邮箱",
                auth_type="app_password",
                receive_enabled=True,
                send_enabled=True,
                data_namespace=account_id,
                capabilities=(
                    "receive",
                    "send",
                    "archive",
                    "mail_facts",
                    "imap",
                    "smtp",
                    "folder_discovery",
                    "outbound_archive",
                ),
                provider_settings={
                    "profile_id": "qq",
                    "imap_host": "imap.qq.com",
                    "imap_port": 993,
                    "imap_security": "ssl",
                    "smtp_host": cfg.qq_smtp_host,
                    "smtp_port": cfg.qq_smtp_port,
                    "smtp_security": "ssl",
                    "inbox_name": "INBOX",
                    "uid_overlap": 10,
                },
            )
        )
    return accounts


def current_receive_account_id(cfg: AppConfig) -> str:
    runtime_account_id = str(getattr(cfg, "runtime_account_id", "") or "").strip()
    if runtime_account_id:
        return runtime_account_id
    return stable_account_id("gmail", cfg.gmail_address)


def current_send_account_id(cfg: AppConfig) -> str:
    runtime_account_id = str(getattr(cfg, "runtime_account_id", "") or "").strip()
    if runtime_account_id:
        return runtime_account_id
    return stable_account_id("qq", cfg.qq_email)
