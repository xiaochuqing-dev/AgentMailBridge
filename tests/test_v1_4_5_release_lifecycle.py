"""v1.4.5 Release Lifecycle 与 Gmail Baseline 定向回归。"""

from __future__ import annotations

import os
from pathlib import Path

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.mail_accounts import (
    current_receive_account_id,
    current_send_account_id,
)
from agent_mail_bridge.models import OperationStatus, ServiceResult
from scripts.release_lifecycle_validation import (
    _close_runtime_handles,
    _expected_mcp_tool_count,
    _same_persistent_facts,
    _seed_baseline,
    _snapshot,
    _uninstall,
)


def test_lifecycle_uses_versioned_mcp_tool_counts():
    assert _expected_mcp_tool_count("1.6.0") == 7
    assert _expected_mcp_tool_count("1.7.0") == 11


def test_gmail_diagnostic_uses_account_specific_oauth_paths(
    tmp_cfg, monkeypatch
):
    tmp_cfg.gmail_receive_backend = "gmail_api"
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    gmail_id = current_receive_account_id(tmp_cfg)
    runtime = service._account_router.context(
        gmail_id, capability="receive"
    ).config
    assert Path(runtime.gmail_api_token_path) != Path(
        tmp_cfg.gmail_api_token_path
    )

    captured = {}

    def fake_reverify(cfg):
        captured["credentials"] = Path(cfg.gmail_api_credentials_path)
        captured["token"] = Path(cfg.gmail_api_token_path)
        captured["scopes"] = list(cfg.gmail_api_scopes)
        return ServiceResult(OperationStatus.SUCCESS, message="ok")

    monkeypatch.setattr(
        "agent_mail_bridge.gmail_api_auth.reverify_gmail_authorization",
        fake_reverify,
    )
    result = service.diagnose_gmail_api()
    assert result.ok
    assert captured["credentials"] == Path(runtime.gmail_api_credentials_path)
    assert captured["token"] == Path(runtime.gmail_api_token_path)
    assert captured["scopes"] == [
        "https://www.googleapis.com/auth/gmail.readonly"
    ]


def test_gmail_diagnostic_rejects_non_gmail_account(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    result = service.diagnose_gmail_api(current_send_account_id(tmp_cfg))
    assert not result.ok
    assert result.error_code == "gmail_api_not_configured"


def test_lifecycle_snapshot_comparison_checks_only_persistent_facts():
    before = {
        "db_integrity": "ok",
        "schema_version": 3,
        "counts": {"accounts": 4, "packages": 4},
        "account_ids": ["acct_a", "acct_b"],
        "raw_eml_count": 4,
        "attachment_count": 4,
        "package_file_hashes": {"received/mail/pkg/raw.eml": "abc"},
        "oauth_file_hashes": {"OAuth/accounts/a/token.json": "def"},
        "config_exists": True,
        "gui_settings_exists": True,
        "credential_exists": True,
    }
    after = {**before, "db_integrity": "ok"}
    assert _same_persistent_facts(before, after)
    after["package_file_hashes"] = {"received/mail/pkg/raw.eml": "changed"}
    assert not _same_persistent_facts(before, after)


def test_lifecycle_baseline_covers_all_account_capability_shapes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE", "1")
    home = tmp_path / "User Home"
    try:
        cfg, _gmail_id = _seed_baseline(home)
    finally:
        _close_runtime_handles()
    snapshot = _snapshot(home, cfg, credential_exists=True)
    assert snapshot["db_integrity"] == "ok"
    assert snapshot["counts"] == {
        "accounts": 5,
        "packages": 4,
        "outbound": 1,
        "scheduler": 4,
    }
    assert snapshot["raw_eml_count"] == 4
    assert snapshot["attachment_count"] == 4


def test_lifecycle_uninstall_uses_latest_inno_uninstaller(
    tmp_path, monkeypatch
):
    older = tmp_path / "unins000.exe"
    newer = tmp_path / "unins001.exe"
    older.touch()
    newer.touch()
    older_mtime = older.stat().st_mtime_ns
    newer_mtime = older_mtime + 1_000_000
    os.utime(older, ns=(older_mtime, older_mtime))
    os.utime(newer, ns=(newer_mtime, newer_mtime))
    captured = {}

    def fake_run(command, *, env, timeout):
        captured["command"] = command

    monkeypatch.setattr(
        "scripts.release_lifecycle_validation._run", fake_run
    )
    _uninstall(tmp_path, {})
    assert captured["command"][0] == str(newer)


def test_inno_installer_supports_randomized_lifecycle_identity():
    root = Path(__file__).resolve().parent.parent
    content = (
        root / "packaging" / "windows" / "AgentMailBridge.iss"
    ).read_text(encoding="utf-8")
    assert "#ifndef MyAppName" in content
    assert "#ifndef MyAppId" in content
    assert "#ifndef MyOutputBaseFilename" in content
    assert "OutputBaseFilename={#MyOutputBaseFilename}" in content
