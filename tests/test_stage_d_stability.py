"""阶段 D：大数据量、资源稳定性与失败恢复测试。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import get_connection
from agent_mail_bridge.mail_send import SmtpStageError
from agent_mail_bridge.models import OperationStatus
from agent_mail_bridge.performance import run_stability_benchmark


def test_isolated_large_data_benchmark_outputs_measured_metrics(tmp_path: Path):
    output = tmp_path / "performance.json"

    report = run_stability_benchmark(records=1_000, cycles=5, output=output)

    assert report["isolated"]
    assert report["records"]["received"] == 1_000
    assert report["database"]["integrity_check"] == "ok"
    assert report["queries"]["refresh_bundle"]["maximum_ms"] > 0
    assert report["log_rotation"]["rotated"]
    assert report["resources"]["threads_after"] == report["resources"]["threads_before"]
    assert json.loads(output.read_text(encoding="utf-8"))["cycles"] == 5


@pytest.mark.parametrize("records,cycles", [(999, 5), (1000, 4), (100001, 5)])
def test_benchmark_rejects_unreasonable_parameters(tmp_path: Path, records: int, cycles: int):
    with pytest.raises(ValueError):
        run_stability_benchmark(
            records=records, cycles=cycles, output=tmp_path / "invalid.json"
        )


def test_date_query_uses_existing_index(tmp_cfg):
    plan = get_connection(tmp_cfg.db_path).execute(
        "EXPLAIN QUERY PLAN SELECT * FROM received_messages "
        "WHERE saved_date = ? ORDER BY received_at ASC",
        ("2026-07-11",),
    ).fetchall()

    assert any("idx_received_messages_saved_date" in row[3] for row in plan)


def test_repeated_refresh_keeps_thread_count_stable(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    before = threading.active_count()

    for _ in range(100):
        service.get_history(100)
        service.get_recent_logs(100)
        service.get_mcp_history(100)

    assert threading.active_count() == before
    assert get_connection(tmp_cfg.db_path).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_smtp_failure_then_recovery_is_explicit_and_idempotent(tmp_cfg, monkeypatch):
    source = tmp_cfg.data_root_path / "retry.txt"
    source.write_text("retry", encoding="utf-8")
    calls = []

    def fail_once(_cfg, _message):
        calls.append("failed")
        raise SmtpStageError("connect", "模拟网络断开")

    monkeypatch.setattr("agent_mail_bridge.mail_send._smtp_send_with_stage", fail_once)
    service = ApplicationService(tmp_cfg)
    first = service.send_file(source, request_id="recovery-001")
    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage",
        lambda _cfg, _message: calls.append("sent"),
    )
    second = service.send_file(source, request_id="recovery-001")
    third = service.send_file(source, request_id="recovery-001")

    assert first.status == OperationStatus.FAILED
    assert second.status == OperationStatus.SUCCESS
    assert third.status == OperationStatus.DUPLICATE
    assert calls == ["failed", "sent"]
