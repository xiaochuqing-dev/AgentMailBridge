"""AgentMailBridge 正式 PySide6 桌面界面入口。"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.desktop_runtime import SingleInstanceGuard
from agent_mail_bridge.ui.setup_wizard import SetupWizard, needs_setup
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font

__all__ = ["BridgeWindow", "main"]


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Agent 邮箱桥接工具")
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    cfg = load_config()
    if needs_setup(cfg):
        wizard = SetupWizard(cfg)
        if wizard.exec() == 0:
            raise SystemExit(0)
        cfg = load_config()
    service = ApplicationService(cfg)
    service.initialize()
    guard = SingleInstanceGuard(service.cfg.data_root_path)
    if not guard.acquire():
        QMessageBox.information(None, "Agent 邮箱桥接工具", "程序已在运行，请从系统托盘打开主窗口。")
        raise SystemExit(0)
    window = BridgeWindow(service)
    window.instance_guard = guard
    if "--background" in sys.argv and window.tray_available:
        window.hide()
    else:
        window.show()
    exit_code = app.exec()
    guard.release()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
