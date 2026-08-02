from __future__ import annotations

from pathlib import Path

from agent_mail_bridge.config import _DEFAULT_GMAIL_API_SCOPES
from agent_mail_bridge.mcp_server import SERVER_VERSION, _all_tools
from agent_mail_bridge.reconciliation_evidence import (
    COMPOSITE_EVIDENCE_WINDOW_SECONDS,
    decide_reconciliation_candidate,
)
from agent_mail_bridge.send_reconciliation import EVIDENCE_PRIORITY
from agent_mail_bridge.version import __version__
from scripts import v1_7_real_agent_validation


ROOT = Path(__file__).resolve().parents[1]


def test_v172_version_and_windows_metadata_are_consistent():
    assert __version__ == SERVER_VERSION == "1.7.2"
    for relative in (
        "packaging/windows/version_info.txt",
        "packaging/windows/version_info_mcp.txt",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "u'FileVersion', u'1.7.2'" in content
        assert "u'ProductVersion', u'1.7.2'" in content
    installer = (ROOT / "packaging/windows/AgentMailBridge.iss").read_text(
        encoding="utf-8"
    )
    assert '#define MyAppVersion "1.7.2"' in installer


def test_v172_keeps_gmail_readonly_and_eleven_mcp_tools():
    assert _DEFAULT_GMAIL_API_SCOPES == (
        "https://www.googleapis.com/auth/gmail.readonly"
    )
    tools = _all_tools()
    assert len(tools) == 11
    assert {str(tool["name"]) for tool in tools} == {
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


def test_message_id_is_not_a_strong_reconciliation_evidence_type():
    assert "exact_message_id" not in EVIDENCE_PRIORITY
    assert EVIDENCE_PRIORITY == (
        "exact_outbound_id",
        "exact_provider_id",
        "exact_raw_hash",
    )
    assert COMPOSITE_EVIDENCE_WINDOW_SECONDS == 604800


def test_pure_decision_refuses_one_message_id_without_composite():
    decision = decide_reconciliation_candidate(
        strong_matches={},
        evidence_priority=(),
        message_id_candidates={"old-package"},
        composite_candidates=set(),
    )
    assert decision.status == "unmatched"
    assert decision.evidence_type == "weak_message_id_only"
    assert decision.candidate_count == 1
    assert decision.candidate_id is None


def test_pure_decision_marks_conflicting_provider_and_raw_evidence_ambiguous():
    decision = decide_reconciliation_candidate(
        strong_matches={
            "exact_provider_id": {"package-a"},
            "exact_raw_hash": {"package-b"},
        },
        evidence_priority=("exact_provider_id", "exact_raw_hash"),
    )
    assert decision.status == "ambiguous"
    assert decision.evidence_type == "conflicting_strong_evidence"
    assert decision.candidate_count == 2
    assert decision.candidate_id is None


def test_explicit_provider_mapping_overrides_conflicting_weak_message_id():
    decision = decide_reconciliation_candidate(
        strong_matches={"exact_provider_id": {"mapped-package"}},
        evidence_priority=("exact_provider_id",),
        message_id_candidates={"reused-message-id-package"},
        message_id_override_evidence={"exact_provider_id"},
    )
    assert decision.status == "matched"
    assert decision.candidate_id == "mapped-package"
    assert decision.decision_reason == "strong_evidence_overrode_message_id"


def test_v172_lifecycle_defaults_and_upgrade_backup_are_current():
    builder = (ROOT / "scripts/build_lifecycle_installers.ps1").read_text(
        encoding="utf-8"
    )
    assert '[string]$OldVersion = "1.7.1"' in builder
    assert '[string]$NewVersion = "1.7.2"' in builder

    lifecycle = (ROOT / "scripts/release_lifecycle_validation.py").read_text(
        encoding="utf-8"
    )
    assert 'parser.add_argument("--old-version", default="1.7.1")' in lifecycle
    assert 'parser.add_argument("--new-version", default="1.7.2")' in lifecycle
    assert 'label="before_v1_7_2_upgrade"' in lifecycle
    assert "verify_database_backup(" in lifecycle
    assert "temporary.cleanup()" in lifecycle
    assert "if final_uninstall_needed and install_dir.exists():" in lifecycle


def test_v172_real_agent_read_validation_requests_refresh_and_current_version():
    assert "sync.ensure_fresh" in v1_7_real_agent_validation.CAPABILITIES
    prompt = v1_7_real_agent_validation._natural_prompt(
        "read",
        marker="unused",
        attachment_names=[],
    )
    assert "v1.7.2" in prompt
    assert "v1.7.0" not in prompt
