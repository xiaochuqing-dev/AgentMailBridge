from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_mail_bridge.agent_integration import AgentAccessError
from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    AGENT_INTEGRATION_MIGRATION_KEY,
    get_agent_client,
    query_agent_client_permissions,
    query_recent_mcp_audit_events,
)
from agent_mail_bridge.maintenance import backup_dir
from agent_mail_bridge.mcp_client_config import (
    ClientConfigError,
    apply_client_config,
    preview_client_config,
    restore_client_config,
)
from agent_mail_bridge.mcp_server import McpServer
from scripts.agent_client_real_e2e import _client_command


ALL_READ_CAPABILITIES = [
    "mail.search",
    "mail.get",
    "resource.read",
    "resource.prepare",
    "sync.status",
    "sync.ensure_fresh",
    "workspace.list",
]


def _create_enabled_client(
    service: ApplicationService,
    tmp_cfg,
    tmp_path: Path,
    *,
    capabilities: list[str] | None = None,
):
    service.synchronize_mail_accounts()
    accounts = service.list_mail_accounts().details["accounts"]
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    tmp_cfg.allowed_send_roots = [workspace]
    workspace_id = service.list_agent_workspaces().details["workspace_details"][0][
        "workspace_id"
    ]
    created = service.create_agent_client(
        client_type="codex",
        display_name="Codex 测试",
        capabilities=capabilities or ALL_READ_CAPABILITIES,
        account_ids=[accounts[0]["account_id"]],
        workspace_ids=[workspace_id],
    )
    assert created.ok
    client_id = created.details["client"]["client_id"]
    token = created.details["scoped_token"]
    assert service.set_agent_client_state(client_id, "active", enabled=True).ok
    return client_id, token, accounts[0]["account_id"], workspace_id


def _initialize(server: McpServer):
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "Codex", "version": "test"},
            },
        }
    )
    server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    return response


def test_client_identity_wrong_token_pause_revoke_and_isolation(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    client_a, token_a, _account, _workspace = _create_enabled_client(
        service, tmp_cfg, tmp_path
    )
    second = service.create_agent_client(
        client_type="custom", display_name="Custom B"
    )
    client_b = second.details["client"]["client_id"]
    token_b = second.details["scoped_token"]
    assert service.set_agent_client_state(client_b, "active", enabled=True).ok

    assert service.resolve_agent_identity(client_a, token_a).client_id == client_a
    rotated = service.rotate_agent_client_token(client_a)
    assert rotated.ok
    rotated_token = rotated.details["scoped_token"]
    with pytest.raises(AgentAccessError) as old_token:
        service.resolve_agent_identity(client_a, token_a)
    assert old_token.value.code == "client_auth_failed"
    assert (
        service.resolve_agent_identity(client_a, rotated_token).client_id
        == client_a
    )
    token_a = rotated_token
    with pytest.raises(AgentAccessError, match="身份验证失败") as wrong:
        service.resolve_agent_identity(client_a, token_b)
    assert wrong.value.code == "client_auth_failed"

    assert service.set_agent_client_state(client_a, "paused").ok
    with pytest.raises(AgentAccessError) as paused:
        service.resolve_agent_identity(client_a, token_a)
    assert paused.value.code == "client_disabled"
    assert service.resolve_agent_identity(client_b, token_b).client_id == client_b
    assert service.set_agent_client_state(client_a, "revoked").ok
    with pytest.raises(AgentAccessError) as revoked:
        service.resolve_agent_identity(client_a, token_a)
    assert revoked.value.code == "client_revoked"
    assert service.resolve_agent_identity(client_b, token_b).client_id == client_b


def test_global_gate_two_client_account_matrix_and_immediate_isolation(
    tmp_cfg, tmp_path
):
    service = ApplicationService(tmp_cfg)
    service.synchronize_mail_accounts()
    accounts = service.list_mail_accounts().details["accounts"]
    assert len(accounts) >= 2
    account_a = str(accounts[0]["account_id"])
    account_b = str(accounts[1]["account_id"])

    created_a = service.create_agent_client(
        client_type="codex",
        display_name="Client A",
        capabilities=["mail.search"],
        account_ids=[account_a],
    )
    created_b = service.create_agent_client(
        client_type="claude_code",
        display_name="Client B",
        capabilities=["mail.search"],
        account_ids=[account_b],
    )
    for created in (created_a, created_b):
        assert created.ok
        assert service.set_agent_client_state(
            created.details["client"]["client_id"], "active", enabled=True
        ).ok

    server_a = McpServer(
        service,
        client_id=created_a.details["client"]["client_id"],
        client_token=created_a.details["scoped_token"],
    )
    server_b = McpServer(
        service,
        client_id=created_b.details["client"]["client_id"],
        client_token=created_b.details["scoped_token"],
    )
    assert "result" in _initialize(server_a)
    assert "result" in _initialize(server_b)

    def search(server: McpServer, account_id: str, request_id: int):
        return server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "search_mails",
                    "arguments": {"account_id": account_id},
                },
            }
        )["result"]["structuredContent"]

    assert search(server_a, account_b, 10)["error_code"] == "agent_access_disabled"
    assert search(server_b, account_a, 11)["error_code"] == "agent_access_disabled"
    assert search(server_a, account_a, 12)["error_code"] == "agent_access_disabled"
    assert search(server_b, account_b, 13)["error_code"] == "agent_access_disabled"

    tmp_cfg.mcp_mail_read_enabled = True
    assert search(server_a, account_b, 18)["error_code"] == "account_denied"
    assert search(server_b, account_a, 19)["error_code"] == "account_denied"
    assert search(server_a, account_a, 14)["ok"] is True
    assert search(server_b, account_b, 15)["ok"] is True
    assert service.set_agent_client_state(
        created_a.details["client"]["client_id"], "paused"
    ).ok
    assert search(server_a, account_a, 16)["error_code"] == "client_disabled"
    assert search(server_b, account_b, 17)["ok"] is True


