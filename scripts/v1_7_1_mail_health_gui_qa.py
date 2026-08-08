"""用隔离数据生成 v1.7.1 邮件运行状态页 DPI/主题验收截图。"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QWidget

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection, get_connection, upsert_mailboxes
from agent_mail_bridge.mailbox_checkpoint import finish_mailbox_attempt
from agent_mail_bridge.ui.health_page import format_mail_health
from agent_mail_bridge.ui.main_window import BridgeWindow, format_size


def _config(root: Path, workspace: Path) -> AppConfig:
    cfg = AppConfig(
        gmail_address="mail-health-qa@example.test",
        qq_email="mail-health-send@example.test",
        owner_gmail="owner@example.test",
        data_root=root / "data",
        gmail_api_credentials_path=root / "oauth" / "credentials.json",
        gmail_api_token_path=root / "oauth" / "token.json",
    )
    cfg.allowed_send_roots = [workspace]
    cfg.mcp_mail_read_enabled = True
    return cfg


def _seed(service: ApplicationService, root: Path, workspace: Path) -> None:
    if not service.synchronize_mail_accounts().ok:
        raise RuntimeError("GUI QA 账号初始化失败")
    account = service.list_mail_accounts().details["accounts"][0]
    account_id = str(account["account_id"])
    client = service.create_agent_client(
        client_type="codex",
        display_name="邮件健康 QA Client",
        config_mode="managed",
        permission_mode="recommended",
        account_scope_mode="all",
        workspace_scope_mode="all",
    )
    if not client.ok:
        raise RuntimeError("GUI QA Client 初始化失败")
    client_id = str(client.details["client"]["client_id"])
    mailbox_rows = upsert_mailboxes(
        service.cfg.db_path,
        account_id,
        [
            {
                "external_ref": f"qa-folder-{index}",
                "raw_name": f"QA/长期运行/目录/{index:02d}",
                "display_name": f"长期运行验证目录 {index:02d}",
                "mailbox_role": "sent" if index == 1 else "other",
                "role_source": "qa",
            }
            for index in range(1, 13)
        ],
    )
    for index, mailbox in enumerate(mailbox_rows):
        finish_mailbox_attempt(
            service.cfg.db_path,
            mailbox_id=str(mailbox["mailbox_id"]),
            account_id=account_id,
            uidvalidity=700 + index,
            uidnext=1000 + index,
            highestmodseq=500 + index,
            last_uid=900 + index,
            checkpoint={"last_uid": 900 + index},
            result="failed" if index % 4 == 0 else "no_changes",
            error="qa_network_failure" if index % 4 == 0 else "",
            current_attempt={"stage": "membership_snapshot"},
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    statuses = (
        ["delivery_unknown"] * 6
        + ["sent_archive_failed"] * 4
        + ["sending"] * 2
        + ["pending_confirmation"] * 3
        + ["sent"] * 8
        + ["cancelled"] * 2
    )
    connection = get_connection(service.cfg.db_path)
    request_ids: list[str] = []
    for index, status in enumerate(statuses):
        request_id = f"qa-health-request-{index:03d}"
        request_ids.append(request_id)
        connection.execute(
            """
            INSERT INTO send_requests
                (send_request_id, client_id, idempotency_key, operation,
                 sender_account_id, send_mode, subject, body_text, body_html,
                 status, current_stage, recovery_required, delivery_status,
                 message_id, created_at, updated_at)
            VALUES (?, ?, ?, 'new', ?, 'confirm', ?, '', '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                client_id,
                f"qa-idempotency-{index:03d}",
                account_id,
                "这是一条用于验证长主题不遮挡其他控件的邮件运行状态记录 "
                f"{index:03d}",
                status,
                "smtp_data_started" if status == "sending" else "created",
                int(status in {"delivery_unknown", "sent_archive_failed"}),
                "delivery_unknown"
                if status == "delivery_unknown"
                else "sent" if status in {"sent", "sent_archive_failed"} else "not_sent",
                f"<qa-health-{index:03d}@example.test>",
                old if status == "cancelled" else now,
                old if status == "cancelled" else now,
            ),
        )
    for index, request_id in enumerate(request_ids[10:12], start=1):
        connection.execute(
            """
            INSERT INTO send_execution_leases
                (send_request_id, lease_owner, process_id, acquired_at,
                 heartbeat_at, lease_expires_at, attempt_no, current_stage,
                 fixed_message_id, fixed_mime_sha256, updated_at)
            VALUES (?, ?, ?, ?, ?, '2000-01-01 00:00:00', 1,
                    'smtp_data_started', ?, NULL, ?)
            """,
            (
                request_id,
                f"qa-owner-{index}",
                f"qa-process-{index}",
                old,
                old,
                f"<qa-health-lease-{index}@example.test>",
                old,
            ),
        )
    for index in range(130):
        connection.execute(
            """
            INSERT INTO health_issues
                (issue_id, category, issue_code, severity, entity_type,
                 entity_id, state, details_json, first_seen_at, last_seen_at)
            VALUES (?, 'qa_layout', 'qa_long_running_issue', ?, 'qa_entity', ?,
                    'open', '{}', ?, ?)
            """,
            (
                f"qa-health-issue-{index:03d}",
                "error" if index < 12 else "warning",
                f"qa-entity-with-a-long-stable-identifier-{index:03d}",
                now,
                now,
            ),
        )
    for index in range(5):
        connection.execute(
            """
            INSERT INTO reconciliation_records
                (reconciliation_id, entity_type, entity_id, account_id,
                 status, evidence_type, confidence, candidate_count,
                 details_json, first_seen_at, last_seen_at)
            VALUES (?, 'sent_observation', ?, ?, 'ambiguous',
                    'qa_multiple_candidates', 'manual_review', 3, '{}', ?, ?)
            """,
            (f"qa-recon-{index}", f"qa-sent-{index}", account_id, now, now),
        )
    connection.commit()

    for request_id in request_ids[-2:]:
        snapshot = service.cfg.send_dir / "agent_requests" / request_id
        snapshot.mkdir(parents=True, exist_ok=True)
        _sparse_file(snapshot / "snapshot.bin", 8_765_432)
    _sparse_file(
        service.cfg.received_dir / "mail" / "qa-package" / "raw.eml",
        12_345_678,
    )
    _sparse_file(
        workspace / ".agentmailbridge" / "mail" / "qa-work-copy" / "body.txt",
        4_567_890,
    )
    _sparse_file(root / "data" / "backups" / "qa-capacity.bin", 23_456_789)


