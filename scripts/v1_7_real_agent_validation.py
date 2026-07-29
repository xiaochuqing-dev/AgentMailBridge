"""Run privacy-safe v1.7 acceptance with real Codex, Claude Code, or Hermes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.database import (
    close_connection,
    query_mailboxes,
    query_recent_mcp_audit_events,
    set_mailbox_sync_enabled,
)
from agent_mail_bridge.mail_threading import query_sent_mappings
from agent_mail_bridge.mcp_client_config import (
    client_config_path,
    detect_client,
)
from agent_mail_bridge.send_requests import (
    get_send_request,
    list_send_requests,
)
from agent_mail_bridge.version import __version__


CLIENTS = {"codex", "claude_code", "hermes"}
SCENARIOS = {"read", "confirm_reply", "autonomous_send"}
DISPLAY_NAMES = {
    "codex": "Codex v1.7 真实验收",
    "claude_code": "Claude Code v1.7 真实验收",
    "hermes": "Hermes v1.7 真实验收",
}
CAPABILITIES = [
    "mail.search",
    "mail.get",
    "resource.read",
    "resource.prepare",
    "sync.status",
    "workspace.list",
    "mail.accounts.list",
    "mailboxes.list",
    "mail.send",
    "send.status",
]
MCP_TOOL_NAMES = [
    "search_mails",
    "get_mail",
    "read_mail_resource",
    "prepare_mail_resources",
    "list_agent_workspaces",
    "get_mail_sync_status",
    "list_mail_accounts",
    "list_mailboxes",
    "send_mail",
    "get_send_request_status",
]
READ_TOOL_NAMES = {
    "list_mail_accounts",
    "list_mailboxes",
    "search_mails",
    "get_mail",
    "read_mail_resource",
    "prepare_mail_resources",
}


def _activate_packaged_mcp_from_env() -> str:
    value = os.getenv("AGENT_MAIL_BRIDGE_PACKAGED_MCP", "").strip()
    if not value:
        return "source"
    executable = Path(value).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    source_config = ROOT / ".env"
    if (
        not os.getenv("AGENT_MAIL_BRIDGE_CONFIG", "").strip()
        and source_config.is_file()
    ):
        # The packaged executable normally uses LocalAppData. Real-candidate
        # validation must share the source process's explicitly selected data.
        os.environ["AGENT_MAIL_BRIDGE_CONFIG"] = str(source_config)
    import agent_mail_bridge.application_service as service_module
    import agent_mail_bridge.mcp_client_config as config_module

    runtime = SimpleNamespace(frozen=True, source_root=ROOT)
    config_module.mcp_launch = lambda: (str(executable), [])
    config_module.get_runtime_paths = lambda: runtime
    service_module.mcp_launch = lambda: (str(executable), [])
    return "packaged"


def _status(ok: bool, **facts: Any) -> dict[str, Any]:
    return {"status": "PASS" if ok else "FAIL", **facts}


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _write_evidence(evidence: dict[str, Any], output: Path) -> None:
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"v1.7 real Agent evidence written: {target}")


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
        allowed = ",".join(
            f"mcp__agent-mail-bridge__{name}" for name in MCP_TOOL_NAMES
        )
        return [
            _native_claude(executable),
            "-p",
            prompt,
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            allowed,
            "--max-turns",
            "24",
        ]
    executable = shutil.which("hermes")
    if not executable:
        raise FileNotFoundError("hermes")
    return [executable, "--oneshot", prompt]


def _run_agent(
    client_type: str,
    prompt: str,
    workspace: Path,
    *,
    cfg,
    timeout_seconds: int,
) -> dict[str, Any]:
    process_env = os.environ.copy()
    process_env.update(
        {
            "MCP_MAIL_READ_ENABLED": "true",
            "ALLOWED_SEND_ROOTS": os.pathsep.join(
                str(path) for path in cfg.effective_allowed_send_roots
            ),
        }
    )
    command = _command(client_type, prompt, workspace)
    if command[0].casefold().endswith((".cmd", ".bat")):
        command = [
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(command),
        ]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=process_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    combined = (completed.stdout or "") + (completed.stderr or "")
    return {
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "combined": combined,
    }


def _natural_prompt(
    scenario: str,
    *,
    marker: str,
    attachment_names: list[str],
) -> str:
    privacy = (
        "全程不要展示密码、授权码、OAuth Token、Client Token、完整邮箱地址、"
        "私人正文或附件内容；最终只简短说明是否完成以及是否等待用户确认。"
    )
    if scenario == "read":
        return (
            "请使用我已经授权的邮箱能力完成一次真实回归。先了解当前可读邮箱账号和"
            "邮箱目录；不要限定某一个年份。验收必须各包含至少一次仅限收件箱目录"
            "和仅限已发送目录的历史搜索，不能用两次全目录搜索代替。找出与 "
            "AgentMailBridge v1.7.0 有关的邮件，选择最近一封含可预览附件的邮件。"
            "读取邮件和一个附件，并把整封邮件的完整资料准备到当前项目。若相同资料"
            "已经存在且 Hash 一致，请复用，不要制造无意义的重命名副本。不要发送邮件。"
            + privacy
        )
    relative_names = "、".join(f"validation-input/{name}" for name in attachment_names)
    if scenario == "confirm_reply":
        return (
            "请先了解当前获准的邮箱账号和目录，在全部历史范围内找到一封 QQ 账号"
            "收件箱中的 AgentMailBridge 已收测试邮件；搜索时必须使用 "
            "direction=inbound，并选择详情中明确标为“收到”、发件人不是该 QQ "
            "账号且含附件的邮件，优先选择主题含 163-to-qq 的事实，不得选择已发送"
            "副本。读取并确认它确实含附件，然后使用已获准的 QQ 发件账号回复发件人；"
            "正文说明 "
            f"{marker} 确认发送测试成功，并附上当前项目中的 {relative_names}。"
            "这次必须在发送前让我确认；看到待确认状态后停止，不要尝试绕过确认或重复提交。"
            + privacy
        )
    return (
        "请先了解当前获准的邮箱账号和目录，并在全部历史范围内分别核对收件箱和"
        "已发送中的 AgentMailBridge v1.7.0 测试邮件，读取其中一封及一个附件。"
        "随后使用获准的 QQ 发件账号向我已配置的 163 邮箱账号新建一封邮件，"
        f"主题为 {marker}，正文说明自主发送真实验收成功，并附上当前项目中的 "
        f"{relative_names}。本次使用已经授权的自主发送模式，只提交一次；若状态不明确，"
        "查询原请求状态，不要更换请求标识重发。"
        + privacy
    )


def _workspace_record(
    service: ApplicationService, requested: Path
) -> tuple[str, Path]:
    target = requested.resolve()
    for row in service.list_agent_workspaces().details.get(
        "workspace_details", []
    ):
        root = Path(str(row.get("display_path") or "")).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        target.mkdir(parents=True, exist_ok=True)
        return str(row["workspace_id"]), target
    raise RuntimeError("workspace_not_authorized")


def _client_scope_modes(client_type: str) -> dict[str, str]:
    if client_type == "claude_code":
        return {
            "account_scope_mode": "selected",
            "send_account_scope_mode": "selected",
            "workspace_scope_mode": "selected",
            "attachment_scope_mode": "selected",
        }
    return {
        "account_scope_mode": "all",
        "send_account_scope_mode": "all",
        "workspace_scope_mode": "all",
        "attachment_scope_mode": "all",
    }


def _configure_client(
    service: ApplicationService,
    *,
    client_type: str,
    account_ids: list[str],
    send_account_ids: list[str],
    workspace_id: str,
    send_mode: str,
) -> tuple[str, str, dict[str, Any]]:
    display_name = DISPLAY_NAMES[client_type]
    clients = service.list_agent_clients(include_revoked=False).details.get(
        "clients", []
    )
    existing = next(
        (
            row
            for row in clients
            if row.get("client_type") == client_type
            and row.get("display_name") == display_name
        ),
        None,
    )
    modes = _client_scope_modes(client_type)
    selected_accounts = account_ids if modes["account_scope_mode"] == "selected" else []
    selected_send = (
        send_account_ids
        if modes["send_account_scope_mode"] == "selected"
        else []
    )
    selected_workspaces = (
        [workspace_id] if modes["workspace_scope_mode"] == "selected" else []
    )
    selected_attachments = (
        [workspace_id]
        if modes["attachment_scope_mode"] == "selected"
        else []
    )
    if existing is None:
        created = service.create_agent_client(
            client_type=client_type,
            display_name=display_name,
            config_mode="managed",
            capabilities=CAPABILITIES,
            account_ids=selected_accounts,
            workspace_ids=selected_workspaces,
            send_account_ids=selected_send,
            attachment_workspace_ids=selected_attachments,
            permission_mode="custom",
            mailbox_scope_mode="all",
            send_mode=send_mode,
            **modes,
        )
        if not created.ok:
            raise RuntimeError(
                str(created.error_code or "client_create_failed")
            )
        client_id = str(created.details["client"]["client_id"])
        token = str(created.details["scoped_token"])
        created_now = True
    else:
        client_id = str(existing["client_id"])
        token = service._agent_integration.get_scoped_token(client_id)
        created_now = False
        updated = service.set_agent_client_permissions(
            client_id,
            capabilities=CAPABILITIES,
            account_ids=selected_accounts,
            workspace_ids=selected_workspaces,
            send_account_ids=selected_send,
            attachment_workspace_ids=selected_attachments,
            permission_mode="custom",
            mailbox_scope_mode="all",
            send_mode=send_mode,
            **modes,
        )
        if not updated.ok:
            raise RuntimeError(
                str(updated.error_code or "permission_save_failed")
            )
    activated = service.set_agent_client_state(
        client_id, "active", enabled=True
    )
    if not activated.ok:
        raise RuntimeError(str(activated.error_code or "client_enable_failed"))
    preview = service.preview_agent_client_config(client_id)
    if not preview.ok:
        raise RuntimeError(str(preview.error_code or "config_preview_failed"))
    preview_text = str(preview.details.get("preview") or "")
    applied = service.apply_agent_client_config(
        str(preview.details["plan_id"])
    )
    if not applied.ok:
        raise RuntimeError(str(applied.error_code or "config_apply_failed"))
    return client_id, token, {
        "created_now": created_now,
        "redacted_preview": token not in preview_text,
        "backup_created": bool(applied.details.get("backup")),
        "scope_mode": modes["account_scope_mode"],
        "send_account_scope_mode": modes["send_account_scope_mode"],
        "send_mode": send_mode,
    }


def _audit_rows(
    service: ApplicationService, client_id: str, after_id: int
) -> list[dict[str, Any]]:
    return [
        row
        for row in query_recent_mcp_audit_events(service.cfg.db_path, 500)
        if str(row.get("client_id") or "") == client_id
        and int(row.get("id") or 0) > after_id
    ]


def _audit_baseline(service: ApplicationService, client_id: str) -> int:
    return max(
        (
            int(row.get("id") or 0)
            for row in query_recent_mcp_audit_events(
                service.cfg.db_path, 500
            )
            if str(row.get("client_id") or "") == client_id
        ),
        default=0,
    )


def _read_audit_facts(
    service: ApplicationService, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    successful = {
        str(row.get("tool_name") or "")
        for row in rows
        if row.get("status") == "success"
    }
    mailbox_roles = {
        str(row["mailbox_id"]): str(row.get("mailbox_role") or "other")
        for row in query_mailboxes(service.cfg.db_path, enabled_only=True)
    }
    searched_mailbox_ids = set()
    for row in rows:
        if (
            row.get("tool_name") != "search_mails"
            or row.get("status") != "success"
        ):
            continue
        for field in str(row.get("query_summary") or "").split("; "):
            if field.startswith("mailbox_id="):
                searched_mailbox_ids.add(field.partition("=")[2])
    searched_roles = {
        mailbox_roles.get(mailbox_id, "")
        for mailbox_id in searched_mailbox_ids
    }
    history_all = any(
        row.get("tool_name") == "search_mails"
        and row.get("status") == "success"
        and (
            "time_scope=all" in str(row.get("query_summary") or "")
            or "date_from=" in str(row.get("query_summary") or "")
        )
        for row in rows
    )
    complete = False
    source_unchanged = False
    prepared_resource_count = 0
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
        if details.get("mode") == "complete":
            complete = bool(details.get("atomic_publish"))
            source_unchanged = bool(
                details.get("source_archive_unchanged")
            )
            prepared_resource_count = max(
                prepared_resource_count, len(details.get("hashes") or [])
            )
    return {
        "expected_tools": READ_TOOL_NAMES.issubset(successful),
        "historical_scope": history_all,
        "inbox_searched": "inbox" in searched_roles,
        "sent_searched": "sent" in searched_roles,
        "complete_atomic_package": complete,
        "source_archive_unchanged": source_unchanged,
        "prepared_resource_count": prepared_resource_count,
        "successful_tool_count": len(successful & READ_TOOL_NAMES),
    }


def _new_send_requests(
    service: ApplicationService,
    *,
    client_id: str,
    before_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        row
        for row in list_send_requests(service.cfg.db_path, limit=200)
        if str(row.get("client_id") or "") == client_id
        and str(row.get("send_request_id") or "") not in before_ids
    ]


def _make_attachments(workspace: Path) -> tuple[list[Path], str]:
    marker = (
        f"[AMB-v{__version__}-AGENT-{datetime.now():%Y%m%d-%H%M%S}-"
        f"{uuid.uuid4().hex[:8]}]"
    )
    root = workspace / "validation-input"
    root.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:8]
    text_file = root / f"AgentMailBridge-v17-{suffix}.txt"
    csv_file = root / f"AgentMailBridge-v17-{suffix}.csv"
    text_file.write_text(
        "AgentMailBridge v1.7 real Agent validation\n",
        encoding="utf-8",
        newline="\n",
    )
    csv_file.write_text(
        "check,status\nreal_agent,pass\n",
        encoding="utf-8",
        newline="\n",
    )
    return [text_file, csv_file], marker


def _package_raw_matches(
    service: ApplicationService, package_id: str, expected_hash: str
) -> bool:
    detail = service.get_mail_message(package_id)
    if not detail.ok:
        return False
    message = dict(detail.details.get("message") or {})
    raw = dict(message.get("raw_eml") or {})
    raw_path = Path(str(message.get("package_root") or "")) / str(
        raw.get("path") or ""
    )
    return bool(
        package_id
        and expected_hash
        and message.get("direction") == "outbound"
        and raw.get("sha256") == expected_hash
        and raw_path.is_file()
        and hashlib.sha256(raw_path.read_bytes()).hexdigest() == expected_hash
    )


def _account_for_address(
    accounts: list[dict[str, Any]], address: str
) -> str:
    target = address.strip().casefold()
    return next(
        (
            str(row["account_id"])
            for row in accounts
            if str(row.get("email_address") or "").strip().casefold()
            == target
        ),
        "",
    )


def _request_recipient_account(
    accounts: list[dict[str, Any]], request: dict[str, Any]
) -> str:
    for recipient in request.get("recipients") or []:
        if not isinstance(recipient, dict):
            continue
        if str(recipient.get("recipient_type") or "").casefold() != "to":
            continue
        address = str(recipient.get("email_address") or "")
        account_id = _account_for_address(accounts, address)
        if account_id:
            return account_id
    return ""


def _wait_for_delivery_and_mapping(
    service: ApplicationService,
    *,
    accounts: list[dict[str, Any]],
    request: dict[str, Any],
    marker: str,
    attachment_paths: list[Path],
    attempts: int = 12,
    interval: int = 8,
) -> dict[str, bool | int]:
    sender_account_id = str(request.get("sender_account_id") or "")
    recipient_account_id = _request_recipient_account(accounts, request)
    package_id = str(request.get("package_id") or "")
    expected = {
        path.name: (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in attachment_paths
    }
    delivery = None
    mapped = False
    original_sync: dict[str, bool] = {}
    mailbox_rows = query_mailboxes(service.cfg.db_path, enabled_only=True)
    for row in mailbox_rows:
        account_id = str(row.get("account_id") or "")
        role = str(row.get("mailbox_role") or "")
        external_ref = str(row.get("external_ref") or "")
        should_enable = (
            account_id == recipient_account_id and role == "inbox"
        ) or (
            account_id == sender_account_id
            and role == "sent"
            and not external_ref.startswith("local:sent:")
        )
        if should_enable:
            mailbox_id = str(row["mailbox_id"])
            original_sync[mailbox_id] = bool(row.get("sync_enabled"))
            set_mailbox_sync_enabled(
                service.cfg.db_path, mailbox_id, True
            )
    try:
        for attempt in range(max(1, attempts)):
            for account_id in {
                sender_account_id,
                recipient_account_id,
            } - {""}:
                service.receive(
                    account_id=account_id,
                    limit=100,
                    unseen_only=False,
                    mark_seen=False,
                    wait_for_process_lock=15,
                )
            if recipient_account_id:
                result = service.search_mail_facts(
                    marker, account_id=recipient_account_id, limit=10
                )
                messages = list(result.details.get("messages") or [])
                if messages:
                    delivery = dict(messages[0])
            mapped = bool(
                package_id
                and query_sent_mappings(
                    service.cfg.db_path, package_id=package_id
                )
            )
            if delivery is not None and mapped:
                break
            if attempt + 1 < attempts:
                time.sleep(interval)
    finally:
        for mailbox_id, enabled in original_sync.items():
            set_mailbox_sync_enabled(
                service.cfg.db_path, mailbox_id, enabled
            )
    delivered_attachments: dict[str, tuple[int, str]] = {}
    if delivery:
        detail = service.get_mail_message(str(delivery["package_id"]))
        message = dict(detail.details.get("message") or {}) if detail.ok else {}
        delivered_attachments = {
            str(row.get("display_name") or ""): (
                int(row.get("size_bytes") or 0),
                str(row.get("sha256") or ""),
            )
            for row in message.get("resources") or []
            if row.get("internal_type") == "attachment"
        }
    return {
        "delivery_observed": delivery is not None,
        "attachment_hashes_match": delivered_attachments == expected,
        "sent_mapping": mapped,
        "attachment_count": len(expected),
    }


def _finalize_send(
    args: argparse.Namespace,
    service: ApplicationService,
    evidence: dict[str, Any],
) -> int:
    marker = str(evidence.get("marker") or "")
    client_fingerprint = str(evidence.get("client_fingerprint") or "")
    request = next(
        (
            row
            for row in list_send_requests(service.cfg.db_path, limit=200)
            if _fingerprint(str(row.get("client_id") or ""))
            == client_fingerprint
            and marker
            in (
                str(row.get("subject") or "")
                + str(row.get("body_text") or "")
            )
        ),
        None,
    )
    checks = evidence.setdefault("checks", {})
    scenario = str(evidence.get("scenario") or "")
    prefix = (
        "confirmation"
        if scenario == "confirm_reply"
        else "autonomous"
    )
    if request is None:
        checks[f"{prefix}_final_status"] = _status(False)
        evidence["overall"] = "FAIL"
        _write_evidence(evidence, args.output)
        return 1
    status = str(request.get("status") or "")
    client_id = str(request.get("client_id") or "")
    successful_tools = {
        str(row.get("tool_name") or "")
        for row in query_recent_mcp_audit_events(
            service.cfg.db_path, 500
        )
        if str(row.get("client_id") or "") == client_id
        and row.get("status") == "success"
    }
    end_to_end_tools = {
        "list_mail_accounts",
        "list_mailboxes",
        "search_mails",
        "get_mail",
        "send_mail",
    }
    checks["agent_selected_mail_capabilities"] = _status(
        end_to_end_tools.issubset(successful_tools),
        successful_tool_count=len(successful_tools),
        combined_read_send_workflow=True,
    )
    if scenario == "confirm_reply":
        checks["confirmation_final_status"] = _status(
            status == "sent"
            and int(request.get("smtp_attempt_count") or 0) == 1,
            send_status=status,
            smtp_attempts=int(request.get("smtp_attempt_count") or 0),
        )
    accounts = list(
        service.list_mail_accounts().details.get("accounts") or []
    )
    attachment_names = list(evidence.get("attachment_names") or [])
    attachment_paths = [
        args.workspace.resolve() / "validation-input" / name
        for name in attachment_names
    ]
    delivery = _wait_for_delivery_and_mapping(
        service,
        accounts=accounts,
        request=request,
        marker=marker,
        attachment_paths=attachment_paths,
    )
    checks[f"{prefix}_delivery_and_sent"] = _status(
        all(
            bool(value)
            for key, value in delivery.items()
            if key != "attachment_count"
        ),
        **delivery,
    )
    checks[f"{prefix}_outbound_raw"] = _status(
        _package_raw_matches(
            service,
            str(request.get("package_id") or ""),
            str(request.get("raw_eml_sha256") or ""),
        )
    )
    if scenario == "confirm_reply":
        evidence["confirmation_finalized"] = True
    evidence["overall"] = (
        "PASS"
        if all(
            isinstance(row, dict) and row.get("status") == "PASS"
            for row in checks.values()
        )
        else "FAIL"
    )
    _write_evidence(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-external-agent", action="store_true")
    parser.add_argument("--confirm-real-send", action="store_true")
    parser.add_argument("--finalize-confirmation", action="store_true")
    parser.add_argument("--finalize-send", action="store_true")
    parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        default=600,
        help="Real Agent process timeout, clamped to 60-3600 seconds.",
    )
    args = parser.parse_args()
    agent_timeout_seconds = max(
        60, min(int(args.agent_timeout_seconds), 3600)
    )
    if not args.confirm_external_agent:
        raise SystemExit(
            "Refusing external Agent execution without "
            "--confirm-external-agent"
        )
    if (
        args.scenario in {"confirm_reply", "autonomous_send"}
        and not args.confirm_real_send
    ):
        raise SystemExit(
            "Real send scenarios require --confirm-real-send"
        )
    if (
        not args.scenario
        and not args.finalize_confirmation
        and not args.finalize_send
    ):
        raise SystemExit(
            "--scenario, --finalize-confirmation or --finalize-send is required"
        )

    runtime_mode = _activate_packaged_mcp_from_env()
    cfg = load_config()
    service = ApplicationService(cfg)
    if not service.initialize().ok:
        raise SystemExit("initialization_failed")
    if args.finalize_confirmation or args.finalize_send:
        evidence = json.loads(args.output.read_text(encoding="utf-8"))
        try:
            return _finalize_send(args, service, evidence)
        finally:
            close_connection()

    original_read_gate = bool(cfg.mcp_mail_read_enabled)
    checks: dict[str, Any] = {}
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "client": args.client,
        "scenario": args.scenario,
        "runtime": runtime_mode,
        "agent_timeout_seconds": agent_timeout_seconds,
        "checks": checks,
    }
    token = ""
    try:
        if not original_read_gate:
            enabled = service.set_mcp_mail_read_access(True)
            if not enabled.ok:
                raise RuntimeError("read_gate_enable_failed")
        accounts = list(
            service.list_mail_accounts().details.get("accounts") or []
        )
        account_ids = [
            str(row["account_id"])
            for row in accounts
            if row.get("enabled") and row.get("receive_enabled")
        ]
        send_account_ids = [
            str(row["account_id"])
            for row in accounts
            if row.get("enabled")
            and row.get("send_enabled")
            and "send" in set(row.get("capabilities") or [])
        ]
        if not account_ids or len(send_account_ids) < 2:
            raise RuntimeError("real_account_scope_unavailable")
        workspace_id, workspace = _workspace_record(
            service, args.workspace
        )
        send_mode = (
            "autonomous"
            if args.scenario == "autonomous_send"
            else "confirm"
        )
        client_id, token, client_facts = _configure_client(
            service,
            client_type=args.client,
            account_ids=account_ids,
            send_account_ids=send_account_ids,
            workspace_id=workspace_id,
            send_mode=send_mode,
        )
        evidence["client_fingerprint"] = _fingerprint(client_id)
        checks["managed_client_configuration"] = _status(
            client_facts["redacted_preview"],
            created_now=client_facts["created_now"],
            backup_created=client_facts["backup_created"],
            account_scope_mode=client_facts["scope_mode"],
            send_account_scope_mode=client_facts[
                "send_account_scope_mode"
            ],
            send_mode=client_facts["send_mode"],
        )
        detection = detect_client(args.client)
        checks["real_client_installed"] = _status(
            detection.installed
            and detection.status == "managed_supported",
            detection_status=detection.status,
            version=str(detection.version or "").splitlines()[0],
        )
        connection = service.test_agent_client_connection(client_id)
        checks["eleven_tool_connection"] = _status(
            connection.ok
            and int(connection.details.get("tool_count") or 0) == 11,
            tool_count=int(connection.details.get("tool_count") or 0),
        )

        attachment_paths: list[Path] = []
        marker = ""
        if args.scenario != "read":
            attachment_paths, marker = _make_attachments(workspace)
            evidence["marker"] = marker
            evidence["attachment_names"] = [
                path.name for path in attachment_paths
            ]
        prompt = _natural_prompt(
            str(args.scenario),
            marker=marker,
            attachment_names=[path.name for path in attachment_paths],
        )
        forbidden_prompt_terms = [
            *MCP_TOOL_NAMES,
            *account_ids,
            "package_id",
        ]
        checks["natural_language_prompt_gate"] = _status(
            not any(term in prompt for term in forbidden_prompt_terms)
        )
        before_request_ids = {
            str(row.get("send_request_id") or "")
            for row in list_send_requests(cfg.db_path, limit=200)
        }
        audit_after_id = _audit_baseline(service, client_id)
        completed = _run_agent(
            args.client,
            prompt,
            workspace,
            cfg=cfg,
            timeout_seconds=agent_timeout_seconds,
        )
        combined = str(completed.pop("combined"))
        checks["real_agent_process"] = _status(
            completed["returncode"] == 0 and token not in combined,
            exit_code=completed["returncode"],
            elapsed_seconds=completed["elapsed_seconds"],
            token_exposed=token in combined,
        )
        rows = _audit_rows(service, client_id, audit_after_id)
        if args.scenario == "read":
            read_facts = _read_audit_facts(service, rows)
            checks["real_agent_read_workflow"] = _status(
                all(
                    bool(value)
                    for key, value in read_facts.items()
                    if key
                    not in {
                        "prepared_resource_count",
                        "successful_tool_count",
                    }
                ),
                **read_facts,
            )
        else:
            successful_tools = {
                str(row.get("tool_name") or "")
                for row in rows
                if row.get("status") == "success"
            }
            checks["agent_selected_mail_capabilities"] = _status(
                {
                    "list_mail_accounts",
                    "list_mailboxes",
                    "search_mails",
                    "get_mail",
                    "send_mail",
                }.issubset(successful_tools),
                successful_tool_count=len(successful_tools),
            )
            requests = _new_send_requests(
                service,
                client_id=client_id,
                before_ids=before_request_ids,
            )
            request = requests[0] if len(requests) == 1 else {}
            if args.scenario == "confirm_reply":
                checks["agent_pending_confirmation"] = _status(
                    len(requests) == 1
                    and request.get("operation") == "reply"
                    and request.get("status") == "pending_confirmation"
                    and int(request.get("smtp_attempt_count") or 0) == 0
                    and not request.get("package_id"),
                    request_count=len(requests),
                    smtp_attempts=int(
                        request.get("smtp_attempt_count") or 0
                    ),
                    send_status=str(request.get("status") or ""),
                )
                evidence["confirmation_finalized"] = False
            else:
                checks["agent_autonomous_send_once"] = _status(
                    len(requests) == 1
                    and request.get("operation") == "new"
                    and request.get("status") == "sent"
                    and int(request.get("smtp_attempt_count") or 0) == 1,
                    request_count=len(requests),
                    smtp_attempts=int(
                        request.get("smtp_attempt_count") or 0
                    ),
                    send_status=str(request.get("status") or ""),
                )
                delivery = _wait_for_delivery_and_mapping(
                    service,
                    accounts=accounts,
                    request=request,
                    marker=marker,
                    attachment_paths=attachment_paths,
                )
                checks["autonomous_delivery_and_sent"] = _status(
                    all(
                        bool(value)
                        for key, value in delivery.items()
                        if key != "attachment_count"
                    ),
                    **delivery,
                )
                checks["autonomous_outbound_raw"] = _status(
                    _package_raw_matches(
                        service,
                        str(request.get("package_id") or ""),
                        str(request.get("raw_eml_sha256") or ""),
                    )
                )
        checks["secret_not_in_evidence"] = _status(
            token not in json.dumps(evidence, ensure_ascii=False)
        )
    except (
        FileNotFoundError,
        IndexError,
        KeyError,
        OSError,
        RuntimeError,
        StopIteration,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        evidence["failure_type"] = type(exc).__name__
        evidence["failure_code"] = str(
            getattr(exc, "code", "") or getattr(exc, "args", [""])[0]
        )[:120]
    finally:
        if not original_read_gate:
            restored = service.set_mcp_mail_read_access(False)
            checks["read_gate_restored"] = _status(restored.ok)
        close_connection()

    evidence["overall"] = (
        "PASS"
        if checks
        and all(
            isinstance(row, dict) and row.get("status") == "PASS"
            for row in checks.values()
        )
        and not evidence.get("failure_type")
        else "FAIL"
    )
    _write_evidence(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
