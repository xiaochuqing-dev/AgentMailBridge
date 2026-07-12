"""阶段 5.5 GUI 安全发送、反馈和脱敏诊断回归测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, SendResult, ServiceResult
from agent_mail_bridge.ui.main_window import BridgeWindow
from agent_mail_bridge.ui.theme import build_stylesheet, load_interface_font


@pytest.fixture(scope="module")
def stage_qt_app():
    app = QApplication.instance() or QApplication([])
    app.setFont(load_interface_font())
    app.setStyleSheet(build_stylesheet())
    return app


@pytest.fixture()
def stage_window(stage_qt_app, tmp_cfg):
    window = BridgeWindow(ApplicationService(tmp_cfg))
    window.show()
    stage_qt_app.processEvents()
    yield window
    window.request_quit()
    stage_qt_app.processEvents()


def _wait_for_task(window: BridgeWindow, app: QApplication) -> None:
    deadline = time.monotonic() + 2  # GUI 测试最多等待 2 秒。
    while window.task_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_send_starts_disabled_and_selection_shows_metadata(
    stage_window, monkeypatch, tmp_path: Path
):
    source = stage_window.service.cfg.data_root_path / "大数据资料.md"
    source.write_text("测试内容", encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )

    assert not stage_window.send_action_button.isEnabled()
    assert stage_window.choose_send_file()
    assert stage_window.send_action_button.isEnabled()
    assert stage_window.send_file_name_value.text() == "大数据资料.md"
    assert stage_window.send_file_size_value.text() != "—"
    assert stage_window.send_file_modified_value.text() != "—"
    assert stage_window.send_path_edit.toolTip() == str(source.resolve())


def test_modified_file_is_blocked_and_selection_is_cleared(
    stage_window, monkeypatch
):
    source = stage_window.service.cfg.data_root_path / "will-change.txt"
    source.write_text("第一次内容", encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )
    assert stage_window.choose_send_file()
    source.write_text("已被修改且长度不同", encoding="utf-8")

    stage_window.send_selected_file()

    assert stage_window.send_selection is None
    assert not stage_window.send_action_button.isEnabled()
    assert "重新选择" in stage_window.message_bar.label.text()


def test_send_requires_confirmation_and_clears_after_success(
    stage_window, stage_qt_app, monkeypatch
):
    source = stage_window.service.cfg.data_root_path / "confirmed.md"
    source.write_text("确认发送", encoding="utf-8")
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )
    assert stage_window.choose_send_file()

    calls: list[Path] = []

    def fake_send(path: Path, *, subject=None, expected_sha256=None):
        assert expected_sha256
        calls.append(path)
        return SendResult(
            OperationStatus.SUCCESS,
            send_status="sent",
            source_path=str(path),
            subject=subject or "",
            message="发送完成",
        )

    monkeypatch.setattr(stage_window.service, "send_user_selected_file", fake_send)
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )
    stage_window.send_selected_file()
    assert calls == []
    assert stage_window.send_selection is not None

    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    stage_window.send_selected_file()
    assert not stage_window.send_action_button.isEnabled()
    assert not stage_window.send_progress.isHidden()
    _wait_for_task(stage_window, stage_qt_app)

    assert calls == [source.resolve()]
    assert stage_window.send_selection is None
    assert not stage_window.send_action_button.isEnabled()
    assert not stage_window.send_progress.isVisible()
    assert "已清空本次选择" in stage_window.send_status_label.text()


def test_dangerous_file_preview_only_reveals_location(
    stage_window, monkeypatch
):
    source = stage_window.service.cfg.data_root_path / "unsafe.ps1"
    source.write_text("Write-Host test", encoding="utf-8")
    revealed: list[Path] = []
    monkeypatch.setattr(stage_window, "_reveal_file", revealed.append)

    stage_window._preview_path(str(source))

    assert revealed == [source]
    assert "资源管理器定位" in stage_window.message_bar.label.text()


def test_friendly_error_mapping_and_details_are_redacted(stage_window):
    raw = (
        f"认证失败 {stage_window.service.cfg.qq_auth_code} "
        f"{stage_window.service.cfg.gmail_address}"
    )
    result = ServiceResult(
        OperationStatus.FAILED,
        error_code="qq_smtp_diagnose_failed",
        message=raw,
    )

    stage_window._show_service_result(result)

    assert "QQ SMTP 连接失败" in stage_window.message_bar.label.text()
    assert stage_window.service.cfg.qq_auth_code not in stage_window.last_error_details
    assert stage_window.service.cfg.gmail_address not in stage_window.last_error_details
    assert stage_window.error_details_button.isEnabled()


def test_diagnostic_report_excludes_secrets_and_private_paths(tmp_cfg, tmp_path: Path):
    service = ApplicationService(tmp_cfg)
    destination = tmp_path / "diagnostic.md"

    result = service.export_diagnostic_report(destination)

    assert result.ok
    content = destination.read_text(encoding="utf-8")
    assert tmp_cfg.gmail_app_password not in content
    assert tmp_cfg.qq_auth_code not in content
    assert tmp_cfg.gmail_address not in content
    assert tmp_cfg.qq_email not in content
    assert str(tmp_cfg.data_root_path) not in content
    assert "私人绝对路径" in content


def test_diagnostic_report_does_not_overwrite_existing_file(tmp_cfg, tmp_path: Path):
    service = ApplicationService(tmp_cfg)
    destination = tmp_path / "existing.md"
    destination.write_text("保留内容", encoding="utf-8")

    result = service.export_diagnostic_report(destination)

    assert result.status == OperationStatus.FAILED
    assert result.error_code == "report_exists"
    assert destination.read_text(encoding="utf-8") == "保留内容"


def test_manual_refresh_runs_in_background_and_updates_time(
    stage_window, stage_qt_app, monkeypatch
):
    original_collect = stage_window._collect_refresh_result

    def delayed_collect():
        time.sleep(0.05)  # 模拟较慢的本地数据库刷新，单位：秒。
        return original_collect()

    monkeypatch.setattr(stage_window, "_collect_refresh_result", delayed_collect)

    started_at = time.monotonic()
    stage_window.request_refresh(stage_window.logs_refresh_button)

    assert time.monotonic() - started_at < 0.04
    assert stage_window.task_active
    assert not stage_window.logs_refresh_button.isEnabled()
    assert stage_window.all_diagnose_button.isEnabled()
    _wait_for_task(stage_window, stage_qt_app)
    assert stage_window.last_refresh_at is not None
    assert "最后刷新" in stage_window.logs_refresh_label.text()


def test_config_change_marks_unsaved_and_successful_save_clears_state(
    stage_window, monkeypatch
):
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.save_env_values",
        lambda values: saved.update(values),
    )
    stage_window._set_combo_data(stage_window.network_combo, "direct")
    assert stage_window._config_dirty
    assert "未保存" in stage_window.unsaved_config_label.text()

    stage_window.save_advanced_config()

    assert not stage_window._config_dirty
    assert saved["GMAIL_NETWORK_MODE"] == "direct"


def test_advanced_save_rolls_back_startup_when_env_write_fails(
    stage_window, monkeypatch
):
    startup_changes: list[bool] = []
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.StartupManager.is_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.StartupManager.set_enabled",
        startup_changes.append,
    )

    def fail_save(_values):
        raise OSError("模拟只读配置")

    monkeypatch.setattr(
        "agent_mail_bridge.ui.main_window.save_env_values",
        fail_save,
    )
    stage_window.startup_check.setChecked(True)
    stage_window.save_advanced_config()

    assert startup_changes == [True, False]
    assert "已回滚" in stage_window.message_bar.label.text()
