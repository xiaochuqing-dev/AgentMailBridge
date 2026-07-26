"""对最终 MCP EXE 执行真实本地归档读取验收，证据不含邮件内容。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.utils import sha256_of_file
from agent_mail_bridge.version import __version__


EXPECTED_TOOLS = [
    "submit_result",
    "search_mails",
    "get_mail",
    "read_mail_resource",
    "prepare_mail_resources",
    "list_agent_workspaces",
    "get_mail_sync_status",
]


class RpcSession:
    def __init__(self, executable: Path, env: dict[str, str]):
        self.process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.next_id = 1
        self.responses: list[dict[str, Any]] = []

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params or {}},
                ensure_ascii=False,
            )
            + "\n"
        )
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("MCP 在返回 JSON-RPC 响应前退出")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MCP stdout 出现非协议输出") from exc
        if response.get("id") != request_id:
            raise RuntimeError("MCP JSON-RPC 响应 id 不匹配")
        self.responses.append(response)
        return response

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if "error" in response:
            raise RuntimeError(f"MCP 工具协议错误：{name}")
        return dict(response["result"]["structuredContent"])

    def initialize(self) -> dict[str, Any]:
        response = self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "release-lifecycle-validation",
                    "version": __version__,
                },
            },
        )
        self.notify("notifications/initialized")
        return dict(response["result"])

    def close(self) -> tuple[int, str]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.process.stdin.close()
        remainder = self.process.stdout.read()
        stderr = self.process.stderr.read()
        code = self.process.wait(timeout=30)
        if remainder.strip():
            for line in remainder.splitlines():
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("MCP stdout 尾部出现非协议输出") from exc
        return code, stderr


def _tool_ok(payload: dict[str, Any]) -> bool:
    return bool(payload.get("ok")) and str(payload.get("status")) in {
        "success",
        "partial",
    }


def _audit_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM mcp_audit_events").fetchone()
    return int(row[0] if row else 0)


def _base_environment(cfg, *, enabled: bool, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "MCP_MAIL_READ_ENABLED": "true" if enabled else "false",
            "ALLOWED_SEND_ROOTS": str(workspace),
            "DATA_ROOT": str(cfg.data_root_path),
            "AGENT_MAIL_BRIDGE_CONFIG": str(cfg.loaded_env_path),
        }
    )
    return env


def _provider_validation(
    session: RpcSession,
    *,
    provider: str,
    account_id: str,
    workspace: Path,
    outside: Path,
) -> dict[str, Any]:
    search = session.tool(
        "search_mails",
        {"account_id": account_id, "time_scope": "all", "limit": 20},
    )
    messages = list(search.get("messages") or [])
    if not _tool_ok(search) or not messages:
        return {"status": "NOT_TESTED", "reason": "no_local_archive"}
    if any(str(item.get("account_id") or "") != account_id for item in messages):
        return {"status": "FAIL", "reason": "account_filter_mismatch"}
    package_id = str(messages[0].get("package_id") or messages[0].get("mail_id") or "")
    if not package_id:
        return {"status": "FAIL", "reason": "missing_package_id"}

    mail_result = session.tool("get_mail", {"package_id": package_id})
    mail = dict(mail_result.get("mail") or {})
    resources = list(mail.get("resources") or [])
    raw = session.tool(
        "read_mail_resource",
        {"package_id": package_id, "resource_id": "raw.eml", "mode": "raw"},
    )
    body_resource = next(
        (
            item
            for item in resources
            if str(item.get("internal_type") or "")
            in {"body_plain", "body_readable"}
            and item.get("resource_id")
        ),
        None,
    )
    body = (
        session.tool(
            "read_mail_resource",
            {
                "package_id": package_id,
                "resource_id": str(body_resource["resource_id"]),
                "mode": "text",
                "max_chars": 2000,
            },
        )
        if body_resource
        else {"ok": False}
    )
    prepare_resource = next(
        (
            item
            for item in resources
            if str(item.get("internal_type") or "") in {"attachment", "inline_image"}
            and item.get("resource_id")
        ),
        body_resource,
    )
    if prepare_resource is None:
        return {"status": "FAIL", "reason": "no_preparable_resource"}
    resource_id = str(prepare_resource["resource_id"])
    resource_type = str(prepare_resource.get("internal_type") or "")
    selected_read = session.tool(
        "read_mail_resource",
        {
            "package_id": package_id,
            "resource_id": resource_id,
            "mode": "preview" if resource_type == "inline_image" else "text",
            "max_chars": 2000,
        },
    )
    selected_read_ok = _tool_ok(selected_read) or (
        resource_type == "attachment"
        and str(selected_read.get("error_code") or "") == "binary_resource"
    )
    prepared = session.tool(
        "prepare_mail_resources",
        {
            "package_id": package_id,
            "resource_ids": [resource_id],
            "target_workspace": str(workspace),
            "target_subdir": f"mcp-{provider}",
            "overwrite_policy": "overwrite",
        },
    )
    prepared_rows = list(prepared.get("prepared") or [])
    hash_ok = False
    if prepared_rows:
        prepared_path = Path(str(prepared_rows[0].get("prepared_path") or ""))
        expected_hash = str(prepared_rows[0].get("sha256") or "")
        hash_ok = bool(
            prepared_path.is_file()
            and expected_hash
            and sha256_of_file(prepared_path).casefold() == expected_hash.casefold()
        )
    denied = session.tool(
        "prepare_mail_resources",
        {
            "package_id": package_id,
            "resource_ids": [resource_id],
            "target_workspace": str(outside),
        },
    )
    ownership_ok = (
        str(mail.get("account_id") or "") == account_id
        and all(
            str(item.get("package_id") or package_id) == package_id
            for item in resources
        )
    )
    checks = {
        "search": _tool_ok(search),
        "account_filter": True,
        "get": _tool_ok(mail_result),
        "read_raw": _tool_ok(raw),
        "read_body": _tool_ok(body),
        "read_selected_resource": selected_read_ok,
        "prepare": _tool_ok(prepared),
        "prepare_hash": hash_ok,
        "path_whitelist": (
            not bool(denied.get("ok"))
            and str(denied.get("error_code") or "")
            in {
                "workspace_not_allowed",
                "workspace_not_found",
                "workspace_required",
                "path_not_allowed",
            }
        ),
        "ownership": ownership_ok,
        "utf8": str(body.get("encoding") or "").casefold() == "utf-8",
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "resource_count": len(resources),
        "attachment_or_inline_prepared": resource_type
        in {"attachment", "inline_image"},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-real-read", action="store_true")
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not args.confirm_real_read:
        raise SystemExit("未显式确认真实邮件读取")
    if not executable.is_file():
        raise SystemExit("MCP EXE 不存在")

    cfg = load_config()
    service = ApplicationService(cfg)
    if not service.initialize().ok:
        raise SystemExit("AgentMailBridge 初始化失败")
    before_opt_in = bool(cfg.mcp_mail_read_enabled)
    accounts = {
        str(item.get("provider") or ""): str(item.get("account_id") or "")
        for item in service.list_mail_accounts().details.get("accounts") or []
        if item.get("enabled") and item.get("receive_enabled")
    }
    before_audit = _audit_count(cfg.db_path)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "executable": "AgentMailBridgeMCP.exe",
        "persistent_opt_in_before": before_opt_in,
        "checks": {},
        "providers": {},
    }

    with tempfile.TemporaryDirectory(prefix="amb-mcp-real-read-") as temporary:
        root = Path(temporary)
        workspace = root / "allowed-workspace"
        outside = root / "outside-workspace"
        workspace.mkdir()
        outside.mkdir()

        denied_session = RpcSession(
            executable,
            _base_environment(cfg, enabled=False, workspace=workspace),
        )
        denied_init = denied_session.initialize()
        denied = denied_session.tool(
            "search_mails", {"time_scope": "all", "limit": 1}
        )
        denied_code, denied_stderr = denied_session.close()
        evidence["checks"]["default_deny"] = (
            str(denied.get("error_code") or "") == "read_access_disabled"
        )
        evidence["checks"]["default_deny_eof"] = denied_code == 0
        evidence["checks"]["default_deny_stdout_purity"] = (
            "Traceback" not in denied_stderr
            and denied_init.get("serverInfo", {}).get("version") == __version__
        )

        session = RpcSession(
            executable,
            _base_environment(cfg, enabled=True, workspace=workspace),
        )
        initialized = session.initialize()
        listed = session.call("tools/list")["result"]["tools"]
        workspaces = session.tool("list_agent_workspaces", {})
        sync = session.tool("get_mail_sync_status", {})
        evidence["checks"]["initialize"] = (
            initialized.get("serverInfo", {}).get("version") == __version__
        )
        evidence["checks"]["tools_list"] = [
            str(item.get("name") or "") for item in listed
        ] == EXPECTED_TOOLS
        evidence["checks"]["list_agent_workspaces"] = _tool_ok(workspaces)
        evidence["checks"]["get_mail_sync_status"] = _tool_ok(sync)

        for provider in ("qq", "163", "gmail"):
            account_id = accounts.get(provider)
            evidence["providers"][provider] = (
                _provider_validation(
                    session,
                    provider=provider,
                    account_id=account_id,
                    workspace=workspace,
                    outside=outside,
                )
                if account_id
                else {"status": "NOT_TESTED", "reason": "account_not_configured"}
            )
        code, stderr = session.close()
        evidence["checks"]["stdout_purity"] = "Traceback" not in stderr
        evidence["checks"]["eof_exit"] = code == 0

    after_cfg = load_config()
    after_audit = _audit_count(after_cfg.db_path)
    evidence["persistent_opt_in_after"] = bool(after_cfg.mcp_mail_read_enabled)
    evidence["checks"]["opt_in_restored"] = (
        bool(after_cfg.mcp_mail_read_enabled) == before_opt_in
    )
    evidence["checks"]["temporary_workspace_removed"] = not root.exists()
    evidence["checks"]["audit_records"] = after_audit > before_audit
    provider_statuses = [
        str(item.get("status") or "") for item in evidence["providers"].values()
    ]
    evidence["overall"] = (
        "PASS"
        if all(evidence["checks"].values())
        and provider_statuses
        and all(status == "PASS" for status in provider_statuses)
        else "FAIL"
        if "FAIL" in provider_statuses or not all(evidence["checks"].values())
        else "PARTIAL"
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"MCP real read validation {evidence['overall']}: {output}")
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