def test_permissions_default_deny_and_exact_account_workspace_scope(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    client_id, token, account_id, workspace_id = _create_enabled_client(
        service,
        tmp_cfg,
        tmp_path,
        capabilities=["mail.search", "workspace.list"],
    )
    identity = service.resolve_agent_identity(client_id, token)
    service.require_agent_capability(identity, "mail.search")
    with pytest.raises(AgentAccessError) as denied:
        service.require_agent_capability(identity, "mail.get")
    assert denied.value.code == "capability_denied"
    assert service.require_agent_account(identity, account_id) == account_id
    with pytest.raises(AgentAccessError) as account_denied:
        service.require_agent_account(identity, "acct_ffffffffffffffffffffffff")
    assert account_denied.value.code == "account_denied"
    selected, path = service.require_agent_workspace(identity, workspace_id)
    assert selected == workspace_id and Path(path).is_dir()
    with pytest.raises(AgentAccessError) as workspace_denied:
        service.require_agent_workspace(identity, "workspace-not-allowed")
    assert workspace_denied.value.code == "workspace_denied"
    rows = query_agent_client_permissions(tmp_cfg.db_path, client_id)
    assert {row["capability"] for row in rows} >= {
        "mail.search",
        "account.access",
        "workspace.access",
    }


def test_mcp_unknown_client_denied_and_per_client_audit(tmp_cfg, tmp_path):
    unknown = McpServer(
        ApplicationService(tmp_cfg), client_id="client_" + "0" * 24, client_token="bad"
    )
    denied = _initialize(unknown)
    assert denied["error"]["data"]["error_code"] == "unknown_client"

    service = ApplicationService(tmp_cfg)
    tmp_cfg.mcp_mail_read_enabled = True
    client_id, token, account_id, _workspace = _create_enabled_client(
        service, tmp_cfg, tmp_path, capabilities=["mail.search"]
    )
    server = McpServer(service, client_id=client_id, client_token=token)
    assert "result" in _initialize(server)
    result = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "search_mails",
                "arguments": {"account_id": account_id},
            },
        }
    )
    assert result["result"]["isError"] is False
    denied_get = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_mail",
                "arguments": {"mail_id": "missing"},
            },
        }
    )
    assert (
        denied_get["result"]["structuredContent"]["error_code"]
        == "capability_denied"
    )
    audit = query_recent_mcp_audit_events(tmp_cfg.db_path, 20)
    search = next(row for row in audit if row["tool_name"] == "search_mails")
    assert search["client_id"] == client_id
    assert search["client_type"] == "codex"
    assert search["capability"] == "mail.search"
    assert search["account_id"] == account_id
    assert all("ambc_" not in json.dumps(row) for row in audit)


