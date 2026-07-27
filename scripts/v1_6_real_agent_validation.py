"""使用真实 Codex、Claude Code 或 Hermes 验证 v1.6 Agent 邮件工作流。

脚本只把脱敏结论写入证据文件。Agent 的原始输出仅在内存中用于判定，
不会写入日志或报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.agent_integration import AgentAccessError
from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.database import close_connection, query_recent_mcp_audit_events
from agent_mail_bridge.mcp_client_config import (
    SERVER_KEY,
    client_config_path,
    detect_client,
)
from agent_mail_bridge.version import __version__


CLIENTS = {"codex", "claude_code", "hermes"}
CURRENT_MARKER = f"[AMB-v{__version__}-E2E] MIME-qq-to-163-"
DISPLAY_NAMES = {
    "codex": "Codex v1.6 真实验收",
    "claude_code": "Claude Code v1.6 真实验收",
    "hermes": "Hermes v1.6 真实验收",
}
EXPECTED_TOOLS = {
    "search_mails",
    "get_mail",
    "read_mail_resource",
    "prepare_mail_resources",
}


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _other_config_state(client_type: str, path: Path) -> tuple[str, set[str]]:
    if not path.is_file():
        return _fingerprint("{}"), set()
    raw = path.read_bytes()
    comments = {
        line.strip()
        for line in raw.decode("utf-8-sig", errors="replace").splitlines()
        if line.strip().startswith("#")
    }
    if client_type == "codex":
        data: Any = tomllib.loads(raw.decode("utf-8-sig"))
        servers = data.get("mcp_servers")
        server_container = "mcp_servers"
    elif client_type == "hermes":
        yaml = YAML(typ="safe")
        data = yaml.load(raw.decode("utf-8-sig")) or {}
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
        server_container = "mcp_servers"
    else:
        data = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        server_container = "mcpServers"
    if isinstance(servers, dict):
        servers.pop(SERVER_KEY, None)
        if not servers and isinstance(data, dict):
            data.pop(server_container, None)
    canonical = json.dumps(
        _plain(data), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return _fingerprint(canonical), comments


def _native_codex(wrapper: str) -> str:
    npm_root = Path(wrapper).resolve().parent
    candidates = list(
        (
            npm_root
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
        ).glob("codex-win32-*/vendor/*/bin/codex.exe")
    )
    return str(candidates[0]) if candidates else wrapper


def _native_claude(wrapper: str) -> str:
    candidate = (
        Path(wrapper).resolve().parent
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "bin"
        / "claude.exe"
    )
    return str(candidate) if candidate.is_file() else wrapper


def _command(client_type: str, prompt: str, workspace: Path) -> list[str]:
    if client_type == "codex":
        executable = shutil.which("codex")
        if not executable:
            raise FileNotFoundError("codex")
        return [
            _native_codex(executable),
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "workspace-write",
            "-C",
            str(workspace),
            "-c",
            'mcp_servers.agent-mail-bridge.default_tools_approval_mode="approve"',
            prompt,
        ]
    if client_type == "claude_code":
        executable = shutil.which("claude")
        if not executable:
            raise FileNotFoundError("claude")
        return [
            _native_claude(executable),
            "-p",
            prompt,
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            (
                "mcp__agent-mail-bridge__search_mails,"
                "mcp__agent-mail-bridge__get_mail,"
                "mcp__agent-mail-bridge__read_mail_resource,"
                "mcp__agent-mail-bridge__prepare_mail_resources,"
                "mcp__agent-mail-bridge__list_agent_workspaces"
            ),
        ]
    executable = shutil.which("hermes")
    if not executable:
        raise FileNotFoundError("hermes")
    return [executable, "--oneshot", prompt]


def _run(
    command: list[str],
    workspace: Path,
    *,
    env_overrides: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    if command[0].casefold().endswith((".cmd", ".bat")):
        command = [
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(command),
        ]
    process_env = os.environ.copy()
    process_env.update(env_overrides)
    return subprocess.run(
        command,
        cwd=workspace,
        env=process_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=420,
        check=False,
    )


def _tool_prompt(
    client_type: str,
    *,
    qq_account_id: str,
    account_163_id: str,
    workspace_id: str,
) -> str:
    history_step = (
        " Also search account_id "
        f"{qq_account_id} with time_scope date_range, date_from 2024-01-01, "
        "date_to 2024-12-31, ensure_fresh false, limit 1; confirm one "
        "archived 2024 mail is returned, without quoting its content."
    )
    return (
        "Use only the AgentMailBridge MCP tools; do not use shell, filesystem, "
        "browser, send, or submit_result. Search account_id "
        f"{account_163_id} for query {CURRENT_MARKER} with ensure_fresh false "
        "and limit 1. Call get_mail on the result. From its resource list, call "
        "read_mail_resource on one Markdown or CSV attachment using the suitable "
        "bounded text/csv preview mode. Then call prepare_mail_resources on that "
        f"mail with mode complete, target_workspace {workspace_id}, and "
        "overwrite_policy rename."
        f"{history_step} "
        "Never reproduce mail body, subject, addresses, IDs, paths, filenames, "
        "tokens, or resource content in the final answer. Finish with exactly PASS."
    )


def _create_or_reuse_client(
    service: ApplicationService,
    client_type: str,
    *,
    qq_account_id: str,
    account_163_id: str,
    workspace_id: str,
) -> tuple[str, str, dict[str, Any]]:
    clients = service.list_agent_clients(include_revoked=False).details.get(
        "clients", []
    )
    existing = next(
        (
            item
            for item in clients
            if item.get("client_type") == client_type
            and item.get("display_name") == DISPLAY_NAMES[client_type]
        ),
        None,
    )
    if existing:
        client_id = str(existing["client_id"])
        token = service._agent_integration.get_scoped_token(client_id)
        created_now = False
    else:
        if client_type == "claude_code":
            created = service.create_agent_client(
                client_type=client_type,
                display_name=DISPLAY_NAMES[client_type],
                config_mode="managed",
                capabilities=[
                    "mail.search",
                    "mail.get",
                    "resource.read",
                    "resource.prepare",
                    "workspace.list",
                    "sync.status",
                ],
                account_ids=[account_163_id, qq_account_id],
                workspace_ids=[workspace_id],
                permission_mode="custom",
                account_scope_mode="selected",
                workspace_scope_mode="selected",
            )
        else:
            created = service.create_agent_client(
                client_type=client_type,
                display_name=DISPLAY_NAMES[client_type],
                config_mode="managed",
                permission_mode="recommended",
                account_scope_mode="all",
                workspace_scope_mode="all",
            )
        if not created.ok:
            raise RuntimeError(created.error_code or "client_create_failed")
        client_id = str(created.details["client"]["client_id"])
        token = str(created.details["scoped_token"])
        created_now = True

    if client_type == "claude_code":
        permissions = service.set_agent_client_permissions(
            client_id,
            capabilities=[
                "mail.search",
                "mail.get",
                "resource.read",
                "resource.prepare",
                "workspace.list",
                "sync.status",
            ],
            account_ids=[account_163_id, qq_account_id],
            workspace_ids=[workspace_id],
            permission_mode="custom",
            account_scope_mode="selected",
            workspace_scope_mode="selected",
        )
    else:
        permissions = service.set_agent_client_permissions(
            client_id,
            capabilities=[],
            account_ids=[],
            workspace_ids=[],
            permission_mode="recommended",
            account_scope_mode="all",
            workspace_scope_mode="all",
        )
    if not permissions.ok:
        raise RuntimeError(permissions.error_code or "permission_save_failed")
    activated = service.set_agent_client_state(client_id, "active", enabled=True)
    if not activated.ok:
        raise RuntimeError(activated.error_code or "client_activate_failed")
    return client_id, token, {"created_now": created_now}


def _audit_checks(
    service: ApplicationService,
    client_id: str,
    *,
    require_history: bool,
    after_id: int = 0,
) -> dict[str, Any]:
    rows = [
        row
        for row in query_recent_mcp_audit_events(service.cfg.db_path, 500)
        if str(row.get("client_id") or "") == client_id
        and int(row.get("id") or 0) > int(after_id)
    ]
    successful = {
        str(row.get("tool_name") or "")
        for row in rows
        if row.get("status") == "success"
    }
    current_search = any(
        row.get("tool_name") == "search_mails"
        and row.get("status") == "success"
        and CURRENT_MARKER in str(row.get("query_summary") or "")
        for row in rows
    )
    history_search = any(
        row.get("tool_name") == "search_mails"
        and row.get("status") == "success"
        and "date_from=2024-01-01" in str(row.get("query_summary") or "")
        and "date_to=2024-12-31" in str(row.get("query_summary") or "")
        for row in rows
    )
    complete = False
    for row in rows:
        if (
            row.get("tool_name") != "prepare_mail_resources"
            or row.get("status") != "success"
        ):
            continue
        try:
            details = json.loads(str(row.get("details_json") or "{}"))
        except json.JSONDecodeError:
            continue
        if (
            details.get("mode") == "complete"
            and details.get("atomic_publish") is True
            and details.get("source_archive_unchanged") is True
            and len(details.get("hashes") or []) >= 5
        ):
            complete = True
            break
    return {
        "expected_tools": EXPECTED_TOOLS.issubset(successful),
        "current_mail_search": current_search,
        "history_2024_search": history_search if require_history else True,
        "complete_atomic_package": complete,
        "successful_tool_names": sorted(successful & EXPECTED_TOOLS),
    }


def _write_evidence(evidence: dict[str, Any], output: Path) -> None:
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Real Agent evidence written: {target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-external-agent", action="store_true")
    parser.add_argument("--reuse-agent-audit", action="store_true")
    args = parser.parse_args()
    if not args.confirm_external_agent:
        raise SystemExit(
            "Refusing external Agent execution without --confirm-external-agent"
        )

    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    service = ApplicationService(cfg)
    service.initialize()
    original_read_gate = bool(cfg.mcp_mail_read_enabled)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "client": args.client,
        "checks": {},
    }
    checks = evidence["checks"]
    token = ""
    client_id = ""
    try:
        if not original_read_gate:
            enabled = service.set_mcp_mail_read_access(True)
            if not enabled.ok:
                raise RuntimeError(enabled.error_code or "read_gate_enable_failed")

        accounts = service.list_mail_accounts().details.get("accounts") or []
        qq = next(
            item
            for item in accounts
            if item.get("provider") == "qq"
            and item.get("enabled")
            and item.get("receive_enabled")
        )
        account_163 = next(
            item
            for item in accounts
            if item.get("provider") == "163"
            and item.get("enabled")
            and item.get("receive_enabled")
        )
        qq_account_id = str(qq["account_id"])
        account_163_id = str(account_163["account_id"])
        current = service.search_mail_facts(
            CURRENT_MARKER, account_id=account_163_id, limit=1
        )
        history = service.search_mail_facts(
            "",
            account_id=qq_account_id,
            date_from="2024-01-01 00:00:00",
            date_to="2024-12-31 23:59:59",
            limit=1,
        )
        checks["real_mail_fixtures"] = {
            "status": (
                "PASS"
                if current.details.get("messages")
                and history.details.get("messages")
                else "FAIL"
            )
        }
        workspace_rows = service.list_agent_workspaces().details.get(
            "workspace_details", []
        )
        workspace_row = next(
            item
            for item in workspace_rows
            if Path(str(item["display_path"])).resolve()
            == workspace.parent.resolve()
            or Path(str(item["display_path"])).resolve() == workspace.resolve()
        )
        workspace_id = str(workspace_row["workspace_id"])

        detection = detect_client(args.client)
        checks["supported_installed_version"] = {
            "status": (
                "PASS"
                if detection.installed
                and detection.status == "managed_supported"
                else "FAIL"
            ),
            "detection_status": detection.status,
            "version": str(detection.version or "").splitlines()[0],
        }
        client_id, token, client_facts = _create_or_reuse_client(
            service,
            args.client,
            qq_account_id=qq_account_id,
            account_163_id=account_163_id,
            workspace_id=workspace_id,
        )
        evidence["client_fingerprint"] = _fingerprint(client_id)
        evidence["client_created_now"] = client_facts["created_now"]

        config_path = client_config_path(args.client)
        before_digest, before_comments = _other_config_state(
            args.client, config_path
        )
        preview = service.preview_agent_client_config(client_id)
        if not preview.ok:
            raise RuntimeError(preview.error_code or "config_preview_failed")
        preview_text = str(preview.details.get("preview") or "")
        checks["redacted_preview"] = {
            "status": "PASS" if token not in preview_text else "FAIL"
        }
        applied = service.apply_agent_client_config(
            str(preview.details["plan_id"])
        )
        if not applied.ok:
            raise RuntimeError(applied.error_code or "config_apply_failed")
        after_digest, after_comments = _other_config_state(
            args.client, config_path
        )
        checks["managed_config_preserves_other_fields"] = {
            "status": (
                "PASS"
                if before_digest == after_digest
                and before_comments.issubset(after_comments)
                else "FAIL"
            ),
            "backup_created": bool(applied.details.get("backup")),
        }

        connected = service.test_agent_client_connection(client_id)
        checks["seven_tool_connection"] = {
            "status": (
                "PASS"
                if connected.ok and connected.details.get("tool_count") == 7
                else "FAIL"
            ),
            "tool_count": int(connected.details.get("tool_count") or 0),
        }

        identity = service.resolve_agent_identity(client_id, token)
        if args.client == "claude_code":
            other_account_id = next(
                (
                    str(item["account_id"])
                    for item in accounts
                    if str(item.get("account_id") or "")
                    not in {qq_account_id, account_163_id}
                ),
                "",
            )
            denied_code = ""
            try:
                service.require_agent_account(identity, other_account_id)
            except AgentAccessError as exc:
                denied_code = exc.code
            checks["selected_scope_denies_other_account"] = {
                "status": (
                    "PASS"
                    if other_account_id and denied_code == "account_denied"
                    else "FAIL"
                )
            }
        else:
            checks["dynamic_all_scopes"] = {
                "status": (
                    "PASS"
                    if identity.account_scope_mode == "all"
                    and identity.workspace_scope_mode == "all"
                    else "FAIL"
                )
            }

        prompt = _tool_prompt(
            args.client,
            qq_account_id=qq_account_id,
            account_163_id=account_163_id,
            workspace_id=workspace_id,
        )
        audit_after_id = 0
        if args.reuse_agent_audit:
            prior = (
                json.loads(args.output.read_text(encoding="utf-8"))
                if args.output.is_file()
                else {}
            )
            prior_process = dict(
                (prior.get("checks") or {}).get("real_agent_process") or {}
            )
            prior_workflow = dict(
                (prior.get("checks") or {}).get("real_agent_mcp_workflow") or {}
            )
            if (
                prior_process.get("status") != "PASS"
                or prior_workflow.get("status") != "PASS"
            ):
                raise RuntimeError("prior_agent_evidence_not_reusable")
            checks["real_agent_process"] = {
                **prior_process,
                "reused_prior_process": True,
            }
        else:
            audit_after_id = max(
                (
                    int(row.get("id") or 0)
                    for row in query_recent_mcp_audit_events(
                        service.cfg.db_path, 500
                    )
                    if str(row.get("client_id") or "") == client_id
                ),
                default=0,
            )
            started = time.monotonic()
            completed = _run(
                _command(args.client, prompt, workspace),
                workspace,
                env_overrides={
                    "MCP_MAIL_READ_ENABLED": "true",
                    "ALLOWED_SEND_ROOTS": os.pathsep.join(
                        str(path) for path in cfg.effective_allowed_send_roots
                    ),
                },
            )
            elapsed = round(time.monotonic() - started, 2)
            combined = (completed.stdout or "") + (completed.stderr or "")
            checks["real_agent_process"] = {
                "status": (
                    "PASS"
                    if completed.returncode == 0
                    and token not in combined
                    else "FAIL"
                ),
                "exit_code": completed.returncode,
                "elapsed_seconds": elapsed,
                "token_exposed": token in combined,
                "completion_marker_seen": "PASS" in combined,
            }
        close_connection()
        audit = _audit_checks(
            service,
            client_id,
            require_history=True,
            after_id=audit_after_id,
        )
        checks["real_agent_mcp_workflow"] = {
            "status": "PASS" if all(
                value
                for key, value in audit.items()
                if key != "successful_tool_names"
            )
            else "FAIL",
            **audit,
        }

        paused = service.set_agent_client_state(client_id, "paused")
        paused_code = ""
        try:
            service.resolve_agent_identity(client_id, token)
        except AgentAccessError as exc:
            paused_code = exc.code
        resumed = service.set_agent_client_state(
            client_id, "active", enabled=True
        )
        resumed_ok = False
        if resumed.ok:
            resumed_ok = (
                service.resolve_agent_identity(client_id, token).client_id
                == client_id
            )
        checks["pause_and_resume_immediate"] = {
            "status": (
                "PASS"
                if paused.ok
                and paused_code == "client_disabled"
                and resumed_ok
                else "FAIL"
            )
        }

        old_token = token
        rotated = service.rotate_agent_client_token(client_id)
        if not rotated.ok:
            raise RuntimeError(rotated.error_code or "token_rotation_failed")
        token = str(rotated.details["scoped_token"])
        old_code = ""
        try:
            service.resolve_agent_identity(client_id, old_token)
        except AgentAccessError as exc:
            old_code = exc.code
        new_ok = (
            service.resolve_agent_identity(client_id, token).client_id
            == client_id
        )
        checks["coordinated_token_rotation"] = {
            "status": (
                "PASS"
                if old_code == "client_auth_failed"
                and new_ok
                and rotated.details.get("config_updated") is True
                else "FAIL"
            )
        }

        backups = service.list_agent_client_config_backups(
            client_id
        ).details.get("backups", [])
        restored = (
            service.restore_agent_client_config(str(backups[0]["backup_id"]))
            if backups
            else None
        )
        reapplied_ok = False
        if restored and restored.ok:
            repreview = service.preview_agent_client_config(client_id)
            if repreview.ok:
                reapplied = service.apply_agent_client_config(
                    str(repreview.details["plan_id"])
                )
                reapplied_ok = reapplied.ok
        checks["backup_restore_and_reapply"] = {
            "status": (
                "PASS"
                if restored is not None and restored.ok and reapplied_ok
                else "FAIL"
            )
        }
        final_connection = service.test_agent_client_connection(client_id)
        checks["post_rotation_connection"] = {
            "status": "PASS" if final_connection.ok else "FAIL"
        }
    except (
        AgentAccessError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        StopIteration,
        subprocess.TimeoutExpired,
        tomllib.TOMLDecodeError,
    ) as exc:
        evidence["failure_type"] = type(exc).__name__
        evidence["failure_code"] = str(
            getattr(exc, "code", "") or getattr(exc, "args", [""])[0]
        )[:120]
    finally:
        if not original_read_gate:
            restored_gate = service.set_mcp_mail_read_access(False)
            checks["read_gate_restored"] = {
                "status": "PASS" if restored_gate.ok else "FAIL"
            }

    evidence["overall"] = (
        "PASS"
        if checks
        and all(
            isinstance(item, dict) and item.get("status") == "PASS"
            for item in checks.values()
        )
        and not evidence.get("failure_type")
        else "FAIL"
    )
    _write_evidence(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
