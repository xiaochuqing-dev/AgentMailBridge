"""v1.4.1 多账号 GUI 入口与账号选择回归。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.ui import account_management
from agent_mail_bridge.ui.account_management import (
    AccountTypeDialog,
    open_account_dialog,
)
from agent_mail_bridge.ui.main_window import BridgeWindow


@pytest.fixture(scope="module")
def runtime_qt_app():
    return QApplication.instance() or QApplication([])


def test_add_account_dialog_creates_second_gmail(runtime_qt_app, tmp_cfg):
    service = ApplicationService(tmp_cfg)
    dialog = AccountTypeDialog(service)
    dialog.email_edit.setText("second-ui@gmail.com")
    dialog.display_name_edit.setText("第二个 Gmail")
    dialog.backend_combo.setCurrentIndex(
        dialog.backend_combo.findData("imap")
    )
    dialog.secret_edit.setText("ui-test-secret")
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_account_id.startswith("acct_")
    accounts = service.list_mail_accounts().details["accounts"]
    assert any(
        item["email_address"] == "second-ui@gmail.com"
        for item in accounts
    )


def test_legacy_account_card_uses_account_scoped_runtime_dialog(
    runtime_qt_app, tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    seen: list[str] = []

    class FakeRuntimeDialog:
        def __init__(self, _service, account_id, _parent):
            seen.append(account_id)

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(
        account_management, "RuntimeAccountDialog", FakeRuntimeDialog
    )
    assert open_account_dialog(service, AccountTypeDialog.GMAIL)
    assert len(seen) == 1


def test_main_window_lists_runtime_accounts_and_sender_receiver_choices(
    runtime_qt_app, tmp_cfg
):
    service = ApplicationService(tmp_cfg)
    assert service.create_mail_account(
        provider="gmail",
        email_address="runtime-ui@gmail.com",
        receive_backend="imap",
        secret="runtime-secret",
    ).ok
    assert service.create_mail_account(
        provider="qq",
        email_address="runtime-ui@qq.com",
        secret="runtime-qq-secret",
    ).ok
    window = BridgeWindow(service)
    window.show()
    runtime_qt_app.processEvents()
    window.refresh()
    runtime_qt_app.processEvents()

    dynamic_addresses = {
        card.email_label.text()
        for card in window._dynamic_account_cards
    }
    receiver_addresses = {
        window.receive_account_combo.itemText(index)
        for index in range(window.receive_account_combo.count())
    }
    sender_addresses = {
        window.send_account_combo.itemText(index)
        for index in range(window.send_account_combo.count())
    }
    assert "runtime-ui@gmail.com" in dynamic_addresses
    assert "runtime-ui@qq.com" in dynamic_addresses
    assert any("runtime-ui@gmail.com" in text for text in receiver_addresses)
    assert any("runtime-ui@qq.com" in text for text in sender_addresses)
    window.request_quit()
