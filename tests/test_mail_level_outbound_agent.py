"""邮件级发件、Agent 工作区和兼容迁移高价值回归。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    backfill_legacy_outbound_messages,
    get_connection,
    insert_sent_file,
    outbound_mail_migration_needed,
)
from agent_mail_bridge.models import OperationStatus


def test_manual_message_body_three_attachments_two_links_is_one_mail(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    sources = []
    for index, name in enumerate(("报告一.txt", "报告二.txt", "结果.csv"), 1):
        path = tmp_path / name
        path.write_text(f"content-{index}", encoding="utf-8")
        sources.append(path)
    captured = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, message: captured.append(message),
    )

    result = ApplicationService(tmp_cfg).send_user_selected_mail(
        subject="完整交付",
        body_text="这是邮件正文。",
        attachment_paths=sources,
        links=[
            {"url": "https://example.com/page", "display_text": "说明"},
            "https://example.com/file.pdf",
        ],
    )

    assert result.status == OperationStatus.SUCCESS
    assert result.attachment_count == 3
    assert result.link_count == 2
    assert len(captured) == 1
    assert captured[0]["To"] == tmp_cfg.owner_gmail
    assert len(list(captured[0].iter_attachments())) == 3
    assert "这是邮件正文" in captured[0].get_body(preferencelist=("plain",)).get_content()
    assert "相关链接" in captured[0].get_body(preferencelist=("plain",)).get_content()
    connection = get_connection(tmp_cfg.db_path)
    assert connection.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM outbound_resources").fetchone()[0] == 3
    assert connection.execute("SELECT COUNT(*) FROM outbound_links").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM sent_files").fetchone()[0] == 3


def test_body_only_and_subject_only_are_valid_single_messages(
    tmp_cfg, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, message: sent.append(message),
    )
    service = ApplicationService(tmp_cfg)
    body_only = service.send_user_selected_mail(
        subject=None, body_text="只有正文", attachment_paths=[], links=[]
    )
    subject_only = service.send_user_selected_mail(
        subject="只有主题", body_text="", attachment_paths=[], links=[]
    )
    assert body_only.ok and subject_only.ok
    assert len(sent) == 2
    assert get_connection(tmp_cfg.db_path).execute(
        "SELECT COUNT(*) FROM outbound_messages"
    ).fetchone()[0] == 2


def test_empty_manual_mail_is_rejected_before_smtp(tmp_cfg, monkeypatch):
    called = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, _message: called.append(True),
    )
    result = ApplicationService(tmp_cfg).send_user_selected_mail(
        subject=None, body_text="", attachment_paths=[], links=[]
    )
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "empty_message"
    assert called == []
    assert get_connection(tmp_cfg.db_path).execute(
        "SELECT COUNT(*) FROM outbound_messages"
    ).fetchone()[0] == 0


def test_zero_byte_long_unicode_attachment_is_valid(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    source = tmp_path / ("超长混合 Unicode 文件名 mixed-" * 6 + ".txt")
    source.write_bytes(b"")
    captured = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, message: captured.append(message),
    )
    result = ApplicationService(tmp_cfg).send_user_selected_mail(
        subject="零字节附件", body_text="", attachment_paths=[source], links=[]
    )
    assert result.ok
    attachment = list(captured[0].iter_attachments())[0]
    assert attachment.get_filename() == source.name
    assert attachment.get_payload(decode=True) == b""


def test_total_attachment_size_limit_blocks_before_smtp(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    tmp_cfg.max_send_file_mb = 1
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"a" * 600_000)
    second.write_bytes(b"b" * 600_000)
    called = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, _message: called.append(True),
    )
    result = ApplicationService(tmp_cfg).send_user_selected_mail(
        subject="总大小", body_text="", attachment_paths=[first, second], links=[]
    )
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "total_size_too_large"
    assert called == []


def test_duplicate_filenames_from_different_paths_remain_two_attachments(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    first = tmp_path / "a" / "同名.txt"
    second = tmp_path / "b" / "同名.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, message: captured.append(message),
    )
    result = ApplicationService(tmp_cfg).send_user_selected_mail(
        subject="同名附件", body_text="", attachment_paths=[first, second], links=[]
    )
    assert result.ok
    attachments = list(captured[0].iter_attachments())
    assert [item.get_filename() for item in attachments] == ["同名.txt", "同名.txt"]
    assert [item.get_payload(decode=True) for item in attachments] == [b"one", b"two"]


def test_smtp_failure_is_one_failed_outbound_mail(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    source = tmp_path / "failure.txt"
    source.write_text("failure", encoding="utf-8")

    def fail(_cfg, _message):
        from agent_mail_bridge.mail_send import SmtpStageError
        raise SmtpStageError("send", "模拟发送失败")

    monkeypatch.setattr("agent_mail_bridge.mail_send._smtp_send_with_stage", fail)
    result = ApplicationService(tmp_cfg).send_user_selected_mail(
        subject="失败测试", body_text="正文", attachment_paths=[source], links=[]
    )
    assert result.status == OperationStatus.FAILED
    rows = get_connection(tmp_cfg.db_path).execute(
        "SELECT status, attachment_count FROM outbound_messages"
    ).fetchall()
    assert [(row["status"], row["attachment_count"]) for row in rows] == [("failed", 1)]


def test_submit_result_maps_to_one_agent_outbound_and_duplicate(
    tmp_cfg, monkeypatch
):
    source = tmp_cfg.data_root_path / "agent-result.md"
    source.write_text("agent result", encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, _message: None,
    )
    service = ApplicationService(tmp_cfg)
    first = service.submit_result(source, title="Agent 交付", request_id="stable-agent-1")
    second = service.submit_result(source, title="Agent 交付", request_id="stable-agent-1")
    assert first.status == OperationStatus.SUCCESS
    assert second.status == OperationStatus.DUPLICATE
    rows = get_connection(tmp_cfg.db_path).execute(
        "SELECT source_origin, request_id, attachment_count FROM outbound_messages"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("agent_mcp", "stable-agent-1", 1)]


def test_legacy_sent_backfill_is_idempotent_and_limited(tmp_cfg):
    insert_sent_file(
        tmp_cfg.db_path,
        source_path="legacy.txt",
        send_copy_path="send/legacy.txt",
        sent_copy_path="sent/legacy.txt",
        sha256="hash",
        subject="旧发送记录",
        from_email="test@qq.com",
        to_email="owner@gmail.com",
        sent_at="2026-07-01 01:02:03",
        status="sent",
    )
    first = backfill_legacy_outbound_messages(tmp_cfg.db_path)
    second = backfill_legacy_outbound_messages(tmp_cfg.db_path)
    assert first["migrated"] == 1
    assert second["migrated"] == 0
    row = get_connection(tmp_cfg.db_path).execute(
        "SELECT body_text, legacy_limited FROM outbound_messages"
    ).fetchone()
    assert row["body_text"] == ""
    assert row["legacy_limited"] == 1


def test_outbound_migration_detection_for_old_sent_schema(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE sent_files (id INTEGER PRIMARY KEY, subject TEXT)"
    )
    connection.commit()
    connection.close()
    assert outbound_mail_migration_needed(db_path)


def test_workspace_authorization_persists_and_rejects_overlap(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "agent_mail_bridge.security.Path.home",
        classmethod(lambda cls: Path("C:/__agent_mail_bridge_test_home__")),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "protected-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "protected-roaming"))
    workspace = tmp_path / "projects" / "mail-project"
    nested = workspace / "docs"
    nested.mkdir(parents=True)
    env_path = tmp_path / "managed.env"
    service = ApplicationService(tmp_cfg)

    added = service.add_agent_workspace(workspace, env_path=env_path)
    duplicate = service.add_agent_workspace(workspace, env_path=env_path)
    nested_result = service.add_agent_workspace(nested, env_path=env_path)
    assert added.ok
    assert duplicate.status == OperationStatus.NO_CHANGES
    assert nested_result.status == OperationStatus.NO_CHANGES
    assert dotenv_values(env_path)["ALLOWED_SEND_ROOTS"] == str(workspace.resolve())
    assert service.submit_result(
        workspace / "missing.txt", request_id="authorized-missing"
    ).error_code == "file_not_found"
    deliverable = workspace / "final-report.md"
    deliverable.write_text("final", encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, _message: None,
    )
    delivered = service.submit_result(
        deliverable, title="最终报告", request_id="authorized-file"
    )
    assert delivered.status == OperationStatus.SUCCESS

    removed = service.remove_agent_workspace(workspace, env_path=env_path)
    assert removed.ok
    assert service.submit_result(
        deliverable, request_id="unauthorized-file"
    ).error_code == "path_not_allowed"


@pytest.mark.parametrize(
    "candidate",
    [Path.home(), Path.home() / "AppData"],
)
def test_workspace_authorization_rejects_broad_sensitive_roots(
    tmp_cfg, tmp_path: Path, candidate: Path
):
    if not candidate.exists():
        pytest.skip("当前平台没有该目录")
    result = ApplicationService(tmp_cfg).add_agent_workspace(
        candidate, env_path=tmp_path / "managed.env"
    )
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "workspace_not_allowed"


def test_sensitive_delivery_file_is_rejected_inside_authorized_workspace(
    tmp_cfg, tmp_path: Path
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    secret = workspace / ".env"
    secret.write_text("SECRET=test", encoding="utf-8")
    service = ApplicationService(tmp_cfg)
    service.add_agent_workspace(workspace, env_path=tmp_path / "managed.env")
    result = service.submit_result(secret, request_id="sensitive-file")
    assert result.error_code == "path_not_allowed"


def test_workspace_authorization_rejects_drive_root(tmp_cfg, tmp_path: Path):
    result = ApplicationService(tmp_cfg).add_agent_workspace(
        Path(tmp_path.anchor), env_path=tmp_path / "managed.env"
    )
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "workspace_not_allowed"


@pytest.mark.parametrize(
    "environment_name",
    ["WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"],
)
def test_workspace_authorization_rejects_existing_system_roots(
    tmp_cfg, tmp_path: Path, environment_name: str
):
    raw_path = os.environ.get(environment_name, "")
    if not raw_path or not Path(raw_path).exists():
        pytest.skip(f"{environment_name} 在当前系统不可用")
    result = ApplicationService(tmp_cfg).add_agent_workspace(
        raw_path, env_path=tmp_path / "managed.env"
    )
    assert result.status == OperationStatus.FAILED
    assert result.error_code == "workspace_not_allowed"


def test_authorized_workspace_symlink_escape_is_rejected(
    tmp_cfg, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "agent_mail_bridge.security.Path.home",
        classmethod(lambda cls: Path("C:/__agent_mail_bridge_test_home__")),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "protected-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "protected-roaming"))
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不能创建测试符号链接：{exc}")
    service = ApplicationService(tmp_cfg)
    assert service.add_agent_workspace(
        workspace, env_path=tmp_path / "managed.env"
    ).ok
    result = service.submit_result(
        link / secret.name, request_id="symlink-escape"
    )
    assert result.error_code == "path_not_allowed"
