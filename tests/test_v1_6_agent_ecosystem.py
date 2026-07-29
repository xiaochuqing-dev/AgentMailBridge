from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    create_mail_account,
    get_history_import_run,
    insert_agent_config_backup,
    query_agent_config_backups,
    update_agent_client,
)
from agent_mail_bridge.mail_accounts import MailAccount, stable_account_id
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mcp_client_config import (
    ClientConfigError,
    ClientDetection,
    apply_client_config,
    detect_client,
    mcp_client_command,
    preview_client_config,
)
from agent_mail_bridge.mcp_server import McpServer
from agent_mail_bridge.models import OperationStatus, ReceiveResult
from agent_mail_bridge.receive_rules import ALL_SCANNED
from agent_mail_bridge.utils import sha256_of_file


READ_CAPABILITIES = [
    "mail.search",
    "mail.get",
    "resource.read",
    "resource.prepare",
    "sync.status",
    "sync.ensure_fresh",
    "workspace.list",
]


def _archive_complete_fixture(cfg) -> str:
    cfg.receive_rule_mode = ALL_SCANNED
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "owner@example.com"
    message["Cc"] = "copy@example.com"
    message["Message-ID"] = "<complete-v16@example.com>"
    message["Subject"] = "v1.6 完整邮件资料"
    message.set_content("这是完整邮件正文。\nhttps://example.com/reference")
    message.add_alternative(
        '<html><body><p>这是完整邮件正文。</p><img src="cid:v16-inline"></body></html>',
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    html_part.add_related(
        b"\x89PNG\r\n\x1a\ninline-fixture",
        maintype="image",
        subtype="png",
        cid="<v16-inline>",
        filename="内嵌图片.png",
        disposition="inline",
    )
    message.add_attachment(
        "列一,列二\n甲,1\n".encode("utf-8"),
        maintype="text",
        subtype="csv",
        filename="历史 数据.csv",
    )
    message.add_attachment(
        b"\x89PNG\r\n\x1a\nfixture",
        maintype="image",
        subtype="png",
        filename="截图.png",
    )
    raw = message.as_bytes()
    normalized = normalized_mail_from_raw(
        raw,
        backend="imap",
        backend_message_id="complete-v16",
        thread_id="thread-v16",
        uid="1600",
        received_at="2026-07-27 08:00:00",
        saved_date="2026-07-27",
        max_attachment_bytes=cfg.max_attachment_bytes,
        mailbox_ref="imap:INBOX",
    )
    return str(process_normalized_mail(cfg, normalized)["package_id"])


def _activate(service: ApplicationService, created) -> tuple[str, str]:
    client_id = str(created.details["client"]["client_id"])
    token = str(created.details["scoped_token"])
    assert service.set_agent_client_state(
        client_id, "active", enabled=True
    ).ok
    return client_id, token


def test_complete_mail_package_is_atomic_auditable_and_source_immutable(
    tmp_cfg, tmp_path
):
    workspace = tmp_path / "Agent 可用资料目录"
    workspace.mkdir()
    tmp_cfg.allowed_send_roots = [workspace]
    tmp_cfg.mcp_mail_read_enabled = True
    package_id = _archive_complete_fixture(tmp_cfg)
    service = ApplicationService(tmp_cfg)
    message = service.get_mail_message(package_id).details["message"]
    source_root = Path(message["package_root"])
    before = {
        path.relative_to(source_root).as_posix(): sha256_of_file(path)
        for path in source_root.rglob("*")
        if path.is_file()
    }

    result = service.prepare_mail_resources(
        package_id,
        [],
        mode="complete",
        target_workspace=str(workspace),
        overwrite_policy="rename",
    )

    assert result.ok, result.message
    assert result.details["mode"] == "complete"
    assert result.details["atomic_publish"] is True
    target = Path(result.details["target_directory"])
    assert (target / "邮件正文.md").is_file()
    assert (target / "原始邮件.eml").read_bytes() == (
        source_root / str(message["raw_eml"]["path"])
    ).read_bytes()
    assert (target / "邮件信息.json").is_file()
    assert (target / "完整资料manifest.json").is_file()
    assert (target / "原始归档manifest.json").is_file()
    assert (target / "附件" / "历史 数据.csv").is_file()
    assert (target / "附件" / "截图.png").is_file()
    assert (target / "邮件内图片" / "内嵌图片.png").is_file()
    manifest = json.loads(
        (target / "完整资料manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["package_id"] == package_id
    assert manifest["source_archive_immutable"] is True
    assert result.details["source_archive_file_count"] == len(before)
    assert all(item["sha256"] for item in result.details["prepared"])
    after = {
        path.relative_to(source_root).as_posix(): sha256_of_file(path)
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(target.parent.glob("*.tmp"))

    repeated = service.prepare_mail_resources(
        package_id,
        [],
        mode="complete",
        target_workspace=str(workspace),
        overwrite_policy="rename",
    )
    assert repeated.ok
    assert repeated.details["reused"] is True
    assert repeated.details["target_directory"] == str(target)

    source_before_modified_copy = {
        path.relative_to(source_root).as_posix(): sha256_of_file(path)
        for path in source_root.rglob("*")
        if path.is_file()
    }
    (target / "邮件正文.md").write_text("用户修改的工作副本", encoding="utf-8")
    regenerated = service.prepare_mail_resources(
        package_id,
        [],
        mode="complete",
        target_workspace=str(workspace),
        overwrite_policy="rename",
    )
    assert regenerated.ok
    assert regenerated.details["reused"] is False
    assert regenerated.details["existing_copy_modified"] is True
    assert regenerated.details["target_directory"] != str(target)
    assert (target / "邮件正文.md").read_text(encoding="utf-8") == "用户修改的工作副本"

    shutil.rmtree(Path(regenerated.details["target_directory"]))
    source_after_deleted_copy = {
        path.relative_to(source_root).as_posix(): sha256_of_file(path)
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert source_after_deleted_copy == source_before_modified_copy


def test_complete_mail_package_hash_mismatch_leaves_no_partial_result(
    tmp_cfg, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tmp_cfg.allowed_send_roots = [workspace]
    tmp_cfg.mcp_mail_read_enabled = True
    package_id = _archive_complete_fixture(tmp_cfg)
    service = ApplicationService(tmp_cfg)
    message = service.get_mail_message(package_id).details["message"]
    attachment = next(
        item
        for item in message["resources"]
        if item["internal_type"] == "attachment"
    )
    Path(attachment["absolute_path"]).write_bytes(b"tampered")

    result = service.prepare_mail_resources(
        package_id,
        [],
        mode="complete",
        target_workspace=str(workspace),
    )

    assert not result.ok
    assert result.error_code == "complete_mail_hash_mismatch"
    package_output = workspace / ".agentmailbridge" / "mail" / package_id
    assert not list(package_output.glob("*.tmp")) if package_output.exists() else True
    assert not (package_output / "完整邮件资料").exists()


def test_recommended_dynamic_scopes_follow_new_accounts_and_directories(
    tmp_cfg, tmp_path
):
    first_workspace = tmp_path / "first"
    first_workspace.mkdir()
    tmp_cfg.allowed_send_roots = [first_workspace]
    service = ApplicationService(tmp_cfg)
    service.synchronize_mail_accounts()
    created = service.create_agent_client(
        client_type="hermes",
        display_name="Hermes 推荐",
        permission_mode="recommended",
        account_scope_mode="all",
        workspace_scope_mode="all",
    )
    client_id, token = _activate(service, created)
    first_identity = service.resolve_agent_identity(client_id, token)
    assert "result.submit" not in first_identity.capabilities
    assert first_identity.account_scope_mode == "all"

    future_id = stable_account_id("163", "future-v16@163.com")
    create_mail_account(
        tmp_cfg.db_path,
        MailAccount(
            account_id=future_id,
            provider="163",
            email_address="future-v16@163.com",
            display_name="Future 163",
            auth_type="authorization_code",
            receive_enabled=True,
            send_enabled=True,
            capabilities=("receive", "send", "archive", "mail_facts"),
        ),
    )
    second_workspace = tmp_path / "second"
    second_workspace.mkdir()
    tmp_cfg.allowed_send_roots.append(second_workspace)

    refreshed = service.resolve_agent_identity(client_id, token)
    assert future_id in refreshed.account_ids
    _workspace_id, selected_path = service.require_agent_workspace(
        refreshed, str(second_workspace)
    )
    assert Path(selected_path) == second_workspace.resolve()


def test_selected_scope_does_not_expand_when_new_account_is_added(
    tmp_cfg, tmp_path
):
    workspace = tmp_path / "selected"
    workspace.mkdir()
    tmp_cfg.allowed_send_roots = [workspace]
    service = ApplicationService(tmp_cfg)
    service.synchronize_mail_accounts()
    account_id = str(service.list_mail_accounts().details["accounts"][0]["account_id"])
    workspace_id = str(
        service.list_agent_workspaces().details["workspace_details"][0][
            "workspace_id"
        ]
    )
    created = service.create_agent_client(
        client_type="custom",
        display_name="旧式明确选择",
        capabilities=["mail.search", "workspace.list"],
        account_ids=[account_id],
        workspace_ids=[workspace_id],
    )
    client_id, token = _activate(service, created)
    future_id = stable_account_id("163", "selected-future@163.com")
    create_mail_account(
        tmp_cfg.db_path,
        MailAccount(
            account_id=future_id,
            provider="163",
            email_address="selected-future@163.com",
            display_name="Selected Future",
            auth_type="authorization_code",
            receive_enabled=True,
            send_enabled=True,
            capabilities=("receive", "send"),
        ),
    )
    identity = service.resolve_agent_identity(client_id, token)
    assert identity.account_ids == frozenset({account_id})


def test_submit_result_requires_client_directory_scope(tmp_cfg, tmp_path):
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    tmp_cfg.allowed_send_roots = [allowed, denied]
    service = ApplicationService(tmp_cfg)
    service.synchronize_mail_accounts()
    workspace_id = next(
        row["workspace_id"]
        for row in service.list_agent_workspaces().details["workspace_details"]
        if Path(row["display_path"]) == allowed.resolve()
    )
    created = service.create_agent_client(
        client_type="custom",
        display_name="Scoped Submit",
        capabilities=["result.submit"],
        workspace_ids=[str(workspace_id)],
    )
    client_id, token = _activate(service, created)
    source = denied / "result.md"
    source.write_text("result", encoding="utf-8")
    server = McpServer(service, client_id=client_id, client_token=token)
    server.initialized = True

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "submit_result",
                "arguments": {"file_path": str(source)},
            },
        }
    )

    structured = response["result"]["structuredContent"]
    assert structured["error_code"] == "workspace_denied"


def test_hermes_yaml_round_trip_and_preview_never_leaks_other_secrets(
    tmp_path,
):
    target = tmp_path / "private-user" / "config.yaml"
    target.parent.mkdir()
    target.write_text(
        """
# keep this comment
api_key: third-party-secret
mcp_servers:
  other:
    command: other
    env:
      PASSWORD: existing-password
future_setting:
  enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    token = "ambc_v16_scoped_secret"
    plan = preview_client_config(
        client_id="client_" + "6" * 24,
        client_type="hermes",
        client_token=token,
        target_path=target,
    )
    assert token not in plan.preview
    assert "third-party-secret" not in plan.preview
    assert "existing-password" not in plan.preview
    assert str(target.parent) not in plan.preview
    applied = apply_client_config(plan, backup_root=tmp_path / "backups")
    parsed = YAML(typ="safe").load(target.read_text(encoding="utf-8"))
    assert parsed["api_key"] == "third-party-secret"
    assert parsed["future_setting"]["enabled"] is True
    assert parsed["mcp_servers"]["other"]["env"]["PASSWORD"] == "existing-password"
    assert (
        parsed["mcp_servers"]["agent-mail-bridge"]["env"][
            "AGENT_MAIL_BRIDGE_CLIENT_TOKEN"
        ]
        == token
    )
    assert "# keep this comment" in target.read_text(encoding="utf-8")
    assert applied.backup_path.is_file()

    untouched = tmp_path / "untouched.yaml"
    original = b"# only unrelated settings\nmodel: local\n"
    untouched.write_bytes(original)
    remove_plan = preview_client_config(
        client_id="client_" + "7" * 24,
        client_type="hermes",
        client_token=token,
        target_path=untouched,
        action="remove",
    )
    assert remove_plan.planned_bytes == original
    assert remove_plan.original_hash == remove_plan.applied_hash


def test_unknown_future_client_version_falls_back_to_assisted(monkeypatch):
    monkeypatch.setattr(
        "agent_mail_bridge.mcp_client_config._detect_executable_version",
        lambda _client_type: ("client.exe", "99.0.0 future"),
    )
    detection = detect_client("hermes")
    assert detection.installed is True
    assert detection.config_mode == "assisted"
    assert detection.status == "version_unverified"


def test_hermes_assisted_command_uses_one_env_group_before_args():
    command = mcp_client_command(
        "hermes",
        client_id="client_" + "8" * 24,
        client_token="ambc_hermes_scoped",
    )
    assert command.count("--env") == 1
    assert "AGENT_MAIL_BRIDGE_CLIENT_ID=" in command
    assert "AGENT_MAIL_BRIDGE_CLIENT_TOKEN=" in command
    if "--args" in command:
        assert command.index("--env") < command.index("--args")


def test_claude_assisted_command_separates_env_from_server_name():
    command = mcp_client_command(
        "claude_code",
        client_id="client_" + "9" * 24,
        client_token="ambc_claude_scoped",
    )
    assert command.startswith("claude ")
    assert "--env" in command
    assert command.index("--env") < command.index("--transport")
    assert command.index("--scope") < command.index("agent-mail-bridge")


def test_token_rotation_database_failure_restores_previous_token(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    created = service.create_agent_client(
        client_type="custom",
        display_name="Token rollback",
    )
    client_id, old_token = _activate(service, created)

    def fail_update(*_args, **_kwargs):
        raise sqlite3.OperationalError("fault injection")

    with monkeypatch.context() as context:
        context.setattr(
            "agent_mail_bridge.agent_integration.update_agent_client",
            fail_update,
        )
        result = service.rotate_agent_client_token(client_id)

    assert not result.ok
    assert result.error_code == "token_rotation_failed"
    assert service.resolve_agent_identity(client_id, old_token).client_id == client_id


def test_token_rotation_config_failure_restores_previous_token_and_config(
    tmp_cfg, tmp_path, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    created = service.create_agent_client(
        client_type="codex",
        display_name="Managed token rollback",
        config_mode="managed",
    )
    client_id, old_token = _activate(service, created)
    target = tmp_path / "codex-private" / "config.toml"
    target.parent.mkdir()
    original = b'[mcp_servers.other]\ncommand = "preserved"\n'
    target.write_bytes(original)
    update_agent_client(
        tmp_cfg.db_path,
        client_id,
        config_mode="managed",
        config_location=str(target),
        config_status="reload_required",
    )
    monkeypatch.setattr(
        "agent_mail_bridge.application_service.detect_client",
        lambda *_args, **_kwargs: ClientDetection(
            client_type="codex",
            installed=True,
            executable="codex.exe",
            version="codex-cli 0.145.0",
            config_path=target,
            config_mode="managed",
            status="managed_supported",
        ),
    )
    monkeypatch.setattr(
        "agent_mail_bridge.application_service.apply_client_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ClientConfigError("config_write_failed", "fault injection")
        ),
    )

    result = service.rotate_agent_client_token(client_id)

    assert not result.ok
    assert result.error_code == "config_write_failed"
    assert "旧 token 保持有效" in result.message
    assert service.resolve_agent_identity(client_id, old_token).client_id == client_id
    assert target.read_bytes() == original


def test_agent_config_backup_retention_limits_count_age_and_keeps_latest(
    tmp_cfg,
):
    service = ApplicationService(tmp_cfg)
    created = service.create_agent_client(
        client_type="custom",
        display_name="Backup retention",
    )
    client_id = str(created.details["client"]["client_id"])
    backup_root = (
        tmp_cfg.data_root_path / "backups" / "agent_client_configs"
    )
    backup_root.mkdir(parents=True)
    now = datetime.now()
    timestamp_rows: list[tuple[str, str]] = []
    for index in range(25):
        backup_id = f"backup_{index:02d}"
        backup_path = backup_root / f"{backup_id}.bak"
        backup_path.write_text(f"backup {index}", encoding="utf-8")
        insert_agent_config_backup(
            tmp_cfg.db_path,
            backup_id=backup_id,
            client_id=client_id,
            target_type="codex",
            original_path="config.toml",
            backup_path=str(backup_path),
            original_hash=f"original-{index}",
            applied_hash=f"applied-{index}",
            status="applied",
        )
        timestamp_rows.append(
            (
                (now - timedelta(days=index)).strftime("%Y-%m-%d %H:%M:%S"),
                backup_id,
            )
        )
    with sqlite3.connect(tmp_cfg.db_path) as conn:
        conn.executemany(
            """
            UPDATE agent_client_config_backups
            SET created_at = ?
            WHERE backup_id = ?
            """,
            timestamp_rows,
        )
        conn.commit()

    assert service._prune_agent_config_backups(client_id) == 5
    retained = query_agent_config_backups(tmp_cfg.db_path, client_id)
    assert len(retained) == 20
    latest_id = str(retained[0]["backup_id"])
    with sqlite3.connect(tmp_cfg.db_path) as conn:
        conn.execute(
            """
            UPDATE agent_client_config_backups
            SET created_at = ?
            WHERE client_id = ? AND backup_id <> ?
            """,
            (
                (now - timedelta(days=91)).strftime("%Y-%m-%d %H:%M:%S"),
                client_id,
                latest_id,
            ),
        )
        conn.commit()

    assert service._prune_agent_config_backups(client_id) == 19
    final_rows = query_agent_config_backups(tmp_cfg.db_path, client_id)
    assert [row["backup_id"] for row in final_rows] == [latest_id]
    assert Path(final_rows[0]["backup_path"]).is_file()


def test_history_import_segments_persists_progress_cancel_and_resume(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    service.synchronize_mail_accounts()
    account_id = str(service.list_mail_accounts().details["accounts"][0]["account_id"])
    calls: list[tuple[datetime, datetime]] = []

    def fake_segment(**kwargs):
        calls.append((kwargs["date_from"], kwargs["date_to"]))
        if kwargs.get("progress_callback"):
            kwargs["progress_callback"](
                {
                    "fetched": 2,
                    "matched": 2,
                    "saved": 1,
                    "duplicates": 1,
                }
            )
        return ReceiveResult(
            OperationStatus.SUCCESS,
            backend="imap",
            scanned=2,
            matched=2,
            saved=1,
            duplicates=1,
            message="ok",
        )

    monkeypatch.setattr(service, "historical_rescan", fake_segment)
    result = service.import_historical_mails(
        account_id=account_id,
        preset="2024",
    )
    assert result.ok
    assert len(calls) >= 3
    assert calls[0][0] == datetime(2024, 1, 1)
    assert calls[-1][1] == datetime(2024, 12, 31, 23, 59, 59)
    run = get_history_import_run(tmp_cfg.db_path, result.scan_id)
    assert run and run["status"] == "completed"
    assert run["saved"] == len(calls)
    assert run["total_segments"] == len(calls)

    cancel_event = threading.Event()
    calls.clear()

    def cancel_after_first(**kwargs):
        value = fake_segment(**kwargs)
        cancel_event.set()
        return value

    monkeypatch.setattr(service, "historical_rescan", cancel_after_first)
    cancelled = service.import_historical_mails(
        account_id=account_id,
        preset="2024",
        cancel_event=cancel_event,
    )
    assert cancelled.status == OperationStatus.CANCELLED
    cancelled_run = get_history_import_run(tmp_cfg.db_path, cancelled.scan_id)
    assert cancelled_run and cancelled_run["status"] == "cancelled"
    assert cancelled_run["next_segment_index"] == 2

    cancel_event.clear()
    calls.clear()
    monkeypatch.setattr(service, "historical_rescan", fake_segment)
    resumed = service.resume_history_import(cancelled.scan_id)
    assert resumed.ok
    resumed_run = get_history_import_run(tmp_cfg.db_path, cancelled.scan_id)
    assert resumed_run and resumed_run["status"] == "completed"
    assert resumed_run["preset"] == "2024"
    assert len(calls) == int(resumed_run["total_segments"]) - 1
    assert resumed_run["saved"] == resumed_run["total_segments"]


def test_history_partial_keeps_earliest_retry_segment_and_resume_clears_failure(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    service.synchronize_mail_accounts()
    account_id = str(service.list_mail_accounts().details["accounts"][0]["account_id"])
    segment_calls = 0

    def first_pass(**_kwargs):
        nonlocal segment_calls
        segment_calls += 1
        if segment_calls == 1:
            return ReceiveResult(
                OperationStatus.PARTIAL,
                backend="imap",
                scanned=1,
                matched=1,
                saved=1,
                failed=0,
                message="one message deferred",
            )
        return ReceiveResult(
            OperationStatus.NO_CHANGES,
            backend="imap",
            message="ok",
        )

    monkeypatch.setattr(service, "historical_rescan", first_pass)
    partial = service.import_historical_mails(
        account_id=account_id,
        preset="2024",
    )
    assert partial.status == OperationStatus.PARTIAL
    row = get_history_import_run(tmp_cfg.db_path, partial.scan_id)
    assert row and row["status"] == "partial"
    assert row["next_segment_index"] == 1

    monkeypatch.setattr(
        service,
        "historical_rescan",
        lambda **_kwargs: ReceiveResult(
            OperationStatus.NO_CHANGES,
            backend="imap",
            duplicates=1,
            message="deduplicated",
        ),
    )
    resumed = service.resume_history_import(partial.scan_id)
    assert resumed.status == OperationStatus.SUCCESS
    row = get_history_import_run(tmp_cfg.db_path, partial.scan_id)
    assert row and row["status"] == "completed"
    assert row["preset"] == "2024"
    assert row["next_segment_index"] == int(row["total_segments"]) + 1
