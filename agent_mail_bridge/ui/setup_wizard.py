"""首次使用的简洁配置向导。"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QPushButton, QVBoxLayout

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.ui.settings_store import save_env_values


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
        self.qq_auth = QLineEdit(cfg.qq_auth_code)
        self.qq_auth.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("数据目录", self.data_root)
        form.addRow("Gmail 收件邮箱", self.gmail)
        form.addRow("收件方式", self.backend)
        form.addRow("QQ 发件邮箱（可稍后填写）", self.qq)
        form.addRow("QQ 授权码（可稍后填写）", self.qq_auth)
        layout.addLayout(form)
        save = QPushButton("保存并进入主界面")
        save.clicked.connect(self.accept)
        layout.addWidget(save)

    def accept(self) -> None:
        email = self.gmail.text().strip()
        if not email or "@" not in email:
            return
        save_env_values({
            "DATA_ROOT": self.data_root.text().strip(),
            "GMAIL_ADDRESS": email,
            "OWNER_GMAIL": email,
            "GMAIL_RECEIVE_BACKEND": self.backend.text().strip() or "gmail_api",
            "QQ_EMAIL": self.qq.text().strip(),
            "QQ_AUTH_CODE": self.qq_auth.text(),
        })
        super().accept()
