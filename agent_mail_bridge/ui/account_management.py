"""邮箱账号新增与编辑界面，以及首次向导复用的账号保存逻辑。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.credentials import GMAIL_IMAP_SECRET, QQ_SMTP_SECRET
from agent_mail_bridge.models import OperationStatus, ServiceResult
from agent_mail_bridge.ui.settings_store import save_env_values
from agent_mail_bridge.ui.theme import DANGER, PURPLE, SUCCESS, WARNING


FIXED_SECRET_MASK = "•" * 16


def _valid_email(value: str) -> bool:
    local, separator, domain = value.partition("@")
    return bool(local and separator and "." in domain and not value.endswith("."))


class AccountSettingsController:
    """主窗口和首次向导共用的账号校验、凭据与配置回滚逻辑。"""

    def __init__(self, service: ApplicationService):
        self.service = service

    def save_gmail(
        self,
        email: str,
        backend: str,
        new_secret: str = "",
    ) -> ServiceResult:
        email = email.strip()
        if not _valid_email(email):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_gmail_address",
                message="请输入有效的 Gmail 地址",
            )
        if backend not in {"gmail_api", "imap"}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_receive_backend",
                message="请选择 Gmail API 或 Gmail IMAP",
            )
        previous_secret = self.service.cfg.gmail_app_password
        if backend == "imap" and not (new_secret.strip() or previous_secret):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="imap_secret_required",
                message="请配置 Gmail IMAP 应用专用密码（Google 生成）",
            )
        if new_secret.strip():
            saved = self.service.set_credential(GMAIL_IMAP_SECRET, new_secret)
            if not saved.ok:
                return saved
        try:
            save_env_values(
                {
                    "GMAIL_ADDRESS": email,
                    "OWNER_GMAIL": email,
                    "GMAIL_APP_PASSWORD": "",
                    "GMAIL_RECEIVE_BACKEND": backend,
                }
            )
        except OSError as exc:
            if new_secret.strip():
                if previous_secret:
                    self.service.set_credential(GMAIL_IMAP_SECRET, previous_secret)
                else:
                    self.service.delete_credential(GMAIL_IMAP_SECRET)
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="gmail_config_write_failed",
                message=f"保存 Gmail 账号失败：{exc}",
            )
        self.service.cfg.gmail_address = email
        self.service.cfg.owner_gmail = email
        self.service.cfg.gmail_receive_backend = backend
        return ServiceResult(OperationStatus.SUCCESS, message="Gmail 收件账号已保存")

    def save_qq(self, email: str, new_secret: str = "") -> ServiceResult:
        email = email.strip()
        if not _valid_email(email) or not email.lower().endswith("@qq.com"):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_qq_address",
                message="请输入有效的 QQ 邮箱地址",
            )
        previous_secret = self.service.cfg.qq_auth_code
        if not (new_secret.strip() or previous_secret):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="qq_secret_required",
                message="请配置 QQ SMTP 授权码（QQ 邮箱生成）",
            )
        if new_secret.strip():
            saved = self.service.set_credential(QQ_SMTP_SECRET, new_secret)
            if not saved.ok:
                return saved
        try:
            save_env_values({"QQ_EMAIL": email, "QQ_AUTH_CODE": ""})
        except OSError as exc:
            if new_secret.strip():
                if previous_secret:
                    self.service.set_credential(QQ_SMTP_SECRET, previous_secret)
                else:
                    self.service.delete_credential(QQ_SMTP_SECRET)
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="qq_config_write_failed",
                message=f"保存 QQ 发件账号失败：{exc}",
            )
        self.service.cfg.qq_email = email
        return ServiceResult(OperationStatus.SUCCESS, message="QQ 发件账号已保存")


class CredentialEditor(QFrame):
    """只展示固定掩码和配置状态，旧 secret 永不写入输入框。"""

    def __init__(self, title: str, explanation: str, configured: bool):
        super().__init__()
        self.setObjectName("credentialCard")
        self._configured = configured
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        heading = QLabel(title)
        heading.setObjectName("fieldLabel")
        layout.addWidget(heading)
        explanation_label = QLabel(explanation)
        explanation_label.setObjectName("hint")
        explanation_label.setWordWrap(True)
        layout.addWidget(explanation_label)

        self.stack = QStackedWidget()
        current = QWidget()
        current_layout = QHBoxLayout(current)
        current_layout.setContentsMargins(0, 0, 0, 0)
        self.mask_label = QLabel(FIXED_SECRET_MASK if configured else "○ 未配置")
        self.mask_label.setObjectName("credentialMask")
        self.status_label = QLabel("✓ 已配置" if configured else "○ 未配置")
        self.status_label.setObjectName("successText" if configured else "hint")
        self.modify_button = QPushButton("修改" if configured else "配置")
        self.modify_button.setObjectName("outlinePurple")
        self.modify_button.clicked.connect(self.begin_edit)
        current_layout.addWidget(self.mask_label)
        current_layout.addWidget(self.status_label)
        current_layout.addStretch(1)
        current_layout.addWidget(self.modify_button)

        edit = QWidget()
        edit_layout = QHBoxLayout(edit)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText(f"输入新的{title}")
        cancel = QPushButton("取消")
        cancel.setObjectName("textButton")
        cancel.clicked.connect(self.cancel_edit)
        edit_layout.addWidget(self.secret_edit, 1)
        edit_layout.addWidget(cancel)

        self.stack.addWidget(current)
        self.stack.addWidget(edit)
        layout.addWidget(self.stack)
        self.stack.setCurrentIndex(0 if configured else 1)

    @property
    def configured(self) -> bool:
        return self._configured

    def begin_edit(self) -> None:
        self.secret_edit.clear()
        self.stack.setCurrentIndex(1)
        self.secret_edit.setFocus()

    def cancel_edit(self) -> None:
        self.secret_edit.clear()
        self.stack.setCurrentIndex(0 if self._configured else 1)

    def pending_secret(self) -> str:
        return self.secret_edit.text() if self.stack.currentIndex() == 1 else ""

    def set_configured(self, configured: bool) -> None:
        self._configured = configured
        self.secret_edit.clear()
        self.mask_label.setText(FIXED_SECRET_MASK if configured else "○ 未配置")
        self.status_label.setText("✓ 已配置" if configured else "○ 未配置")
        self.status_label.setObjectName("successText" if configured else "hint")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.modify_button.setText("修改" if configured else "配置")
        self.stack.setCurrentIndex(0 if configured else 1)


class _AccountDialog(QDialog):
    def __init__(self, service: ApplicationService, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.controller = AccountSettingsController(service)
        self.setModal(True)
        self.setMinimumSize(680, 590)
        self.resize(760, 680)

    def _status_label(self) -> QLabel:
        label = QLabel("准备就绪")
        label.setObjectName("hint")
        label.setWordWrap(True)
        return label

    def _show_result(self, result: ServiceResult) -> None:
        self.result_label.setText(result.message or result.status.value)
        self.result_label.setStyleSheet(
            f"color: {SUCCESS if result.ok else WARNING if result.status == OperationStatus.CANCELLED else DANGER};"
        )

    def _running(self, text: str) -> None:
        self.result_label.setText(text)
        self.result_label.setStyleSheet(f"color: {PURPLE};")
        QApplication.processEvents()


class GmailAccountDialog(_AccountDialog):
    """Gmail API 与 Gmail IMAP 条件互斥的专属账号配置页。"""

    def __init__(self, service: ApplicationService, parent: QWidget | None = None):
        super().__init__(service, parent)
        self.setWindowTitle("Gmail 收件账号")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        title = QLabel("Gmail 收件账号")
        title.setObjectName("pageTitle")
        subtitle = QLabel("选择一种清晰的连接方式；切换不会删除另一种方式已有的凭据或 OAuth 文件。")
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        email_label = QLabel("Gmail 地址")
        email_label.setObjectName("fieldLabel")
        self.email_edit = QLineEdit(service.cfg.gmail_address)
        self.email_edit.setPlaceholderText("name@gmail.com")
        root.addWidget(email_label)
        root.addWidget(self.email_edit)

        mode_label = QLabel("连接方式")
        mode_label.setObjectName("fieldLabel")
        root.addWidget(mode_label)
        mode_row = QHBoxLayout()
        self.api_mode_button = QPushButton("Gmail API（OAuth 授权）")
        self.imap_mode_button = QPushButton("Gmail IMAP（配置简单）")
        for button in (self.api_mode_button, self.imap_mode_button):
            button.setCheckable(True)
            button.setMinimumHeight(64)
            button.setObjectName("accountChoice")
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.api_mode_button, 0)
        group.addButton(self.imap_mode_button, 1)
        mode_row.addWidget(self.api_mode_button, 1)
        mode_row.addWidget(self.imap_mode_button, 1)
        root.addLayout(mode_row)

        self.gmail_stack = QStackedWidget()
        self.api_page = self._build_api_page()
        self.imap_page = self._build_imap_page()
        self.gmail_stack.addWidget(self.api_page)
        self.gmail_stack.addWidget(self.imap_page)
        root.addWidget(self.gmail_stack, 1)
        self.api_mode_button.clicked.connect(lambda: self._select_backend("gmail_api"))
        self.imap_mode_button.clicked.connect(lambda: self._select_backend("imap"))

        self.result_label = self._status_label()
        root.addWidget(self.result_label)
        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存 / 完成")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        root.addLayout(actions)

        initial = service.cfg.gmail_receive_backend
        self._select_backend("imap" if initial == "imap" else "gmail_api")
        self.refresh_status()

    def _build_api_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("accountPanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel("Gmail API 专属配置")
        heading.setObjectName("sectionTitle")
        note = QLabel("选择 credentials.json 后，程序会验证并自动导入受控用户目录；无需手动寻找 OAuth 文件夹。")
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(note)
        self.oauth_client_status = QLabel("○ 未导入")
        self.oauth_auth_status = QLabel("○ 未授权")
        self.api_connection_status = QLabel("○ 尚未测试")
        layout.addWidget(self.oauth_client_status)
        layout.addWidget(self.oauth_auth_status)
        layout.addWidget(self.api_connection_status)
        buttons = QHBoxLayout()
        self.import_button = QPushButton("选择 / 导入 credentials.json")
        self.import_button.clicked.connect(self.import_oauth_json)
        self.authorize_button = QPushButton("开始 Gmail OAuth 授权")
        self.authorize_button.clicked.connect(self.authorize)
        self.api_test_button = QPushButton("测试 Gmail API 连接")
        self.api_test_button.clicked.connect(self.test_api)
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.authorize_button)
        buttons.addWidget(self.api_test_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _build_imap_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("accountPanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel("Gmail IMAP 专属配置")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.imap_credential = CredentialEditor(
            "Gmail IMAP 应用专用密码（Google 生成）",
            "这是由 Google 生成、专门用于 Gmail IMAP 登录的应用专用密码，不是 AgentMailBridge 密码，也不是 Gmail 主密码。",
            bool(self.service.cfg.gmail_app_password),
        )
        layout.addWidget(self.imap_credential)
        actions = QHBoxLayout()
        self.delete_imap_button = QPushButton("删除凭据")
        self.delete_imap_button.setObjectName("textButton")
        self.delete_imap_button.clicked.connect(self.delete_imap_credential)
        self.imap_test_button = QPushButton("测试 IMAP 连接")
        self.imap_test_button.clicked.connect(self.test_imap)
        actions.addWidget(self.delete_imap_button)
        actions.addStretch(1)
        actions.addWidget(self.imap_test_button)
        layout.addLayout(actions)
        layout.addStretch(1)
        return page

    @property
    def selected_backend(self) -> str:
        return "gmail_api" if self.gmail_stack.currentWidget() is self.api_page else "imap"

    def _select_backend(self, backend: str) -> None:
        api = backend == "gmail_api"
        self.api_mode_button.setChecked(api)
        self.imap_mode_button.setChecked(not api)
        self.gmail_stack.setCurrentWidget(self.api_page if api else self.imap_page)

    def refresh_status(self) -> None:
        credentials = self.service.cfg.gmail_api_credentials_path.exists()
        oauth = self.service.get_oauth_status().details
        state = str(oauth.get("state", "NOT_CONFIGURED"))
        authorized = state in {"READY", "TOKEN_EXPIRED_REFRESHABLE"}
        self.oauth_client_status.setText("✓ OAuth 客户端配置：已导入" if credentials else "○ OAuth 客户端配置：未导入")
        self.oauth_client_status.setStyleSheet(f"color: {SUCCESS if credentials else WARNING};")
        self.oauth_auth_status.setText("✓ Gmail 授权：已授权" if authorized else "○ Gmail 授权：未授权")
        self.oauth_auth_status.setStyleSheet(f"color: {SUCCESS if authorized else WARNING};")
        self.import_button.setText("替换 credentials.json" if credentials else "选择 / 导入 credentials.json")
        self.delete_imap_button.setEnabled(self.imap_credential.configured)

    def import_oauth_json(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self, "选择 Gmail OAuth 客户端配置", "", "JSON 文件 (*.json)"
        )
        if not source:
            self._show_result(ServiceResult(OperationStatus.CANCELLED, message="已取消导入"))
            return
        replace = False
        if self.service.cfg.gmail_api_credentials_path.exists():
            answer = QMessageBox.question(
                self,
                "替换 OAuth 客户端配置",
                "确认替换已导入的客户端配置吗？现有 token 不会被删除；如不兼容，请重新授权。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._show_result(ServiceResult(OperationStatus.CANCELLED, message="已取消替换"))
                return
            replace = True
        self._running("正在验证并导入 OAuth 客户端配置…")
        result = self.service.import_oauth_credentials(Path(source), replace=replace)
        self.refresh_status()
        if result.ok and replace:
            result.message += "；现有 token 已保留，如连接失败请重新授权"
        self._show_result(result)

    def _save_current(self) -> ServiceResult:
        secret = self.imap_credential.pending_secret() if self.selected_backend == "imap" else ""
        result = self.controller.save_gmail(self.email_edit.text(), self.selected_backend, secret)
        if result.ok and secret:
            self.imap_credential.set_configured(True)
        return result

    def authorize(self) -> None:
        saved = self.controller.save_gmail(self.email_edit.text(), "gmail_api")
        if not saved.ok:
            self._show_result(saved)
            return
        self._running("正在打开浏览器进行 Gmail OAuth 授权…")
        result = self.service.authorize_gmail_api()
        self.refresh_status()
        self._show_result(result)

    def test_api(self) -> None:
        saved = self.controller.save_gmail(self.email_edit.text(), "gmail_api")
        if not saved.ok:
            self._show_result(saved)
            return
        self._running("正在测试 Gmail API 连接…")
        result = self.service.diagnose_gmail_api()
        self.api_connection_status.setText("✓ 连接：正常" if result.ok else "✕ 连接：失败")
        self.api_connection_status.setStyleSheet(f"color: {SUCCESS if result.ok else DANGER};")
        self._show_result(result)

    def test_imap(self) -> None:
        result = self.controller.save_gmail(
            self.email_edit.text(), "imap", self.imap_credential.pending_secret()
        )
        if not result.ok:
            self._show_result(result)
            return
        self.imap_credential.set_configured(True)
        self._running("正在测试 Gmail IMAP 连接…")
        self._show_result(self.service.diagnose_imap())

    def delete_imap_credential(self) -> None:
        answer = QMessageBox.question(
            self,
            "删除 Gmail IMAP 凭据",
            "删除后 Gmail IMAP 将无法连接，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._show_result(ServiceResult(OperationStatus.CANCELLED, message="已取消删除"))
            return
        result = self.service.delete_credential(GMAIL_IMAP_SECRET)
        if result.ok:
            self.imap_credential.set_configured(False)
        self.refresh_status()
        self._show_result(result)

    def accept(self) -> None:
        result = self._save_current()
        self._show_result(result)
        if result.ok:
            super().accept()


class QQAccountDialog(_AccountDialog):
    """QQ SMTP 发件账号的唯一配置页面。"""

    def __init__(self, service: ApplicationService, parent: QWidget | None = None):
        super().__init__(service, parent)
        self.setWindowTitle("QQ 发件账号")
        self.setMinimumSize(640, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("QQ 发件账号")
        title.setObjectName("pageTitle")
        subtitle = QLabel("发件身份、SMTP 授权码和连接测试统一在此管理。")
        subtitle.setObjectName("hint")
        root.addWidget(title)
        root.addWidget(subtitle)
        label = QLabel("QQ 邮箱地址")
        label.setObjectName("fieldLabel")
        self.email_edit = QLineEdit(service.cfg.qq_email)
        self.email_edit.setPlaceholderText("123456@qq.com")
        root.addWidget(label)
        root.addWidget(self.email_edit)
        self.qq_credential = CredentialEditor(
            "QQ SMTP 授权码（QQ 邮箱生成）",
            "QQ SMTP 授权码由 QQ 邮箱生成，不是 AgentMailBridge 密码。",
            bool(service.cfg.qq_auth_code),
        )
        root.addWidget(self.qq_credential)
        credential_actions = QHBoxLayout()
        self.delete_button = QPushButton("删除授权码")
        self.delete_button.setObjectName("textButton")
        self.delete_button.setEnabled(self.qq_credential.configured)
        self.delete_button.clicked.connect(self.delete_credential)
        test = QPushButton("测试 QQ SMTP 连接")
        test.clicked.connect(self.test_connection)
        credential_actions.addWidget(self.delete_button)
        credential_actions.addStretch(1)
        credential_actions.addWidget(test)
        root.addLayout(credential_actions)
        root.addStretch(1)
        self.result_label = self._status_label()
        root.addWidget(self.result_label)
        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存 / 完成")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        root.addLayout(actions)

    def _save_current(self) -> ServiceResult:
        result = self.controller.save_qq(
            self.email_edit.text(), self.qq_credential.pending_secret()
        )
        if result.ok and self.qq_credential.pending_secret():
            self.qq_credential.set_configured(True)
            self.delete_button.setEnabled(True)
        return result

    def test_connection(self) -> None:
        result = self._save_current()
        if not result.ok:
            self._show_result(result)
            return
        self._running("正在测试 QQ SMTP 连接…")
        self._show_result(self.service.diagnose_qq_smtp())

    def delete_credential(self) -> None:
        answer = QMessageBox.question(
            self,
            "删除 QQ SMTP 授权码",
            "删除后 QQ SMTP 将无法发件，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._show_result(ServiceResult(OperationStatus.CANCELLED, message="已取消删除"))
            return
        result = self.service.delete_credential(QQ_SMTP_SECRET)
        if result.ok:
            self.qq_credential.set_configured(False)
            self.delete_button.setEnabled(False)
        self._show_result(result)

    def accept(self) -> None:
        result = self._save_current()
        self._show_result(result)
        if result.ok:
            super().accept()


class AccountTypeDialog(QDialog):
    """v1.0.0 的邮箱扩展说明，不复用已有账号编辑路由。"""

    GMAIL = "gmail"
    QQ = "qq"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("添加邮箱账号")
        self.setModal(True)
        self.setMinimumSize(620, 430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("添加邮箱账号")
        title.setObjectName("pageTitle")
        hint = QLabel("这是未来邮箱扩展入口，不会修改当前 Gmail 或 QQ 邮箱账号。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        current = QFrame()
        current.setObjectName("accountPanel")
        current_layout = QVBoxLayout(current)
        current_layout.setContentsMargins(16, 14, 16, 14)
        current_title = QLabel("当前已支持")
        current_title.setObjectName("sectionTitle")
        current_layout.addWidget(current_title)
        current_layout.addWidget(QLabel("✓ Gmail 收件：通过左侧 Gmail 账号卡片管理已有账号"))
        current_layout.addWidget(QLabel("✓ QQ 发件：通过左侧 QQ 账号卡片管理已有账号"))
        layout.addWidget(current)

        future = QFrame()
        future.setObjectName("card")
        future_layout = QVBoxLayout(future)
        future_layout.setContentsMargins(16, 14, 16, 14)
        future_title = QLabel("未来可扩展")
        future_title.setObjectName("sectionTitle")
        future_layout.addWidget(future_title)
        future_layout.addWidget(QLabel("Outlook · 163 邮箱 · 企业邮箱 · 更多邮箱服务"))
        future_note = QLabel("当前 v1.0.0 暂不支持新增第二个同类型账号或其他邮箱服务。")
        future_note.setObjectName("hint")
        future_note.setWordWrap(True)
        future_layout.addWidget(future_note)
        layout.addWidget(future)
        layout.addStretch(1)
        close = QPushButton("我知道了")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


def open_account_dialog(
    service: ApplicationService,
    account_type: str,
    parent: QWidget | None = None,
) -> bool:
    dialog: QDialog
    if account_type == AccountTypeDialog.GMAIL:
        dialog = GmailAccountDialog(service, parent)
    elif account_type == AccountTypeDialog.QQ:
        dialog = QQAccountDialog(service, parent)
    else:
        return False
    return dialog.exec() == QDialog.DialogCode.Accepted


def open_add_account_dialog(
    service: ApplicationService, parent: QWidget | None = None
) -> bool:
    del service
    return AccountTypeDialog(parent).exec() == QDialog.DialogCode.Accepted
