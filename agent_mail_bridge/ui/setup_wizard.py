"""首次使用向导，复用正式账号管理组件与业务逻辑。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.ui.account_management import AccountTypeDialog, open_account_dialog
from agent_mail_bridge.ui.settings_store import import_legacy_env


def needs_setup(cfg: AppConfig) -> bool:
    return not bool(cfg.gmail_address and cfg.owner_gmail)


class SetupWizard(QDialog):
    """首次启动只负责引导，账号保存与后续编辑使用同一套实现。"""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.service = ApplicationService(cfg)
        self.setWindowTitle("首次配置向导")
        self.setMinimumSize(620, 430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 22)
        layout.setSpacing(13)

        title = QLabel("欢迎使用 AgentMailBridge")
        title.setObjectName("pageTitle")
        hint = QLabel(
            "先配置一个具备收件能力的账号（当前阶段为 Gmail）；"
            "QQ 邮箱发件能力可以现在配置，也可以稍后添加。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        self.gmail_status = QLabel()
        self.qq_status = QLabel()
        layout.addWidget(self.gmail_status)
        gmail = QPushButton("配置 Gmail 邮箱账号")
        gmail.setObjectName("primaryButton")
        gmail.setMinimumHeight(48)
        gmail.clicked.connect(lambda: self.configure(AccountTypeDialog.GMAIL))
        layout.addWidget(gmail)
        layout.addWidget(self.qq_status)
        qq = QPushButton("配置 QQ 邮箱账号（可选）")
        qq.setObjectName("outlinePurple")
        qq.setMinimumHeight(44)
        qq.clicked.connect(lambda: self.configure(AccountTypeDialog.QQ))
        layout.addWidget(qq)

        migrate = QPushButton("从旧版 .env 迁移配置")
        migrate.setObjectName("textButton")
        migrate.clicked.connect(self.import_legacy_config)
        layout.addWidget(migrate, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        enter = QPushButton("完成并进入主界面")
        enter.setObjectName("primaryButton")
        enter.clicked.connect(self.accept)
        layout.addWidget(enter, 0, Qt.AlignmentFlag.AlignRight)
        self.refresh_status()

    def refresh_status(self) -> None:
        gmail_ready = bool(self.cfg.gmail_address and self.cfg.owner_gmail)
        qq_ready = bool(self.cfg.qq_email and self.cfg.qq_auth_code)
        self.gmail_status.setText(
            f"Gmail 邮箱账号：{self.cfg.gmail_address}" if gmail_ready else "Gmail 邮箱账号：未配置（当前收件必需）"
        )
        self.gmail_status.setObjectName("successText" if gmail_ready else "hint")
        self.qq_status.setText(
            f"QQ 邮箱账号：{self.cfg.qq_email}" if qq_ready else "QQ 邮箱账号：未配置（可选）"
        )
        self.qq_status.setObjectName("successText" if qq_ready else "hint")
        for label in (self.gmail_status, self.qq_status):
            label.style().unpolish(label)
            label.style().polish(label)

    def configure(self, account_type: str) -> None:
        open_account_dialog(self.service, account_type, self)
        self.refresh_status()

    def import_legacy_config(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择旧版 .env",
            "",
            "环境配置 (.env *.env);;所有文件 (*)",
        )
        if not source:
            return
        try:
            result = import_legacy_env(Path(source))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "旧配置导入失败", str(exc))
            return
        QMessageBox.information(
            self,
            "旧配置已导入",
            f"已导入 {len(result.imported_keys)} 项非敏感配置，并安全迁移凭据。",
        )
        super().accept()

    def accept(self) -> None:
        if not (self.cfg.gmail_address and self.cfg.owner_gmail):
            QMessageBox.warning(self, "尚未完成", "请先配置当前支持的 Gmail 收件能力。")
            return
        super().accept()
