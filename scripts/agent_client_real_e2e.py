"""使用真实 Codex/Claude Code 可执行程序验证四个邮件事实工具。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    close_connection,
    get_mail_package,
    query_recent_mcp_audit_events,
)
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mcp_client_config import generic_mcp_json
from agent_mail_bridge.receive_rules import ALL_SCANNED
from agent_mail_bridge.utils import sha256_of_file


MARKER = "AMB-REAL-AGENT-E2E-20260726"
EXPECTED_TOOLS = {
    "search_mails",
    "get_mail",
    "read_mail_resource",
    "prepare_mail_resources",
}


def _close_runtime() -> None:
    close_connection()
    logging.shutdown()


def _cfg(root: Path, workspace: Path) -> AppConfig:
    cfg = AppConfig(
        gmail_address="synthetic@example.com",
        gmail_app_password="not-used",
        qq_email="sender@example.com",
        qq_auth_code="not-used",
        owner_gmail="owner@example.com",
        data_root=root / "data",
        gmail_api_credentials_path=root / "oauth" / "credentials.json",
        gmail_api_token_path=root / "oauth" / "token.json",
        max_attachment_mb=25,
        max_send_file_mb=25,
        log_level="WARNING",
    )
    cfg.mcp_mail_read_enabled = True
    cfg.allowed_send_roots = [workspace]
    cfg.receive_rule_mode = ALL_SCANNED
    return cfg


def _archive(cfg: AppConfig) -> tuple[str, str, str]:
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "owner@example.com"
    message["Message-ID"] = "<agent-client-real-e2e@local.test>"
    message["Subject"] = MARKER
    message.set_content("Synthetic deterministic mail body for Agent E2E.")
    content = b"agent-client-real-e2e-attachment"
    message.add_attachment(
        content,
        maintype="text",
        subtype="plain",
        filename="e2e.txt",
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="synthetic-e2e",
        thread_id="",
        uid="synthetic-e2e",
        received_at=now,
        saved_date=now[:10],
        max_attachment_bytes=cfg.max_attachment_bytes,
        mailbox_ref="imap:INBOX",
    )
    package_id = process_normalized_mail(cfg, normalized)["package_id"]
    service = ApplicationService(cfg)
    message_facts = service.get_mail_message(package_id).details["message"]
    resource = next(
        row
        for row in message_facts["resources"]
        if row.get("display_name") == "e2e.txt"
    )
    return package_id, str(resource["resource_id"]), str(resource["sha256"])


def _client_command(
    client: str,
    executable: str,
    *,
    config_path: Path,
    client_id: str,
    token: str,
    workspace: Path,
    server_env: dict[str, str] | None = None,
) -> list[str]:
    prompt = (
        "Use only the AgentMailBridge MCP tools. Search for the exact subject "
        f"{MARKER} with ensure_fresh set to false. Then call get_mail, read "
        "the e2e.txt resource, and prepare "
        "that same resource into the authorized workspace. Do not use shell or "
        "filesystem tools. Finish with the single word PASS."
    )
    if client == "claude_code":
        return [
            executable,
            "-p",
            prompt,
            "--strict-mcp-config",
            "--mcp-config",
            str(config_path),
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "mcp__agent-mail-bridge__search_mails,"
            "mcp__agent-mail-bridge__get_mail,"
            "mcp__agent-mail-bridge__read_mail_resource,"
            "mcp__agent-mail-bridge__prepare_mail_resources",
        ]
    command = json.dumps(sys.executable)
    args = json.dumps(["-m", "agent_mail_bridge.mcp_server"])
    codex_env_overrides: list[str] = []
    for key in (
        "AGENT_MAIL_BRIDGE_DISABLE_DOTENV",
        "AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE",
        "AGENT_MAIL_BRIDGE_HOME",
        "AGENT_MAIL_BRIDGE_CONFIG",
        "DATA_ROOT",
        "MCP_MAIL_READ_ENABLED",
        "ALLOWED_SEND_ROOTS",
        "PYTHONPATH",
    ):
        if server_env and key in server_env:
            codex_env_overrides.extend(
                [
                    "-c",
                    "mcp_servers.agent-mail-bridge.env."
                    f"{key}={json.dumps(server_env[key])}",
                ]
            )
    return [
        executable,
        "exec",
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        "workspace-write",
        "-C",
        str(workspace),
        "-c",
        f"mcp_servers.agent-mail-bridge.command={command}",
        "-c",
        f"mcp_servers.agent-mail-bridge.args={args}",
        "-c",
        "mcp_servers.agent-mail-bridge.default_tools_approval_mode=\"approve\"",
        "-c",
        "mcp_servers.agent-mail-bridge.env."
        f"AGENT_MAIL_BRIDGE_CLIENT_ID={json.dumps(client_id)}",
        "-c",
        "mcp_servers.agent-mail-bridge.env."
        f"AGENT_MAIL_BRIDGE_CLIENT_TOKEN={json.dumps(token)}",
        *codex_env_overrides,
        prompt,
    ]


def _run_command(command: list[str], env: dict[str, str], cwd: Path):
    if command[0].casefold().endswith((".cmd", ".bat")):
        command_line = subprocess.list2cmdline(command)
        command = ["cmd.exe", "/d", "/s", "/c", command_line]
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


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


def run(client: str) -> dict[str, object]:
    executable = shutil.which("codex" if client == "codex" else "claude")
    if not executable:
        return {"client": client, "status": "NOT_INSTALLED"}
    if client == "codex":
        executable = _native_codex(executable)
    else:
        executable = _native_claude(executable)
    with tempfile.TemporaryDirectory(prefix="amb-agent-e2e-") as raw_root:
        root = Path(raw_root)
        workspace = root / "workspace"
        workspace.mkdir()
        cfg = _cfg(root, workspace)
        service = ApplicationService(cfg)
        service.initialize()
        service.synchronize_mail_accounts()
        package_id, resource_id, expected_hash = _archive(cfg)
        package = get_mail_package(cfg.db_path, package_id) or {}
        account_id = str(package.get("account_id") or "")
        workspace_id = str(
            service.list_agent_workspaces().details["workspace_details"][0][
                "workspace_id"
            ]
        )
        created = service.create_agent_client(
            client_type=client,
            display_name=f"{client} real E2E",
            capabilities=[
                "mail.search",
                "mail.get",
                "resource.read",
                "resource.prepare",
                "workspace.list",
            ],
            account_ids=[account_id],
            workspace_ids=[workspace_id],
        )
        if not created.ok:
            _close_runtime()
            return {"client": client, "status": "SETUP_FAILED"}
        client_id = str(created.details["client"]["client_id"])
        token = str(created.details["scoped_token"])
        service.set_agent_client_state(client_id, "active", enabled=True)
        config_path = root / "mcp.json"
        config_path.write_text(
            generic_mcp_json(client_id=client_id, client_token=token),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "AGENT_MAIL_BRIDGE_DISABLE_DOTENV": "1",
                "AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE": "1",
                "AGENT_MAIL_BRIDGE_HOME": str(root / "runtime"),
                "AGENT_MAIL_BRIDGE_CONFIG": str(root / "runtime" / "config.env"),
                "DATA_ROOT": str(cfg.data_root_path),
                "MCP_MAIL_READ_ENABLED": "true",
                "ALLOWED_SEND_ROOTS": str(workspace),
                "PYTHONPATH": str(ROOT),
            }
        )
        command = _client_command(
            client,
            executable,
            config_path=config_path,
            client_id=client_id,
            token=token,
            workspace=workspace,
            server_env=env,
        )
        try:
            completed = _run_command(command, env, workspace)
        except subprocess.TimeoutExpired:
            _close_runtime()
            return {"client": client, "status": "TIMEOUT"}
        combined = (completed.stdout or "") + (completed.stderr or "")
        token_exposed = token in combined
        audit = query_recent_mcp_audit_events(cfg.db_path, 100)
        successful_tools = {
            str(row["tool_name"])
            for row in audit
            if row.get("client_id") == client_id
            and row.get("status") == "success"
        }
        prepared = list(
            (workspace / ".agentmailbridge" / "mail" / package_id).glob(
                "e2e*.txt"
            )
        )
        prepared_ok = bool(
            prepared
            and prepared[0].is_file()
            and sha256_of_file(prepared[0]) == expected_hash
        )
        passed = (
            completed.returncode == 0
            and not token_exposed
            and EXPECTED_TOOLS.issubset(successful_tools)
            and prepared_ok
        )
        result = {
            "client": client,
            "status": "PASS" if passed else "FAIL",
            "exit_code": completed.returncode,
            "audit_tools": sorted(successful_tools & EXPECTED_TOOLS),
            "prepared_hash_verified": prepared_ok,
            "token_exposed": token_exposed,
        }
        if not passed:
            safe_diagnostic = combined.replace(token, "[REDACTED]")
            safe_diagnostic = safe_diagnostic.replace(str(root), "[TEMP]")
            safe_diagnostic = safe_diagnostic.replace(str(ROOT), "[PROJECT]")
            result["diagnostic"] = " ".join(safe_diagnostic.split())[-4000:]
        _close_runtime()
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client", choices=("codex", "claude_code"), required=True
    )
    args = parser.parse_args()
    result = run(args.client)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "NOT_INSTALLED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
