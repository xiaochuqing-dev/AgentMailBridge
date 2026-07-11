"""阶段 A：GUI 全局文件信任入口与 MCP 目录边界测试。"""

from __future__ import annotations

from pathlib import Path

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import get_send_by_request_id
from agent_mail_bridge.models import OperationStatus
from agent_mail_bridge.utils import sha256_of_file


def test_manual_global_file_uses_verified_snapshot(tmp_cfg, tmp_path, monkeypatch):
    source = tmp_path / "桌面等价目录" / "很长的中文文件名_专项结果.txt"
    source.parent.mkdir()
    source.write_text("全局文件内容", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, message: captured.append(message),
    )

    result = ApplicationService(tmp_cfg).send_user_selected_file(
        source,
        request_id="manual-global-001",
        expected_sha256=sha256_of_file(source),
    )
    row = get_send_by_request_id(tmp_cfg.db_path, "manual-global-001")
    attachment = next(captured[0].iter_attachments())

    assert result.status == OperationStatus.SUCCESS
    assert attachment.get_filename() == source.name
    assert attachment.get_payload(decode=True) == source.read_bytes()
    assert row is not None
    assert row["source_origin"] == "manual_gui"
    assert row["original_filename"] == source.name
    assert row["size_bytes"] == source.stat().st_size
    assert Path(row["source_path"]).is_relative_to(tmp_cfg.data_root_path)
    assert sha256_of_file(Path(row["source_path"])) == sha256_of_file(source)


def test_manual_send_rejects_changed_source(tmp_cfg, tmp_path, monkeypatch):
    source = tmp_path / "Downloads" / "result.md"
    source.parent.mkdir()
    source.write_text("first", encoding="utf-8")
    expected = sha256_of_file(source)
    source.write_text("second", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda *_args: calls.append(True),
    )

    result = ApplicationService(tmp_cfg).send_user_selected_file(
        source, request_id="manual-changed-001", expected_sha256=expected
    )

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "file_changed"
    assert calls == []


def test_manual_empty_file_is_allowed_but_dangerous_file_is_rejected(
    tmp_cfg, tmp_path, monkeypatch
):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    dangerous = tmp_path / "run.ps1"
    dangerous.write_text("Write-Host test", encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage", lambda *_args: None
    )
    service = ApplicationService(tmp_cfg)

    accepted = service.send_user_selected_file(empty, request_id="manual-empty-001")
    rejected = service.send_user_selected_file(dangerous, request_id="manual-risk-001")

    assert accepted.status == OperationStatus.SUCCESS
    assert rejected.error_code == "file_type_not_allowed"


def test_mcp_cannot_reuse_manual_global_trust(tmp_cfg, tmp_path):
    outside = tmp_path / "other-drive-equivalent" / "result.txt"
    outside.parent.mkdir()
    outside.write_text("outside", encoding="utf-8")

    result = ApplicationService(tmp_cfg).submit_result(
        outside, request_id="mcp-still-restricted-001"
    )

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "path_not_allowed"