def test_json_config_merge_backup_idempotency_conflict_remove_and_restore(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(
        json.dumps(
            {
                "unknown": {"keep": True},
                "mcpServers": {"other": {"command": "other"}},
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )
    token = "ambc_test_secret_not_for_logs"
    plan = preview_client_config(
        client_id="client_" + "1" * 24,
        client_type="claude_desktop",
        client_token=token,
        target_path=target,
    )
    assert token not in plan.preview
    applied = apply_client_config(plan, backup_root=tmp_path / "backups")
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["unknown"] == {"keep": True}
    assert "other" in parsed["mcpServers"]
    assert parsed["mcpServers"]["agent-mail-bridge"]["env"][
        "AGENT_MAIL_BRIDGE_CLIENT_TOKEN"
    ] == token

    again = preview_client_config(
        client_id="client_" + "1" * 24,
        client_type="claude_desktop",
        client_token=token,
        target_path=target,
    )
    second = apply_client_config(again, backup_root=tmp_path / "backups")
    assert second.changed is False

    stale = preview_client_config(
        client_id="client_" + "1" * 24,
        client_type="claude_desktop",
        client_token=token,
        target_path=target,
    )
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ClientConfigError) as concurrent:
        apply_client_config(stale, backup_root=tmp_path / "backups")
    assert concurrent.value.code == "config_changed_concurrently"

    target.write_bytes(stale.planned_bytes)
    remove = preview_client_config(
        client_id="client_" + "1" * 24,
        client_type="claude_desktop",
        client_token=token,
        target_path=target,
        action="remove",
    )
    removed = apply_client_config(remove, backup_root=tmp_path / "backups")
    assert "agent-mail-bridge" not in json.loads(
        target.read_text(encoding="utf-8")
    )["mcpServers"]
    restored_hash = restore_client_config(
        target_path=target,
        backup_path=removed.backup_path,
        applied_hash=removed.applied_hash,
    )
    assert restored_hash == removed.original_hash
    assert "agent-mail-bridge" in json.loads(
        target.read_text(encoding="utf-8")
    )["mcpServers"]
    assert applied.backup_path.is_file()


def test_claude_code_user_and_project_json_fixtures_preserve_unknown_fields(
    tmp_path,
):
    for name in ("claude-user.json", ".mcp.json"):
        target = tmp_path / name
        target.write_text(
            json.dumps(
                {
                    "projects": {"保留项目": {"allowedTools": ["Read"]}},
                    "mcpServers": {"unrelated": {"command": "existing"}},
                    "futureField": {"formatVersion": 99},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plan = preview_client_config(
            client_id="client_" + "5" * 24,
            client_type="claude_code",
            client_token="fixture-scoped-token",
            config_scope="project" if name == ".mcp.json" else "user",
            target_path=target,
        )
        apply_client_config(plan, backup_root=tmp_path / "backups")
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed["projects"]["保留项目"]["allowedTools"] == ["Read"]
        assert parsed["futureField"]["formatVersion"] == 99
        assert parsed["mcpServers"]["unrelated"]["command"] == "existing"
        assert (
            parsed["mcpServers"]["agent-mail-bridge"]["env"][
                "AGENT_MAIL_BRIDGE_CLIENT_ID"
            ]
            == "client_" + "5" * 24
        )


def test_codex_toml_merge_preserves_unrelated_sections_unicode_and_rollback(tmp_path):
    target = tmp_path / "中文 路径" / "config.toml"
    target.parent.mkdir()
    target.write_text(
        'model = "gpt-test"\n\n[mcp_servers.other]\ncommand = "other"\n',
        encoding="utf-8",
    )
    token = "ambc_codex_scoped"
    plan = preview_client_config(
        client_id="client_" + "2" * 24,
        client_type="codex",
        client_token=token,
        target_path=target,
    )
    applied = apply_client_config(plan, backup_root=tmp_path / "backups")
    text = target.read_text(encoding="utf-8")
    assert 'model = "gpt-test"' in text
    assert "[mcp_servers.other]" in text
    assert "[mcp_servers.agent-mail-bridge]" in text
    assert token in text and token not in plan.preview
    assert not list(target.parent.glob("*.tmp"))

    restore_client_config(
        target_path=target,
        backup_path=applied.backup_path,
        applied_hash=applied.applied_hash,
    )
    restored = target.read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in restored
    assert "[mcp_servers.agent-mail-bridge]" not in restored


def test_malformed_configs_are_never_overwritten(tmp_path):
    json_target = tmp_path / "bad.json"
    json_target.write_text("{bad", encoding="utf-8")
    with pytest.raises(ClientConfigError) as json_error:
        preview_client_config(
            client_id="client_" + "3" * 24,
            client_type="claude_code",
            client_token="ambc_x",
            target_path=json_target,
        )
    assert json_error.value.code == "config_parse_failed"
    assert json_target.read_text(encoding="utf-8") == "{bad"

    toml_target = tmp_path / "bad.toml"
    toml_target.write_text("[bad\n", encoding="utf-8")
    with pytest.raises(ClientConfigError) as toml_error:
        preview_client_config(
            client_id="client_" + "4" * 24,
            client_type="codex",
            client_token="ambc_y",
            target_path=toml_target,
        )
    assert toml_error.value.code == "config_parse_failed"
    assert toml_target.read_text(encoding="utf-8") == "[bad\n"


def test_client_rows_never_expose_token_hash_or_credential_ref(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    client_id, _token, _account, _workspace = _create_enabled_client(
        service, tmp_cfg, tmp_path
    )
    listed = service.list_agent_clients().details["clients"]
    row = next(item for item in listed if item["client_id"] == client_id)
    assert "token_hash" not in row
    assert "credential_ref" not in row
    assert row["token_stored"] is True
    stored = get_agent_client(tmp_cfg.db_path, client_id)
    assert stored and len(stored["token_hash"]) == 64


def test_gui_service_connection_test_runs_real_stdio_tools_list(tmp_cfg, tmp_path):
    service = ApplicationService(tmp_cfg)
    client_id, _token, _account, _workspace = _create_enabled_client(
        service, tmp_cfg, tmp_path
    )
    result = service.test_agent_client_connection(client_id)
    assert result.ok
    assert result.details["tool_count"] == 11
    assert set(result.details["tools"]) == {
        "submit_result",
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
    }


def test_v1_4_style_database_migration_backup_and_v1_5_retention(
    tmp_cfg, tmp_path
):
    with sqlite3.connect(tmp_cfg.db_path) as connection:
        connection.execute("DROP TABLE agent_client_config_backups")
        connection.execute("DROP TABLE agent_client_permissions")
        connection.execute("DROP TABLE agent_clients")
        connection.execute(
            "DELETE FROM migration_metadata WHERE migration_key = ?",
            (AGENT_INTEGRATION_MIGRATION_KEY,),
        )
        connection.commit()

    migrated = ApplicationService(tmp_cfg)
    assert migrated.initialize().ok
    assert list(
        backup_dir(tmp_cfg).glob("*before_v1_6_agent_ecosystem*.db")
    )
    with sqlite3.connect(tmp_cfg.db_path) as connection:
        migration = connection.execute(
            "SELECT status, schema_version FROM migration_metadata "
            "WHERE migration_key = ?",
            (AGENT_INTEGRATION_MIGRATION_KEY,),
        ).fetchone()
        clients = connection.execute(
            "SELECT COUNT(*) FROM agent_clients"
        ).fetchone()[0]
    assert migration == ("completed", 2)
    assert clients == 0

    client_id, _token, _account, _workspace = _create_enabled_client(
        migrated, tmp_cfg, tmp_path
    )
    restarted = ApplicationService(tmp_cfg)
    assert restarted.initialize().ok
    rows = restarted.list_agent_clients().details["clients"]
    assert any(row["client_id"] == client_id for row in rows)


def test_codex_real_e2e_command_forwards_isolated_server_environment(tmp_path):
    server_env = {
        "DATA_ROOT": str(tmp_path / "数据"),
        "MCP_MAIL_READ_ENABLED": "true",
        "PYTHONPATH": str(tmp_path / "源码"),
    }
    command = _client_command(
        "codex",
        "codex.exe",
        config_path=tmp_path / "unused.json",
        client_id="client_test",
        token="ambc_test",
        workspace=tmp_path,
        server_env=server_env,
    )
    assert "--json" in command
    assert "ensure_fresh set to false" in command[-1]
    for key, value in server_env.items():
        assert (
            f"mcp_servers.agent-mail-bridge.env.{key}="
            f"{json.dumps(value)}"
        ) in command
