"""桌面交互与视觉产品化的高价值回归测试。"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
)

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ReceiveResult, ServiceResult
from agent_mail_bridge.ui.branding import (
    BRAND_CANDIDATES,
    agent_icon,
    find_brand_asset,
)
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import (
    THEME_TOKENS,
    build_stylesheet,
    load_interface_font,
    theme_background_path,
)
from agent_mail_bridge.ui.widgets import ThemeBackground


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


def test_official_agent_brand_icons_are_packaged():
    assert not agent_icon("codex").isNull()
    assert not agent_icon("claude_code").isNull()
    assert not agent_icon("claude_desktop").isNull()
    assert not agent_icon("hermes").isNull()
    assert agent_icon("custom").isNull()


def _wait(window: BridgeWindow, app: QApplication) -> None:
    deadline = time.monotonic() + 2
    while window.task_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_inbox_is_default_workspace_with_refresh_feedback(product_window, product_qt_app):
    assert product_window.page_stack.currentWidget() is product_window.pages["inbox"]
    product_window.request_refresh(product_window.inbox_refresh_button)
    assert not product_window.inbox_refresh_button.isEnabled()
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
    action_widget = product_window.files_table.cellWidget(0, 3)
    assert action_widget is not None
    assert {button.text() for button in action_widget.findChildren(QPushButton)} == {
        "打开",
        "复制路径",
    }


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
    assert "QMainWindow, QDialog" in stylesheet
    assert "QFrame#accountPanel, QFrame#credentialCard" in stylesheet
    assert "QPushButton#accountChoice:checked" in stylesheet
    assert "QPushButton#textButton, QPushButton#compactButton" in stylesheet
    assert "QScrollArea#accountListScroll, QWidget#accountList" in stylesheet
    assert "color: #C6BEFF" in stylesheet
    assert "#242736" in stylesheet


def test_blue_and_coral_theme_switch_refreshes_dynamic_controls(product_window):
    product_window.message_bar.set_message("正在执行", "working")
    product_window.apply_theme("cloud_blue")
    blue = THEME_TOKENS["cloud_blue"]["accent"]
    blue_icon_key = product_window.service_rows["core"].icon_label.pixmap().cacheKey()
    assert blue in QApplication.instance().styleSheet()
    assert blue in product_window.message_bar.styleSheet()
    assert theme_background_path("cloud_blue").is_file()

    product_window.apply_theme("coral")
    coral = THEME_TOKENS["coral"]["accent"]
    coral_icon_key = product_window.service_rows["core"].icon_label.pixmap().cacheKey()
    assert coral in QApplication.instance().styleSheet()
    assert coral in product_window.message_bar.styleSheet()
    assert theme_background_path("coral").is_file()
    assert coral_icon_key != blue_icon_key
    assert product_window.theme_value_label.text() == "珊瑚霞"
    assert product_window.window_background.theme == "coral"
    assert product_window.window_background.background_path == theme_background_path("coral")

    product_window.apply_theme("cloud_blue")


def test_light_themes_do_not_keep_legacy_purple_component_colors():
    legacy_component_colors = {
        "#4A3E87",
        "#F4F1FF",
        "#D8D0FF",
        "#FBFAFF",
        "#E4DFFF",
        "#F5F2FF",
        "#EEE9FF",
        "#BDB4F8",
        "#FAF9FF",
        "#EEEAFD",
    }
    for theme in ("cloud_blue", "coral"):
        stylesheet = build_stylesheet(theme)
        assert not legacy_component_colors.intersection(stylesheet)
        assert "background-image" not in stylesheet
        assert "QScrollArea QWidget#qt_scrollarea_viewport" in stylesheet
        assert "rgba(" in stylesheet


@pytest.mark.parametrize(
    ("theme", "expected_surface"),
    (("cloud_blue", "#EDF7FF"), ("coral", "#FFF2F4")),
)
def test_light_theme_dialog_surface_is_not_system_gray(
    product_qt_app, theme, expected_surface
):
    previous = product_qt_app.styleSheet()
    dialog = QDialog()
    try:
        product_qt_app.setStyleSheet(build_stylesheet(theme))
        dialog.resize(260, 160)
        dialog.show()
        product_qt_app.processEvents()
        actual = dialog.grab().toImage().pixelColor(4, 4)
        assert actual == QColor(expected_surface)
    finally:
        dialog.close()
        product_qt_app.setStyleSheet(previous)


@pytest.mark.parametrize("theme", ("cloud_blue", "coral"))
def test_theme_background_paints_visible_edge_to_edge_image(product_qt_app, theme):
    background = ThemeBackground(theme)
    background.resize(640, 360)
    background.show()
    product_qt_app.processEvents()
    image = background.grab().toImage()
    background.close()

    assert background.background_path == theme_background_path(theme)
    assert image.width() >= 640 and image.height() >= 360
    colors = []
    for x_ratio, y_ratio in (
        (0.05, 0.05), (0.5, 0.05), (0.95, 0.05),
        (0.05, 0.5), (0.5, 0.5), (0.95, 0.5),
        (0.05, 0.95), (0.5, 0.95), (0.95, 0.95),
    ):
        color = image.pixelColor(
            min(image.width() - 1, int(image.width() * x_ratio)),
            min(image.height() - 1, int(image.height() * y_ratio)),
        )
        colors.append((color.red(), color.green(), color.blue()))
    luminance = [sum(color) for color in colors]
    assert max(luminance) - min(luminance) >= 20
    assert min(luminance) < 720


def test_compact_agent_overview_rows_do_not_expand_or_overlap(
    product_window, product_qt_app
):
    product_window._refresh_agent_overview([
        {"client_type": "codex", "display_name": "Codex", "enabled": True},
        {
            "client_type": "claude_code",
            "display_name": "Claude Code",
            "enabled": True,
        },
        {"client_type": "hermes", "display_name": "Hermes", "enabled": True},
    ])
    product_qt_app.processEvents()

    assert len(product_window.agent_overview_rows) == 3
    for row in product_window.agent_overview_rows:
        assert row.height() == 46
        tag = next(label for label in row.findChildren(QLabel) if label.objectName() == "tag")
        manage = row.findChild(QPushButton)
        assert manage is not None
        assert tag.geometry().right() < manage.geometry().left()
        assert tag.geometry().bottom() < row.height()

    claude_row = next(
        row
        for row in product_window.agent_overview_rows
        if row.property("clientType") == "claude_code"
    )
    claude_icon = next(
        label
        for label in claude_row.findChildren(QLabel)
        if label.property("lineIconKind")
    )
    assert claude_icon.property("lineIconKind") == "terminal"
    assert all("QA" not in label.text() for label in claude_row.findChildren(QLabel))


def test_gui_fixture_never_reads_project_oauth_files(product_window, tmp_path):
    assert product_window.service.cfg.gmail_api_credentials_path.parent == tmp_path
    assert product_window.service.cfg.gmail_api_token_path.parent == tmp_path


def test_managed_client_apply_button_accepts_and_starts_task(
    product_window, product_qt_app, monkeypatch
):
    preview = ServiceResult(
        OperationStatus.SUCCESS,
        details={
            "target_path": ".codex/config.toml",
            "preview": '{"token": "<redacted>"}',
            "plan_id": "plan_test",
        },
    )
    monkeypatch.setattr(
        product_window.service,
        "preview_agent_client_config",
        lambda _client_id: preview,
    )
    started: dict[str, object] = {}

    def capture_task(title, task, finished, *, refresh_on_finish=True):
        started.update(
            title=title,
            task=task,
            finished=finished,
            refresh_on_finish=refresh_on_finish,
        )

    monkeypatch.setattr(product_window, "_run_task", capture_task)

    def click_apply(dialog):
        button_box = dialog.findChild(QDialogButtonBox)
        assert button_box is not None
        apply_button = next(
            button
            for button in button_box.findChildren(QPushButton)
            if button.text() == "备份并应用"
        )
        apply_button.click()
        product_qt_app.processEvents()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", click_apply)
    product_window._configure_agent_client(
        {
            "client_id": "client_test",
            "client_type": "codex",
            "installed": True,
            "install_status": "managed_supported",
        }
    )

    assert started["refresh_on_finish"] is False
    assert callable(started["task"])
