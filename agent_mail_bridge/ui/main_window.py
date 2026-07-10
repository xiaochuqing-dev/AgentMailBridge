"""基于 PySide6 的 AgentMailBridge 正式桌面主窗口。"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSpinBox,
    QStackedWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.models import OperationStatus, ReceiveResult, SendResult, ServiceResult
from agent_mail_bridge.security import SecurityError, assert_within_allowed_roots
from agent_mail_bridge.ui.settings_store import save_env_values
from agent_mail_bridge.ui.theme import (
    DANGER,
    PURPLE,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
)
from agent_mail_bridge.ui.widgets import (
    AccountCard,
    DataTable,
    MessageBar,
    NavButton,
    StatCard,
    StatusRow,
    TipRow,
    ToggleSwitch,
    draw_status_dot,
    format_size,
    horizontal_line,
    paint_app_icon,
)

AUTO_RECEIVE_DEFAULT_MINUTES = 3  # 自动收件默认间隔，单位：分钟
PREVIEW_MAX_BYTES = 128 * 1024  # 文本预览上限，单位：字节
SAFE_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".py", ".toml", ".ini"}
SAFE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def _fill_background(widget: QWidget, color: str) -> None:
    """使用调色板填充背景，避免局部样式表污染子控件。"""
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(color))
    widget.setAutoFillBackground(True)
    widget.setPalette(palette)


class _ValueSink:
    """兼容旧骨架测试使用的 set 接口。"""

    def __init__(self, setter: Callable[[str], None]):
        self.setter = setter
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value
        self.setter(value)

    def get(self) -> str:
        return self.value


class _TaskSignals(QObject):
    finished = Signal(object)


class _TaskRunner(QRunnable):
    """在线程池中执行应用服务调用。"""

    def __init__(self, operation: Callable[[], ServiceResult]):
        super().__init__()
        self.operation = operation
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as exc:
            result = ServiceResult(OperationStatus.FAILED, message=str(exc))
        self.signals.finished.emit(result)


class TitleBar(QWidget):
    """参考设计图实现的无边框窗口标题栏。"""

    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self.window_ref = window
        self.drag_position: QPoint | None = None
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(self, "#FFFFFF")
        self.setFixedHeight(56)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(8)

        icon = QLabel()
        paint_app_icon(icon)
        title = QLabel("Agent 邮箱桥接工具")
        title.setObjectName("appTitle")
        version = QLabel("v1.0.0")
        version.setObjectName("version")
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(version)
        layout.addStretch(1)

        status_box = QWidget()
        status_layout = QHBoxLayout(status_box)
        status_layout.setContentsMargins(12, 0, 12, 0)
        status_layout.setSpacing(5)
        status_box.setStyleSheet("background: #ECFAF1; border-radius: 15px;")
        status_box.setFixedHeight(30)
        status_layout.addWidget(draw_status_dot())
        self.status_label = QLabel("服务已启动")
        self.status_label.setObjectName("successText")
        self.status_label.setStyleSheet(f"color: {SUCCESS}; font-size: 11px; font-weight: 700;")
        status_layout.addWidget(self.status_label)
        layout.addWidget(status_box)
        layout.addStretch(1)

        theme_button = QPushButton("日  月")
        theme_button.setObjectName("titleButton")
        theme_button.setFixedSize(64, 30)
        theme_button.setToolTip("当前使用设计图浅色主题")
        theme_button.clicked.connect(self._show_theme_notice)
        layout.addWidget(theme_button)

        minimize = QPushButton("—")
        maximize = QPushButton("□")
        close = QPushButton("×")
        for button in (minimize, maximize):
            button.setObjectName("titleButton")
            button.setFixedSize(42, 38)
        close.setObjectName("closeButton")
        close.setFixedSize(42, 38)
        minimize.clicked.connect(window.showMinimized)
        maximize.clicked.connect(self._toggle_maximized)
        close.clicked.connect(window.close)
        layout.addWidget(minimize)
        layout.addWidget(maximize)
        layout.addWidget(close)

    def _show_theme_notice(self) -> None:
        if hasattr(self.window_ref, "show_message"):
            self.window_ref.show_message("当前按设计图使用浅色主题", "normal")

    def _toggle_maximized(self) -> None:
        if self.window_ref.isMaximized():
            self.window_ref.showNormal()
        else:
            self.window_ref.showMaximized()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self.window_ref.isMaximized():
                self.window_ref.move(event.globalPosition().toPoint() - self.drag_position)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self.drag_position = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximized()
        super().mouseDoubleClickEvent(event)


class BridgeWindow(QMainWindow):
    """正式界面只组织交互，业务能力全部调用 ApplicationService。"""

    def __init__(self, service: ApplicationService):
        super().__init__()
        self.service = service
        self.task_active = False
        self._active_runner: _TaskRunner | None = None
        self._task_callback: Callable[[ServiceResult], None] | None = None
        self.closed = False
        self.thread_pool = QThreadPool.globalInstance()
        self.task_buttons: list[QPushButton] = []
        self.file_rows: list[dict] = []
        self.log_rows: list[dict] = []
        self.history_rows: dict[str, list[dict]] = {"received": [], "sent": []}
        self.mcp_rows: list[dict] = []
        self.selected_send_path = ""
        self.status_var = _ValueSink(lambda value: self.show_message(value, "working"))
        self.error_var = _ValueSink(lambda value: self.show_message(value, "error"))
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._automatic_receive)
        self.setWindowTitle("Agent 邮箱桥接工具")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1272, 900)
        self.setMinimumSize(1120, 760)
        self._build()
        self._load_auto_receive_preferences()
        QTimer.singleShot(0, self.refresh)

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("windowRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(root, "#FFFFFF")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        body = QWidget()
        body.setObjectName("bodySurface")
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(body, "#FFFFFF")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.sidebar = self._build_sidebar()
        self.central_panel = self._build_central_panel()
        self.right_panel = self._build_right_panel()
        body_layout.addWidget(self.sidebar)
        body_layout.addWidget(self.central_panel, 1)
        body_layout.addWidget(self.right_panel)
        outer.addWidget(body, 1)

        self.size_grip = QSizeGrip(root)
        self.size_grip.setFixedSize(16, 16)
        self.size_grip.raise_()

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidebar")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(panel, "#FFFFFF")
        panel.setFixedWidth(230)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 14, 10, 12)
        layout.setSpacing(8)

        add_account = QPushButton("＋  添加邮箱账号")
        add_account.setObjectName("primaryButton")
        add_account.setFixedHeight(40)
        add_account.clicked.connect(lambda: self.show_message("当前版本支持一个 Gmail 收件账号和一个 QQ 发件账号"))
        layout.addWidget(add_account)
        layout.addSpacing(3)

        label = QLabel("我的邮箱账号")
        label.setObjectName("fieldLabel")
        layout.addWidget(label)
        self.gmail_card = AccountCard("M", "Gmail（收件邮箱）", "未配置", "作为主要收件邮箱｜自动收取", "#EA4335")
        self.qq_card = AccountCard("Q", "QQ 邮箱（发件）", "未配置", "用于发送邮件附件", "#21A4E8")
        self.gmail_card.clicked.connect(lambda: self.select_page("basic"))
        self.qq_card.clicked.connect(lambda: self.select_page("advanced"))
        layout.addWidget(self.gmail_card)
        layout.addWidget(self.qq_card)

        identity = QPushButton("Q   设置发件身份                              ›")
        identity.setObjectName("outlinePurple")
        identity.setFixedHeight(42)
        identity.clicked.connect(lambda: self.select_page("advanced"))
        layout.addWidget(identity)
        layout.addSpacing(12)

        self.nav_buttons: dict[str, NavButton] = {}
        nav_items = (
            ("dashboard", "●", "仪表盘"),
            ("basic", "◆", "账号与配置"),
            ("inbox", "□", "收邮箱"),
            ("send", "▷", "发邮件"),
            ("history", "○", "历史记录"),
            ("logs", "≡", "日志"),
            ("agent", "◇", "Agent 接口"),
        )
        for key, icon, text in nav_items:
            button = NavButton(icon, text)
            button.clicked.connect(lambda checked=False, page=key: self.select_page(page))
            self.nav_buttons[key] = button
            layout.addWidget(button)
        self.nav_buttons["basic"].setChecked(True)
        layout.addStretch(1)

        settings = NavButton("◆", "设置")
        about = NavButton("i", "关于")
        settings.clicked.connect(lambda: self.select_page("advanced"))
        about.clicked.connect(self._show_about)
        layout.addWidget(settings)
        layout.addWidget(about)
        return panel

    def _build_central_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("centralPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(panel, "#FFFFFF")
        panel.setMinimumWidth(620)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        tabs = QWidget()
        tabs.setObjectName("tabBar")
        tabs.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        tabs.setFixedHeight(52)
        _fill_background(tabs, "#FFFFFF")
        tab_layout = QHBoxLayout(tabs)
        tab_layout.setContentsMargins(23, 0, 0, 0)
        tab_layout.setSpacing(4)
        self.tab_buttons: dict[str, QPushButton] = {}
        for key, text in (("basic", "基础配置"), ("inbox", "收邮箱"), ("send", "发邮件"), ("advanced", "高级设置")):
            button = QPushButton(text)
            button.setObjectName("tabButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda checked=False, page=key: self.select_page(page))
            self.tab_buttons[key] = button
            tab_layout.addWidget(button)
        self.tab_buttons["basic"].setChecked(True)
        tab_layout.addStretch(1)
        layout.addWidget(tabs)

        self.page_stack = QStackedWidget()
        self.pages = {
            "basic": self._build_basic_page(),
            "inbox": self._build_inbox_page(),
            "send": self._build_send_page(),
            "advanced": self._build_advanced_page(),
            "history": self._build_history_page(),
            "logs": self._build_logs_page(),
            "agent": self._build_agent_page(),
        }
        for page in self.pages.values():
            self.page_stack.addWidget(page)
        layout.addWidget(self.page_stack, 1)
        return panel

    def _build_basic_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("pageSurface")
        _fill_background(content, "#FFFFFF")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 17, 20, 17)
        layout.setSpacing(9)

        title = QLabel("邮箱连接与快捷操作")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        first_row = QHBoxLayout()
        first_row.setSpacing(14)
        backend_box, self.backend_combo = self._field_combo(
            "邮箱类型",
            (("Gmail（自动选择）", "auto"), ("Gmail IMAP", "imap"), ("Gmail API", "gmail_api")),
        )
        email_box, self.gmail_email_edit = self._field_edit("邮箱地址（收件邮箱）", self.service.cfg.gmail_address)
        first_row.addWidget(backend_box, 1)
        first_row.addWidget(email_box, 1)
        layout.addLayout(first_row)

        password_box, self.gmail_password_edit = self._field_edit(
            "应用专用密码 / 授权状态", self.service.cfg.gmail_app_password, password=True
        )
        layout.addWidget(password_box)

        option_row = QHBoxLayout()
        option_row.setSpacing(12)
        auto_label = QLabel("自动收取")
        auto_label.setObjectName("fieldLabel")
        self.auto_switch = ToggleSwitch()
        self.auto_switch.toggled.connect(self._toggle_auto_receive)
        interval_label = QLabel("检查间隔")
        interval_label.setObjectName("fieldLabel")
        self.interval_combo = QComboBox()
        for minutes in (1, 3, 5, 10, 30):
            suffix = "（推荐）" if minutes == AUTO_RECEIVE_DEFAULT_MINUTES else ""
            self.interval_combo.addItem(f"每 {minutes} 分钟{suffix}", minutes)
        self.interval_combo.currentIndexChanged.connect(self._reschedule_auto_receive)
        self.interval_combo.setFixedWidth(170)
        option_row.addWidget(auto_label)
        option_row.addWidget(self.auto_switch)
        option_row.addStretch(1)
        option_row.addWidget(interval_label)
        option_row.addWidget(self.interval_combo)
        layout.addLayout(option_row)

        rule_row = QHBoxLayout()
        rule_label = QLabel("可接收件人规则")
        rule_label.setObjectName("fieldLabel")
        self.self_mail_check = QCheckBox("仅收取本人 Gmail 且重要的邮件")
        self.self_mail_check.setChecked(self.service.cfg.auto_receive_only_self_mail)
        help_label = QLabel("ⓘ")
        help_label.setToolTip("只接收可信 Gmail 自发自收邮件，避免结果邮件形成循环")
        help_label.setStyleSheet(f"color: {TEXT_MUTED};")
        rule_row.addWidget(rule_label)
        rule_row.addSpacing(10)
        rule_row.addWidget(self.self_mail_check)
        rule_row.addStretch(1)
        rule_row.addWidget(help_label)
        layout.addLayout(rule_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        test_button = self._button("●  测试连接", self.test_connection, primary=True)
        save_button = self._button("□  保存配置", self.save_basic_config)
        self.receive_button = self._button("↓  手动收取", self.receive)
        self.send_button = self._button("▷  手动选择发送", self.choose_and_send)
        mcp_button = self._button(
            "◇  Agent 提交结果（MCP）",
            lambda: self.select_page("agent"),
            outline=True,
        )
        self.task_buttons.extend((test_button, self.receive_button, self.send_button))
        for button in (test_button, save_button, self.receive_button, self.send_button, mcp_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.message_bar = MessageBar()
        layout.addWidget(self.message_bar)
        layout.addWidget(horizontal_line())

        file_header = QHBoxLayout()
        file_title = QLabel("今日收到文件")
        file_title.setObjectName("sectionTitle")
        file_hint = QLabel("（保存至已接收文件夹中）")
        file_hint.setObjectName("hint")
        view_button = self._button("查  查看今日文件 / 最新筛选", self.select_latest_file, text_only=True)
        open_button = self._button("□  打开今日接收文件夹", self.open_today_folder)
        file_header.addWidget(file_title)
        file_header.addWidget(file_hint)
        file_header.addStretch(1)
        file_header.addWidget(view_button)
        file_header.addWidget(open_button)
        layout.addLayout(file_header)

        self.files_table = DataTable(["文件名", "大小", "保存路径", "收取时间", "操作"])
        self.files_table.setFixedHeight(146)
        self.files_table.cellDoubleClicked.connect(self._preview_table_file)
        self.files_table.cellClicked.connect(self._file_action_clicked)
        self._configure_file_table(self.files_table)
        layout.addWidget(self.files_table)
        file_note = QLabel("ⓘ  文件接收后会自动按规则保存至此目录；危险附件只保存并标记，不会自动执行。")
        file_note.setObjectName("hint")
        layout.addWidget(file_note)
        layout.addWidget(horizontal_line())

        log_header = QHBoxLayout()
        log_title = QLabel("最近日志")
        log_title.setObjectName("sectionTitle")
        more_logs = self._button("查看更多日志  →", lambda: self.select_page("logs"), text_only=True)
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        log_header.addWidget(more_logs)
        layout.addLayout(log_header)
        self.logs_table = DataTable(["时间", "级别", "消息"])
        self.logs_table.setFixedHeight(152)
        self._configure_log_table(self.logs_table)
        layout.addWidget(self.logs_table)
        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _build_inbox_page(self) -> QWidget:
        page, layout = self._standard_page("收邮箱", "查看今日接收文件，并可安全预览或定位到目录。")
        tools = QHBoxLayout()
        self.inbox_search = QLineEdit()
        self.inbox_search.setPlaceholderText("搜索文件名或路径")
        self.inbox_search.textChanged.connect(self._filter_inbox)
        receive = self._button("↓  立即收取", self.receive, primary=True)
        refresh = self._button("刷新", self.refresh)
        self.task_buttons.append(receive)
        tools.addWidget(self.inbox_search, 1)
        tools.addWidget(receive)
        tools.addWidget(refresh)
        layout.addLayout(tools)
        self.inbox_table = DataTable(["文件名", "大小", "保存路径", "收取时间", "状态"])
        self.inbox_table.cellDoubleClicked.connect(self._preview_inbox_file)
        self._configure_file_table(self.inbox_table)
        layout.addWidget(self.inbox_table, 1)
        return page

    def _build_send_page(self) -> QWidget:
        page, layout = self._standard_page("发邮件", "使用 QQ 邮箱把白名单目录内的本地结果发送到绑定 Gmail。")
        card = QFrame()
        card.setObjectName("card")
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 17, 20, 18)
        form.setSpacing(10)
        source_label = QLabel("待发送文件")
        source_label.setObjectName("fieldLabel")
        source_row = QHBoxLayout()
        self.send_path_edit = QLineEdit()
        self.send_path_edit.setReadOnly(True)
        self.send_path_edit.setPlaceholderText("请选择 DATA_ROOT 或允许目录内的文件")
        choose = self._button("选择文件", self.choose_send_file)
        source_row.addWidget(self.send_path_edit, 1)
        source_row.addWidget(choose)
        self.subject_edit = QLineEdit()
        self.subject_edit.setPlaceholderText("可选；留空时使用默认主题")
        self.recipient_edit = QLineEdit(self.service.cfg.owner_gmail)
        self.recipient_edit.setReadOnly(True)
        send = self._button("▷  发送到绑定 Gmail", self.send_selected_file, primary=True)
        self.task_buttons.append(send)
        form.addWidget(source_label)
        form.addLayout(source_row)
        form.addWidget(QLabel("邮件主题"))
        form.addWidget(self.subject_edit)
        form.addWidget(QLabel("固定收件人"))
        form.addWidget(self.recipient_edit)
        form.addWidget(send, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        history_title = QLabel("最近发送记录")
        history_title.setObjectName("sectionTitle")
        layout.addWidget(history_title)
        self.sent_table = DataTable(["文件", "主题", "收件人", "发送时间", "状态"])
        layout.addWidget(self.sent_table, 1)
        return page

    def _build_advanced_page(self) -> QWidget:
        page, layout = self._standard_page("高级设置", "配置 QQ 发件身份、网络模式和 Gmail API 授权。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        qq_box, self.qq_email_edit = self._field_edit("QQ 邮箱（发件身份）", self.service.cfg.qq_email)
        auth_box, self.qq_auth_edit = self._field_edit("QQ SMTP 授权码", self.service.cfg.qq_auth_code, password=True)
        network_box, self.network_combo = self._field_combo(
            "Gmail 网络模式", (("自动选择", "auto"), ("直连", "direct"), ("SOCKS5", "socks5"))
        )
        data_box, self.data_root_edit = self._field_edit("本地数据目录（只读）", str(self.service.cfg.data_root_path))
        self.data_root_edit.setReadOnly(True)
        grid.addWidget(qq_box, 0, 0)
        grid.addWidget(auth_box, 0, 1)
        grid.addWidget(network_box, 1, 0)
        grid.addWidget(data_box, 1, 1)
        layout.addLayout(grid)

        limits = QHBoxLayout()
        limits.addWidget(QLabel("单次最多收取"))
        self.fetch_limit_spin = QSpinBox()
        self.fetch_limit_spin.setRange(1, 200)
        self.fetch_limit_spin.setValue(self.service.cfg.max_fetch_limit)
        self.fetch_limit_spin.setSuffix(" 封")
        limits.addWidget(self.fetch_limit_spin)
        limits.addSpacing(12)
        limits.addWidget(QLabel("最大发送文件"))
        self.send_limit_spin = QSpinBox()
        self.send_limit_spin.setRange(1, 100)
        self.send_limit_spin.setValue(self.service.cfg.max_send_file_mb)
        self.send_limit_spin.setSuffix(" MB")
        limits.addWidget(self.send_limit_spin)
        limits.addStretch(1)
        layout.addLayout(limits)

        actions = QHBoxLayout()
        save = self._button("保存高级设置", self.save_advanced_config, primary=True)
        auth = self._button("Gmail API 显式授权", self.authorize_gmail_api)
        imap = self._button("诊断 IMAP", lambda: self._diagnose("正在诊断 Gmail IMAP", self.service.diagnose_imap))
        api = self._button("诊断 Gmail API", lambda: self._diagnose("正在诊断 Gmail API", self.service.diagnose_gmail_api))
        smtp = self._button("诊断 QQ SMTP", lambda: self._diagnose("正在诊断 QQ SMTP", self.service.diagnose_qq_smtp))
        self.task_buttons.extend((auth, imap, api, smtp))
        for button in (save, auth, imap, api, smtp):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addWidget(horizontal_line())

        status_title = QLabel("当前脱敏配置")
        status_title.setObjectName("sectionTitle")
        layout.addWidget(status_title)
        self.config_summary = QTextEdit()
        self.config_summary.setReadOnly(True)
        self.config_summary.setMinimumHeight(220)
        layout.addWidget(self.config_summary, 1)
        return page

    def _build_history_page(self) -> QWidget:
        page, layout = self._standard_page("历史记录", "汇总最近接收与发送记录，越界旧路径不会开放。")
        self.history_table = DataTable(["方向", "主题 / 文件", "时间", "状态", "本地路径"])
        layout.addWidget(self.history_table, 1)
        return page

    def _build_logs_page(self) -> QWidget:
        page, layout = self._standard_page("日志", "查看应用服务产生的结构化事件，不显示密码或 OAuth token。")
        tools = QHBoxLayout()
        self.log_filter = QComboBox()
        self.log_filter.addItems(["全部级别", "INFO", "SUCCESS", "WARNING", "ERROR"])
        self.log_filter.currentTextChanged.connect(self._populate_full_logs)
        refresh = self._button("刷新", self.refresh)
        tools.addWidget(self.log_filter)
        tools.addStretch(1)
        tools.addWidget(refresh)
        layout.addLayout(tools)
        self.full_logs_table = DataTable(["时间", "级别", "事件", "消息"])
        self._configure_log_table(self.full_logs_table, full=True)
        layout.addWidget(self.full_logs_table, 1)
        return page

    def _build_agent_page(self) -> QWidget:
        page, layout = self._standard_page(
            "Agent 接口",
            "本机 stdio MCP 只允许提交白名单目录内的结果文件，收件人固定。",
        )
        status_card = QFrame()
        status_card.setObjectName("card")
        status_grid = QGridLayout(status_card)
        status_grid.setContentsMargins(18, 14, 18, 14)
        status_grid.setHorizontalSpacing(18)
        status_grid.setVerticalSpacing(8)
        status_grid.addWidget(QLabel("MCP 状态"), 0, 0)
        self.mcp_status_label = QLabel("可用 · stdio · 仅本机")
        self.mcp_status_label.setStyleSheet(f"color: {SUCCESS}; font-weight: 700;")
        status_grid.addWidget(self.mcp_status_label, 0, 1)
        status_grid.addWidget(QLabel("固定 Gmail"), 1, 0)
        self.mcp_recipient_label = QLabel(self.service.cfg.owner_gmail or "未配置")
        status_grid.addWidget(self.mcp_recipient_label, 1, 1)
        status_grid.addWidget(QLabel("允许目录"), 2, 0)
        self.mcp_roots_label = QLabel(
            "；".join(str(path) for path in self.service.cfg.effective_allowed_send_roots)
        )
        self.mcp_roots_label.setWordWrap(True)
        status_grid.addWidget(self.mcp_roots_label, 2, 1)
        layout.addWidget(status_card)

        command_title = QLabel("本地启动与 Agent 配置")
        command_title.setObjectName("sectionTitle")
        layout.addWidget(command_title)
        self.mcp_command_text = QTextEdit()
        self.mcp_command_text.setReadOnly(True)
        self.mcp_command_text.setFixedHeight(104)
        self.mcp_command_text.setPlainText(
            "启动命令：python -m agent_mail_bridge.mcp_server\n\n"
            "Codex：codex mcp add agent-mail-bridge -- python -m agent_mail_bridge.mcp_server\n"
            "Claude Code：claude mcp add agent-mail-bridge -- python -m agent_mail_bridge.mcp_server"
        )
        layout.addWidget(self.mcp_command_text)
        actions = QHBoxLayout()
        actions.addWidget(self._button("复制 Codex 配置", lambda: self._copy_mcp_config("codex")))
        actions.addWidget(self._button("复制 Claude Code 配置", lambda: self._copy_mcp_config("claude")))
        actions.addWidget(self._button("刷新调用记录", self.refresh, primary=True))
        actions.addStretch(1)
        layout.addLayout(actions)

        calls_title = QLabel("最近 MCP 调用")
        calls_title.setObjectName("sectionTitle")
        layout.addWidget(calls_title)
        self.mcp_table = DataTable(
            ["调用时间", "request_id", "文件路径", "发送状态", "错误代码"]
        )
        header = self.mcp_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.mcp_table, 1)
        security = QLabel(
            "安全边界：Agent 不能指定收件人、读取凭据、修改邮箱配置、删除文件或扫描任意目录。"
        )
        security.setObjectName("hint")
        security.setWordWrap(True)
        layout.addWidget(security)
        return page

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("rightPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(panel, "#FCFCFE")
        panel.setFixedWidth(306)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 24, 20, 18)
        layout.setSpacing(12)

        title = QLabel("服务状态")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.service_rows = {
            "service": StatusRow("◉", "服务状态", "运行中"),
            "receive": StatusRow("○", "上次收取时间"),
            "send": StatusRow("◇", "上次发件时间"),
            "auto": StatusRow("□", "自动收件状态", "未开启"),
            "qq": StatusRow("Q", "QQ 邮箱", "未配置"),
        }
        for row in self.service_rows.values():
            layout.addWidget(row)
        layout.addWidget(horizontal_line())

        stats_title = QLabel("今日统计")
        stats_title.setObjectName("sectionTitle")
        layout.addWidget(stats_title)
        stats = QGridLayout()
        stats.setSpacing(9)
        self.stat_cards = {
            "received": StatCard("statPurple", "M", "收取附件", PURPLE),
            "saved": StatCard("statGreen", "✓", "保存文件", SUCCESS),
            "sent": StatCard("statBlue", "↑", "发送邮件", "#2394C8"),
            "errors": StatCard("statRed", "⚠", "失败 / 错误", DANGER),
        }
        stats.addWidget(self.stat_cards["received"], 0, 0)
        stats.addWidget(self.stat_cards["saved"], 0, 1)
        stats.addWidget(self.stat_cards["sent"], 1, 0)
        stats.addWidget(self.stat_cards["errors"], 1, 1)
        layout.addLayout(stats)
        layout.addWidget(horizontal_line())

        tips_title = QLabel("快捷提示")
        tips_title.setObjectName("sectionTitle")
        layout.addWidget(tips_title)
        layout.addWidget(TipRow("M", "Gmail 仅自动收取本人发送给本人的邮件，保留重要安全高效。", PURPLE))
        layout.addWidget(TipRow("Q", "发送文件请调用 QQ 发件身份，支持多格式结果传送。", "#329BC5"))
        layout.addWidget(TipRow("!", "如需调整服务或安全设置，请前往“高级设置”进行配置。", WARNING))
        help_button = self._button("查看帮助文档  →", self._show_help, text_only=True)
        layout.addWidget(help_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return panel

    def _standard_page(self, title: str, description: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("pageSurface")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(page, "#FFFFFF")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        hint = QLabel(description)
        hint.setObjectName("hint")
        layout.addWidget(heading)
        layout.addWidget(hint)
        layout.addWidget(horizontal_line())
        return page, layout

    def _field_edit(self, label: str, value: str, *, password: bool = False) -> tuple[QWidget, QLineEdit]:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        title = QLabel(label)
        title.setObjectName("fieldLabel")
        edit = QLineEdit(value)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(title)
        layout.addWidget(edit)
        return box, edit

    def _field_combo(self, label: str, items: tuple[tuple[str, str], ...]) -> tuple[QWidget, QComboBox]:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        title = QLabel(label)
        title.setObjectName("fieldLabel")
        combo = QComboBox()
        for text, value in items:
            combo.addItem(text, value)
        layout.addWidget(title)
        layout.addWidget(combo)
        return box, combo

    def _button(
        self,
        label: str,
        callback: Callable,
        *,
        primary: bool = False,
        outline: bool = False,
        text_only: bool = False,
    ) -> QPushButton:
        button = QPushButton(label)
        if primary:
            button.setObjectName("primaryButton")
        elif outline:
            button.setObjectName("outlinePurple")
        elif text_only:
            button.setObjectName("textButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _configure_file_table(self, table: DataTable) -> None:
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

    def _configure_log_table(self, table: DataTable, *, full: bool = False) -> None:
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        if full:
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def select_page(self, name: str) -> None:
        target = "basic" if name == "dashboard" else name
        if target not in self.pages:
            target = "basic"
        self.page_stack.setCurrentWidget(self.pages[target])
        for key, button in self.tab_buttons.items():
            button.setChecked(key == target)
        nav_target = name if name in self.nav_buttons else target
        for key, button in self.nav_buttons.items():
            button.setChecked(key == nav_target)

    def receive(self) -> None:
        self._run_task("正在收取邮件，请稍候", self.service.receive, self._show_receive_result)

    def choose_and_send(self) -> None:
        if not self.choose_send_file():
            return
        self.select_page("send")

    def choose_send_file(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择待发送文件", str(self.service.cfg.data_root_path), "所有文件 (*.*)"
        )
        if not path:
            self.show_message("已取消选择文件")
            return False
        self.selected_send_path = path
        self.send_path_edit.setText(path)
        self.show_message("文件已选择，请确认主题后发送", "normal")
        return True

    def send_selected_file(self) -> None:
        path = self.send_path_edit.text().strip() or self.selected_send_path
        if not path:
            self.show_message("请先选择待发送文件", "error")
            return
        subject = self.subject_edit.text().strip() or None
        self._run_task(
            "正在发送文件，请勿重复点击",
            lambda: self.service.send_file(Path(path), subject=subject),
            self._show_send_result,
        )

    def test_connection(self) -> None:
        backend = self.backend_combo.currentData()
        if backend == "gmail_api" or (backend == "auto" and self.service.cfg.gmail_api_configured):
            self._diagnose("正在测试 Gmail API 连接", self.service.diagnose_gmail_api)
        else:
            self._diagnose("正在测试 Gmail IMAP 连接", self.service.diagnose_imap)

    def _diagnose(self, title: str, operation: Callable[[], ServiceResult]) -> None:
        self._run_task(title, operation, self._show_service_result)

    def authorize_gmail_api(self) -> None:
        self._run_task("正在进行 Gmail API 授权", self.service.authorize_gmail_api, self._show_service_result)

    def save_basic_config(self) -> None:
        email = self.gmail_email_edit.text().strip()
        backend = str(self.backend_combo.currentData())
        password = self.gmail_password_edit.text()
        if not self._valid_email(email):
            self.show_message("请输入有效的 Gmail 地址", "error")
            return
        if backend == "imap" and not password.strip():
            self.show_message("IMAP 模式需要 Gmail 应用专用密码", "error")
            return
        minutes = int(self.interval_combo.currentData() or AUTO_RECEIVE_DEFAULT_MINUTES)
        try:
            save_env_values(
                {
                    "GMAIL_ADDRESS": email,
                    "OWNER_GMAIL": email,
                    "GMAIL_APP_PASSWORD": password,
                    "GMAIL_RECEIVE_BACKEND": backend,
                    "AUTO_RECEIVE_ONLY_SELF_MAIL": str(self.self_mail_check.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE": str(self.auto_switch.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE_INTERVAL_MINUTES": str(minutes),
                }
            )
        except OSError as exc:
            self.show_message(f"保存配置失败：{exc}", "error")
            return
        self.service.cfg.gmail_address = email
        self.service.cfg.owner_gmail = email
        self.service.cfg.gmail_app_password = password
        self.service.cfg.gmail_receive_backend = backend
        self.service.cfg.auto_receive_only_self_mail = self.self_mail_check.isChecked()
        self.recipient_edit.setText(email)
        self.refresh()
        self.show_message("配置已安全保存并在当前运行中生效", "success")

    def save_advanced_config(self) -> None:
        qq_email = self.qq_email_edit.text().strip()
        qq_auth = self.qq_auth_edit.text()
        if qq_email and not self._valid_email(qq_email):
            self.show_message("请输入有效的 QQ 邮箱地址", "error")
            return
        if bool(qq_email) != bool(qq_auth.strip()):
            self.show_message("QQ 邮箱和 SMTP 授权码必须同时填写", "error")
            return
        network_mode = str(self.network_combo.currentData())
        try:
            save_env_values(
                {
                    "QQ_EMAIL": qq_email,
                    "QQ_AUTH_CODE": qq_auth,
                    "GMAIL_NETWORK_MODE": network_mode,
                    "MAX_FETCH_LIMIT": str(self.fetch_limit_spin.value()),
                    "MAX_SEND_FILE_MB": str(self.send_limit_spin.value()),
                }
            )
        except OSError as exc:
            self.show_message(f"保存高级设置失败：{exc}", "error")
            return
        self.service.cfg.qq_email = qq_email
        self.service.cfg.qq_auth_code = qq_auth
        self.service.cfg.gmail_network_mode = network_mode
        self.service.cfg.max_fetch_limit = self.fetch_limit_spin.value()
        self.service.cfg.max_send_file_mb = self.send_limit_spin.value()
        self.refresh()
        self.show_message("高级设置已安全保存", "success")

    def refresh(self) -> None:
        if self.task_active:
            self.show_message("当前任务尚未完成", "working")
            return
        try:
            status = self.service.get_config_and_connection_status().details
            self.file_rows = self.service.get_today_files().details.get("files", [])
            self.log_rows = self.service.get_recent_logs(100).details.get("events", [])
            self.history_rows = self.service.get_history(100).details
            self.mcp_rows = self.service.get_mcp_history(100).details.get("calls", [])
        except Exception as exc:
            self.show_message(f"刷新界面失败：{exc}", "error")
            return
        self._apply_config_to_controls(status)
        self._populate_files(self.files_table, self.file_rows, actions=True)
        self._populate_files(self.inbox_table, self.file_rows, actions=False)
        self._populate_logs(self.logs_table, self.log_rows[:30])
        self._populate_full_logs()
        self._populate_sent_history()
        self._populate_history()
        self._populate_mcp_history()
        self._update_right_panel(status)
        self.show_message("状态已刷新", "normal")

    def _apply_config_to_controls(self, status: dict) -> None:
        cfg = self.service.cfg
        self.gmail_card.email_label.setText(cfg.gmail_address or "未配置")
        self.qq_card.email_label.setText(cfg.qq_email or "未配置")
        self.recipient_edit.setText(cfg.owner_gmail or cfg.gmail_address)
        self._set_combo_data(self.backend_combo, cfg.gmail_receive_backend)
        self._set_combo_data(self.network_combo, cfg.gmail_network_mode)
        masked = status.get("config", {})
        summary_lines = [
            f"收件后端：{status.get('receive_backend', '—')}",
            f"Gmail：{masked.get('gmail_address') or '未配置'}",
            f"Gmail 密钥：{masked.get('gmail_app_password') or '未配置'}",
            f"Gmail API：{status.get('gmail_api', {}).get('state', '—')}",
            f"QQ 邮箱：{masked.get('qq_email') or '未配置'}",
            f"QQ 授权码：{masked.get('qq_auth_code') or '未配置'}",
            f"网络模式：{masked.get('gmail_network_mode', '—')}",
            f"数据目录：{masked.get('data_root', '—')}",
            f"允许发送目录：{', '.join(masked.get('allowed_send_roots', []))}",
            f"Gmail API 权限：{masked.get('gmail_api_scopes', '—')}",
        ]
        self.config_summary.setPlainText("\n".join(summary_lines))

    def _update_right_panel(self, status: dict) -> None:
        backend = status.get("receive_backend", "—")
        oauth = status.get("gmail_api", {}).get("state", "—")
        qq = status.get("qq_smtp", "not_configured")
        self.service_rows["service"].set_value("● 运行中", success=True)
        self.service_rows["auto"].set_value("已开启" if self.auto_switch.isChecked() else "未开启", success=self.auto_switch.isChecked())
        qq_text = self.service.cfg.qq_email or "未配置"
        self.service_rows["qq"].set_value(qq_text, success=qq == "configured")
        receive_time = self._latest_event_time(("receive", "收件"))
        send_time = self._latest_event_time(("send", "sent", "发件", "发送"))
        self.service_rows["receive"].set_value(receive_time)
        self.service_rows["send"].set_value(send_time)
        self.title_bar.status_label.setText(f"服务已启动 · {backend}")
        self.title_bar.status_label.setToolTip(f"Gmail API：{oauth}")

        today = datetime.now().strftime("%Y-%m-%d")
        sent_today = sum(1 for row in self.history_rows.get("sent", []) if str(row.get("sent_at", "")).startswith(today) and row.get("status") in {"sent", "success"})
        error_today = sum(1 for row in self.log_rows if str(row.get("created_at", "")).startswith(today) and str(row.get("level", "")).upper() in {"ERROR", "FAILED"})
        self.stat_cards["received"].set_count(len(self.file_rows))
        self.stat_cards["saved"].set_count(sum(1 for row in self.file_rows if row.get("status") in {"saved", "ok", "normal"}))
        self.stat_cards["sent"].set_count(sent_today)
        self.stat_cards["errors"].set_count(error_today)

    def _populate_files(self, table: DataTable, rows: list[dict], *, actions: bool) -> None:
        table.setRowCount(0)
        for row_index, row in enumerate(rows):
            table.insertRow(row_index)
            path = str(row.get("saved_path") or row.get("body_file_path") or "")
            values = [
                str(row.get("saved_filename") or Path(path).name or "未命名文件"),
                format_size(row.get("size_bytes")),
                path,
                self._short_time(row.get("created_at") or row.get("received_at")),
                "复制路径  ·  预览  ·  打开" if actions else str(row.get("status") or "saved"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                if column == 4 and actions:
                    item.setForeground(QColor(PURPLE))
                table.setItem(row_index, column, item)

    def _populate_logs(self, table: DataTable, rows: list[dict]) -> None:
        table.setRowCount(0)
        for row_index, row in enumerate(rows):
            table.insertRow(row_index)
            level = str(row.get("level", "INFO")).upper()
            values = [self._short_time(row.get("created_at"), include_date=True), level, str(row.get("message", ""))]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setForeground(QColor(self._level_color(level)))
                table.setItem(row_index, column, item)

    def _populate_full_logs(self) -> None:
        if not hasattr(self, "full_logs_table"):
            return
        selected = self.log_filter.currentText()
        rows = self.log_rows
        if selected != "全部级别":
            rows = [row for row in rows if str(row.get("level", "")).upper() == selected]
        self.full_logs_table.setRowCount(0)
        for index, row in enumerate(rows):
            self.full_logs_table.insertRow(index)
            level = str(row.get("level", "INFO")).upper()
            values = [
                self._short_time(row.get("created_at"), include_date=True),
                level,
                str(row.get("event_type", "")),
                str(row.get("message", "")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setForeground(QColor(self._level_color(level)))
                self.full_logs_table.setItem(index, column, item)

    def _populate_sent_history(self) -> None:
        rows = self.history_rows.get("sent", [])
        self.sent_table.setRowCount(0)
        for index, row in enumerate(rows):
            self.sent_table.insertRow(index)
            path = str(row.get("source_path") or row.get("sent_copy_path") or "")
            values = [
                Path(path).name or "—",
                str(row.get("subject") or "—"),
                str(row.get("to_email") or "—"),
                self._short_time(row.get("sent_at"), include_date=True),
                str(row.get("status") or "—"),
            ]
            for column, value in enumerate(values):
                self.sent_table.setItem(index, column, QTableWidgetItem(value))

    def _populate_history(self) -> None:
        received = [("收件", row) for row in self.history_rows.get("received", [])]
        sent = [("发件", row) for row in self.history_rows.get("sent", [])]
        combined = received + sent
        combined.sort(key=lambda pair: str(pair[1].get("created_at") or pair[1].get("sent_at") or pair[1].get("received_at") or ""), reverse=True)
        self.history_table.setRowCount(0)
        for index, (direction, row) in enumerate(combined):
            self.history_table.insertRow(index)
            path = str(row.get("body_file_path") or row.get("source_path") or row.get("sent_copy_path") or "")
            title = str(row.get("subject") or Path(path).name or "—")
            time_value = row.get("created_at") or row.get("sent_at") or row.get("received_at")
            values = [direction, title, self._short_time(time_value, include_date=True), str(row.get("status") or "—"), path]
            for column, value in enumerate(values):
                self.history_table.setItem(index, column, QTableWidgetItem(value))

    def _populate_mcp_history(self) -> None:
        if not hasattr(self, "mcp_table"):
            return
        self.mcp_recipient_label.setText(self.service.cfg.owner_gmail or "未配置")
        self.mcp_roots_label.setText(
            "；".join(str(path) for path in self.service.cfg.effective_allowed_send_roots)
        )
        self.mcp_table.setRowCount(0)
        for index, row in enumerate(self.mcp_rows):
            self.mcp_table.insertRow(index)
            values = [
                self._short_time(row.get("created_at"), include_date=True),
                str(row.get("request_id") or "—"),
                str(row.get("file_path") or "已隐藏越界路径"),
                str(row.get("status") or "—"),
                str(row.get("error_code") or "—"),
            ]
            for column, value in enumerate(values):
                self.mcp_table.setItem(index, column, QTableWidgetItem(value))

    def _filter_inbox(self, text: str) -> None:
        keyword = text.strip().lower()
        if not keyword:
            rows = self.file_rows
        else:
            rows = [
                row for row in self.file_rows
                if keyword in str(row.get("saved_filename", "")).lower()
                or keyword in str(row.get("saved_path", "")).lower()
            ]
        self._populate_files(self.inbox_table, rows, actions=False)

    def _run_task(
        self,
        title: str,
        operation: Callable[[], ServiceResult],
        callback: Callable[[ServiceResult], None],
    ) -> None:
        if self.task_active:
            self.error_var.set("已有任务正在运行，请勿重复点击")
            return
        self.task_active = True
        self.status_var.set(title)
        for button in self.task_buttons:
            button.setEnabled(False)
        runner = _TaskRunner(operation)
        self._task_callback = callback
        runner.signals.finished.connect(self._finish_task)
        # 保留 Python 包装对象，避免任务完成前信号对象被回收。
        self._active_runner = runner
        self.thread_pool.start(runner)

    @Slot(object)
    def _finish_task(self, result: ServiceResult) -> None:
        """在 GUI 线程完成状态更新和结果展示。"""
        callback = self._task_callback
        self._task_callback = None
        self._active_runner = None
        if self.closed:
            return
        self.task_active = False
        for button in self.task_buttons:
            button.setEnabled(True)
        self.refresh()
        if callback is not None:
            callback(result)

    def _show_receive_result(self, result: ServiceResult) -> None:
        if isinstance(result, ReceiveResult):
            summary = (
                f"收件完成：扫描 {result.scanned}，保存 {result.saved}，"
                f"重复 {result.duplicates}，失败 {result.failed}"
            )
            if result.status in {OperationStatus.FAILED, OperationStatus.AUTH_REQUIRED}:
                reason = result.message or (result.errors[0] if result.errors else result.error_code)
                message = f"收件失败：{reason or '原因未知'}；{summary}"
            elif result.status == OperationStatus.PARTIAL:
                reason = result.message or (result.errors[0] if result.errors else "部分邮件处理失败")
                message = f"{summary}；{reason}"
            else:
                message = summary
        else:
            message = result.message or result.status.value
        self.show_message(message, "success" if result.ok else "error")

    def _show_send_result(self, result: ServiceResult) -> None:
        if isinstance(result, SendResult):
            messages = {
                "sent": "邮件发送并归档成功",
                "sent_archive_failed": "邮件已发送，但本地归档失败；请勿重复发送",
                "failed": "SMTP 发送失败，可以安全重试",
                "duplicate": "检测到重复发送请求，未再次发信",
            }
            message = messages.get(result.send_status, result.message or result.send_status)
        else:
            message = result.message or result.status.value
        self.show_message(message, "success" if result.ok else "error")

    def _show_service_result(self, result: ServiceResult) -> None:
        self.show_message(result.message or result.status.value, "success" if result.ok else "error")

    def show_message(self, text: str, kind: str = "normal") -> None:
        if hasattr(self, "message_bar"):
            self.message_bar.set_message(text, kind)

    def _load_auto_receive_preferences(self) -> None:
        minutes_text = os.getenv("GUI_AUTO_RECEIVE_INTERVAL_MINUTES", str(AUTO_RECEIVE_DEFAULT_MINUTES))
        try:
            minutes = max(1, int(minutes_text))
        except ValueError:
            minutes = AUTO_RECEIVE_DEFAULT_MINUTES
        self._set_combo_data(self.interval_combo, minutes)
        enabled = os.getenv("GUI_AUTO_RECEIVE", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.auto_switch.setChecked(enabled)
        self._reschedule_auto_receive()

    def _toggle_auto_receive(self, enabled: bool) -> None:
        if enabled:
            minutes = int(self.interval_combo.currentData() or AUTO_RECEIVE_DEFAULT_MINUTES)
            self.auto_timer.start(minutes * 60 * 1000)
            self.show_message(f"自动收件已开启，每 {minutes} 分钟检查一次", "success")
        else:
            self.auto_timer.stop()
            self.show_message("自动收件已关闭", "normal")
        if hasattr(self, "service_rows"):
            self.service_rows["auto"].set_value("已开启" if enabled else "未开启", success=enabled)

    def _reschedule_auto_receive(self) -> None:
        if hasattr(self, "auto_switch") and self.auto_switch.isChecked():
            minutes = int(self.interval_combo.currentData() or AUTO_RECEIVE_DEFAULT_MINUTES)
            self.auto_timer.start(minutes * 60 * 1000)

    def _automatic_receive(self) -> None:
        if self.task_active:
            return
        self._run_task("自动收件正在运行", self.service.receive, self._show_receive_result)

    def open_today_folder(self) -> None:
        today_folder = self.service.cfg.received_dir / datetime.now().strftime("%Y-%m-%d")
        target = today_folder if today_folder.exists() else self.service.cfg.received_dir
        try:
            assert_within_allowed_roots(target, [self.service.cfg.data_root_path])
            os.startfile(str(target))
        except (OSError, SecurityError) as exc:
            self.show_message(f"打开目录失败：{exc}", "error")

    def select_latest_file(self) -> None:
        if not self.file_rows:
            self.show_message("今日暂未收到文件")
            return
        self.files_table.selectRow(0)
        self._preview_path(str(self.file_rows[0].get("saved_path", "")))

    def _preview_table_file(self, row: int, column: int) -> None:
        del column
        item = self.files_table.item(row, 0)
        if item:
            self._preview_path(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def _preview_inbox_file(self, row: int, column: int) -> None:
        del column
        item = self.inbox_table.item(row, 0)
        if item:
            self._preview_path(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def _file_action_clicked(self, row: int, column: int) -> None:
        if column != 4:
            return
        item = self.files_table.item(row, 0)
        if item:
            path = str(item.data(Qt.ItemDataRole.UserRole) or "")
            QApplication.clipboard().setText(path)
            self.show_message("文件路径已复制；双击该行可安全预览", "success")

    def _preview_path(self, raw_path: str) -> None:
        if not raw_path:
            self.show_message("文件路径不可用", "error")
            return
        path = Path(raw_path)
        try:
            assert_within_allowed_roots(path, [self.service.cfg.data_root_path])
        except SecurityError:
            self.show_message("已阻止访问 DATA_ROOT 之外的路径", "error")
            return
        if not path.exists() or not path.is_file():
            self.show_message("文件不存在或已移动", "error")
            return
        suffix = path.suffix.lower()
        if suffix in SAFE_TEXT_SUFFIXES:
            self._show_text_preview(path)
        elif suffix in SAFE_IMAGE_SUFFIXES:
            self._show_image_preview(path)
        else:
            self._reveal_file(path)
            self.show_message("该类型不在安全预览列表中，已在资源管理器定位", "normal")

    def _show_text_preview(self, path: Path) -> None:
        data = path.read_bytes()[:PREVIEW_MAX_BYTES]
        text = data.decode("utf-8", errors="replace")
        dialog = QDialog(self)
        dialog.setWindowTitle(f"安全预览 - {path.name}")
        dialog.resize(760, 520)
        layout = QVBoxLayout(dialog)
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor)
        close = self._button("关闭", dialog.accept, primary=True)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _show_image_preview(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.show_message("图片预览失败", "error")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"安全预览 - {path.name}")
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setPixmap(pixmap.scaled(760, 540, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(label)
        dialog.exec()

    def _reveal_file(self, path: Path) -> None:
        try:
            subprocess.Popen(["explorer", f"/select,{path}"])
        except OSError as exc:
            self.show_message(f"定位文件失败：{exc}", "error")

    def _show_mcp_notice(self) -> None:
        self.select_page("agent")
        self.show_message("Agent 接口页已打开")

    def _copy_mcp_config(self, target: str) -> None:
        commands = {
            "codex": "codex mcp add agent-mail-bridge -- python -m agent_mail_bridge.mcp_server",
            "claude": "claude mcp add agent-mail-bridge -- python -m agent_mail_bridge.mcp_server",
        }
        command = commands.get(target)
        if command is None:
            self.show_message("未知的 Agent 配置类型", "error")
            return
        QApplication.clipboard().setText(command)
        self.show_message("MCP 配置命令已复制", "success")

    def _show_help(self) -> None:
        QMessageBox.information(self, "帮助", "详细配置、诊断和安全说明请查看项目 README.md。")

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于 AgentMailBridge",
            "AgentMailBridge v1.0.0\n\n本地优先、单用户的邮箱桥接工具。\n正式界面使用 PySide6，核心能力复用 ApplicationService。",
        )

    @staticmethod
    def _valid_email(value: str) -> bool:
        local, separator, domain = value.partition("@")
        return bool(local and separator and "." in domain and " " not in value)

    @staticmethod
    def _short_time(value, *, include_date: bool = False) -> str:
        text = str(value or "")
        if not text:
            return "—"
        if include_date:
            return text[:19]
        return text[11:19] if len(text) >= 19 else text

    @staticmethod
    def _level_color(level: str) -> str:
        return {
            "SUCCESS": SUCCESS,
            "INFO": SUCCESS,
            "WARNING": WARNING,
            "ERROR": DANGER,
            "FAILED": DANGER,
        }.get(level, TEXT_MUTED)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _latest_event_time(self, keywords: tuple[str, ...]) -> str:
        for row in self.log_rows:
            haystack = f"{row.get('event_type', '')} {row.get('message', '')}".lower()
            if any(keyword.lower() in haystack for keyword in keywords):
                return self._short_time(row.get("created_at"), include_date=True)
        return "—"

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed = True
        self.auto_timer.stop()
        event.accept()

    def resizeEvent(self, event) -> None:
        if hasattr(self, "size_grip"):
            self.size_grip.move(self.width() - 16, self.height() - 16)
        super().resizeEvent(event)
