"""Provider/Adapter 能力注册表。

Adapter 声明稳定能力边界并指向既有实现，不重复实现协议。
Generic IMAP/SMTP 只接通连接测试与目录发现基础；Microsoft 仍为 planned，
两者都不会被误报为已正式支持收发。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderAdapter:
    provider: str
    display_name: str
    authentication_types: tuple[str, ...]
    available_capabilities: tuple[str, ...]
    implemented_capabilities: tuple[str, ...]
    receive_backends: tuple[str, ...] = ()
    send_backends: tuple[str, ...] = ()
    status: str = "planned"

    def supports(self, capability: str) -> bool:
        return capability in self.implemented_capabilities


_ADAPTERS = {
    "gmail": ProviderAdapter(
        provider="gmail",
        display_name="Gmail",
        authentication_types=("oauth2", "app_password"),
        available_capabilities=("receive", "send"),
        implemented_capabilities=("receive", "archive", "mail_facts"),
        receive_backends=("gmail_api", "imap"),
        status="receive_supported",
    ),
    "qq": ProviderAdapter(
        provider="qq",
        display_name="QQ 邮箱",
        authentication_types=("app_password",),
        available_capabilities=("receive", "send"),
        implemented_capabilities=("send", "outbound_archive"),
        send_backends=("smtp",),
        status="send_supported",
    ),
    "generic_imap_smtp": ProviderAdapter(
        provider="generic_imap_smtp",
        display_name="标准 IMAP/SMTP",
        authentication_types=("password", "app_password"),
        available_capabilities=(
            "receive", "send", "connection_test", "folder_discovery"
        ),
        implemented_capabilities=("connection_test", "folder_discovery"),
        receive_backends=("imap",),
        send_backends=("smtp",),
        status="planned",
    ),
    "microsoft": ProviderAdapter(
        provider="microsoft",
        display_name="Microsoft / Outlook",
        authentication_types=("oauth2",),
        available_capabilities=("receive", "send"),
        implemented_capabilities=(),
        receive_backends=("microsoft_graph",),
        send_backends=("microsoft_graph",),
    ),
}


def get_provider_adapter(provider: str) -> ProviderAdapter:
    key = str(provider or "").strip().casefold()
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise ValueError(f"未知邮箱 Provider：{provider}") from exc


def list_provider_adapters() -> tuple[ProviderAdapter, ...]:
    return tuple(_ADAPTERS.values())
