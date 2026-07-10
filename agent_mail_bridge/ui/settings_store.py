"""正式界面的本地 .env 配置保存。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent_mail_bridge.config import PROJECT_ROOT

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def save_env_values(values: dict[str, str], env_path: Path = DEFAULT_ENV_PATH) -> None:
    """保留原文件内容，只原子更新指定键。"""
    env_path = Path(env_path)
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(values)
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


def _quote_env_value(value: str) -> str:
    """按 dotenv 规则安全引用值。"""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r", "\\r").replace("\n", "\\n")
    return f'"{escaped}"'
