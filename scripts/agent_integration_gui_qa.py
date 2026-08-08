"""用隔离数据生成主要页面的 Qt DPI/主题验收截图。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path


def _early_option(name: str) -> str | None:
    try:
        index = sys.argv.index(name)
    except ValueError:
        return None
    return sys.argv[index + 1] if index + 1 < len(sys.argv) else None


_requested_scale = _early_option("--scale")
if _requested_scale and _requested_scale != "system":
    requested_dpr = float(_requested_scale)
    os.environ.pop("QT_SCALE_FACTOR", None)
    os.environ["QT_SCREEN_SCALE_FACTORS"] = (
        "1.0001" if abs(requested_dpr - 1.0) < 0.0001 else str(requested_dpr)
    )

from PySide6.QtCore import QPoint, QRect, QSettings
from PySide6.QtWidgets import QApplication, QAbstractScrollArea

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import load_interface_font


def _config(root: Path, workspace: Path) -> AppConfig:
    cfg = AppConfig(
        gmail_address="gui-qa@example.test",
        qq_email="gui-qa-send@example.test",
        owner_gmail="owner@example.test",
        data_root=root / "data",
        gmail_api_credentials_path=root / "oauth" / "credentials.json",
        gmail_api_token_path=root / "oauth" / "token.json",
    )
    cfg.allowed_send_roots = [workspace]
    cfg.mcp_mail_read_enabled = True
    return cfg


def _seed_clients(service: ApplicationService) -> None:
    service.synchronize_mail_accounts()
    accounts = service.list_mail_accounts().details["accounts"]
    account_ids = [str(item["account_id"]) for item in accounts[:1]]
    workspace_ids = [
        str(item["workspace_id"])
        for item in service.list_agent_workspaces().details["workspace_details"]
    ]
    codex = service.create_agent_client(
        client_type="codex",
        display_name="Codex",
        config_mode="managed",
        permission_mode="recommended",
        account_scope_mode="all",
        workspace_scope_mode="all",
    )
    if codex.ok:
        service.set_agent_client_state(
            str(codex.details["client"]["client_id"]),
            "active",
            enabled=True,
        )
    service.create_agent_client(
        client_type="claude_code",
        display_name="Claude Code",
        config_mode="managed",
        capabilities=["mail.search", "mail.get", "resource.read"],
        account_ids=account_ids,
        workspace_ids=workspace_ids,
    )
    hermes = service.create_agent_client(
        client_type="hermes",
        display_name="Hermes",
        config_mode="managed",
        permission_mode="recommended",
        account_scope_mode="all",
        workspace_scope_mode="all",
    )
    if hermes.ok:
        service.set_agent_client_state(
            str(hermes.details["client"]["client_id"]),
            "active",
            enabled=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--theme",
        choices=("cloud_blue", "coral", "dark", "light"),
        default="cloud_blue",
    )
    parser.add_argument("--scale", default="system")
    parser.add_argument("--expected-dpr", type=float)
    parser.add_argument(
        "--page",
        choices=(
            "inbox",
            "send",
            "agent",
            "settings",
            "files_data",
            "history",
            "pending_send",
            "advanced",
            "logs",
            "maintenance",
            "about",
        ),
        default="agent",
    )
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=960)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["GUI_THEME"] = args.theme
    os.environ["AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE"] = "1"

    with tempfile.TemporaryDirectory(prefix="amb-agent-gui-qa-") as raw_root:
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
        app.setFont(load_interface_font())
        cfg = _config(root, workspace)
        service = ApplicationService(cfg)
        if not service.initialize().ok:
            raise RuntimeError("GUI QA 隔离服务初始化失败")
        _seed_clients(service)
        window = BridgeWindow(service)
        window.resize(args.width, args.height)
        window.select_page(args.page)
        window.refresh()
        window.show()
        for _ in range(8):
            app.processEvents()

        page = window.pages[args.page]
        scroll_areas = list(page.findChildren(QAbstractScrollArea))
        if isinstance(page, QAbstractScrollArea):
            scroll_areas.append(page)
        horizontal_scrollbars = [
            area.objectName() or area.metaObject().className()
            for area in scroll_areas
            if area.horizontalScrollBar().isVisible()
            and area.horizontalScrollBar().maximum() > 0
        ]
        toolbar_controls_within_bounds = True
        toolbar_mode = "not_applicable"
        if args.page == "inbox":
            toolbar = window.receive_tools_widget
            toolbar_mode = str(toolbar.property("responsiveMode") or "")
            toolbar_controls_within_bounds = all(
                toolbar.rect().adjusted(-1, -1, 1, 1).contains(
                    QRect(control.mapTo(toolbar, QPoint(0, 0)), control.size())
                )
                for control in (
                    window.auto_switch,
                    window.interval_combo,
                    window.receive_account_combo,
                    window.inbox_test_button,
                    window.history_rescan_button,
                    window.receive_button,
                )
            )
        image = window.grab()
        if not image.save(str(output)):
            raise RuntimeError("GUI QA 截图保存失败")
        result = {
            "theme": args.theme,
            "page": args.page,
            "qt_scale_factor": args.scale,
            "window_size": [window.width(), window.height()],
            "image_size": [image.width(), image.height()],
            "device_pixel_ratio": image.devicePixelRatio(),
            "expected_device_pixel_ratio": args.expected_dpr,
            "horizontal_scrollbars": horizontal_scrollbars,
            "toolbar_mode": toolbar_mode,
            "toolbar_controls_within_bounds": toolbar_controls_within_bounds,
            "background_asset": (
                window.window_background.background_path.name
                if window.window_background.background_path is not None
                else None
            ),
            "client_rows": window.agent_client_table.rowCount(),
            "screenshot": output.name,
            "status": (
                "PASS"
                if not horizontal_scrollbars
                and toolbar_controls_within_bounds
                and window.agent_client_table.rowCount() == 3
                and (
                    args.expected_dpr is None
                    or abs(image.devicePixelRatio() - args.expected_dpr) <= 0.02
                )
                else "FAIL"
            ),
        }
        if args.evidence:
            evidence_path = args.evidence.resolve()
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        window.quitting = True
        window.close()
        app.processEvents()
        close_connection()
        logging.shutdown()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
