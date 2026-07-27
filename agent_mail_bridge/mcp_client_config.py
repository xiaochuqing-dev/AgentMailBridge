"""MCP Client 发现、配置预览、备份、幂等合并与恢复。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from agent_mail_bridge.runtime_paths import get_runtime_paths


SERVER_KEY = "agent-mail-bridge"
CLIENT_ID_ENV = "AGENT_MAIL_BRIDGE_CLIENT_ID"
CLIENT_TOKEN_ENV = "AGENT_MAIL_BRIDGE_CLIENT_TOKEN"
SUPPORTED_CLIENTS = {
    "claude_code",
    "codex",
    "hermes",
    "claude_desktop",
    "custom",
}
KNOWN_VERSION_PATTERNS = {
    "codex": re.compile(r"^(?:codex-cli\s+)?0\.145\.0(?:\s|$)", re.IGNORECASE),
    "claude_code": re.compile(r"^2\.1\.220(?:\s|$)", re.IGNORECASE),
    "hermes": re.compile(r"^Hermes Agent v0\.19\.0(?:\s|$)", re.IGNORECASE),
}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_TOML_SERVER_HEADER = re.compile(
    r"^\s*\[\s*mcp_servers\.(?:agent-mail-bridge|\"agent-mail-bridge\"|'agent-mail-bridge')"
    r"(?:\.[^\]]+)?\s*\]\s*(?:#.*)?$"
)
_TOML_ANY_HEADER = re.compile(r"^\s*\[\[?.+\]?\]\s*(?:#.*)?$")


class ClientConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ClientDetection:
    client_type: str
    installed: bool
    executable: str | None
    version: str | None
    config_path: Path | None
    config_mode: str
    status: str


@dataclass(frozen=True)
class ConfigPlan:
    client_id: str
    client_type: str
    config_scope: str
    target_path: Path
    original_hash: str
    original_mtime_ns: int | None
    original_exists: bool
    planned_bytes: bytes
    applied_hash: str
    preview: str
    action: str


@dataclass(frozen=True)
class ConfigApplyResult:
    backup_id: str
    backup_path: Path
    original_hash: str
    applied_hash: str
    target_path: Path
    changed: bool


def mcp_launch() -> tuple[str, list[str]]:
    paths = get_runtime_paths()
    if paths.frozen:
        return str(paths.install_root / "AgentMailBridgeMCP.exe"), []
    return sys.executable, ["-m", "agent_mail_bridge.mcp_server"]


def mcp_client_command(
    client: str,
    *,
    client_id: str | None = None,
    client_token: str | None = None,
    scope: str = "user",
) -> str:
    command, args = mcp_launch()
    runtime = get_runtime_paths()
    normalized = str(client or "").strip().casefold()
    env_items: list[str] = []
    if not runtime.frozen:
        env_items.extend(["--env", f"PYTHONPATH={runtime.source_root}"])
    if client_id:
        env_items.extend(["--env", f"{CLIENT_ID_ENV}={client_id}"])
    if client_token:
        env_items.extend(["--env", f"{CLIENT_TOKEN_ENV}={client_token}"])
    if normalized in {"claude", "claude_code"}:
        prefix = [
            "claude",
            "mcp",
            "add",
            *env_items,
            "--transport",
            "stdio",
            "--scope",
            scope,
            SERVER_KEY,
            "--",
        ]
    elif normalized == "hermes":
        prefix = ["hermes", "mcp", "add", SERVER_KEY, "--command", command]
        hermes_env: list[str] = []
        for key_value_index in range(0, len(env_items), 2):
            if env_items[key_value_index] == "--env":
                hermes_env.append(env_items[key_value_index + 1])
        if hermes_env:
            prefix.extend(["--env", *hermes_env])
        if args:
            prefix.extend(["--args", *args])
        return subprocess.list2cmdline(prefix)
    else:
        prefix = [normalized or client, "mcp", "add", SERVER_KEY, *env_items, "--"]
    return subprocess.list2cmdline([*prefix, command, *args])


def generic_mcp_json(
    *,
    client_id: str | None = None,
    client_token: str | None = None,
) -> str:
    entry = _stdio_entry(client_id=client_id, client_token=client_token)
    return json.dumps(
        {"mcpServers": {SERVER_KEY: entry}},
        ensure_ascii=False,
        indent=2,
    )


def codex_mcp_toml(
    *,
    client_id: str | None = None,
    client_token: str | None = None,
) -> str:
    return _codex_server_block(
        _stdio_entry(client_id=client_id, client_token=client_token)
    ).strip()


def hermes_mcp_yaml(
    *,
    client_id: str | None = None,
    client_token: str | None = None,
) -> str:
    entry = _stdio_entry(client_id=client_id, client_token=client_token)
    return _merge_hermes_yaml(b"", entry, remove=False).decode("utf-8").strip()


def detect_client(
    client_type: str,
    *,
    config_scope: str = "user",
    project_root: Path | None = None,
) -> ClientDetection:
    normalized = str(client_type or "").strip().casefold()
    if normalized not in SUPPORTED_CLIENTS:
        raise ClientConfigError("unsupported_client_version", "不支持的 MCP Client 类型")
    executable, version = _detect_executable_version(normalized)
    installed = bool(executable) if normalized != "custom" else True
    config_path = (
        None
        if normalized == "custom"
        else client_config_path(
            normalized, config_scope=config_scope, project_root=project_root
        )
    )
    supported_version = _is_supported_version(normalized, version)
    mode = (
        "manual"
        if normalized == "custom"
        else "managed"
        if installed and supported_version
        else "assisted"
    )
    status = (
        "manual_only"
        if normalized == "custom"
        else "not_installed"
        if not installed
        else "managed_supported"
        if supported_version
        else "version_unverified"
    )
    return ClientDetection(
        client_type=normalized,
        installed=installed,
        executable=executable,
        version=version,
        config_path=config_path,
        config_mode=mode,
        status=status,
    )


def client_config_path(
    client_type: str,
    *,
    config_scope: str = "user",
    project_root: Path | None = None,
) -> Path:
    normalized = str(client_type or "").strip().casefold()
    scope = str(config_scope or "user").strip().casefold()
    if normalized == "codex":
        if scope == "project":
            if project_root is None:
                raise ClientConfigError(
                    "config_not_found", "项目级 Codex 配置需要明确项目根目录"
                )
            return Path(project_root).resolve() / ".codex" / "config.toml"
        codex_home = os.getenv("CODEX_HOME")
        return (
            Path(codex_home).expanduser().resolve()
            if codex_home
            else Path.home() / ".codex"
        ) / "config.toml"
    if normalized == "claude_code":
        if scope == "project":
            if project_root is None:
                raise ClientConfigError(
                    "config_not_found", "项目级 Claude Code 配置需要明确项目根目录"
                )
            return Path(project_root).resolve() / ".mcp.json"
        return Path.home() / ".claude.json"
    if normalized == "claude_desktop":
        appdata = os.getenv("APPDATA")
        if not appdata:
            raise ClientConfigError(
                "config_not_found", "未找到 Windows APPDATA 配置目录"
            )
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if normalized == "hermes":
        hermes_home = os.getenv("HERMES_HOME")
        if hermes_home:
            return Path(hermes_home).expanduser().resolve() / "config.yaml"
        local = os.getenv("LOCALAPPDATA")
        if os.name == "nt" and local:
            return Path(local) / "hermes" / "config.yaml"
        return Path.home() / ".hermes" / "config.yaml"
    raise ClientConfigError("config_not_found", "自定义 Client 没有可自动修改的配置路径")


def preview_client_config(
    *,
    client_id: str,
    client_type: str,
    client_token: str,
    config_scope: str = "user",
    project_root: Path | None = None,
    target_path: Path | None = None,
    action: str = "apply",
) -> ConfigPlan:
    normalized = str(client_type or "").strip().casefold()
    if normalized not in {
        "codex",
        "claude_code",
        "hermes",
        "claude_desktop",
    }:
        raise ClientConfigError(
            "unsupported_client_version", "该 Client 仅支持辅助或手动配置"
        )
    target = (
        Path(target_path).expanduser().resolve()
        if target_path is not None
        else client_config_path(
            normalized, config_scope=config_scope, project_root=project_root
        ).resolve()
    )
    try:
        original = target.read_bytes() if target.is_file() else b""
        mtime = target.stat().st_mtime_ns if target.is_file() else None
    except OSError as exc:
        raise ClientConfigError("config_not_found", "无法读取目标 Client 配置") from exc
    original_hash = _sha256(original)
    entry = _stdio_entry(client_id=client_id, client_token=client_token)
    remove = action == "remove"
    try:
        if normalized == "codex":
            planned = _merge_codex_toml(original, entry, remove=remove)
        elif normalized == "hermes":
            planned = _merge_hermes_yaml(original, entry, remove=remove)
        else:
            planned = _merge_json_config(original, entry, remove=remove)
    except ClientConfigError:
        raise
    except (UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ClientConfigError(
            "config_parse_failed", "Client 配置格式损坏，已拒绝覆盖"
        ) from exc
    applied_hash = _sha256(planned)
    preview = _config_preview(
        client_type=normalized,
        target=target,
        entry=entry,
        action=action,
    )
    return ConfigPlan(
        client_id=client_id,
        client_type=normalized,
        config_scope=config_scope,
        target_path=target,
        original_hash=original_hash,
        original_mtime_ns=mtime,
        original_exists=target.is_file(),
        planned_bytes=planned,
        applied_hash=applied_hash,
        preview=preview,
        action=action,
    )


def apply_client_config(
    plan: ConfigPlan,
    *,
    backup_root: Path,
) -> ConfigApplyResult:
    target = plan.target_path
    current_exists = target.is_file()
    try:
        current = target.read_bytes() if current_exists else b""
        current_mtime = target.stat().st_mtime_ns if current_exists else None
    except OSError as exc:
        raise ClientConfigError("config_not_found", "无法复核目标 Client 配置") from exc
    if (
        _sha256(current) != plan.original_hash
        or current_mtime != plan.original_mtime_ns
    ):
        raise ClientConfigError(
            "config_changed_concurrently", "配置已被其他程序修改，请重新预览"
        )
    backup_id = "cfg_" + uuid.uuid4().hex
    backup_dir = Path(backup_root).resolve() / plan.client_id
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        _protect_path(backup_dir, directory=True)
        if current_exists:
            backup_path = backup_dir / f"{backup_id}.bak"
            _atomic_replace(backup_path, current)
        else:
            backup_path = backup_dir / f"{backup_id}.missing"
            _atomic_replace(backup_path, b"")
        _protect_path(backup_path, directory=False)
    except OSError as exc:
        raise ClientConfigError("config_backup_failed", "创建 Client 配置备份失败") from exc
    changed = current != plan.planned_bytes
    if changed:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace(target, plan.planned_bytes)
            if _sha256(target.read_bytes()) != plan.applied_hash:
                raise OSError("配置写入后 Hash 复核失败")
        except OSError as exc:
            try:
                if current_exists:
                    _atomic_replace(target, current)
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
            raise ClientConfigError(
                "config_write_failed", "Client 配置写入失败，已尝试恢复原文件"
            ) from exc
    return ConfigApplyResult(
        backup_id=backup_id,
        backup_path=backup_path,
        original_hash=plan.original_hash,
        applied_hash=plan.applied_hash,
        target_path=target,
        changed=changed,
    )


def restore_client_config(
    *,
    target_path: Path,
    backup_path: Path,
    applied_hash: str | None,
) -> str:
    target = Path(target_path).resolve()
    backup = Path(backup_path).resolve()
    if not backup.is_file():
        raise ClientConfigError("config_restore_failed", "配置备份不存在")
    current = target.read_bytes() if target.is_file() else b""
    if applied_hash and _sha256(current) != applied_hash:
        raise ClientConfigError(
            "config_changed_concurrently", "当前配置已变化，已拒绝覆盖恢复"
        )
    try:
        if backup.suffix == ".missing":
            if target.exists():
                target.unlink()
            restored = b""
        else:
            restored = backup.read_bytes()
            _atomic_replace(target, restored)
        return _sha256(restored)
    except OSError as exc:
        raise ClientConfigError("config_restore_failed", "恢复 Client 配置失败") from exc


def _stdio_entry(
    *,
    client_id: str | None,
    client_token: str | None,
) -> dict[str, Any]:
    command, args = mcp_launch()
    env: dict[str, str] = {}
    runtime = get_runtime_paths()
    if not runtime.frozen:
        env["PYTHONPATH"] = str(runtime.source_root)
    if client_id:
        env[CLIENT_ID_ENV] = client_id
    if client_token:
        env[CLIENT_TOKEN_ENV] = client_token
    return {
        "type": "stdio",
        "command": command,
        "args": args,
        "env": env,
    }


def _merge_json_config(
    original: bytes,
    entry: dict[str, Any],
    *,
    remove: bool,
) -> bytes:
    if original:
        try:
            data = json.loads(original.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClientConfigError(
                "config_parse_failed", "JSON 配置损坏，已拒绝覆盖"
            ) from exc
    else:
        data = {}
    if not isinstance(data, dict):
        raise ClientConfigError("config_parse_failed", "JSON 配置根节点必须是对象")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError("config_parse_failed", "mcpServers 必须是对象")
    if remove:
        servers.pop(SERVER_KEY, None)
    else:
        servers[SERVER_KEY] = entry
    return (
        json.dumps(data, ensure_ascii=False, indent=2)
        .rstrip()
        .encode("utf-8")
        + b"\n"
    )


def _merge_codex_toml(
    original: bytes,
    entry: dict[str, Any],
    *,
    remove: bool,
) -> bytes:
    text = original.decode("utf-8-sig") if original else ""
    if text.strip():
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ClientConfigError(
                "config_parse_failed", "TOML 配置损坏，已拒绝覆盖"
            ) from exc
        servers = parsed.get("mcp_servers", {})
        if servers is not None and not isinstance(servers, dict):
            raise ClientConfigError(
                "config_parse_failed", "mcp_servers 必须是 TOML table"
            )
    lines = text.splitlines(keepends=True)
    retained: list[str] = []
    skipping = False
    for line in lines:
        if _TOML_SERVER_HEADER.match(line):
            skipping = True
            continue
        if skipping and _TOML_ANY_HEADER.match(line):
            skipping = False
        if not skipping:
            retained.append(line)
    merged = "".join(retained).rstrip()
    if not remove:
        block = _codex_server_block(entry).strip()
        merged = f"{merged}\n\n{block}" if merged else block
    planned = (merged.rstrip() + "\n").encode("utf-8") if merged else b""
    if planned:
        try:
            tomllib.loads(planned.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ClientConfigError(
                "config_parse_failed", "生成的 Codex 配置未通过 TOML 校验"
            ) from exc
    return planned


def _merge_hermes_yaml(
    original: bytes,
    entry: dict[str, Any],
    *,
    remove: bool,
) -> bytes:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    try:
        data = yaml.load(original.decode("utf-8-sig")) if original else CommentedMap()
    except Exception as exc:  # noqa: BLE001
        raise ClientConfigError(
            "config_parse_failed", "Hermes YAML 配置损坏，已拒绝覆盖"
        ) from exc
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise ClientConfigError(
            "config_parse_failed", "Hermes YAML 配置根节点必须是对象"
        )
    servers = data.get("mcp_servers")
    if servers is None:
        if remove:
            return original
        servers = CommentedMap()
        data["mcp_servers"] = servers
    if not isinstance(servers, dict):
        raise ClientConfigError(
            "config_parse_failed", "Hermes mcp_servers 必须是对象"
        )
    if remove:
        servers.pop(SERVER_KEY, None)
    else:
        hermes_entry = CommentedMap()
        hermes_entry["command"] = str(entry["command"])
        if entry.get("args"):
            hermes_entry["args"] = list(entry["args"])
        if entry.get("env"):
            hermes_entry["env"] = CommentedMap(
                (str(key), str(value))
                for key, value in dict(entry["env"]).items()
            )
        servers[SERVER_KEY] = hermes_entry
    stream = io.StringIO()
    try:
        yaml.dump(data, stream)
        planned = stream.getvalue().encode("utf-8")
        YAML(typ="safe").load(planned.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ClientConfigError(
            "config_parse_failed", "生成的 Hermes YAML 未通过校验"
        ) from exc
    return planned


def _codex_server_block(entry: dict[str, Any]) -> str:
    args = json.dumps(entry.get("args") or [], ensure_ascii=False)
    command = json.dumps(str(entry["command"]), ensure_ascii=False)
    lines = [
        f"[mcp_servers.{SERVER_KEY}]",
        f"command = {command}",
        f"args = {args}",
        "enabled = true",
        "startup_timeout_sec = 20",
    ]
    env = entry.get("env") or {}
    if env:
        lines.extend(["", f"[mcp_servers.{SERVER_KEY}.env]"])
        for key in sorted(env):
            lines.append(
                f"{key} = {json.dumps(str(env[key]), ensure_ascii=False)}"
            )
    return "\n".join(lines) + "\n"


def _detect_claude_desktop() -> str | None:
    candidates: list[Path] = []
    local = os.getenv("LOCALAPPDATA")
    program_files = os.getenv("ProgramFiles")
    if local:
        candidates.extend(
            [
                Path(local) / "AnthropicClaude" / "Claude.exe",
                Path(local) / "Programs" / "Claude" / "Claude.exe",
            ]
        )
    if program_files:
        candidates.append(Path(program_files) / "Claude" / "Claude.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


@lru_cache(maxsize=8)
def _detect_executable_version(
    client_type: str,
) -> tuple[str | None, str | None]:
    executable: str | None
    if client_type == "codex":
        executable = shutil.which("codex")
    elif client_type == "claude_code":
        executable = shutil.which("claude")
    elif client_type == "claude_desktop":
        executable = _detect_claude_desktop()
    elif client_type == "hermes":
        executable = shutil.which("hermes")
    else:
        return None, None
    version = (
        _safe_version(executable)
        if executable and client_type in {"codex", "claude_code", "hermes"}
        else None
    )
    return executable, version


def _safe_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join((completed.stdout or completed.stderr or "").split())
    text = re.split(
        r"\s+Install directory\s*:",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return text[:120] or None


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_supported_version(
    client_type: str, version: str | None
) -> bool:
    pattern = KNOWN_VERSION_PATTERNS.get(client_type)
    return bool(pattern and version and pattern.search(version.strip()))


def _config_preview(
    *,
    client_type: str,
    target: Path,
    entry: dict[str, Any],
    action: str,
) -> str:
    env_names = sorted(str(key) for key in (entry.get("env") or {}))
    command_name = Path(str(entry.get("command") or "")).name
    summary = {
        "操作": "移除" if action == "remove" else "新增或更新",
        "Client": client_type,
        "目标文件": _redacted_path(target),
        "配置项": SERVER_KEY,
        "command": command_name,
        "args": list(entry.get("args") or []),
        "环境变量名": env_names,
        "环境变量值": "全部隐藏",
        "写入保护": "先备份、Hash/mtime 冲突检测、原子替换、失败回滚",
        "生效方式": (
            "执行 /reload-mcp 或重启 Hermes"
            if client_type == "hermes"
            else "reload 或重启 Client"
        ),
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _redacted_path(path: Path) -> str:
    resolved = Path(path)
    parent = resolved.parent.name
    return str(Path(parent) / resolved.name) if parent else resolved.name


def _protect_path(path: Path, *, directory: bool) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        # Windows ACL 与受管目录策略可能拒绝 chmod；备份仍位于当前用户 DATA_ROOT。
        pass
