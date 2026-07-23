"""邮箱账号新增与编辑界面，以及首次向导复用的账号保存逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
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


class _DialogTaskSignals(QObject):
    finished = Signal(object)


class _DialogTaskRunner(QRunnable):
    """账号窗口的短任务后台执行器。"""

    def __init__(self, operation: Callable[[], ServiceResult]):
        super().__init__()
        self.operation = operation
        self.signals = _DialogTaskSignals()

    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as exc:  # noqa: BLE001
            result = ServiceResult(
                OperationStatus.FAILED,
                error_code="internal_error",
                message=f"后台任务失败：{type(exc).__name__}",
            )
        self.signals.finished.emit(result)


class _OAuthWorker(QObject):
    """长时间 OAuth 会话 Worker；不直接访问任何 QWidget。"""

    progress = Signal(object)
    finished = Signal(str, object)

    def __init__(
        self,
        service: ApplicationService,
        timeout_seconds: float,
        account_id: str | None = None,
    ):
        super().__init__()
        self.service = service
        self.account_id = account_id
        self.session = service.create_gmail_oauth_session(
            account_id=account_id,
            progress_callback=self.progress.emit,
            timeout_seconds=timeout_seconds,
        )

    @Slot()
    def run(self) -> None:
        try:
            result = self.service.authorize_gmail_api(
                account_id=self.account_id,
                session=self.session,
            )
        except Exception as exc:  # noqa: BLE001
            result = ServiceResult(
                OperationStatus.FAILED,
                error_code="internal_error",
                message=f"OAuth Worker 失败：{type(exc).__name__}",
            )
        self.finished.emit(self.session.session_id, result)


def _valid_email(value: str) -> bool:
    local, separator, domain = value.partition("@")
    return bool(local and separator and "." in domain and not value.endswith("."))


def _mask_email_for_display(value: str) -> str:
    local, separator, domain = value.strip().partition("@")
    if not separator:
        return "未填写"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain.lower()}"


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
        synced = self.service.synchronize_mail_accounts()
        if not synced.ok:
            return ServiceResult(
                OperationStatus.PARTIAL,
                error_code="account_model_sync_failed",
                message="Gmail 配置已保存，账号模型将在下次启动时重试同步",
            )
        return ServiceResult(OperationStatus.SUCCESS, message="Gmail 邮箱账号已保存")

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
        synced = self.service.synchronize_mail_accounts()
        if not synced.ok:
            return ServiceResult(
                OperationStatus.PARTIAL,
                error_code="account_model_sync_failed",
                message="QQ 邮箱配置已保存，账号模型将在下次启动时重试同步",
            )
        return ServiceResult(OperationStatus.SUCCESS, message="QQ 邮箱账号已保存")


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
        self._background_active = False
        self._background_runner: _DialogTaskRunner | None = None
        self._background_button: QPushButton | None = None
        self._background_button_text = ""
        self._close_after_background = False
        self.setModal(True)
        self.setMinimumSize(680, 590)
        self.resize(760, 680)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._prepare_application_exit)

    def _status_label(self) -> QLabel:
        label = QLabel("准备就绪")
        label.setObjectName("hint")
        label.setWordWrap(True)
        return label

    def _show_result(self, result: ServiceResult) -> None:
        self.result_label.setText(result.message or result.status.value)
        color = (
            SUCCESS
            if result.status in {OperationStatus.SUCCESS, OperationStatus.NO_CHANGES}
            else WARNING
            if result.status in {OperationStatus.CANCELLED, OperationStatus.PARTIAL}
            else DANGER
        )
        self.result_label.setStyleSheet(
            f"color: {color};"
        )

    def _running(self, text: str) -> None:
        self.result_label.setText(text)
        self.result_label.setStyleSheet(f"color: {PURPLE};")

    def _background_busy(self) -> bool:
        if not self._background_active:
            return False
        self._show_result(
            ServiceResult(
                OperationStatus.CANCELLED,
                error_code="operation_busy",
                message="已有后台任务正在运行，请稍候",
            )
        )
        return True

    def _run_background(
        self,
        operation: Callable[[], ServiceResult],
        callback: Callable[[ServiceResult], None],
        *,
        button: QPushButton | None = None,
        working_text: str = "正在执行…",
    ) -> bool:
        if self._background_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="operation_busy",
                    message="已有后台任务正在运行，请稍候",
                )
            )
            return False
        self._background_active = True
        self._background_button = button
        if button is not None:
            self._background_button_text = button.text()
            button.setText(working_text)
            button.setEnabled(False)
        runner = _DialogTaskRunner(operation)
        self._background_runner = runner

        def finish(result: ServiceResult) -> None:
            completed_button = self._background_button
            if completed_button is not None:
                completed_button.setText(self._background_button_text)
                completed_button.setEnabled(True)
            self._background_active = False
            self._background_runner = None
            self._background_button = None
            self._background_button_text = ""
            callback(result)
            if self._close_after_background:
                self._close_after_background = False
                QDialog.reject(self)

        runner.signals.finished.connect(finish)
        QThreadPool.globalInstance().start(runner)
        return True

    def reject(self) -> None:
        if self._background_active:
            self._close_after_background = True
            self._running("正在等待当前连接测试安全结束…")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._background_active:
            self._close_after_background = True
            self._running("正在等待当前连接测试安全结束…")
            event.ignore()
            return
        super().closeEvent(event)

    @Slot()
    def _prepare_application_exit(self) -> None:
        if self._background_active:
            QThreadPool.globalInstance().waitForDone()


class GmailAccountDialog(_AccountDialog):
    """Gmail API 与 Gmail IMAP 条件互斥的专属账号配置页。"""

    def __init__(self, service: ApplicationService, parent: QWidget | None = None):
        super().__init__(service, parent)
        self.setMinimumSize(680, 700)
        self.resize(760, 740)
        self._oauth_thread: QThread | None = None
        self._oauth_worker: _OAuthWorker | None = None
        self._oauth_session = None
        self._oauth_session_id: str | None = None
        self._oauth_authorization_url: str | None = None
        self._oauth_active = False
        self._authorized_unverified = False
        self._close_after_oauth = False
        self._oauth_timeout_seconds = 300.0
        self.setWindowTitle("Gmail 邮箱账号")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        title = QLabel("Gmail 邮箱账号")
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
        if service.cfg.gmail_address:
            self.email_edit.setReadOnly(True)
            self.email_edit.setToolTip(
                "账号地址是稳定身份的一部分；如需换地址，请添加新账号并移除旧账号。"
            )
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
        self.dialog_cancel_button = QPushButton("取消")
        self.dialog_cancel_button.clicked.connect(self.reject)
        self.dialog_save_button = QPushButton("保存 / 完成")
        self.dialog_save_button.setObjectName("primaryButton")
        self.dialog_save_button.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(self.dialog_cancel_button)
        actions.addWidget(self.dialog_save_button)
        root.addLayout(actions)

        initial = service.cfg.gmail_receive_backend
        self._select_backend("imap" if initial == "imap" else "gmail_api")
        self.email_edit.textChanged.connect(self._update_oauth_expected_account)
        self._update_oauth_expected_account()
        self.refresh_status()

    def _build_api_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("accountPanel")
        self.api_content = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel("Gmail API 专属配置")
        heading.setObjectName("sectionTitle")
        self.api_setup_note = QLabel(
            "选择 credentials.json 后，程序会验证并自动导入受控用户目录；无需手动寻找 OAuth 文件夹。"
        )
        self.api_setup_note.setObjectName("hint")
        self.api_setup_note.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(self.api_setup_note)
        self.oauth_client_status = QLabel("○ 未导入")
        self.oauth_auth_status = QLabel("○ 未授权")
        self.api_connection_status = QLabel("○ 尚未测试")
        layout.addWidget(self.oauth_client_status)

        self.clear_token_button = QPushButton("清除 Token")
        self.clear_token_button.setObjectName("textButton")
        self.clear_token_button.setToolTip(
            "只删除本地 Token；Desktop app 凭据和 Google 账号授权不会被删除"
        )
        self.clear_token_button.setMinimumWidth(120)
        self.clear_token_button.clicked.connect(self.clear_oauth_token)
        auth_status_row = QHBoxLayout()
        auth_status_row.setContentsMargins(0, 0, 0, 0)
        auth_status_row.setSpacing(8)
        auth_status_row.addWidget(self.oauth_auth_status)
        auth_status_row.addStretch(1)
        auth_status_row.addWidget(self.clear_token_button)
        layout.addLayout(auth_status_row)

        self.reverify_button = QPushButton("重新验证 Gmail API")
        self.reverify_button.setObjectName("textButton")
        self.reverify_button.clicked.connect(self.reverify_gmail_api)
        self.reverify_button.setMinimumWidth(152)
        connection_status_row = QHBoxLayout()
        connection_status_row.setContentsMargins(0, 0, 0, 0)
        connection_status_row.setSpacing(8)
        connection_status_row.addWidget(self.api_connection_status)
        connection_status_row.addStretch(1)
        connection_status_row.addWidget(self.reverify_button)
        layout.addLayout(connection_status_row)
        self.oauth_client_summary = QLabel("仅接受 Google Desktop app credentials.json")
        self.oauth_client_summary.setObjectName("hint")
        self.oauth_client_summary.setWordWrap(True)
        layout.addWidget(self.oauth_client_summary)
        self.oauth_expected_account = QLabel("")
        self.oauth_expected_account.setObjectName("oauthExpectedAccount")
        self.oauth_expected_account.setWordWrap(True)
        layout.addWidget(self.oauth_expected_account)
        self.oauth_phase_label = QLabel("准备就绪")
        self.oauth_phase_label.setObjectName("fieldLabel")
        self.oauth_phase_label.setWordWrap(True)
        layout.addWidget(self.oauth_phase_label)
        self.oauth_progress = QProgressBar()
        self.oauth_progress.setRange(0, 0)
        self.oauth_progress.setTextVisible(False)
        self.oauth_progress.setFixedHeight(7)
        self.oauth_progress.hide()
        layout.addWidget(self.oauth_progress)
        timeout_note = QLabel(
            "授权最多等待 5 分钟。等待期间可取消、重新打开浏览器或复制同一授权链接。"
            "若长时间无回调，请检查是否仍停留在 Google 页面、浏览器已关闭、"
            "127.0.0.1 被代理或防火墙拦截。"
        )
        timeout_note.setObjectName("hint")
        timeout_note.setWordWrap(True)
        layout.addWidget(timeout_note)
        verification_note = QLabel(
            "Google OAuth 应用“已发布”不等于“已通过 Google 验证”。如出现未经验证提示，"
            "是否继续必须由你在浏览器中自行决定，AgentMailBridge 不会自动绕过。"
        )
        verification_note.setObjectName("hint")
        verification_note.setWordWrap(True)
        layout.addWidget(verification_note)
        self.import_button = QPushButton("选择凭据文件")
        self.import_button.setToolTip("仅接受 Google Desktop app credentials.json")
        self.import_button.clicked.connect(self.import_oauth_json)
        self.authorize_button = QPushButton("授权 Gmail")
        self.authorize_button.clicked.connect(self.authorize)
        self.authorize_button.setToolTip("在浏览器中完成 Gmail OAuth 授权")
        self.api_test_button = QPushButton("测试 API 连接")
        self.api_test_button.clicked.connect(self.test_api)
        self.api_test_button.setToolTip("测试当前 Gmail API Token 和 Gmail Profile")
        self.cancel_oauth_button = QPushButton("取消授权")
        self.cancel_oauth_button.clicked.connect(self.cancel_oauth)
        self.reopen_browser_button = QPushButton("重新打开浏览器")
        self.reopen_browser_button.clicked.connect(self.reopen_oauth_browser)
        self.copy_oauth_link_button = QPushButton("复制授权链接")
        self.copy_oauth_link_button.clicked.connect(self.copy_oauth_link)
        for button in (
            self.import_button,
            self.authorize_button,
            self.api_test_button,
            self.cancel_oauth_button,
            self.reopen_browser_button,
            self.copy_oauth_link_button,
            self.clear_token_button,
            self.reverify_button,
        ):
            button.setMinimumHeight(34)

        self.oauth_idle_actions = QWidget()
        idle_actions = QHBoxLayout(self.oauth_idle_actions)
        idle_actions.setContentsMargins(0, 0, 0, 0)
        idle_actions.setSpacing(8)
        idle_actions.addWidget(self.import_button, 1)
        idle_actions.addWidget(self.authorize_button, 1)
        idle_actions.addWidget(self.api_test_button, 1)

        self.oauth_waiting_actions = QWidget()
        waiting_actions = QHBoxLayout(self.oauth_waiting_actions)
        waiting_actions.setContentsMargins(0, 0, 0, 0)
        waiting_actions.setSpacing(8)
        waiting_actions.addWidget(self.cancel_oauth_button, 1)
        waiting_actions.addWidget(self.reopen_browser_button, 1)
        waiting_actions.addWidget(self.copy_oauth_link_button, 1)

        self.oauth_action_stack = QStackedWidget()
        self.oauth_action_stack.addWidget(self.oauth_idle_actions)
        self.oauth_action_stack.addWidget(self.oauth_waiting_actions)
        self.oauth_action_stack.setCurrentWidget(self.oauth_idle_actions)
        self.oauth_action_stack.setFixedHeight(34)
        layout.addWidget(self.oauth_action_stack)
        self.oauth_error_detail = QLabel("")
        self.oauth_error_detail.setObjectName("oauthResultDetail")
        self.oauth_error_detail.setProperty("severity", "neutral")
        self.oauth_error_detail.setWordWrap(True)
        self.oauth_error_detail.hide()
        layout.addWidget(self.oauth_error_detail)
        for button in (
            self.cancel_oauth_button,
            self.reopen_browser_button,
            self.copy_oauth_link_button,
            self.reverify_button,
        ):
            button.hide()
        layout.addStretch(1)
        return page

    @Slot()
    def _update_oauth_expected_account(self) -> None:
        value = self.email_edit.text().strip()
        if _valid_email(value):
            self.oauth_expected_account.setText(
                "本次授权将与上方 Gmail 地址严格核对："
                f"{_mask_email_for_display(value)}。请在 Google 页面选择同一账号。"
            )
        else:
            self.oauth_expected_account.setText(
                "请先填写上方 Gmail 地址；授权完成后会与 Google 返回账号严格核对。"
            )

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
        oauth = self.service.get_oauth_status().details
        state = str(oauth.get("state", "NOT_CONFIGURED"))
        credentials = state not in {"CREDENTIALS_MISSING", "CREDENTIALS_INVALID"}
        authorized = state in {"READY", "TOKEN_EXPIRED_REFRESHABLE"}
        client_type = str(oauth.get("client_type") or "")
        suffix = str(oauth.get("client_id_suffix") or "")
        project_id = str(oauth.get("project_id") or "")
        self.oauth_client_status.setText(
            "✓ OAuth 客户端配置：Desktop app"
            if credentials
            else "✕ OAuth 客户端配置：未导入或无效"
        )
        self.oauth_client_status.setStyleSheet(f"color: {SUCCESS if credentials else WARNING};")
        if self._authorized_unverified:
            self.oauth_auth_status.setText("Gmail 授权：已取得，API 待重新验证")
            self.oauth_auth_status.setStyleSheet(f"color: {WARNING};")
        else:
            self.oauth_auth_status.setText("✓ Gmail 授权：已授权" if authorized else "○ Gmail 授权：未授权")
            self.oauth_auth_status.setStyleSheet(f"color: {SUCCESS if authorized else WARNING};")
        summary_parts = [client_type] if client_type else ["仅接受 Desktop app"]
        if project_id:
            summary_parts.append(f"项目：{project_id}")
        if suffix:
            summary_parts.append(f"Client ID 尾号：{suffix}")
        self.oauth_client_summary.setText(" · ".join(summary_parts))
        self.import_button.setText("替换凭据文件" if credentials else "选择凭据文件")
        self.authorize_button.setText("重新授权 Gmail" if authorized else "授权 Gmail")
        self.clear_token_button.setVisible(self.service.cfg.gmail_api_token_path.exists())
        self.clear_token_button.setEnabled(
            self.service.cfg.gmail_api_token_path.exists() and not self._oauth_active
        )
        self.reverify_button.setVisible(self._authorized_unverified)
        self.reverify_button.setEnabled(self._authorized_unverified and not self._oauth_active)
        self.delete_imap_button.setEnabled(self.imap_credential.configured)

    def import_oauth_json(self) -> None:
        if self._oauth_active or self._background_busy():
            return
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
        if self._oauth_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="oauth_already_running",
                    message="Gmail OAuth 授权正在进行，请先完成或取消当前会话",
                )
            )
            return
        if self._background_busy():
            return
        saved = self.controller.save_gmail(self.email_edit.text(), "gmail_api")
        if not saved.ok:
            self._show_result(saved)
            return
        self._authorized_unverified = False
        self._oauth_active = True
        self._oauth_authorization_url = None
        self._close_after_oauth = False
        self._set_oauth_running_controls(True)
        self.oauth_phase_label.setText(
            "正在检查桌面应用凭据，目标账号："
            f"{_mask_email_for_display(self.service.cfg.gmail_address)}"
        )
        self.oauth_progress.show()
        self.oauth_error_detail.hide()
        self._running("正在启动 Gmail OAuth 安全授权会话…")

        thread = QThread(self)
        worker = _OAuthWorker(self.service, self._oauth_timeout_seconds)
        worker.moveToThread(thread)
        self._oauth_thread = thread
        self._oauth_worker = worker
        self._oauth_session = worker.session
        self._oauth_session_id = worker.session.session_id
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_oauth_progress)
        worker.finished.connect(self._on_oauth_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(
            lambda session_id=worker.session.session_id: self._on_oauth_thread_finished(
                session_id
            )
        )
        thread.start()

    def test_api(self) -> None:
        if self._oauth_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="oauth_already_running",
                    message="授权进行中，暂不能同时测试 Gmail API 连接",
                )
            )
            return
        if self._background_busy():
            return
        saved = self.controller.save_gmail(self.email_edit.text(), "gmail_api")
        if not saved.ok:
            self._show_result(saved)
            return
        self._running("正在测试 Gmail API 连接…")
        self._run_background(
            self.service.diagnose_gmail_api,
            self._finish_api_test,
            button=self.api_test_button,
            working_text="正在测试…",
        )

    def _finish_api_test(self, result: ServiceResult) -> None:
        self.api_connection_status.setText(
            "✓ 连接：正常"
            if result.status == OperationStatus.SUCCESS
            else "连接：已授权，待重新验证"
            if result.status == OperationStatus.PARTIAL
            else "✕ 连接：失败"
        )
        self.api_connection_status.setStyleSheet(
            f"color: {SUCCESS if result.status == OperationStatus.SUCCESS else WARNING if result.status == OperationStatus.PARTIAL else DANGER};"
        )
        self._authorized_unverified = (
            result.details.get("oauth_state") == "AUTHORIZED_UNVERIFIED"
        )
        self.refresh_status()
        self._show_result(result)

    def reverify_gmail_api(self) -> None:
        if self._oauth_active or self._background_busy():
            return
        self._running("正在重新验证 Gmail API…")
        self._run_background(
            self.service.diagnose_gmail_api,
            self._finish_api_test,
            button=self.reverify_button,
            working_text="正在重新验证…",
        )

    def clear_oauth_token(self) -> None:
        if (
            self._oauth_active
            or self._background_busy()
            or not self.service.cfg.gmail_api_token_path.exists()
        ):
            return
        answer = QMessageBox.question(
            self,
            "清除本地 Gmail Token",
            "清除后需要重新进行浏览器授权。Desktop credentials.json 会保留，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._show_result(
                ServiceResult(OperationStatus.CANCELLED, message="已取消清除本地 Token")
            )
            return
        result = self.service.clear_gmail_oauth_token()
        if result.ok:
            self._authorized_unverified = False
            self.api_connection_status.setText("○ 尚未测试")
            self.api_connection_status.setStyleSheet(f"color: {WARNING};")
        self.refresh_status()
        self._show_result(result)

    @Slot(object)
    def _on_oauth_progress(self, event: dict[str, Any]) -> None:
        if event.get("session_id") != self._oauth_session_id:
            return
        state = str(event.get("state") or "")
        self.oauth_phase_label.setText(str(event.get("message") or state))
        authorization_url = event.get("authorization_url")
        if isinstance(authorization_url, str) and authorization_url:
            self._oauth_authorization_url = authorization_url
        waiting = state == "WAITING_FOR_USER"
        self.cancel_oauth_button.setEnabled(
            state not in {"CANCELLING", "CANCELLED", "TIMED_OUT", "FAILED"}
        )
        for button in (self.reopen_browser_button, self.copy_oauth_link_button):
            button.setVisible(waiting)
            button.setEnabled(waiting and bool(self._oauth_authorization_url))
        if waiting and not bool(event.get("browser_opened", True)):
            self.oauth_error_detail.setText(
                "未能自动打开浏览器，请复制授权链接在本机浏览器中打开。"
            )
            self._set_oauth_result_detail_severity("warning")
            self.oauth_error_detail.show()
        elif state in {"CALLBACK_RECEIVED", "EXCHANGING_TOKEN", "VERIFYING_GMAIL"}:
            self.oauth_error_detail.hide()

    @Slot(str, object)
    def _on_oauth_finished(self, session_id: str, result: ServiceResult) -> None:
        if session_id != self._oauth_session_id:
            return
        oauth_state = str(result.details.get("oauth_state") or "")
        error_code = str(result.error_code or "")
        self._authorized_unverified = oauth_state == "AUTHORIZED_UNVERIFIED"
        self._oauth_authorization_url = None
        self.oauth_progress.hide()
        for button in (
            self.cancel_oauth_button,
            self.reopen_browser_button,
            self.copy_oauth_link_button,
        ):
            button.hide()
        if error_code == "account_mismatch":
            self.oauth_phase_label.setText("授权账号不匹配，Token 未保存")
        else:
            self.oauth_phase_label.setText(
                "已取得授权，但 Gmail API 验证暂未通过"
                if self._authorized_unverified
                else str(result.details.get("title") or result.message or oauth_state)
            )
        detail_parts: list[str] = []
        expected_masked = str(result.details.get("expected_email_masked") or "")
        actual_masked = str(result.details.get("actual_email_masked") or "")
        if error_code == "account_mismatch":
            if expected_masked:
                detail_parts.append(f"当前配置：{expected_masked}")
            if actual_masked:
                detail_parts.append(f"本次授权：{actual_masked}")
            detail_parts.append("Token 未保存。")
        detail_parts.extend(
            [
                str(result.details.get("reason") or ""),
                str(result.details.get("next_step") or ""),
            ]
        )
        if actual_masked and error_code != "account_mismatch":
            detail_parts.append(f"本次授权账号：{actual_masked}")
        detail_text = " ".join(part for part in detail_parts if part)
        if detail_text and result.status != OperationStatus.SUCCESS:
            self.oauth_error_detail.setText(detail_text)
            self._set_oauth_result_detail_severity(
                "warning"
                if result.status in {OperationStatus.CANCELLED, OperationStatus.PARTIAL}
                else "danger"
            )
            self.oauth_error_detail.show()
        else:
            self.oauth_error_detail.hide()
        self.refresh_status()
        self._show_result(result)
        self._notify_oauth_completion()

    def _set_oauth_result_detail_severity(self, severity: str) -> None:
        self.oauth_error_detail.setProperty("severity", severity)
        self.oauth_error_detail.style().unpolish(self.oauth_error_detail)
        self.oauth_error_detail.style().polish(self.oauth_error_detail)

    def _notify_oauth_completion(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        app = QApplication.instance()
        if app is not None:
            app.alert(self, 0)

    def _on_oauth_thread_finished(self, session_id: str) -> None:
        if session_id != self._oauth_session_id:
            return
        thread = self._oauth_thread
        self._oauth_active = False
        self._oauth_worker = None
        self._oauth_session = None
        self._oauth_thread = None
        self._oauth_session_id = None
        self._set_oauth_running_controls(False)
        if thread is not None:
            thread.deleteLater()
        if self._close_after_oauth:
            self._close_after_oauth = False
            if self._background_active:
                self._close_after_background = True
            else:
                QDialog.reject(self)

    def _set_oauth_running_controls(self, running: bool) -> None:
        self.email_edit.setEnabled(not running)
        self.api_setup_note.setVisible(not running)
        self.oauth_action_stack.setCurrentWidget(
            self.oauth_waiting_actions if running else self.oauth_idle_actions
        )
        for button in (
            self.import_button,
            self.authorize_button,
            self.api_test_button,
        ):
            button.setEnabled(not running)
        has_token = self.service.cfg.gmail_api_token_path.exists()
        self.clear_token_button.setVisible(not running and has_token)
        self.clear_token_button.setEnabled(not running and has_token)
        self.api_mode_button.setEnabled(not running)
        self.imap_mode_button.setEnabled(not running)
        self.dialog_save_button.setEnabled(not running)
        self.cancel_oauth_button.setVisible(running)
        self.cancel_oauth_button.setEnabled(running)
        self.reverify_button.setVisible(not running and self._authorized_unverified)
        self.reverify_button.setEnabled(not running and self._authorized_unverified)

    def cancel_oauth(self) -> None:
        session = self._oauth_session
        if not self._oauth_active or session is None:
            return
        self.oauth_phase_label.setText("正在取消授权")
        self.cancel_oauth_button.setEnabled(False)
        session.cancel()

    def reopen_oauth_browser(self) -> None:
        session = self._oauth_session
        if not self._oauth_active or session is None:
            return
        session_id = self._oauth_session_id

        def show_if_current(result: ServiceResult) -> None:
            if self._oauth_active and self._oauth_session_id == session_id:
                self._show_result(result)

        self._run_background(
            session.reopen_browser,
            show_if_current,
            button=self.reopen_browser_button,
            working_text="正在打开…",
        )

    def copy_oauth_link(self) -> None:
        if not self._oauth_active or not self._oauth_authorization_url:
            return
        QGuiApplication.clipboard().setText(self._oauth_authorization_url)
        self._show_result(
            ServiceResult(OperationStatus.SUCCESS, message="授权链接已复制，仅保存在剪贴板中")
        )

    def test_imap(self) -> None:
        if self._oauth_active or self._background_busy():
            return
        result = self.controller.save_gmail(
            self.email_edit.text(), "imap", self.imap_credential.pending_secret()
        )
        if not result.ok:
            self._show_result(result)
            return
        self.imap_credential.set_configured(True)
        self._running("正在测试 Gmail IMAP 连接…")
        self._run_background(
            self.service.diagnose_imap,
            self._show_result,
            button=self.imap_test_button,
            working_text="正在测试…",
        )

    def delete_imap_credential(self) -> None:
        if self._oauth_active or self._background_busy():
            return
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

    def reject(self) -> None:
        if self._oauth_active:
            self._close_after_oauth = True
            self.cancel_oauth()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._oauth_active:
            self._close_after_oauth = True
            self.cancel_oauth()
            event.ignore()
            return
        super().closeEvent(event)

    @Slot()
    def _prepare_application_exit(self) -> None:
        session = self._oauth_session
        thread = self._oauth_thread
        if self._oauth_active and session is not None:
            session.cancel()
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait()
        super()._prepare_application_exit()

    def accept(self) -> None:
        if self._oauth_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="oauth_already_running",
                    message="请先完成或取消当前 Gmail OAuth 授权",
                )
            )
            return
        if self._background_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="operation_busy",
                    message="请等待当前连接测试结束",
                )
            )
            return
        result = self._save_current()
        self._show_result(result)
        if result.ok:
            super().accept()


class QQAccountDialog(_AccountDialog):
    """QQ SMTP 发件账号的唯一配置页面。"""

    def __init__(self, service: ApplicationService, parent: QWidget | None = None):
        super().__init__(service, parent)
        self.setWindowTitle("QQ 邮箱账号")
        self.setMinimumSize(640, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("QQ 邮箱账号")
        title.setObjectName("pageTitle")
        subtitle = QLabel("发件身份、SMTP 授权码和连接测试统一在此管理。")
        subtitle.setObjectName("hint")
        root.addWidget(title)
        root.addWidget(subtitle)
        label = QLabel("QQ 邮箱地址")
        label.setObjectName("fieldLabel")
        self.email_edit = QLineEdit(service.cfg.qq_email)
        self.email_edit.setPlaceholderText("123456@qq.com")
        if service.cfg.qq_email:
            self.email_edit.setReadOnly(True)
            self.email_edit.setToolTip(
                "账号地址是稳定身份的一部分；如需换地址，请添加新账号并移除旧账号。"
            )
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
        self.qq_test_button = QPushButton("测试 QQ SMTP 连接")
        self.qq_test_button.clicked.connect(self.test_connection)
        credential_actions.addWidget(self.delete_button)
        credential_actions.addStretch(1)
        credential_actions.addWidget(self.qq_test_button)
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
        if self._background_busy():
            return
        result = self._save_current()
        if not result.ok:
            self._show_result(result)
            return
        self._running("正在测试 QQ SMTP 连接…")
        self._run_background(
            self.service.diagnose_qq_smtp,
            self._show_result,
            button=self.qq_test_button,
            working_text="正在测试…",
        )

    def delete_credential(self) -> None:
        if self._background_busy():
            return
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
        if self._background_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="operation_busy",
                    message="请等待当前连接测试结束",
                )
            )
            return
        result = self._save_current()
        self._show_result(result)
        if result.ok:
            super().accept()


class AccountTypeDialog(QDialog):
    """创建新的统一邮箱账号，不复用旧账号编辑路由。"""

    GMAIL = "gmail"
    QQ = "qq"
    GENERIC = "generic_imap_smtp"

    def __init__(
        self,
        service: ApplicationService,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.service = service
        self.created_account_id = ""
        self.setWindowTitle("添加邮箱账号")
        self.setModal(True)
        self.setMinimumSize(680, 650)
        self.resize(720, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("添加邮箱账号")
        title.setObjectName("pageTitle")
        hint = QLabel(
            "可添加多个 Gmail、QQ 或 Generic IMAP/SMTP 账号。"
            "Generic 当前只开放连接测试和目录发现，尚未开放正式收发。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        form_card = QFrame()
        form_card.setObjectName("accountPanel")
        form = QFormLayout(form_card)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("Gmail", self.GMAIL)
        self.provider_combo.addItem("QQ 邮箱", self.QQ)
        self.provider_combo.addItem("Generic IMAP/SMTP", self.GENERIC)
        self.display_name_edit = QLineEdit()
        self.display_name_edit.setPlaceholderText("可选的账号显示名称")
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("name@example.com")
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Gmail API（OAuth）", "gmail_api")
        self.backend_combo.addItem("Gmail IMAP（应用专用密码）", "imap")
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("密码或授权码不会写入数据库")
        form.addRow("邮箱类型", self.provider_combo)
        form.addRow("显示名称", self.display_name_edit)
        form.addRow("邮箱地址", self.email_edit)
        self.backend_label = QLabel("Gmail 收件方式")
        form.addRow(self.backend_label, self.backend_combo)
        self.secret_label = QLabel("账号凭据")
        form.addRow(self.secret_label, self.secret_edit)
        layout.addWidget(form_card)

        self.server_panel = QFrame()
        self.server_panel.setObjectName("card")
        server_form = QFormLayout(self.server_panel)
        server_form.setContentsMargins(16, 14, 16, 14)
        server_form.setSpacing(8)
        self.imap_host_edit = QLineEdit()
        self.imap_port_edit = QLineEdit("993")
        self.imap_security_combo = QComboBox()
        self.imap_security_combo.addItem("SSL/TLS", "ssl")
        self.imap_security_combo.addItem("STARTTLS", "starttls")
        self.smtp_host_edit = QLineEdit()
        self.smtp_port_edit = QLineEdit("465")
        self.smtp_security_combo = QComboBox()
        self.smtp_security_combo.addItem("SSL/TLS", "ssl")
        self.smtp_security_combo.addItem("STARTTLS", "starttls")
        server_form.addRow("IMAP 服务器", self.imap_host_edit)
        server_form.addRow("IMAP 端口", self.imap_port_edit)
        server_form.addRow("IMAP 安全", self.imap_security_combo)
        server_form.addRow("SMTP 服务器", self.smtp_host_edit)
        server_form.addRow("SMTP 端口", self.smtp_port_edit)
        server_form.addRow("SMTP 安全", self.smtp_security_combo)
        server_note = QLabel("不接受明文连接；凭据只进入 Windows Credential Manager。")
        server_note.setObjectName("hint")
        server_note.setWordWrap(True)
        server_form.addRow(server_note)
        layout.addWidget(self.server_panel)

        self.result_label = QLabel("准备创建账号")
        self.result_label.setObjectName("hint")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        layout.addStretch(1)
        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        create = QPushButton("创建账号")
        create.setObjectName("primaryButton")
        create.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(create)
        layout.addLayout(actions)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.backend_combo.currentIndexChanged.connect(self._provider_changed)
        self._provider_changed()

    def _provider_changed(self) -> None:
        provider = str(self.provider_combo.currentData() or "")
        is_gmail = provider == self.GMAIL
        is_generic = provider == self.GENERIC
        self.backend_label.setVisible(is_gmail)
        self.backend_combo.setVisible(is_gmail)
        gmail_api = is_gmail and self.backend_combo.currentData() == "gmail_api"
        self.secret_label.setText(
            "QQ SMTP 授权码"
            if provider == self.QQ
            else "IMAP 应用专用密码"
            if is_gmail
            else "IMAP/SMTP 密码或授权码"
        )
        self.secret_label.setVisible(not gmail_api)
        self.secret_edit.setVisible(not gmail_api)
        self.server_panel.setVisible(is_generic)

    def accept(self) -> None:
        provider = str(self.provider_combo.currentData() or "")
        settings: dict[str, Any] = {}
        backend = ""
        if provider == self.GMAIL:
            backend = str(self.backend_combo.currentData() or "gmail_api")
        elif provider == self.GENERIC:
            settings = {
                "imap_host": self.imap_host_edit.text().strip(),
                "imap_port": self.imap_port_edit.text().strip(),
                "imap_security": self.imap_security_combo.currentData(),
                "smtp_host": self.smtp_host_edit.text().strip(),
                "smtp_port": self.smtp_port_edit.text().strip(),
                "smtp_security": self.smtp_security_combo.currentData(),
            }
        result = self.service.create_mail_account(
            provider=provider,
            email_address=self.email_edit.text(),
            display_name=self.display_name_edit.text(),
            receive_backend=backend,
            provider_settings=settings,
            secret=self.secret_edit.text(),
        )
        self.result_label.setText(result.message)
        self.result_label.setStyleSheet(
            f"color: {SUCCESS if result.ok else DANGER};"
        )
        if result.ok:
            self.created_account_id = str(
                result.details.get("account", {}).get("account_id") or ""
            )
            super().accept()


class RuntimeAccountDialog(_AccountDialog):
    """管理非旧配置账号的启停、凭据、连接测试、OAuth 与软移除。"""

    def __init__(
        self,
        service: ApplicationService,
        account_id: str,
        parent: QWidget | None = None,
    ):
        super().__init__(service, parent)
        self.account_id = account_id
        self.account = self._load_account()
        self._oauth_thread: QThread | None = None
        self._oauth_worker: _OAuthWorker | None = None
        self._oauth_active = False
        self._close_after_oauth = False
        self.setWindowTitle("邮箱账号")
        self.setMinimumSize(680, 610)
        self.resize(740, 680)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel(str(self.account.get("display_name") or "邮箱账号"))
        title.setObjectName("pageTitle")
        identity = QLabel(
            f"{self.account.get('email_address')}  "
            f"Provider：{self.account.get('provider')}"
        )
        identity.setObjectName("hint")
        root.addWidget(title)
        root.addWidget(identity)

        self.enabled_check = QCheckBox("启用此账号")
        self.enabled_check.setChecked(bool(self.account.get("enabled")))
        root.addWidget(self.enabled_check)

        provider = str(self.account.get("provider") or "")
        settings = dict(self.account.get("provider_settings") or {})
        self.imap_secret_edit: QLineEdit | None = None
        self.smtp_secret_edit: QLineEdit | None = None
        if provider == "gmail" and settings.get("receive_backend") == "imap":
            self.imap_secret_edit = self._secret_editor(
                root, "更新 IMAP 应用专用密码"
            )
        elif provider == "qq":
            self.smtp_secret_edit = self._secret_editor(
                root, "更新 QQ SMTP 授权码"
            )
        elif provider == "generic_imap_smtp":
            if settings.get("imap_host"):
                self.imap_secret_edit = self._secret_editor(
                    root, "更新 IMAP 密码或授权码"
                )
            if settings.get("smtp_host"):
                self.smtp_secret_edit = self._secret_editor(
                    root, "更新 SMTP 密码或授权码"
                )

        actions_card = QFrame()
        actions_card.setObjectName("accountPanel")
        actions_layout = QVBoxLayout(actions_card)
        actions_layout.setContentsMargins(14, 12, 14, 12)
        self.test_button = QPushButton("测试账号连接")
        self.test_button.clicked.connect(self.test_connection)
        actions_layout.addWidget(self.test_button)
        self.discover_button = QPushButton("发现 IMAP 目录")
        can_discover = provider == "generic_imap_smtp" or (
            provider == "gmail"
            and settings.get("receive_backend") == "imap"
        )
        self.discover_button.setVisible(can_discover)
        self.discover_button.clicked.connect(self.discover_mailboxes)
        actions_layout.addWidget(self.discover_button)

        self.oauth_import_button = QPushButton("导入 OAuth credentials.json")
        self.oauth_authorize_button = QPushButton("开始 Gmail OAuth 授权")
        self.oauth_clear_button = QPushButton("清除本账号 OAuth Token")
        self.oauth_cancel_button = QPushButton("取消正在进行的 OAuth")
        is_oauth = provider == "gmail" and (
            settings.get("receive_backend") == "gmail_api"
        )
        for button in (
            self.oauth_import_button,
            self.oauth_authorize_button,
            self.oauth_clear_button,
            self.oauth_cancel_button,
        ):
            button.setVisible(is_oauth)
            actions_layout.addWidget(button)
        self.oauth_cancel_button.hide()
        self.oauth_import_button.clicked.connect(self.import_oauth)
        self.oauth_authorize_button.clicked.connect(self.authorize_oauth)
        self.oauth_clear_button.clicked.connect(self.clear_oauth)
        self.oauth_cancel_button.clicked.connect(self.cancel_oauth)
        root.addWidget(actions_card)

        remove_card = QFrame()
        remove_card.setObjectName("card")
        remove_layout = QVBoxLayout(remove_card)
        remove_layout.setContentsMargins(14, 12, 14, 12)
        remove_note = QLabel(
            "移除账号会停止后续收发；本地邮件、附件、发件记录和审计默认保留。"
        )
        remove_note.setWordWrap(True)
        remove_layout.addWidget(remove_note)
        self.cleanup_credentials_check = QCheckBox("同时删除本账号凭据")
        self.cleanup_oauth_check = QCheckBox("同时删除本账号 OAuth Token")
        self.cleanup_oauth_check.setVisible(provider == "gmail")
        remove_layout.addWidget(self.cleanup_credentials_check)
        remove_layout.addWidget(self.cleanup_oauth_check)
        self.remove_button = QPushButton("移除账号")
        self.remove_button.setObjectName("dangerButton")
        self.remove_button.clicked.connect(self.remove_account)
        remove_layout.addWidget(self.remove_button)
        root.addWidget(remove_card)

        self.result_label = self._status_label()
        root.addWidget(self.result_label)
        root.addStretch(1)
        footer = QHBoxLayout()
        cancel = QPushButton("关闭")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        footer.addStretch(1)
        footer.addWidget(cancel)
        footer.addWidget(save)
        root.addLayout(footer)

    def _load_account(self) -> dict[str, Any]:
        result = self.service.list_mail_accounts()
        return next(
            (
                dict(item)
                for item in result.details.get("accounts", [])
                if item.get("account_id") == self.account_id
            ),
            {},
        )

    @staticmethod
    def _secret_editor(layout: QVBoxLayout, label_text: str) -> QLineEdit:
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        editor = QLineEdit()
        editor.setEchoMode(QLineEdit.EchoMode.Password)
        editor.setPlaceholderText("留空表示不修改")
        layout.addWidget(label)
        layout.addWidget(editor)
        return editor

    def test_connection(self) -> None:
        self._running("正在测试账号连接…")
        self._run_background(
            lambda: self.service.test_mail_account_connection(self.account_id),
            self._show_result,
            button=self.test_button,
            working_text="正在测试…",
        )

    def discover_mailboxes(self) -> None:
        self._running("正在发现 IMAP 目录…")
        self._run_background(
            lambda: self.service.discover_mail_account_mailboxes(self.account_id),
            self._show_result,
            button=self.discover_button,
            working_text="正在发现…",
        )

    def import_oauth(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Google Desktop credentials.json",
            "",
            "JSON 文件 (*.json)",
        )
        if not source:
            self._show_result(
                ServiceResult(OperationStatus.CANCELLED, message="已取消导入")
            )
            return
        result = self.service.import_oauth_credentials(
            source, replace=False, account_id=self.account_id
        )
        if result.error_code == "oauth_credentials_exists":
            answer = QMessageBox.question(
                self,
                "替换本账号 OAuth 客户端配置",
                "本账号已有 credentials.json。替换不会删除 Token 或影响其他账号，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                result = self.service.import_oauth_credentials(
                    source, replace=True, account_id=self.account_id
                )
        self._show_result(result)

    def authorize_oauth(self) -> None:
        if self._oauth_active:
            return
        try:
            worker = _OAuthWorker(
                self.service, 300.0, account_id=self.account_id
            )
        except Exception as exc:  # noqa: BLE001
            self._show_result(
                ServiceResult(
                    OperationStatus.FAILED,
                    error_code="oauth_session_failed",
                    message=str(exc),
                )
            )
            return
        thread = QThread(self)
        worker.moveToThread(thread)
        self._oauth_thread = thread
        self._oauth_worker = worker
        self._oauth_active = True
        self.oauth_authorize_button.setEnabled(False)
        self.oauth_cancel_button.show()
        worker.progress.connect(
            lambda event: self._running(
                str(event.get("message") or "等待用户完成 OAuth 授权")
            )
        )
        worker.finished.connect(self._oauth_finished)
        worker.finished.connect(thread.quit)
        thread.started.connect(worker.run)
        thread.finished.connect(self._oauth_thread_finished)
        thread.start()

    @Slot(str, object)
    def _oauth_finished(
        self, _session_id: str, result: ServiceResult
    ) -> None:
        self._show_result(result)

    def _oauth_thread_finished(self) -> None:
        self._oauth_active = False
        self.oauth_authorize_button.setEnabled(True)
        self.oauth_cancel_button.hide()
        self._oauth_worker = None
        thread = self._oauth_thread
        self._oauth_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._close_after_oauth:
            self._close_after_oauth = False
            QDialog.reject(self)

    def cancel_oauth(self) -> None:
        if self._oauth_worker is not None:
            self._oauth_worker.session.cancel()
            self._running("正在取消 OAuth 授权…")

    def clear_oauth(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除本账号 OAuth Token",
            "只清除此账号的 Token，Desktop credentials.json 和其他账号不受影响。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._show_result(
                self.service.clear_gmail_oauth_token(self.account_id)
            )

    def remove_account(self) -> None:
        if self._oauth_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    message="请先完成或取消当前 OAuth 授权",
                )
            )
            return
        answer = QMessageBox.question(
            self,
            "移除邮箱账号",
            "账号将停止运行，本地历史数据会保留。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self.service.remove_mail_account(
            self.account_id,
            cleanup_credentials=self.cleanup_credentials_check.isChecked(),
            cleanup_oauth_token=self.cleanup_oauth_check.isChecked(),
        )
        self._show_result(result)
        if result.ok:
            QDialog.accept(self)

    def accept(self) -> None:
        if self._oauth_active:
            self._show_result(
                ServiceResult(
                    OperationStatus.CANCELLED,
                    message="请先完成或取消当前 OAuth 授权",
                )
            )
            return
        result = self.service.update_mail_account(
            self.account_id, enabled=self.enabled_check.isChecked()
        )
        if result.ok and self.imap_secret_edit is not None:
            value = self.imap_secret_edit.text().strip()
            if value:
                result = self.service.set_account_credential(
                    self.account_id, "imap_password", value
                )
        if result.ok and self.smtp_secret_edit is not None:
            value = self.smtp_secret_edit.text().strip()
            if value:
                result = self.service.set_account_credential(
                    self.account_id, "smtp_password", value
                )
        self._show_result(result)
        if result.ok:
            QDialog.accept(self)

    def reject(self) -> None:
        if self._oauth_active:
            self._close_after_oauth = True
            self.cancel_oauth()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._oauth_active:
            self._close_after_oauth = True
            self.cancel_oauth()
            event.ignore()
            return
        super().closeEvent(event)

    @Slot()
    def _prepare_application_exit(self) -> None:
        worker = self._oauth_worker
        thread = self._oauth_thread
        if worker is not None:
            worker.session.cancel()
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait()
        super()._prepare_application_exit()


def open_account_dialog(
    service: ApplicationService,
    account_type: str,
    parent: QWidget | None = None,
) -> bool:
    provider = {
        AccountTypeDialog.GMAIL: "gmail",
        AccountTypeDialog.QQ: "qq",
    }.get(account_type)
    configured_address = {
        AccountTypeDialog.GMAIL: service.cfg.gmail_address,
        AccountTypeDialog.QQ: service.cfg.qq_email,
    }.get(account_type, "")
    if provider and configured_address:
        accounts = service.list_mail_accounts()
        account = next(
            (
                item
                for item in accounts.details.get("accounts", ())
                if item.get("provider") == provider
                and str(item.get("email_address") or "").casefold()
                == configured_address.casefold()
            ),
            None,
        )
        if account is not None:
            return (
                RuntimeAccountDialog(
                    service, str(account["account_id"]), parent
                ).exec()
                == QDialog.DialogCode.Accepted
            )
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
    return (
        AccountTypeDialog(service, parent).exec()
        == QDialog.DialogCode.Accepted
    )


def open_runtime_account_dialog(
    service: ApplicationService,
    account_id: str,
    parent: QWidget | None = None,
) -> bool:
    return (
        RuntimeAccountDialog(service, account_id, parent).exec()
        == QDialog.DialogCode.Accepted
    )
