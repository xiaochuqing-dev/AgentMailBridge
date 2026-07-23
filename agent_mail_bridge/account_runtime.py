"""按 account_id 解析 Provider Adapter、凭据、OAuth 与运行配置。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.credentials import (
    ACCOUNT_IMAP_SECRET,
    ACCOUNT_SMTP_SECRET,
    GMAIL_IMAP_SECRET,
    QQ_SMTP_SECRET,
    CredentialService,
)
from agent_mail_bridge.database import get_mail_account
from agent_mail_bridge.oauth_storage import ensure_account_oauth_storage
from agent_mail_bridge.provider_adapters import ProviderAdapter, get_provider_adapter


class AccountRuntimeError(ValueError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class AccountRuntimeContext:
    account: dict[str, Any]
    adapter: ProviderAdapter
    config: AppConfig

    @property
    def account_id(self) -> str:
        return str(self.account["account_id"])


class AccountRuntimeRouter:
    """业务层唯一账号运行时入口；Provider 差异在此收口。"""

    def __init__(
        self, cfg: AppConfig, credentials: CredentialService
    ) -> None:
        self.cfg = cfg
        self.credentials = credentials

    def get_account(
        self,
        account_id: str,
        *,
        require_enabled: bool = True,
        capability: str | None = None,
    ) -> dict[str, Any]:
        account = get_mail_account(self.cfg.db_path, str(account_id))
        if account is None:
            raise AccountRuntimeError("account_not_found", "邮箱账号不存在或已移除")
        if require_enabled and not account.get("enabled"):
            raise AccountRuntimeError("account_disabled", "邮箱账号已停用")
        if capability == "receive" and not account.get("receive_enabled"):
            raise AccountRuntimeError(
                "capability_not_available", "该账号未启用收件能力"
            )
        if capability == "send" and not account.get("send_enabled"):
            raise AccountRuntimeError(
                "capability_not_available", "该账号未启用发件能力"
            )
        if capability and capability not in set(account.get("capabilities") or ()):
            raise AccountRuntimeError(
                "capability_not_available", f"该账号不支持 {capability} 能力"
            )
        adapter = get_provider_adapter(str(account.get("provider") or ""))
        if capability and not adapter.supports(capability):
            raise AccountRuntimeError(
                "provider_capability_not_implemented",
                f"{adapter.display_name} 的 {capability} 能力尚未正式接通",
            )
        return account

    def context(
        self,
        account_id: str,
        *,
        capability: str | None = None,
        require_enabled: bool = True,
    ) -> AccountRuntimeContext:
        account = self.get_account(
            account_id,
            capability=capability,
            require_enabled=require_enabled,
        )
        adapter = get_provider_adapter(str(account["provider"]))
        runtime_cfg = self._config_for(account)
        return AccountRuntimeContext(account, adapter, runtime_cfg)

    def _config_for(self, account: dict[str, Any]) -> AppConfig:
        runtime_cfg = replace(
            self.cfg,
            runtime_account_id=str(account["account_id"]),
            runtime_provider=str(account["provider"]),
        )
        settings = dict(account.get("provider_settings") or {})
        address = str(account.get("email_address") or "")
        provider = str(account.get("provider") or "")
        is_legacy_gmail = (
            provider == "gmail"
            and address.casefold() == self.cfg.gmail_address.casefold()
        )
        is_legacy_qq = (
            provider == "qq"
            and address.casefold() == self.cfg.qq_email.casefold()
        )
        if provider == "gmail":
            runtime_cfg.gmail_address = address
            runtime_cfg.gmail_receive_backend = str(
                settings.get("receive_backend") or "gmail_api"
            )
            runtime_cfg.gmail_imap_host = str(
                settings.get("imap_host") or self.cfg.gmail_imap_host
            )
            runtime_cfg.gmail_imap_port = int(
                settings.get("imap_port") or self.cfg.gmail_imap_port
            )
            runtime_cfg.gmail_app_password = (
                self.credentials.get_for_account(
                    str(account["account_id"]),
                    ACCOUNT_IMAP_SECRET,
                    legacy_name=GMAIL_IMAP_SECRET if is_legacy_gmail else None,
                    migrate_legacy=is_legacy_gmail,
                )
                or ""
            )
            credentials_path, token_path = ensure_account_oauth_storage(
                account_id=str(account["account_id"]),
                legacy_credentials_path=self.cfg.gmail_api_credentials_path,
                legacy_token_path=self.cfg.gmail_api_token_path,
                copy_legacy=is_legacy_gmail,
            )
            runtime_cfg.gmail_api_credentials_path = credentials_path
            runtime_cfg.gmail_api_token_path = token_path
        elif provider == "qq":
            runtime_cfg.qq_email = address
            runtime_cfg.qq_smtp_host = str(
                settings.get("smtp_host") or self.cfg.qq_smtp_host
            )
            runtime_cfg.qq_smtp_port = int(
                settings.get("smtp_port") or self.cfg.qq_smtp_port
            )
            runtime_cfg.qq_auth_code = (
                self.credentials.get_for_account(
                    str(account["account_id"]),
                    ACCOUNT_SMTP_SECRET,
                    legacy_name=QQ_SMTP_SECRET if is_legacy_qq else None,
                    migrate_legacy=is_legacy_qq,
                )
                or ""
            )
        elif provider == "generic_imap_smtp":
            runtime_cfg.gmail_address = address
            runtime_cfg.gmail_receive_backend = "imap"
            runtime_cfg.gmail_imap_host = str(settings.get("imap_host") or "")
            runtime_cfg.gmail_imap_port = int(settings.get("imap_port") or 993)
            runtime_cfg.gmail_network_mode = "direct"
            runtime_cfg.gmail_app_password = (
                self.credentials.get_for_account(
                    str(account["account_id"]), ACCOUNT_IMAP_SECRET
                )
                or ""
            )
            runtime_cfg.qq_email = address
            runtime_cfg.qq_smtp_host = str(settings.get("smtp_host") or "")
            runtime_cfg.qq_smtp_port = int(settings.get("smtp_port") or 465)
            runtime_cfg.qq_auth_code = (
                self.credentials.get_for_account(
                    str(account["account_id"]), ACCOUNT_SMTP_SECRET
                )
                or ""
            )
        return runtime_cfg

    def oauth_paths(self, account_id: str) -> tuple[Path, Path]:
        context = self.context(account_id, require_enabled=False)
        if context.account.get("provider") != "gmail":
            raise AccountRuntimeError(
                "oauth_not_supported", "该账号不使用 Gmail OAuth"
            )
        return (
            context.config.gmail_api_credentials_path,
            context.config.gmail_api_token_path,
        )
