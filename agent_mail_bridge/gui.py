"""AgentMailBridge 正式 PySide6 桌面界面入口。"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font

__all__ = ["BridgeWindow", "main"]


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Agent 邮箱桥接工具")
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    service = ApplicationService(load_config())
    service.initialize()
    window = BridgeWindow(service)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
