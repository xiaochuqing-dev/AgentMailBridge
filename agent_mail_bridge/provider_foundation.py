"""Generic IMAP/SMTP 的协议基础与保守 Provider Profile。

本模块负责非秘密配置校验、连接测试、目录发现和 Provider Profile。
持续收件与正式发件由共享的 Generic Core 承担。
"""

from __future__ import annotations

import socket
import smtplib
import ssl
from dataclasses import dataclass
from typing import Any, Callable


SECURITY_SSL = "ssl"
SECURITY_STARTTLS = "starttls"
SUPPORTED_TRANSPORT_SECURITY = {SECURITY_SSL, SECURITY_STARTTLS}
FORBIDDEN_PROVIDER_SETTING_KEYS = {
    "password",
    "secret",
    "token",
    "auth_code",
    "client_secret",
    "imap_password",
    "smtp_password",
}

SPECIAL_USE_ROLES = {
    "\\all": "all",
    "\\archive": "archive",
    "\\drafts": "drafts",
    "\\flagged": "flagged",
    "\\junk": "junk",
    "\\sent": "sent",
    "\\trash": "trash",
    "\\inbox": "inbox",
}


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    display_name: str
    domains: tuple[str, ...]
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = SECURITY_SSL
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = SECURITY_SSL
    status: str = "planned"

    def to_settings(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_security": self.imap_security,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_security": self.smtp_security,
        }


PROVIDER_PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        profile_id="gmail",
        display_name="Gmail",
        domains=("gmail.com", "googlemail.com"),
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
        status="receive_supported",
    ),
    ProviderProfile(
        profile_id="qq",
        display_name="QQ 邮箱",
        domains=("qq.com",),
        imap_host="imap.qq.com",
        smtp_host="smtp.qq.com",
        status="implementation_ready_e2e_required",
    ),
    ProviderProfile(
        profile_id="163",
        display_name="163 邮箱",
        domains=("163.com",),
        imap_host="imap.163.com",
        smtp_host="smtp.163.com",
        status="implementation_ready_e2e_required",
    ),
)