def _sparse_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _overflowing_widgets(page: QWidget) -> list[str]:
    overflow: list[str] = []
    for widget in page.findChildren(QWidget):
        if not widget.isVisible() or widget.window() is not page.window():
            continue
        top_left = widget.mapTo(page, QPoint(0, 0))
        if (
            top_left.x() < -2
            or top_left.y() < -2
            or top_left.x() + widget.width() > page.width() + 2
            or top_left.y() + widget.height() > page.height() + 2
        ):
            overflow.append(widget.objectName() or widget.metaObject().className())
    return sorted(set(overflow))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument(
        "--theme",
        choices=("cloud_blue", "coral", "dark", "light"),
        default="cloud_blue",
    )
    parser.add_argument("--scale", default="1.0")
    parser.add_argument("--expected-dpr", type=float)
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    evidence = args.evidence.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    os.environ["GUI_THEME"] = args.theme
    os.environ["QT_SCALE_FACTOR"] = args.scale
    os.environ["AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE"] = "1"

    with tempfile.TemporaryDirectory(
        prefix="amb-v171-health-gui-qa-", ignore_cleanup_errors=True
    ) as raw_root:
        root = Path(raw_root)
        workspace = root / "workspace"
        workspace.mkdir()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            str(root / "settings"),
        )
        app = QApplication.instance() or QApplication([])
        cfg = _config(root, workspace)
        service = ApplicationService(cfg)
        if not service.initialize().ok:
            raise RuntimeError("GUI QA 隔离服务初始化失败")
        _seed(service, root, workspace)
        window = BridgeWindow(service)
        window.resize(args.width, args.height)
        window.select_page("maintenance")
        health = service.get_mail_health_status()
        if not health.ok:
            raise RuntimeError("GUI QA 健康状态读取失败")
        window.mail_health_summary.setPlainText(
            format_mail_health(health.details, format_size)
        )
        scan = service.scan_consistency()
        if not scan.ok:
            raise RuntimeError("GUI QA 一致性扫描失败")
        window._render_consistency_scan(scan.details)
        window.show()
        for _ in range(12):
            app.processEvents()

        page = window.pages["maintenance"]
        horizontal_scrollbars = [
            area.objectName() or area.metaObject().className()
            for area in page.findChildren(QAbstractScrollArea)
            if area.horizontalScrollBar().isVisible()
            and area.horizontalScrollBar().maximum() > 0
        ]
        overflow = _overflowing_widgets(page)
        image = window.grab()
        if not image.save(str(output)):
            raise RuntimeError("GUI QA 截图保存失败")
        result = {
            "theme": args.theme,
            "qt_scale_factor": os.getenv("QT_SCALE_FACTOR", "system"),
            "window_size": [window.width(), window.height()],
            "image_size": [image.width(), image.height()],
            "device_pixel_ratio": image.devicePixelRatio(),
            "expected_device_pixel_ratio": args.expected_dpr,
            "horizontal_scrollbars": horizontal_scrollbars,
            "overflowing_widgets": overflow,
            "issue_count": int(health.details["issue_count"]),
            "mailbox_count": len(health.details["mailboxes"]),
            "delivery_unknown": int(health.details["send"]["delivery_unknown"]),
            "archive_recovery": int(health.details["send"]["archive_recovery"]),
            "repairable_count": int(
                scan.details["repair_preview"]["repairable_count"]
            ),
            "screenshot": output.name,
        }
        result["status"] = (
            "PASS"
            if not horizontal_scrollbars
            and not overflow
            and result["issue_count"] >= 130
            and result["mailbox_count"] >= 12
            and result["delivery_unknown"] >= 6
            and result["archive_recovery"] >= 4
            and result["repairable_count"] >= 3
            and (
                args.expected_dpr is None
                or abs(image.devicePixelRatio() - args.expected_dpr) <= 0.02
            )
            else "FAIL"
        )
        evidence.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.interactive:
            window.quitting = True
            app.exec()
        window.quitting = True
        window.close()
        for _ in range(4):
            app.processEvents()
        close_connection()
        logging.shutdown()
        del window, service, cfg
        gc.collect()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
