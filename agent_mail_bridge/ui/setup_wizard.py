"""首次使用的简洁配置向导。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.credentials import CredentialError, CredentialService, QQ_SMTP_SECRET
from agent_mail_bridge.ui.settings_store import import_legacy_env, save_env_values


def needs_setup(cfg: AppConfig) -> bool:
    return not bool(cfg.gmail_address and cfg.owner_gmail)


class SetupWizard(QDialog):
    """只收集首次必需配置，其他高级项可稍后在主窗口完成。"""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("首次配置向导")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.data_root = QLineEdit(str(cfg.data_root_path))
        self.gmail = QLineEdit(cfg.gmail_address)
        self.backend = QLineEdit(cfg.gmail_receive_backend)
        self.qq = QLineEdit(cfg.qq_email)
        self.qq_auth = QLineEdit()
        self.qq_auth.setEchoMode(QLineEdit.EchoMode.Password)
        self.qq_auth.setPlaceholderText("已配置则留空；不会回显旧授权码")
        form.addRow("数据目录", self.data_root)
        form.addRow("Gmail 收件邮箱", self.gmail)
        form.addRow("收件方式", self.backend)
        form.addRow("QQ 发件邮箱（可稍后填写）", self.qq)
        form.addRow("QQ 授权码（可稍后填写）", self.qq_auth)
        layout.addLayout(form)
        save = QPushButton("保存并进入主界面")
        save.clicked.connect(self.accept)
        layout.addWidget(save)
        migrate = QPushButton("导入旧版 .env")
        migrate.clicked.connect(self.import_legacy_config)
        layout.addWidget(migrate)

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
        email = self.gmail.text().strip()
        if not email or "@" not in email:
            return
        qq_auth = self.qq_auth.text().strip()
        if qq_auth:
            try:
                CredentialService().set(QQ_SMTP_SECRET, qq_auth)
            except CredentialError as exc:
                QMessageBox.warning(self, "凭据保存失败", str(exc))
                return
        try:
            save_env_values({
                "DATA_ROOT": self.data_root.text().strip(),
                "GMAIL_ADDRESS": email,
                "OWNER_GMAIL": email,
                "GMAIL_RECEIVE_BACKEND": self.backend.text().strip() or "gmail_api",
                "QQ_EMAIL": self.qq.text().strip(),
                "QQ_AUTH_CODE": "",
            })
        except OSError as exc:
            QMessageBox.warning(self, "配置保存失败", str(exc))
            return
        super().accept()
