"""正式界面的非敏感本地配置保存与旧配置显式导入。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent_mail_bridge.runtime_paths import get_runtime_paths

_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_SECRET_KEYS = frozenset({"GMAIL_APP_PASSWORD", "QQ_AUTH_CODE"})


def default_env_path() -> Path:
    return get_runtime_paths().config_file


def save_env_values(
    values: dict[str, str],
    env_path: Path | None = None,
    *,
    allow_secret_keys: bool = False,
) -> None:
    """原子更新配置；普通调用拒绝写入非空秘密值。"""
    env_path = Path(env_path) if env_path is not None else default_env_path()
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = {
        key: str(value)
        for key, value in values.items()
        if allow_secret_keys or key not in _SECRET_KEYS or not str(value).strip()
    }
    output: list[str] = []

    for line in existing:
        match = _KEY_PATTERN.match(line)
        key = match.group(1) if match else ""
        if key in pending:
            output.append(f"{key}={_quote_env_value(pending.pop(key))}")
        else:
            output.append(line)

    if pending and output and output[-1].strip():
        output.append("")
    for key, value in pending.items():
        output.append(f"{key}={_quote_env_value(value)}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_path.with_name(f".{env_path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.replace(env_path)


@dataclass(frozen=True)
class ConfigImportResult:
    destination: Path
    imported_keys: tuple[str, ...]
    migrated_secret_keys: tuple[str, ...]


def import_legacy_env(
    source: Path,
    *,
    destination: Path | None = None,
    credential_service=None,
) -> ConfigImportResult:
    """经用户指定旧 .env 后，事务式导入配置和秘密。"""
    from dotenv import dotenv_values
    from agent_mail_bridge.credentials import (
        CredentialService,
        GMAIL_IMAP_SECRET,
        QQ_SMTP_SECRET,
    )

    source = Path(source).expanduser().resolve()
    target = Path(destination).expanduser().resolve() if destination else default_env_path()
    if not source.is_file():
        raise FileNotFoundError(f"旧配置文件不存在：{source}")
    if target.exists():
        raise FileExistsError(f"新配置已存在，未覆盖：{target}")
    if source == target:
        raise ValueError("旧配置和新配置不能是同一个文件")

    parsed = {key: str(value or "") for key, value in dotenv_values(source).items()}
    non_secret = {key: value for key, value in parsed.items() if key not in _SECRET_KEYS}
    secret_names = {
        "GMAIL_APP_PASSWORD": GMAIL_IMAP_SECRET,
        "QQ_AUTH_CODE": QQ_SMTP_SECRET,
    }
    service = credential_service or CredentialService()
    snapshots: dict[str, str | None] = {}
    migrated: list[str] = []

    def rollback_credentials() -> None:
        for env_key in reversed(migrated):
            name = secret_names[env_key]
            previous = snapshots[name]
            try:
                if previous:
                    service.set(name, previous)
                else:
                    service.delete(name)
            except Exception:
                pass

    try:
        for env_key, name in secret_names.items():
            value = parsed.get(env_key, "").strip()
            if not value:
                continue
            snapshots[name] = service.get(name)
            service.set(name, value)
            migrated.append(env_key)
        save_env_values(non_secret, target)
        if migrated:
            save_env_values(
                {key: "" for key in migrated},
                source,
                allow_secret_keys=True,
            )
    except Exception:
        target.unlink(missing_ok=True)
        rollback_credentials()
        raise

    return ConfigImportResult(
        destination=target,
        imported_keys=tuple(sorted(non_secret)),
        migrated_secret_keys=tuple(migrated),
    )


def _quote_env_value(value: str) -> str:
    """按 dotenv 规则安全引用值。"""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r", "\\r").replace("\n", "\\n")
    return f'"{escaped}"'
