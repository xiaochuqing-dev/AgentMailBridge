"""按 account_id 解析 Provider Adapter、凭据、OAuth 与运行配置。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import (
    AppConfig,
    IncomingRuntimeConfig,
    OutgoingRuntimeConfig,
    _effective_receive_backend,
)
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
from agent_mail_bridge.provider_foundation import resolve_imap_id_enabled


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
            runtime_cfg.incoming = IncomingRuntimeConfig(
                backend=_effective_receive_backend(runtime_cfg),
                username=address,
                secret=runtime_cfg.gmail_app_password,
                host=runtime_cfg.gmail_imap_host,
                port=runtime_cfg.gmail_imap_port,
                security=str(settings.get("imap_security") or "ssl"),
                connect_timeout=int(
                    settings.get("connect_timeout")
                    or runtime_cfg.gmail_connect_timeout
                ),
                imap_id_enabled=resolve_imap_id_enabled(settings),
            )
        elif provider == "qq":
            imap_secret = (
                self.credentials.get_for_account(
                    str(account["account_id"]),
                    ACCOUNT_IMAP_SECRET,
                    legacy_name=QQ_SMTP_SECRET if is_legacy_qq else None,
                    migrate_legacy=is_legacy_qq,
                )
                or ""
            )
            smtp_secret = (
                self.credentials.get_for_account(
                    str(account["account_id"]),
                    ACCOUNT_SMTP_SECRET,
                    legacy_name=QQ_SMTP_SECRET if is_legacy_qq else None,
                    migrate_legacy=is_legacy_qq,
                )
                or imap_secret
            )
            runtime_cfg.qq_email = address
            runtime_cfg.qq_smtp_host = str(
                settings.get("smtp_host") or self.cfg.qq_smtp_host
            )
            runtime_cfg.qq_smtp_port = int(
                settings.get("smtp_port") or self.cfg.qq_smtp_port
            )
            runtime_cfg.qq_auth_code = smtp_secret
            runtime_cfg.incoming = IncomingRuntimeConfig(
                backend="imap",
                username=address,
                secret=imap_secret or smtp_secret,
                host=str(settings.get("imap_host") or "imap.qq.com"),
                port=int(settings.get("imap_port") or 993),
                security=str(settings.get("imap_security") or "ssl"),
                connect_timeout=int(
                    settings.get("connect_timeout")
                    or runtime_cfg.gmail_connect_timeout
                ),
            )
            runtime_cfg.outgoing = OutgoingRuntimeConfig(
                backend="smtp",
                username=address,
                secret=smtp_secret or imap_secret,
                host=runtime_cfg.qq_smtp_host,
                port=runtime_cfg.qq_smtp_port,
                security=str(settings.get("smtp_security") or "ssl"),
                connect_timeout=int(
                    settings.get("connect_timeout")
                    or runtime_cfg.qq_smtp_connect_timeout
                ),
            )
        elif provider in {"163", "generic_imap_smtp"}:
            imap_secret = (
                self.credentials.get_for_account(
                    str(account["account_id"]), ACCOUNT_IMAP_SECRET
                )
                or ""
            )
            smtp_secret = (
                self.credentials.get_for_account(
                    str(account["account_id"]), ACCOUNT_SMTP_SECRET
                )
                or imap_secret
            )
            runtime_cfg.incoming = IncomingRuntimeConfig(
                backend="imap" if settings.get("imap_host") else "",
                username=address,
                secret=imap_secret or smtp_secret,
                host=str(settings.get("imap_host") or ""),
                port=int(settings.get("imap_port") or 993),
                security=str(settings.get("imap_security") or "ssl"),
                connect_timeout=int(settings.get("connect_timeout") or 20),
                mailbox=str(settings.get("inbox_name") or "INBOX"),
                uid_overlap=max(
                    0, min(int(settings.get("uid_overlap") or 10), 100)
                ),
                imap_id_enabled=resolve_imap_id_enabled(settings),
            )
            runtime_cfg.outgoing = OutgoingRuntimeConfig(
                backend="smtp" if settings.get("smtp_host") else "",
                username=address,
                secret=smtp_secret or imap_secret,
                host=str(settings.get("smtp_host") or ""),
                port=int(settings.get("smtp_port") or 465),
                security=str(settings.get("smtp_security") or "ssl"),
                connect_timeout=int(settings.get("connect_timeout") or 20),
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
