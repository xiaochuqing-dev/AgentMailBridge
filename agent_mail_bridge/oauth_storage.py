"""Gmail OAuth Desktop credentials 的严格校验与原子存储。"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_mail_bridge.process_lock import ProcessLock
from agent_mail_bridge.runtime_paths import get_runtime_paths


MAX_OAUTH_CREDENTIALS_BYTES = 1024 * 1024
_CLIENT_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{5,}\.apps\.googleusercontent\.com$"
)
_AUTH_ENDPOINTS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
_TOKEN_ENDPOINTS = {
    "https://oauth2.googleapis.com/token",
    "https://accounts.google.com/o/oauth2/token",
}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OAuthImportError(ValueError):
    """OAuth 客户端配置不可安全导入。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class OAuthClientSummary:
    """不含 Client Secret 的安全凭据摘要。"""

    client_type: str
    client_id_suffix: str
    project_id: str


@dataclass(frozen=True)
class ValidatedOAuthCredentials:
    """仅供 OAuth 内部使用；repr 不得暴露完整配置。"""

    raw_bytes: bytes = field(repr=False)
    payload: dict[str, Any] = field(repr=False)
    summary: OAuthClientSummary

    @property
    def installed(self) -> dict[str, Any]:
        return self.payload["installed"]

    @property
    def client_id(self) -> str:
        return str(self.installed["client_id"])


def validate_oauth_credentials_file(path: Path | str) -> ValidatedOAuthCredentials:
    """严格验证 Google Desktop installed-app credentials.json。"""

    source = Path(path).expanduser()
    try:
        source = source.resolve(strict=True)
        stat = source.stat()
    except OSError as exc:
        raise OAuthImportError(
            "credentials_unreadable", "选择的 OAuth 客户端配置文件不存在或无法读取"
        ) from exc
    if not source.is_file():
        raise OAuthImportError(
            "credentials_unreadable", "选择的 OAuth 客户端配置不是普通文件"
        )
    if stat.st_size <= 0:
        raise OAuthImportError(
            "credentials_invalid_json", "OAuth 客户端配置文件为空"
        )
    if stat.st_size > MAX_OAUTH_CREDENTIALS_BYTES:
        raise OAuthImportError(
            "credentials_unreadable", "OAuth 客户端配置文件过大，已拒绝读取"
        )
    try:
        raw = source.read_bytes()
        if len(raw) > MAX_OAUTH_CREDENTIALS_BYTES:
            raise OAuthImportError(
                "credentials_unreadable", "OAuth 客户端配置文件过大，已拒绝读取"
            )
        text = raw.decode("utf-8-sig")
        payload = json.loads(text)
    except UnicodeError as exc:
        raise OAuthImportError(
            "credentials_invalid_json", "OAuth 客户端配置必须是有效的 UTF-8 JSON 文件"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OAuthImportError(
            "credentials_invalid_json", "OAuth 客户端配置不是有效的 JSON 文件"
        ) from exc
    except OSError as exc:
        raise OAuthImportError(
            "credentials_unreadable", "OAuth 客户端配置文件无法读取"
        ) from exc

    if not isinstance(payload, dict):
        raise OAuthImportError(
            "credentials_wrong_type", "OAuth 客户端配置顶层必须是 JSON 对象"
        )
    if set(payload) != {"installed"}:
        raise OAuthImportError(
            "credentials_wrong_type",
            "该 JSON 不是可用的 Desktop app OAuth Client。AgentMailBridge 不接受 Web application 凭据。",
        )
    installed = payload.get("installed")
    if not isinstance(installed, dict):
        raise OAuthImportError(
            "credentials_wrong_type", "OAuth 客户端配置缺少有效的 installed Desktop app 节点"
        )

    required_strings = ("client_id", "client_secret", "auth_uri", "token_uri")
    for field_name in required_strings:
        value = installed.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise OAuthImportError(
                "credentials_missing_field",
                f"Desktop app OAuth 客户端配置缺少必需字段：{field_name}",
            )

    client_id = installed["client_id"].strip()
    if not _CLIENT_ID_PATTERN.fullmatch(client_id):
        raise OAuthImportError(
            "credentials_missing_field", "Desktop app OAuth Client ID 格式无效"
        )
    auth_uri = installed["auth_uri"].strip()
    token_uri = installed["token_uri"].strip()
    if auth_uri not in _AUTH_ENDPOINTS or token_uri not in _TOKEN_ENDPOINTS:
        raise OAuthImportError(
            "credentials_invalid_endpoint",
            "OAuth 凭据包含非 Google 官方 HTTPS 授权端点，已拒绝导入。",
        )

    redirect_uris = installed.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthImportError(
            "credentials_missing_field",
            "Desktop app OAuth 客户端配置缺少 redirect_uris 字符串数组",
        )
    if any(not isinstance(uri, str) or not _valid_loopback_redirect(uri) for uri in redirect_uris):
        raise OAuthImportError(
            "credentials_invalid_endpoint",
            "Desktop app OAuth redirect_uris 必须是标准本地回环地址",
        )

    project_id = installed.get("project_id")
    safe_project_id = project_id.strip()[:80] if isinstance(project_id, str) else ""
    suffix = client_id.rsplit(".apps.googleusercontent.com", 1)[0][-8:]
    return ValidatedOAuthCredentials(
        raw_bytes=raw,
        payload=payload,
        summary=OAuthClientSummary(
            client_type="Desktop app",
            client_id_suffix=suffix,
            project_id=safe_project_id,
        ),
    )


def import_oauth_credentials(
    source: Path,
    *,
    destination: Path | None = None,
    replace: bool = False,
) -> Path:
    """验证并原子复制 credentials.json；失败时保留旧配置。"""

    validated = validate_oauth_credentials_file(source)
    target = (
        Path(destination).expanduser().resolve()
        if destination
        else get_runtime_paths().oauth_root / "credentials.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(target.parent / ".credentials.lock")
    if not lock.acquire(timeout=1.0):
        raise OAuthImportError(
            "credentials_unreadable", "另一个 AgentMailBridge 进程正在更新 OAuth 配置"
        )
    try:
        if target.exists() and not replace:
            raise FileExistsError("OAuth 客户端配置已存在；如需替换请显式确认")
        _atomic_replace_bytes(
            target,
            validated.raw_bytes,
            error_code="credentials_unreadable",
            error_message="OAuth 客户端配置保存失败，原有配置已保留",
        )
    finally:
        lock.release()
    return target


def atomic_write_private_text(
    target: Path | str,
    text: str,
    *,
    error_code: str = "token_save_failed",
    error_message: str = "OAuth Token 保存失败，原有 Token 已保留",
) -> None:
    """同目录落盘、fsync 后原子替换，避免半写文件。"""

    _atomic_replace_bytes(
        Path(target),
        text.encode("utf-8"),
        error_code=error_code,
        error_message=error_message,
    )


def _atomic_replace_bytes(
    target: Path,
    data: bytes,
    *,
    error_code: str,
    error_message: str,
) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    except OAuthImportError:
        raise
    except OSError as exc:
        raise OAuthImportError(error_code, error_message) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _valid_loopback_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in {"", "/"}:
        return False
    return port is None or 1 <= port <= 65535
