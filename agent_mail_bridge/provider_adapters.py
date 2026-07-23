"""Provider/Adapter 能力注册表。

Adapter 声明稳定能力边界并指向既有实现，不重复实现协议。
QQ、163 与 Generic 共享标准协议 Core；Microsoft 仍为 planned。
Provider status 单独表达真实 E2E 是否完成，避免把自动化实现误报为线上验收。
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
        implemented_capabilities=(
            "receive", "send", "archive", "mail_facts",
            "folder_discovery", "outbound_archive",
        ),
        receive_backends=("imap",),
        send_backends=("smtp",),
        status="implementation_ready_e2e_required",
    ),
    "163": ProviderAdapter(
        provider="163",
        display_name="163 邮箱",
        authentication_types=("app_password",),
        available_capabilities=("receive", "send", "folder_discovery"),
        implemented_capabilities=(
            "receive", "send", "archive", "mail_facts",
            "folder_discovery", "outbound_archive",
        ),
        receive_backends=("imap",),
        send_backends=("smtp",),
        status="implementation_ready_e2e_required",
    ),
    "generic_imap_smtp": ProviderAdapter(
        provider="generic_imap_smtp",
        display_name="标准 IMAP/SMTP",
        authentication_types=("password", "app_password"),
        available_capabilities=(
            "receive", "send", "connection_test", "folder_discovery"
        ),
        implemented_capabilities=(
            "receive", "send", "archive", "mail_facts",
            "connection_test", "folder_discovery", "outbound_archive",
        ),
        receive_backends=("imap",),
        send_backends=("smtp",),
        status="implementation_ready_e2e_required",
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
