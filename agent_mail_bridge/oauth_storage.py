"""Gmail OAuth 客户端配置的受控导入。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_mail_bridge.runtime_paths import get_runtime_paths


class OAuthImportError(ValueError):
    """OAuth 客户端配置不可安全导入。"""


def import_oauth_credentials(
    source: Path,
    *,
    destination: Path | None = None,
    replace: bool = False,
) -> Path:
    """验证并原子复制 credentials.json 到当前用户 OAuth 目录。"""

    source = Path(source).expanduser().resolve()
    target = (
        Path(destination).expanduser().resolve()
        if destination
        else get_runtime_paths().oauth_root / "credentials.json"
    )
    if not source.is_file():
        raise FileNotFoundError("选择的 OAuth 客户端配置文件不存在")
    try:
        raw = source.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OAuthImportError("OAuth 客户端配置不是有效的 UTF-8 JSON 文件") from exc
    node = payload.get("installed") or payload.get("web") if isinstance(payload, dict) else None
    if not isinstance(node, dict) or not node.get("client_id") or not node.get("client_secret"):
        raise OAuthImportError("OAuth 客户端配置缺少 installed/web 客户端字段")
    if target.exists() and not replace:
        raise FileExistsError("OAuth 客户端配置已存在；如需替换请显式确认")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(raw, encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
