"""用隔离数据生成 Agent 接入页的 Qt DPI/主题验收截图。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QAbstractScrollArea

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection
from agent_mail_bridge.ui.main_window import BridgeWindow


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
        display_name="Codex 项目工作区",
        config_mode="managed",
        capabilities=[
            "mail.search",
            "mail.get",
            "resource.read",
            "resource.prepare",
        ],
        account_ids=account_ids,
        workspace_ids=workspace_ids,
    )
    if codex.ok:
        service.set_agent_client_state(
            str(codex.details["client"]["client_id"]),
            "active",
            enabled=True,
        )
    service.create_agent_client(
        client_type="claude_code",
        display_name="Claude Code 只读",
        config_mode="managed",
        capabilities=["mail.search", "mail.get", "resource.read"],
        account_ids=account_ids,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
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
        cfg = _config(root, workspace)
        service = ApplicationService(cfg)
        if not service.initialize().ok:
            raise RuntimeError("GUI QA 隔离服务初始化失败")
        _seed_clients(service)
        window = BridgeWindow(service)
        window.resize(1400, 960)
        window.select_page("agent")
        window.refresh()
        window.show()
        for _ in range(8):
            app.processEvents()

        page = window.pages["agent"]
        horizontal_scrollbars = [
            area.objectName() or area.metaObject().className()
            for area in page.findChildren(QAbstractScrollArea)
            if area.horizontalScrollBar().isVisible()
            and area.horizontalScrollBar().maximum() > 0
        ]
        image = window.grab()
        if not image.save(str(output)):
            raise RuntimeError("GUI QA 截图保存失败")
        result = {
            "theme": args.theme,
            "qt_scale_factor": os.getenv("QT_SCALE_FACTOR", "system"),
            "window_size": [window.width(), window.height()],
            "image_size": [image.width(), image.height()],
            "device_pixel_ratio": image.devicePixelRatio(),
            "horizontal_scrollbars": horizontal_scrollbars,
            "client_rows": window.agent_client_table.rowCount(),
            "screenshot": output.name,
            "status": (
                "PASS"
                if not horizontal_scrollbars
                and window.agent_client_table.rowCount() == 2
                else "FAIL"
            ),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        window.quitting = True
        window.close()
        app.processEvents()
        close_connection()
        logging.shutdown()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
