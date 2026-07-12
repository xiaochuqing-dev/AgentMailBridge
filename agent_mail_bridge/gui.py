"""AgentMailBridge 正式 PySide6 桌面界面入口。"""

from __future__ import annotations

import sys
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.desktop_runtime import SingleInstanceGuard
from agent_mail_bridge.ui.setup_wizard import SetupWizard, needs_setup
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font
from agent_mail_bridge.version import __version__

__all__ = ["BridgeWindow", "main"]


def main() -> None:
    headless_exit = _run_headless_mode()
    if headless_exit is not None:
        raise SystemExit(headless_exit)
    # Qt 6 原生处理高 DPI；保留 125%/150% 精确比例，避免额外环境缩放叠加。
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Agent 邮箱桥接工具")
    app.setOrganizationName("AgentMailBridge")
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
        raise SystemExit(0)
    window = BridgeWindow(service)
    window.instance_guard = guard
    guard.set_activation_handler(window.show_from_tray)
    if "--background" in sys.argv and window.tray_available:
        window.hide()
    else:
        window.show()
    exit_code = app.exec()
    guard.release()
    raise SystemExit(exit_code)


def _run_headless_mode() -> int | None:
    """供安装包 smoke、Credential Manager 与在线诊断验收使用。"""
    if "--version" in sys.argv:
        print(__version__)
        return 0
    if "--packaged-self-test" in sys.argv:
        from agent_mail_bridge.credentials import WindowsCredentialBackend
        from agent_mail_bridge.runtime_paths import get_runtime_paths
        from agent_mail_bridge.ui.branding import find_brand_asset

        paths = get_runtime_paths()
        if not paths.frozen or paths.install_root in paths.user_root.parents:
            return 2
        if find_brand_asset() is None:
            return 3
        name = f"packaged_self_test_{uuid.uuid4().hex}"
        value = uuid.uuid4().hex
        backend = WindowsCredentialBackend()
        try:
            backend.write(name, value)
            if backend.read(name) != value:
                return 4
        finally:
            backend.delete(name)
        return 0
    if "--diagnose-gmail-api" in sys.argv:
        return 0 if ApplicationService(load_config()).diagnose_gmail_api().ok else 1
    if "--diagnose-qq-smtp" in sys.argv:
        return 0 if ApplicationService(load_config()).diagnose_qq_smtp().ok else 1
    return None


if __name__ == "__main__":
    main()
