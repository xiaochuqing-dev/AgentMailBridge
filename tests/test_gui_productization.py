"""桌面交互与视觉产品化的高价值回归测试。"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ReceiveResult, ServiceResult
from agent_mail_bridge.ui.branding import BRAND_CANDIDATES, find_brand_asset
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font


@pytest.fixture(scope="module")
def product_qt_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def product_window(product_qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    product_qt_app.processEvents()
    yield window
    window.request_quit()
    product_qt_app.processEvents()


def _wait(window: BridgeWindow, app: QApplication) -> None:
    deadline = time.monotonic() + 2
    while window.task_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_dashboard_is_real_page_with_refresh_feedback(product_window, product_qt_app):
    product_window.select_page("dashboard")
    assert product_window.page_stack.currentWidget() is product_window.pages["dashboard"]
    product_window.request_refresh(product_window.dashboard_refresh_button)
    assert not product_window.dashboard_refresh_button.isEnabled()
    _wait(product_window, product_qt_app)


def test_receive_button_has_running_state_and_recovers(
    product_window, product_qt_app, monkeypatch
):
    monkeypatch.setattr(
        product_window.service,
        "receive",
        lambda: ReceiveResult(OperationStatus.SUCCESS, scanned=1, saved=0),
    )
    product_window.receive_button.click()
    assert product_window.task_active
    assert not product_window.receive_button.isEnabled()
    assert product_window.receive_button.property("taskState") == "running"
    _wait(product_window, product_qt_app)
    assert product_window.receive_button.isEnabled()


def test_partial_and_duplicate_use_warning_feedback(product_window):
    product_window._show_service_result(
        ServiceResult(OperationStatus.DUPLICATE, message="重复请求未执行")
    )
    assert "重复请求" in product_window.message_bar.label.text()
    assert "FFF8E8" in product_window.message_bar.styleSheet()


def test_file_action_text_matches_real_click_behavior(product_window):
    product_window._populate_files(
        product_window.files_table,
        [{"saved_filename": "result.md", "saved_path": "C:/safe/result.md"}],
        actions=True,
    )
    assert product_window.files_table.item(0, 4).text() == "复制路径"


def test_brand_asset_contract_has_stable_expected_paths():
    assert {path.name for path in BRAND_CANDIDATES} == {
        "agentmailbridge.ico",
        "agentmailbridge.png",
        "logo.png",
    }
    asset = find_brand_asset()
    assert asset is not None and asset.is_file()
    branding_dir = asset.parent
    for size in (16, 24, 32, 48, 64, 128, 256):
        image = QImage(str(branding_dir / f"agentmailbridge-{size}.png"))
        assert not image.isNull()
        assert image.width() == size and image.height() == size
    assert (branding_dir / "agentmailbridge.ico").stat().st_size > 0


def test_dark_theme_defines_neutral_text_and_card_surfaces(product_window):
    product_window.apply_theme("dark")
    stylesheet = QApplication.instance().styleSheet()
    assert "QLabel#statusValue" in stylesheet
    assert "QFrame#heroCard" in stylesheet
    assert "#242736" in stylesheet


def test_gui_fixture_never_reads_project_oauth_files(product_window, tmp_path):
    assert product_window.service.cfg.gmail_api_credentials_path.parent == tmp_path
    assert product_window.service.cfg.gmail_api_token_path.parent == tmp_path