class ProviderFoundationError(ValueError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def mailbox_text(value: Any) -> str:
    """把 IMAPClient 或兼容服务端返回的目录名规范为 Unicode。"""
    if not isinstance(value, bytes):
        return str(value)
    try:
        from imapclient.imap_utf7 import decode as decode_imap_utf7

        return decode_imap_utf7(value)
    except (ImportError, TypeError, UnicodeError, ValueError):
        return value.decode("utf-8", errors="replace")


def classify_protocol_error(
    protocol: str, exc: Exception
) -> tuple[str, str]:
    """生成可诊断但不包含服务端原文或凭据的连接错误。"""
    normalized_protocol = str(protocol or "").strip().casefold()
    prefix = "imap" if normalized_protocol == "imap" else "smtp"
    label = prefix.upper()
    if isinstance(exc, ProviderFoundationError):
        return exc.error_code, str(exc)
    text = str(exc).casefold()
    class_name = type(exc).__name__.casefold()
    if isinstance(exc, ssl.SSLError) or any(
        marker in text for marker in ("ssl", "tls", "certificate")
    ):
        return f"{prefix}_tls_failed", f"{label} TLS 连接失败"
    if isinstance(exc, (socket.timeout, TimeoutError)) or any(
        marker in text for marker in ("timeout", "timed out")
    ):
        return f"{prefix}_timeout", f"{label} 连接超时"
    if any(
        marker in text
        for marker in (
            "too many login",
            "too many connection",
            "rate limit",
            "temporarily blocked",
            "try again later",
        )
    ):
        return f"{prefix}_rate_limited", f"{label} 连接频率受限，请稍后重试"
    if (
        isinstance(exc, smtplib.SMTPAuthenticationError)
        or "loginerror" in class_name
        or any(
            marker in text
            for marker in (
                "authenticationfailed",
                "authentication failed",
                "invalid credential",
                "invalid password",
                "[auth",
            )
        )
    ):
        return (
            f"{prefix}_auth_failed",
            f"{label} 认证失败，请检查账号授权码或应用专用密码",
        )
    if (
        isinstance(
            exc,
            (
                ConnectionRefusedError,
                socket.gaierror,
            ),
        )
        or any(
            marker in text
            for marker in (
                "[unavailable]",
                "connection refused",
                "network is unreachable",
                "no route to host",
                "name or service not known",
            )
        )
    ):
        return f"{prefix}_unavailable", f"{label} 服务器暂时不可用"
    if (
        isinstance(exc, smtplib.SMTPServerDisconnected)
        or isinstance(exc, (ConnectionAbortedError, ConnectionResetError))
        or "abort" in class_name
        or any(
            marker in text
            for marker in ("bye", "disconnect", "connection reset")
        )
    ):
        return f"{prefix}_disconnected", f"{label} 连接已断开"
    return f"{prefix}_connection_failed", f"{label} 连接失败：{type(exc).__name__}"


def detect_provider_profile(email_address: str) -> ProviderProfile | None:
    domain = str(email_address or "").strip().casefold().partition("@")[2]
    return next(
        (profile for profile in PROVIDER_PROFILES if domain in profile.domains),
        None,
    )


def validate_server_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """校验 Generic 配置；禁止明文传输和秘密落入 provider_settings。"""
    safe = validate_non_secret_provider_settings(settings)
    result: dict[str, Any] = {}
    for protocol, default_port in (("imap", 993), ("smtp", 465)):
        host = str(safe.get(f"{protocol}_host") or "").strip().casefold()
        if host:
            if any(char.isspace() for char in host) or "/" in host or "\\" in host:
                raise ProviderFoundationError(
                    "invalid_server_host", f"{protocol.upper()} 服务器地址无效"
                )
            try:
                port = int(safe.get(f"{protocol}_port") or default_port)
            except (TypeError, ValueError) as exc:
                raise ProviderFoundationError(
                    "invalid_server_port", f"{protocol.upper()} 端口无效"
                ) from exc
            if not 1 <= port <= 65535:
                raise ProviderFoundationError(
                    "invalid_server_port", f"{protocol.upper()} 端口无效"
                )
            security = str(
                safe.get(f"{protocol}_security") or SECURITY_SSL
            ).strip().casefold()
            if security not in SUPPORTED_TRANSPORT_SECURITY:
                raise ProviderFoundationError(
                    "insecure_transport_rejected",
                    f"{protocol.upper()} 必须使用 SSL/TLS 或 STARTTLS",
                )
            result[f"{protocol}_host"] = host
            result[f"{protocol}_port"] = port
            result[f"{protocol}_security"] = security
    result["profile_id"] = str(safe.get("profile_id") or "manual").strip()[:80]
    inbox_name = str(safe.get("inbox_name") or "INBOX").strip()
    result["inbox_name"] = inbox_name[:255] or "INBOX"
    try:
        uid_overlap = int(safe.get("uid_overlap") or 10)
    except (TypeError, ValueError) as exc:
        raise ProviderFoundationError(
            "invalid_uid_overlap", "UID 重叠扫描数量无效"
        ) from exc
    result["uid_overlap"] = max(0, min(uid_overlap, 100))
    try:
        timeout = int(safe.get("connect_timeout") or 20)
    except (TypeError, ValueError) as exc:
        raise ProviderFoundationError(
            "invalid_connect_timeout", "连接超时时间无效"
        ) from exc
    result["connect_timeout"] = max(5, min(timeout, 120))
    return result


def validate_non_secret_provider_settings(
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    """所有 Provider 共用的持久化秘密拒绝边界。"""
    safe = dict(settings or {})
    if any(
        str(key).casefold() in FORBIDDEN_PROVIDER_SETTING_KEYS
        for key in safe
    ):
        raise ProviderFoundationError(
            "secret_in_provider_settings", "Provider 设置不能保存密码、Token 或授权码"
        )
    return safe


def _mailbox_role(flags: Any, name: str) -> str:
    normalized_flags = {
        (item.decode("ascii", errors="ignore") if isinstance(item, bytes) else str(item))
        .strip()
        .casefold()
        for item in (flags or ())
    }
    for flag, role in SPECIAL_USE_ROLES.items():
        if flag in normalized_flags:
            return role
    if mailbox_text(name).casefold() == "inbox":
        return "inbox"
    return "other"


def discover_imap_mailboxes(
    *,
    settings: dict[str, Any],
    username: str,
    secret: str,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """使用 IMAPClient 解析 LIST/SPECIAL-USE 与 UID checkpoint。"""
    safe = validate_server_settings(settings)
    if not safe.get("imap_host"):
        raise ProviderFoundationError("imap_not_configured", "尚未配置 IMAP 服务器")
    if not str(username).strip() or not secret:
        raise ProviderFoundationError("imap_auth_required", "IMAP 账号或凭据缺失")
    if client_factory is None:
        try:
            from imapclient import IMAPClient
        except ImportError as exc:
            raise ProviderFoundationError(
                "imapclient_missing", "缺少 IMAPClient 运行依赖"
            ) from exc
        client_factory = IMAPClient
    use_ssl = safe["imap_security"] == SECURITY_SSL
    client: Any | None = None
    try:
        client = client_factory(
            safe["imap_host"],
            port=safe["imap_port"],
            ssl=use_ssl,
            timeout=safe["connect_timeout"],
            use_uid=True,
        )
        if not use_ssl:
            client.starttls(ssl_context=ssl.create_default_context())
        client.login(str(username).strip(), secret)
        capabilities = sorted(
            item.decode("ascii", errors="ignore")
            if isinstance(item, bytes)
            else str(item)
            for item in client.capabilities()
        )
        mailboxes: list[dict[str, Any]] = []
        for flags, delimiter, name in client.list_folders():
            normalized_name = mailbox_text(name)
            role = _mailbox_role(flags, normalized_name)
            item: dict[str, Any] = {
                "external_ref": normalized_name,
                "display_name": normalized_name,
                "delimiter": (
                    delimiter.decode("ascii", errors="ignore")
                    if isinstance(delimiter, bytes)
                    else str(delimiter or "")
                ),
                "flags": sorted(
                    flag.decode("ascii", errors="ignore")
                    if isinstance(flag, bytes)
                    else str(flag)
                    for flag in flags
                ),
                "mailbox_role": role,
            }
            if role == "inbox":
                selected = client.select_folder(name, readonly=True)
                item["checkpoint"] = {
                    "uidvalidity": int(selected.get(b"UIDVALIDITY") or 0),
                    "uidnext": int(selected.get(b"UIDNEXT") or 0),
                    "highestmodseq": int(selected.get(b"HIGHESTMODSEQ") or 0),
                }
            mailboxes.append(item)
        return {"capabilities": capabilities, "mailboxes": mailboxes}
    except ProviderFoundationError:
        raise
    except Exception as exc:
        code, message = classify_protocol_error("imap", exc)
        raise ProviderFoundationError(code, message) from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def test_smtp_connection(
    *,
    settings: dict[str, Any],
    username: str,
    secret: str,
    smtp_factory: Callable[..., Any] | None = None,
    smtp_ssl_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """只认证后退出，不发送邮件。"""
    safe = validate_server_settings(settings)
    if not safe.get("smtp_host"):
        raise ProviderFoundationError("smtp_not_configured", "尚未配置 SMTP 服务器")
    if not str(username).strip() or not secret:
        raise ProviderFoundationError("smtp_auth_required", "SMTP 账号或凭据缺失")
    context = ssl.create_default_context()
    smtp_factory = smtp_factory or smtplib.SMTP
    smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL
    client: Any | None = None
    try:
        if safe["smtp_security"] == SECURITY_SSL:
            client = smtp_ssl_factory(
                safe["smtp_host"],
                safe["smtp_port"],
                timeout=safe["connect_timeout"],
                context=context,
            )
        else:
            client = smtp_factory(
                safe["smtp_host"],
                safe["smtp_port"],
                timeout=safe["connect_timeout"],
            )
        client.ehlo()
        if safe["smtp_security"] == SECURITY_STARTTLS:
            client.starttls(context=context)
            client.ehlo()
        client.login(str(username).strip(), secret)
        return {
            "smtp_host": safe["smtp_host"],
            "smtp_port": safe["smtp_port"],
            "authenticated": True,
        }
    except ProviderFoundationError:
        raise
    except Exception as exc:
        code, message = classify_protocol_error("smtp", exc)
        raise ProviderFoundationError(code, message) from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
