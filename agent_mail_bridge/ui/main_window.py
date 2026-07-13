"""基于 PySide6 的 AgentMailBridge 正式桌面主窗口。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSystemTrayIcon,
    QMenu,
    QSizeGrip,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import close_connection
from agent_mail_bridge.credentials import GMAIL_IMAP_SECRET, QQ_SMTP_SECRET
from agent_mail_bridge.desktop_runtime import StartupManager
from agent_mail_bridge.version import __version__
from agent_mail_bridge.mcp_client_config import (
    generic_mcp_json,
    mcp_client_command,
    mcp_launch,
)
from agent_mail_bridge.runtime_paths import get_runtime_paths
from agent_mail_bridge.models import OperationStatus, ReceiveResult, SendResult, ServiceResult
from agent_mail_bridge.security import SecurityError, assert_within_allowed_roots
from agent_mail_bridge.ui.settings_store import save_env_values
from agent_mail_bridge.ui.settings_store import import_legacy_env
from agent_mail_bridge.ui.account_management import (
    AccountTypeDialog,
    open_account_dialog,
    open_add_account_dialog,
)
from agent_mail_bridge.ui.branding import apply_brand_label, brand_icon, find_brand_asset, provider_icon
from agent_mail_bridge.ui.theme import (
    DANGER,
    PURPLE,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
    build_stylesheet,
)
from agent_mail_bridge.ui.widgets import (
    AccountCard,
    DataTable,
    HealthStatusRow,
    MessageBar,
    NavButton,
    StatCard,
    StatusRow,
    TipRow,
    ToggleSwitch,
    draw_status_dot,
    format_size,
    horizontal_line,
    line_icon_pixmap,
    paint_app_icon,
)
from agent_mail_bridge.utils import sha256_of_file

AUTO_RECEIVE_DEFAULT_MINUTES = 3  # 自动收件默认间隔，单位：分钟
PREVIEW_MAX_BYTES = 128 * 1024  # 文本预览上限，单位：字节
SAFE_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".py", ".toml", ".ini"}
SAFE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


@dataclass(frozen=True)
class SendFileSelection:
    """记录用户本次明确选择的文件状态，防止页面残留和文件被替换。"""

    path: Path
    size: int
    modified_ns: int
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> "SendFileSelection":
        stat = path.stat()
        resolved = path.resolve(strict=True)
        return cls(
            path=resolved,
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            sha256=sha256_of_file(resolved),
        )

    def is_unchanged(self) -> bool:
        try:
            current = self.path.stat()
            current_sha = sha256_of_file(self.path)
        except OSError:
            return False
        return (
            current.st_size == self.size
            and current.st_mtime_ns == self.modified_ns
            and current_sha == self.sha256
        )


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
        self.brand_asset_loaded = apply_brand_label(icon, paint_app_icon)
        title = QLabel("Agent 邮箱桥接工具")
        title.setObjectName("appTitle")
        version = QLabel(f"v{__version__}")
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

        self.theme_button = QPushButton()
        self.theme_button.setObjectName("titleButton")
        self.theme_button.setFixedSize(38, 30)
        self.theme_button.clicked.connect(self.window_ref.toggle_theme)
        self.set_theme(self.window_ref.theme_mode)
        layout.addWidget(self.theme_button)

        minimize = QPushButton("—")
        maximize = QPushButton("□")
        close = QPushButton("×")
        for button in (minimize, maximize):
            button.setObjectName("titleButton")
            button.setFixedSize(42, 38)
        close.setObjectName("closeButton")
        close.setFixedSize(42, 38)
        minimize.clicked.connect(window.minimize_to_tray)
        maximize.clicked.connect(self._toggle_maximized)
        close.clicked.connect(window.close)
        layout.addWidget(minimize)
        layout.addWidget(maximize)
        layout.addWidget(close)

    def set_theme(self, theme: str) -> None:
        """用清晰图标显示下一次可切换的主题。"""
        if theme == "dark":
            self.theme_button.setIcon(QIcon(line_icon_pixmap("sun", 18, "#D8DBE8")))
            self.theme_button.setToolTip("切换为浅色模式")
        else:
            self.theme_button.setIcon(QIcon(line_icon_pixmap("moon", 18, "#555B69")))
            self.theme_button.setToolTip("切换为深色模式")
        self.theme_button.setText("")
        self.theme_button.setIconSize(QSize(18, 18))

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
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)
        self.accepting_tasks = True
        self.quitting = False
        self.pending_quit = False
        self.instance_guard = None
        self._notification_times: dict[str, float] = {}
        self.task_buttons: list[QPushButton] = []
        self.manual_receive_buttons: list[QPushButton] = []
        self._active_task_button: QPushButton | None = None
        self._active_task_button_text = ""
        self._task_refresh_on_finish = True
        saved_theme = os.getenv("GUI_THEME", "light").strip().lower()
        self.theme_mode = saved_theme if saved_theme in {"light", "dark"} else "light"
        self.file_rows: list[dict] = []
        self.log_rows: list[dict] = []
        self.history_rows: dict[str, list[dict]] = {"received": [], "sent": []}
        self.mcp_rows: list[dict] = []
        self.managed_file_rows: list[dict] = []
        self.health_fix_target = ""
        self.selected_send_path = ""
        self.send_selection: SendFileSelection | None = None
        self.last_refresh_at: datetime | None = None
        self.last_error_details = ""
        self._config_dirty = False
        self._loading_controls = False
        self.settings = QSettings("AgentMailBridge", "AgentMailBridge")
        self.previous_exit_was_clean = self.settings.value("runtime/clean_exit", True, type=bool)
        self.settings.setValue("runtime/clean_exit", False)
        self.settings.sync()
        self.status_var = _ValueSink(lambda value: self.show_message(value, "working"))
        self.error_var = _ValueSink(lambda value: self.show_message(value, "error"))
        self.auto_timer = QTimer(self)
        self.auto_timer.setSingleShot(True)
        self.auto_timer.timeout.connect(self._automatic_receive)
        self.auto_failures = 0
        self.setWindowTitle("Agent 邮箱桥接工具")
        self.setWindowIcon(brand_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1280, 920)
        self.setMinimumSize(1160, 780)
        self._build()
        self.apply_theme(self.theme_mode)
        self._build_tray()
        self._load_auto_receive_preferences()
        self._wire_config_change_tracking()
        saved_geometry = self.settings.value("window/geometry")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
        # 高频收件工作台始终作为启动首页，避免旧版本页面状态恢复到已删除路由。
        self.select_page("inbox")
        QTimer.singleShot(0, self.refresh)
        if not self.previous_exit_was_clean:
            QTimer.singleShot(
                100,
                lambda: self.show_message(
                    "检测到上次未正常退出；临时发送文件未恢复，请重新选择文件",
                    "error",
                ),
            )

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
        panel.setFixedWidth(238)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 14, 10, 12)
        layout.setSpacing(8)

        self.add_account_button = QPushButton("＋  添加邮箱账号")
        self.add_account_button.setObjectName("primaryButton")
        self.add_account_button.setFixedHeight(46)
        self.add_account_button.clicked.connect(self.open_add_account)
        layout.addWidget(self.add_account_button)
        layout.addSpacing(3)

        label = QLabel("我的邮箱账号")
        label.setObjectName("fieldLabel")
        layout.addWidget(label)
        self.gmail_card = AccountCard(provider_icon("gmail"), "Gmail（收件）", "未配置", "管理已有收件账号", "#EA4335")
        self.qq_card = AccountCard(provider_icon("qq"), "QQ（发件）", "未配置", "管理已有发件账号", "#21A4E8")
        self.gmail_card.clicked.connect(lambda: self.open_account(AccountTypeDialog.GMAIL))
        self.qq_card.clicked.connect(lambda: self.open_account(AccountTypeDialog.QQ))
        layout.addWidget(self.gmail_card)
        layout.addWidget(self.qq_card)
        layout.addSpacing(8)
        layout.addStretch(1)

        self.nav_buttons: dict[str, NavButton] = {}
        nav_specs = (
            ("history", "clock", "历史记录"),
            ("files_data", "database", "文件与数据"),
            ("settings", "settings", "设置"),
            ("about", "info", "关于"),
        )
        nav_card = QFrame()
        nav_card.setObjectName("navCard")
        nav_layout = QVBoxLayout(nav_card)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        for index, (key, icon_kind, text) in enumerate(nav_specs):
            button = NavButton(QIcon(line_icon_pixmap(icon_kind, 17, "#6F7585")), text)
            button.clicked.connect(lambda checked=False, page=key: self.select_page(page))
            self.nav_buttons[key] = button
            nav_layout.addWidget(button)
            if index < len(nav_specs) - 1:
                nav_layout.addWidget(horizontal_line())
        layout.addWidget(nav_card)
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
        for key, text in (("inbox", "收件"), ("send", "发件")):
            button = QPushButton(text)
            button.setObjectName("tabButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda checked=False, page=key: self.select_page(page))
            self.tab_buttons[key] = button
            tab_layout.addWidget(button)
        self.tab_buttons["inbox"].setChecked(True)
        tab_layout.addStretch(1)
        layout.addWidget(tabs)

        self.page_stack = QStackedWidget()
        self.pages = {
            "inbox": self._build_inbox_page(),
            "send": self._build_send_page(),
            "history": self._build_history_page(),
            "files_data": self._build_files_data_page(),
            "settings": self._build_settings_page(),
            "advanced": self._build_advanced_page(),
            "logs": self._build_logs_page(),
            "maintenance": self._build_maintenance_page(),
            "agent": self._build_agent_page(),
            "about": self._build_about_page(),
        }
        for page in self.pages.values():
            self.page_stack.addWidget(page)
        layout.addWidget(self.page_stack, 1)
        return panel

    def _build_dashboard_page(self) -> QWidget:
        page, layout = self._standard_page(
            "仪表盘",
            "查看桥接服务健康状态、今日处理结果和常用操作。",
        )
        health = QFrame()
        health.setObjectName("heroCard")
        health_layout = QHBoxLayout(health)
        health_layout.setContentsMargins(18, 14, 18, 14)
        health_text = QVBoxLayout()
        health_title = QLabel("桥接服务正在运行")
        health_title.setObjectName("sectionTitle")
        self.dashboard_health_detail = QLabel("正在读取邮箱连接和最近任务状态…")
        self.dashboard_health_detail.setObjectName("hint")
        health_text.addWidget(health_title)
        health_text.addWidget(self.dashboard_health_detail)
        health_layout.addLayout(health_text, 1)
        self.dashboard_receive_button = self._button("立即收取", self.receive, primary=True)
        self.task_buttons.append(self.dashboard_receive_button)
        self.manual_receive_buttons.append(self.dashboard_receive_button)
        health_layout.addWidget(self.dashboard_receive_button)
        health_layout.addWidget(self._button("发送文件", self.choose_and_send, outline=True))
        layout.addWidget(health)

        stats = QGridLayout()
        stats.setSpacing(10)
        self.dashboard_stat_cards = {
            "received": StatCard("statPurple", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown), "今日收取", PURPLE),
            "saved": StatCard("statGreen", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), "今日保存", SUCCESS),
            "sent": StatCard("statBlue", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp), "今日发送", "#2394C8"),
            "errors": StatCard("statRed", self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning), "今日异常", DANGER),
        }
        for index, card in enumerate(self.dashboard_stat_cards.values()):
            stats.addWidget(card, index // 2, index % 2)
        layout.addLayout(stats)

        activity_header = QHBoxLayout()
        activity_title = QLabel("最近活动")
        activity_title.setObjectName("sectionTitle")
        self.dashboard_refresh_label = QLabel("尚未刷新")
        self.dashboard_refresh_label.setObjectName("hint")
        self.dashboard_refresh_button = self._button("刷新", text_only=True)
        self.dashboard_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.dashboard_refresh_button)
        )
        activity_header.addWidget(activity_title)
        activity_header.addStretch(1)
        activity_header.addWidget(self.dashboard_refresh_label)
        activity_header.addWidget(self.dashboard_refresh_button)
        layout.addLayout(activity_header)
        self.dashboard_logs_table = DataTable(["时间", "级别", "消息"])
        self._configure_log_table(self.dashboard_logs_table)
        layout.addWidget(self.dashboard_logs_table, 1)
        return page

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
            "应用专用密码（Windows 安全存储）", "", password=True
        )
        self.gmail_password_edit.setPlaceholderText(
            "已配置；留空保持不变" if self.service.cfg.gmail_app_password else "未配置"
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
        help_label = QLabel()
        help_label.setPixmap(line_icon_pixmap("info", 16, PURPLE))
        help_label.setToolTip("只接收可信 Gmail 自发自收邮件，避免结果邮件形成循环")
        help_label.setStyleSheet(f"color: {TEXT_MUTED};")
        rule_row.addWidget(rule_label)
        rule_row.addSpacing(10)
        rule_row.addWidget(self.self_mail_check)
        rule_row.addStretch(1)
        rule_row.addWidget(help_label)
        layout.addLayout(rule_row)

        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        test_button = self._button("测试连接", self.test_connection, primary=True)
        save_button = self._button("保存配置", self.save_basic_config)
        delete_password_button = self._button(
            "删除 IMAP 凭据", lambda: self.delete_credential(GMAIL_IMAP_SECRET), text_only=True
        )
        self.receive_button = self._button("手动收取", self.receive)
        self.send_button = self._button("选择文件发送", self.choose_and_send)
        mcp_button = self._button(
            "Agent 提交结果（MCP）",
            lambda: self.select_page("agent"),
            outline=True,
        )
        self.task_buttons.extend((test_button, self.receive_button, self.send_button))
        self.manual_receive_buttons.append(self.receive_button)
        for index, button in enumerate((
            test_button, save_button, delete_password_button,
            self.receive_button, self.send_button, mcp_button,
        )):
            actions.addWidget(button, index // 3, index % 3)
        actions.setColumnStretch(2, 1)
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
        open_button = self._button("打开今日接收文件夹", self.open_today_folder)
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
        file_note = QLabel("文件接收后会自动按规则保存至此目录；危险附件只保存并标记，不会自动执行。")
        file_note.setObjectName("hint")
        layout.addWidget(file_note)
        layout.addWidget(horizontal_line())

        log_header = QHBoxLayout()
        log_title = QLabel("最近日志")
        log_title.setObjectName("sectionTitle")
        self.home_refresh_label = QLabel("尚未刷新")
        self.home_refresh_label.setObjectName("hint")
        self.home_refresh_button = self._button("刷新", text_only=True)
        self.home_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.home_refresh_button)
        )
        more_logs = self._button("查看更多日志", lambda: self.select_page("logs"), text_only=True)
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        log_header.addWidget(self.home_refresh_label)
        log_header.addWidget(self.home_refresh_button)
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
        page, layout = self._standard_page(
            "收件",
            "日常收件工作台；邮箱地址、OAuth 和 IMAP 凭据请通过左侧 Gmail 账号卡片管理。",
            header_action_label="刷新",
        )
        self.inbox_refresh_button = page.header_action_button
        self.inbox_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.inbox_refresh_button)
        )
        status_card = QFrame()
        status_card.setObjectName("heroCard")
        status_layout = QHBoxLayout(status_card)
        status_layout.setContentsMargins(16, 12, 16, 12)
        status_layout.setSpacing(10)
        account_icon = QLabel()
        account_icon.setFixedSize(46, 46)
        account_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        account_icon.setPixmap(provider_icon("gmail").pixmap(40, 40))
        status_layout.addWidget(account_icon)
        status_text = QVBoxLayout()
        self.receive_account_label = QLabel(self.service.cfg.gmail_address or "尚未配置 Gmail 收件账号")
        self.receive_account_label.setObjectName("minorTitle")
        self.dashboard_health_detail = QLabel("正在读取当前连接状态…")
        self.dashboard_health_detail.setObjectName("hint")
        status_text.addWidget(self.receive_account_label)
        status_text.addWidget(self.dashboard_health_detail)
        status_layout.addLayout(status_text, 1)
        manage = self._button(
            "管理 Gmail 账号",
            lambda: self.open_account(AccountTypeDialog.GMAIL),
            outline=True,
        )
        status_layout.addWidget(manage)
        layout.addWidget(status_card)

        tools = QHBoxLayout()
        tools.setSpacing(9)
        tools.addWidget(QLabel("自动收取"))
        self.auto_switch = ToggleSwitch()
        self.auto_switch.toggled.connect(self._toggle_auto_receive)
        tools.addWidget(self.auto_switch)
        tools.addSpacing(5)
        tools.addWidget(QLabel("检查间隔"))
        self.interval_combo = QComboBox()
        for minutes in (1, 3, 5, 10, 30):
            suffix = "（推荐）" if minutes == AUTO_RECEIVE_DEFAULT_MINUTES else ""
            self.interval_combo.addItem(f"每 {minutes} 分钟{suffix}", minutes)
        self.interval_combo.setFixedWidth(160)
        self.interval_combo.currentIndexChanged.connect(self._reschedule_auto_receive)
        tools.addWidget(self.interval_combo)
        tools.addStretch(1)
        self.inbox_test_button = self._button("测试当前连接", self.test_connection)
        receive = self._button("立即收取", self.receive, primary=True, icon_kind="mail")
        self.receive_button = receive
        self.task_buttons.extend((self.inbox_test_button, receive))
        self.manual_receive_buttons.append(receive)
        tools.addWidget(self.inbox_test_button)
        tools.addWidget(receive)
        layout.addLayout(tools)

        self.self_mail_check = QCheckBox(page)
        self.self_mail_check.setChecked(self.service.cfg.auto_receive_only_self_mail)
        self.self_mail_check.hide()
        preference_card = QFrame()
        preference_card.setObjectName("card")
        preference_row = QHBoxLayout()
        preference_card.setLayout(preference_row)
        preference_row.setContentsMargins(14, 10, 14, 10)
        preference_text = QVBoxLayout()
        preference_title = QLabel("当前收件偏好")
        preference_title.setObjectName("fieldLabel")
        self.preference_summary_label = QLabel()
        self.preference_summary_label.setObjectName("minorTitle")
        preference_text.addWidget(preference_title)
        preference_text.addWidget(self.preference_summary_label)
        preference_row.addLayout(preference_text)
        preference_row.addStretch(1)
        preference_row.addWidget(
            self._button("编辑偏好", self.open_receive_preferences_editor, outline=True)
        )
        layout.addWidget(preference_card)
        self._update_receive_preference_summary()

        self.message_bar = MessageBar()
        layout.addWidget(self.message_bar)
        file_header = QHBoxLayout()
        file_title = QLabel("今日收到文件")
        file_title.setObjectName("sectionTitle")
        self.inbox_search = QLineEdit()
        self.inbox_search.setObjectName("inboxSearch")
        self.inbox_search.setPlaceholderText("搜索收到的文件")
        self.inbox_search.setClearButtonEnabled(True)
        self.inbox_search.addAction(
            QIcon(line_icon_pixmap("search", 17, TEXT_MUTED)),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.inbox_search.setMinimumWidth(330)
        self.inbox_search.setMaximumWidth(460)
        self.inbox_search.textChanged.connect(self._filter_inbox)
        open_button = self._button("打开今日接收文件夹", self.open_today_folder, icon_kind="file")
        file_header.addWidget(file_title)
        file_header.addStretch(1)
        file_header.addWidget(self.inbox_search)
        file_header.addWidget(open_button)
        layout.addLayout(file_header)
        self.files_table = DataTable(["文件名", "大小", "保存路径", "收取时间", "操作"])
        self.files_table.setMinimumHeight(250)
        self.files_table.cellDoubleClicked.connect(self._preview_table_file)
        self._configure_file_table(self.files_table)
        self.inbox_table = self.files_table
        layout.addWidget(self.files_table, 3)

        log_header = QHBoxLayout()
        log_title = QLabel("最近日志")
        log_title.setObjectName("sectionTitle")
        self.home_refresh_label = QLabel("尚未刷新")
        self.home_refresh_label.setObjectName("hint")
        self.home_refresh_label.setMinimumWidth(130)
        self.home_refresh_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        manage_logs = self._button("管理日志", lambda: self.select_page("logs"), outline=True)
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        log_header.addWidget(self.home_refresh_label)
        log_header.addWidget(manage_logs)
        layout.addLayout(log_header)
        self.logs_table = DataTable(["时间", "级别", "消息"])
        self.logs_table.setMinimumHeight(165)
        self.logs_table.setMaximumHeight(235)
        self._configure_log_table(self.logs_table)
        self.logs_refresh_label = self.home_refresh_label
        self.dashboard_refresh_label = self.home_refresh_label
        layout.addWidget(self.logs_table, 1)
        return page

    def _build_send_page(self) -> QWidget:
        page, layout = self._standard_page("发邮件", "用户可手动选择任意位置的普通文件；MCP 和 CLI 仍受目录限制。")
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
        self.send_path_edit.setPlaceholderText("请选择电脑任意位置的普通文件")
        choose = self._button("选择文件", self.choose_send_file)
        source_row.addWidget(self.send_path_edit, 1)
        source_row.addWidget(choose)
        file_actions = QHBoxLayout()
        self.copy_send_path_button = self._button("复制路径", self.copy_selected_send_path, text_only=True)
        self.reveal_send_file_button = self._button("打开所在文件夹", self.reveal_selected_send_file, text_only=True)
        self.preview_send_file_button = self._button("安全预览", self.preview_selected_send_file, text_only=True)
        for button in (
            self.copy_send_path_button,
            self.reveal_send_file_button,
            self.preview_send_file_button,
        ):
            button.setEnabled(False)
            file_actions.addWidget(button)
        file_actions.addStretch(1)

        details = QGridLayout()
        details.setHorizontalSpacing(14)
        details.setVerticalSpacing(6)
        self.send_file_name_value = QLabel("未选择")
        self.send_file_size_value = QLabel("—")
        self.send_file_type_value = QLabel("—")
        self.send_file_modified_value = QLabel("—")
        detail_values = (
            ("文件名", self.send_file_name_value),
            ("大小", self.send_file_size_value),
            ("类型", self.send_file_type_value),
            ("最后修改", self.send_file_modified_value),
        )
        for index, (label, value) in enumerate(detail_values):
            title = QLabel(label)
            title.setObjectName("fieldLabel")
            value.setObjectName("sendFileValue")
            details.addWidget(title, index // 2 * 2, index % 2)
            details.addWidget(value, index // 2 * 2 + 1, index % 2)
        self.subject_edit = QLineEdit()
        self.subject_edit.setPlaceholderText("可选；留空时使用默认主题")
        self.recipient_edit = QLineEdit(self.service.cfg.owner_gmail)
        self.recipient_edit.setReadOnly(True)
        self.send_action_button = self._button("发送到绑定 Gmail", self.send_selected_file, primary=True)
        self.send_action_button.setEnabled(False)
        self.task_buttons.append(self.send_action_button)
        self.send_progress = QProgressBar()
        self.send_progress.setRange(0, 0)
        self.send_progress.setTextVisible(False)
        self.send_progress.setFixedHeight(4)
        self.send_progress.hide()
        self.send_status_label = QLabel("请选择本次要发送的文件")
        self.send_status_label.setObjectName("hint")
        form.addWidget(source_label)
        form.addLayout(source_row)
        form.addLayout(file_actions)
        form.addLayout(details)
        form.addWidget(QLabel("邮件主题"))
        form.addWidget(self.subject_edit)
        form.addWidget(QLabel("固定收件人"))
        form.addWidget(self.recipient_edit)
        form.addWidget(self.send_action_button, 0, Qt.AlignmentFlag.AlignLeft)
        form.addWidget(self.send_progress)
        form.addWidget(self.send_status_label)
        layout.addWidget(card)
        related = QFrame()
        related.setObjectName("card")
        related_layout = QHBoxLayout(related)
        related_layout.setContentsMargins(16, 11, 16, 11)
        related_title = QLabel("Agent 发件 / MCP")
        related_title.setObjectName("minorTitle")
        related_hint = QLabel("按需启动 · stdio · 固定收件人 · 路径白名单")
        related_hint.setObjectName("hint")
        related_layout.addWidget(related_title)
        related_layout.addWidget(related_hint)
        related_layout.addStretch(1)
        related_layout.addWidget(self._button("打开 MCP 面板", lambda: self.select_page("agent"), outline=True))
        layout.addWidget(related)

        history_header = QHBoxLayout()
        history_title = QLabel("最近发送记录")
        history_title.setObjectName("sectionTitle")
        history_header.addWidget(history_title)
        history_header.addStretch(1)
        history_header.addWidget(self._button("管理记录", self.open_send_history, text_only=True))
        layout.addLayout(history_header)
        self.sent_table = DataTable(
            ["文件", "大小", "来源", "request_id", "发送时间", "状态"]
        )
        layout.addWidget(self.sent_table, 1)
        return page

    def _build_files_data_page(self) -> QWidget:
        page, layout = self._standard_page(
            "文件与数据",
            "统一管理收件文件、已发送归档和 Agent 结果；数据维护操作保留安全确认。",
        )
        filters = QHBoxLayout()
        self.file_data_search = QLineEdit()
        self.file_data_search.setPlaceholderText("搜索文件名或路径")
        self.file_data_type_filter = QComboBox()
        self.file_data_type_filter.addItems(["全部类型", "收件文件", "已发送归档", "Agent 结果"])
        self.file_data_source_filter = QComboBox()
        self.file_data_source_filter.addItems(["全部来源", "Gmail", "手动发件", "Agent / MCP"])
        self.file_data_time_filter = QComboBox()
        self.file_data_time_filter.addItems(["全部时间", "今天", "最近 7 天", "最近 30 天"])
        for control in (
            self.file_data_search,
            self.file_data_type_filter,
            self.file_data_source_filter,
            self.file_data_time_filter,
        ):
            if isinstance(control, QLineEdit):
                control.textChanged.connect(self._filter_managed_files)
            else:
                control.currentTextChanged.connect(self._filter_managed_files)
            filters.addWidget(control)
        self.files_data_refresh_button = self._button("刷新")
        self.files_data_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.files_data_refresh_button)
        )
        filters.addWidget(self.files_data_refresh_button)
        layout.addLayout(filters)

        self.managed_files_table = DataTable(["类型", "来源", "文件", "大小", "时间", "本地路径"])
        self.managed_files_table.cellDoubleClicked.connect(self._preview_managed_file)
        header = self.managed_files_table.horizontalHeader()
        for column in (0, 1, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.managed_files_table, 2)

        actions = QHBoxLayout()
        actions.addWidget(self._button("安全预览", self.preview_selected_managed_file))
        actions.addWidget(self._button("打开所在目录", self.reveal_selected_managed_file))
        actions.addWidget(self._button("复制路径", self.copy_selected_managed_file_path, text_only=True))
        actions.addStretch(1)
        layout.addLayout(actions)

        overview = QFrame()
        overview.setObjectName("card")
        overview_layout = QHBoxLayout(overview)
        overview_layout.setContentsMargins(16, 12, 16, 12)
        overview_title = QLabel("数据概览")
        overview_title.setObjectName("minorTitle")
        self.data_overview_label = QLabel("正在读取数据库与存储占用…")
        self.data_overview_label.setObjectName("hint")
        self.data_overview_label.setWordWrap(True)
        overview_layout.addWidget(overview_title)
        overview_layout.addWidget(self.data_overview_label, 1)
        overview_layout.addWidget(
            self._button("数据维护与备份", lambda: self.select_page("maintenance"), outline=True)
        )
        layout.addWidget(overview)
        return page

    def _build_settings_page(self) -> QWidget:
        page, layout = self._standard_page("设置", "管理常用运行、外观与收发限制。账号认证不在此处配置。")
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 15, 18, 16)
        card_layout.setSpacing(12)
        run_title = QLabel("常用设置")
        run_title.setObjectName("sectionTitle")
        card_layout.addWidget(run_title)

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
        card_layout.addLayout(limits)

        self.startup_check = QCheckBox("Windows 开机后在后台启动（默认关闭）")
        self.startup_check.setChecked(StartupManager.is_enabled())
        card_layout.addWidget(self.startup_check)
        appearance = QHBoxLayout()
        appearance.addWidget(QLabel("界面外观"))
        self.theme_value_label = QLabel("深色模式" if self.theme_mode == "dark" else "浅色模式")
        self.theme_value_label.setObjectName("hint")
        appearance.addWidget(self.theme_value_label)
        appearance.addStretch(1)
        appearance.addWidget(self._button("切换主题", self.toggle_theme))
        card_layout.addLayout(appearance)

        self.unsaved_config_label = QLabel("配置未修改")
        self.unsaved_config_label.setObjectName("hint")
        card_layout.addWidget(self.unsaved_config_label)
        card_layout.addWidget(self._button("保存设置", self.save_advanced_config, primary=True), 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)

        advanced_card = QFrame()
        advanced_card.setObjectName("card")
        advanced_layout = QHBoxLayout(advanced_card)
        advanced_layout.setContentsMargins(18, 14, 18, 14)
        advanced_text = QVBoxLayout()
        advanced_title = QLabel("高级设置")
        advanced_title.setObjectName("minorTitle")
        advanced_hint = QLabel("网络模式、Runtime Paths、迁移与高级诊断")
        advanced_hint.setObjectName("hint")
        advanced_text.addWidget(advanced_title)
        advanced_text.addWidget(advanced_hint)
        advanced_layout.addLayout(advanced_text, 1)
        advanced_layout.addWidget(self._button("高级设置", lambda: self.select_page("advanced"), outline=True))
        layout.addWidget(advanced_card)
        layout.addStretch(1)
        return page

    def _build_advanced_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        page.setObjectName("pageSurface")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 22, 20)
        layout.setSpacing(12)
        heading_row = QHBoxLayout()
        title = QLabel("设置 > 高级设置")
        title.setObjectName("pageTitle")
        hint = QLabel("应用级、网络级、本地路径、全局诊断与迁移设置；账号认证统一在左侧账号卡片管理。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        heading_row.addWidget(title)
        heading_row.addStretch(1)
        heading_row.addWidget(self._button("← 返回设置", lambda: self.select_page("settings"), text_only=True))
        layout.addLayout(heading_row)
        layout.addWidget(hint)
        layout.addWidget(horizontal_line())

        run_title = QLabel("网络设置")
        run_title.setObjectName("sectionTitle")
        layout.addWidget(run_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        network_box, self.network_combo = self._field_combo(
            "Gmail 网络模式", (("自动选择", "auto"), ("直连", "direct"), ("SOCKS5", "socks5"))
        )
        grid.addWidget(network_box, 0, 0, 1, 2)
        layout.addLayout(grid)
        save = self._button("保存高级设置", self.save_advanced_config, primary=True)
        layout.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(horizontal_line())

        path_title = QLabel("本地数据与路径")
        path_title.setObjectName("sectionTitle")
        layout.addWidget(path_title)
        path_hint = QLabel("内部路径由程序管理；普通使用只需查看状态或打开对应目录。")
        path_hint.setObjectName("hint")
        layout.addWidget(path_hint)
        runtime_paths = get_runtime_paths()
        path_grid = QGridLayout()
        path_specs = (
            ("数据目录", self.service.cfg.data_root_path),
            ("配置目录", runtime_paths.user_config_root),
            ("OAuth 目录", runtime_paths.oauth_root),
            ("缓存目录", runtime_paths.cache_root),
            ("备份目录", self.service.cfg.data_root_path / "backups"),
        )
        self.path_status_labels: dict[str, QLabel] = {}
        for row, (name, path) in enumerate(path_specs):
            name_label = QLabel(name)
            name_label.setObjectName("fieldLabel")
            status = QLabel("可用" if path.exists() else "尚未创建")
            status.setObjectName("successText" if path.exists() else "hint")
            button = self._button("打开目录", lambda checked=False, target=path: self.open_managed_directory(target))
            self.path_status_labels[name] = status
            path_grid.addWidget(name_label, row, 0)
            path_grid.addWidget(status, row, 1)
            path_grid.addWidget(button, row, 2)
        path_grid.setColumnStretch(1, 1)
        layout.addLayout(path_grid)
        layout.addWidget(horizontal_line())

        diagnostics_title = QLabel("诊断与错误")
        diagnostics_title.setObjectName("sectionTitle")
        layout.addWidget(diagnostics_title)
        diagnostics_hint = QLabel("账号页提供单项连接测试；这里仅保留全局诊断与脱敏报告。")
        diagnostics_hint.setObjectName("hint")
        layout.addWidget(diagnostics_hint)
        diagnostic_actions = QHBoxLayout()
        self.all_diagnose_button = self._button("运行全部连接诊断", self.run_all_connection_diagnostics)
        self.export_diagnosis_button = self._button("导出脱敏诊断报告")
        self.error_details_button = self._button("查看最近错误详情", self.show_last_error_details)
        self.error_details_button.setEnabled(False)
        self.export_diagnosis_button.clicked.connect(self.export_diagnostic_report)
        self.task_buttons.extend((self.all_diagnose_button, self.export_diagnosis_button))
        diagnostic_actions.addWidget(self.all_diagnose_button)
        diagnostic_actions.addWidget(self.export_diagnosis_button)
        diagnostic_actions.addWidget(self.error_details_button)
        diagnostic_actions.addStretch(1)
        layout.addLayout(diagnostic_actions)
        layout.addWidget(horizontal_line())

        migration_title = QLabel("配置与迁移")
        migration_title.setObjectName("sectionTitle")
        layout.addWidget(migration_title)
        migration_actions = QHBoxLayout()
        migration_actions.addWidget(self._button("导入旧版 .env", self.import_legacy_configuration))
        migration_actions.addStretch(1)
        layout.addLayout(migration_actions)

        status_title = QLabel("当前脱敏状态")
        status_title.setObjectName("sectionTitle")
        layout.addWidget(status_title)
        self.config_summary = QTextEdit()
        self.config_summary.setReadOnly(True)
        self.config_summary.setMinimumHeight(170)
        layout.addWidget(self.config_summary)
        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def _build_history_page(self) -> QWidget:
        page, layout = self._standard_page(
            "历史记录",
            "查看收件、发件和 Agent / MCP 业务记录；技术运行日志仍由日志管理负责。",
        )
        filters = QHBoxLayout()
        self.history_type_filter = QComboBox()
        self.history_type_filter.addItems(["全部类型", "收件", "发件", "Agent / MCP"])
        self.history_status_filter = QComboBox()
        self.history_status_filter.addItems(["全部状态", "成功", "失败", "重复", "其他"])
        self.history_time_filter = QComboBox()
        self.history_time_filter.addItems(["全部时间", "今天", "最近 7 天", "最近 30 天"])
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("关键词或 request_id")
        self.history_search.textChanged.connect(self._populate_history)
        for combo in (self.history_type_filter, self.history_status_filter, self.history_time_filter):
            combo.currentTextChanged.connect(self._populate_history)
            filters.addWidget(combo)
        filters.addWidget(self.history_search, 1)
        self.history_refresh_button = self._button("刷新")
        self.history_refresh_button.clicked.connect(lambda: self.request_refresh(self.history_refresh_button))
        filters.addWidget(self.history_refresh_button)
        layout.addLayout(filters)
        self.history_table = DataTable(["类型", "主题 / 文件", "request_id", "时间", "状态", "关联文件"])
        self.history_table.cellDoubleClicked.connect(self._show_history_detail)
        layout.addWidget(self.history_table, 1)
        actions = QHBoxLayout()
        actions.addWidget(self._button("查看详情", self.show_selected_history_detail))
        actions.addWidget(self._button("定位关联文件", self.reveal_selected_history_file))
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def _build_logs_page(self) -> QWidget:
        page, layout = self._standard_page("日志管理", "筛选和查看脱敏技术事件；不会显示密码或 OAuth token。")
        tools = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜索事件或消息")
        self.log_search.textChanged.connect(self._populate_full_logs)
        self.log_filter = QComboBox()
        self.log_filter.addItems(["全部级别", "INFO", "SUCCESS", "WARNING", "ERROR"])
        self.log_filter.currentTextChanged.connect(self._populate_full_logs)
        self.log_time_filter = QComboBox()
        self.log_time_filter.addItems(["全部时间", "今天", "最近 7 天", "最近 30 天"])
        self.log_time_filter.currentTextChanged.connect(self._populate_full_logs)
        self.logs_refresh_button = self._button("刷新")
        self.logs_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.logs_refresh_button)
        )
        self.logs_refresh_label = QLabel("尚未刷新")
        self.logs_refresh_label.setObjectName("hint")
        tools.addWidget(self.log_search, 1)
        tools.addWidget(self.log_filter)
        tools.addWidget(self.log_time_filter)
        tools.addStretch(1)
        tools.addWidget(self.logs_refresh_label)
        tools.addWidget(self.logs_refresh_button)
        layout.addLayout(tools)
        self.full_logs_table = DataTable(["时间", "级别", "事件", "消息"])
        self._configure_log_table(self.full_logs_table, full=True)
        self.full_logs_table.cellDoubleClicked.connect(self._show_log_detail)
        layout.addWidget(self.full_logs_table, 1)
        log_actions = QHBoxLayout()
        log_actions.addWidget(self._button("查看详情", self.show_selected_log_detail))
        self.log_export_button = self._button("导出脱敏诊断信息")
        self.log_export_button.clicked.connect(lambda: self.export_diagnostic_report(self.log_export_button))
        log_actions.addWidget(self.log_export_button)
        log_actions.addWidget(self._button("打开日志目录", self.open_log_folder))
        log_actions.addStretch(1)
        log_actions.addWidget(self._button("← 返回收件", lambda: self.select_page("inbox"), text_only=True))
        layout.addLayout(log_actions)
        return page

    def _build_maintenance_page(self) -> QWidget:
        page, layout = self._standard_page(
            "文件与数据 > 数据维护",
            "备份和扫描默认不删除用户数据；数据库恢复前会再次确认并自动备份当前库。",
        )
        back = QHBoxLayout()
        back.addStretch(1)
        back.addWidget(self._button("← 返回文件与数据", lambda: self.select_page("files_data"), text_only=True))
        layout.addLayout(back)
        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        self.maintenance_refresh_button = self._button("刷新状态", self.refresh_maintenance)
        self.backup_button = self._button("创建备份", self.create_backup, primary=True)
        self.scan_button = self._button("一致性扫描", self.run_consistency_scan)
        self.verify_backup_button = self._button("验证备份", self.choose_verify_backup)
        self.restore_backup_button = self._button("恢复备份", self.choose_restore_backup)
        self.open_backup_button = self._button("打开备份目录", self.open_backup_folder)
        self.export_maintenance_button = self._button("导出维护报告", self.export_maintenance_report)
        for index, button in enumerate((
            self.maintenance_refresh_button, self.backup_button, self.scan_button,
            self.verify_backup_button, self.restore_backup_button,
            self.open_backup_button, self.export_maintenance_button,
        )):
            actions.addWidget(button, index // 4, index % 4)
        actions.setColumnStretch(3, 1)
        layout.addLayout(actions)
        self.maintenance_summary = QTextEdit()
        self.maintenance_summary.setReadOnly(True)
        self.maintenance_summary.setFixedHeight(190)
        layout.addWidget(self.maintenance_summary)
        self.backup_table = DataTable(["备份文件", "大小", "状态"])
        layout.addWidget(self.backup_table, 1)
        return page

    def refresh_maintenance(self) -> None:
        self._run_task(
            "正在读取数据维护状态",
            self.service.get_maintenance_status,
            self._show_maintenance_status,
            button=self.maintenance_refresh_button,
        )

    def _show_maintenance_status(self, result: ServiceResult) -> None:
        if not result.ok:
            self._show_service_result(result)
            return
        details = result.details
        counts = details.get("counts", {})
        lines = [
            f"数据目录：{self.service.cfg.data_root_path}",
            f"SQLite：{details.get('integrity_check', '—')}，{format_size(details.get('database_size_bytes', 0))}",
            f"收件记录：{counts.get('received_messages', 0)}，发件记录：{counts.get('sent_files', 0)}，MCP：{counts.get('mcp_calls', 0)}",
            f"received：{details.get('received', {}).get('files', 0)} 个文件 / {format_size(details.get('received', {}).get('size_bytes', 0))}",
            f"send：{details.get('send', {}).get('files', 0)} 个文件 / {format_size(details.get('send', {}).get('size_bytes', 0))}",
            f"sent：{details.get('sent', {}).get('files', 0)} 个文件 / {format_size(details.get('sent', {}).get('size_bytes', 0))}",
        ]
        self.maintenance_summary.setPlainText("\n".join(lines))
        backups = details.get("backups", [])
        self.backup_table.setRowCount(0)
        for index, backup in enumerate(backups):
            self.backup_table.insertRow(index)
            values = [
                str(backup.get("name", "")),
                format_size(int(backup.get("size_bytes", 0))),
                "有效" if backup.get("status") == "valid" else "损坏",
            ]
            for column, value in enumerate(values):
                self.backup_table.setItem(index, column, QTableWidgetItem(value))
        self._show_service_result(result)

    def create_backup(self) -> None:
        self._run_task(
            "正在创建并校验数据库备份",
            self.service.create_backup,
            lambda result: (self._show_service_result(result), self.refresh_maintenance()),
            button=self.backup_button,
        )

    def run_consistency_scan(self) -> None:
        self._run_task(
            "正在执行只读一致性扫描",
            self.service.scan_consistency,
            self._show_consistency_result,
            button=self.scan_button,
        )

    def _show_consistency_result(self, result: ServiceResult) -> None:
        if result.ok:
            summary = result.details.get("summary", {})
            self.maintenance_summary.setPlainText(
                "一致性扫描结果（未删除任何数据）\n"
                f"缺失：{summary.get('missing', 0)}，孤立：{summary.get('orphan', 0)}，"
                f"Hash 异常：{summary.get('hash_mismatch', 0)}，越界：{summary.get('unsafe_path', 0)}，"
                f"暂存残留：{summary.get('staging_residual', 0)}，无法访问：{summary.get('inaccessible', 0)}"
            )
        self._show_service_result(result)

    def _choose_backup_path(self, title: str) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self, title, str(self.service.cfg.data_root_path / "backups"), "SQLite 备份 (*.db)"
        )
        return path

    def choose_verify_backup(self) -> None:
        path = self._choose_backup_path("选择要验证的数据库备份")
        if not path:
            self.show_message("已取消验证备份", "normal")
            return
        self._run_task(
            "正在验证数据库备份", lambda: self.service.verify_backup(path),
            self._show_service_result, button=self.verify_backup_button,
        )

    def choose_restore_backup(self) -> None:
        path = self._choose_backup_path("选择要恢复的数据库备份")
        if not path:
            self.show_message("已取消恢复备份", "normal")
            return
        confirmation = QMessageBox.question(
            self, "确认恢复数据库",
            "恢复前会自动备份当前数据库。附件文件不会被覆盖。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            self.show_message("已取消数据库恢复", "normal")
            return
        self._run_task(
            "正在校验并恢复数据库", lambda: self.service.restore_backup(path, confirmed=True),
            lambda result: (self._show_service_result(result), self.request_refresh()),
            button=self.restore_backup_button,
        )

    def open_backup_folder(self) -> None:
        folder = self.service.cfg.data_root_path / "backups"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError as exc:
            self.show_message(f"打开备份目录失败：{exc}", "error")
            return
        self.show_message("已打开备份目录", "success")

    def export_maintenance_report(self) -> None:
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存脱敏维护报告",
            str(self.service.cfg.data_root_path / "maintenance-report.md"),
            "Markdown 文件 (*.md)",
        )
        if not destination:
            self.show_message("已取消导出维护报告", "normal")
            return
        self._run_task(
            "正在导出脱敏维护报告",
            lambda: self.service.export_maintenance_report(destination),
            self._show_service_result,
            button=self.export_maintenance_button,
        )

    def _build_agent_page(self) -> QWidget:
        page, layout = self._standard_page(
            "发件 > Agent 发件 / MCP",
            "本机 stdio MCP 只允许提交白名单目录内的结果文件，收件人固定。",
        )
        back = QHBoxLayout()
        back.addStretch(1)
        back.addWidget(self._button("← 返回发件", lambda: self.select_page("send"), text_only=True))
        layout.addLayout(back)
        status_card = QFrame()
        status_card.setObjectName("card")
        status_grid = QGridLayout(status_card)
        status_grid.setContentsMargins(18, 14, 18, 14)
        status_grid.setHorizontalSpacing(18)
        status_grid.setVerticalSpacing(8)
        status_grid.addWidget(QLabel("MCP 状态"), 0, 0)
        mcp_command, _ = mcp_launch()
        packaged_mcp_ready = not get_runtime_paths().frozen or Path(mcp_command).is_file()
        self.mcp_status_label = QLabel(
            "已安装 · 按需启动 · stdio" if packaged_mcp_ready else "内部 MCP 组件缺失"
        )
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
        command, args = mcp_launch()
        launch_text = (
            subprocess.list2cmdline([command, *args])
            if get_runtime_paths().frozen
            else "python -m agent_mail_bridge.mcp_server"
        )
        self.mcp_command_text.setPlainText(
            f"内部接口：{launch_text}\n按需启动，会话结束自动退出\n\n"
            f"Codex：{mcp_client_command('codex')}\n"
            f"Claude Code：{mcp_client_command('claude')}"
        )
        layout.addWidget(self.mcp_command_text)
        actions = QHBoxLayout()
        actions.addWidget(self._button("复制 Codex 配置", lambda: self._copy_mcp_config("codex")))
        actions.addWidget(self._button("复制 Claude Code 配置", lambda: self._copy_mcp_config("claude")))
        actions.addWidget(self._button("复制通用 JSON", lambda: self._copy_mcp_config("json")))
        actions.addWidget(self._button("MCP 自检", self.run_mcp_self_check))
        self.mcp_refresh_button = self._button("刷新调用记录", primary=True)
        self.mcp_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.mcp_refresh_button)
        )
        actions.addWidget(self.mcp_refresh_button)
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

    def _build_about_page(self) -> QWidget:
        page, layout = self._standard_page("关于", "AgentMailBridge v1.0.0 产品与构建信息。")
        card = QFrame()
        card.setObjectName("card")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        logo = QLabel()
        apply_brand_label(logo, paint_app_icon)
        logo.setFixedSize(72, 72)
        asset = find_brand_asset()
        if asset:
            pixmap = QPixmap(str(asset))
            logo.setPixmap(pixmap.scaled(68, 68, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        text = QVBoxLayout()
        name = QLabel("AgentMailBridge")
        name.setObjectName("pageTitle")
        version = QLabel(f"版本 {__version__}")
        version.setObjectName("purpleText")
        position = QLabel("本地优先、单用户、Windows 优先的 Gmail 收件与 QQ 发件桥接工具。")
        position.setObjectName("hint")
        position.setWordWrap(True)
        text.addWidget(name)
        text.addWidget(version)
        text.addWidget(position)
        card_layout.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(text, 1)
        layout.addWidget(card)

        details = QFrame()
        details.setObjectName("card")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(18, 15, 18, 15)
        privacy = QLabel("隐私与安全：邮箱凭据存放在 Windows Credential Manager；OAuth、数据、邮件与附件保留在本机用户目录。")
        privacy.setWordWrap(True)
        details_layout.addWidget(privacy)
        repository = QLabel('<a href="https://github.com/xiaochuqing-dev/AgentMailBridge">开源仓库：github.com/xiaochuqing-dev/AgentMailBridge</a>')
        repository.setOpenExternalLinks(True)
        details_layout.addWidget(repository)
        build_info = QLabel(f"构建版本：{__version__} · Python / PySide6 桌面应用 · MCP stdio 按需启动")
        build_info.setObjectName("hint")
        details_layout.addWidget(build_info)
        links = QHBoxLayout()
        links.addWidget(self._button("查看 LICENSE", lambda: self._open_project_file("LICENSE")))
        links.addWidget(self._button("第三方说明", lambda: self._open_project_file("THIRD_PARTY_NOTICES.md")))
        links.addStretch(1)
        details_layout.addLayout(links)
        layout.addWidget(details)
        layout.addStretch(1)
        return page

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("rightPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(panel, "#FCFCFE")
        panel.setFixedWidth(350)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 24, 20, 18)
        layout.setSpacing(12)

        title = QLabel("服务状态")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.service_rows = {
            "service": StatusRow(line_icon_pixmap("shield", 17, PURPLE), "服务状态", "运行中"),
            "receive": StatusRow(line_icon_pixmap("clock", 17, PURPLE), "上次收取时间"),
            "send": StatusRow(line_icon_pixmap("send", 17, PURPLE), "上次发件时间"),
            "auto": StatusRow(line_icon_pixmap("calendar", 17, PURPLE), "自动收件状态", "未开启"),
            "qq": StatusRow(line_icon_pixmap("mail", 17, PURPLE), "QQ 邮箱", "未配置"),
        }
        service_card = QFrame()
        service_card.setObjectName("card")
        service_layout = QVBoxLayout(service_card)
        service_layout.setContentsMargins(12, 9, 12, 9)
        service_layout.setSpacing(0)
        for row in self.service_rows.values():
            service_layout.addWidget(row)
            if row is not tuple(self.service_rows.values())[-1]:
                service_layout.addWidget(horizontal_line())
        layout.addWidget(service_card)

        stats_title = QLabel("今日统计")
        stats_title.setObjectName("sectionTitle")
        layout.addWidget(stats_title)
        stats = QGridLayout()
        stats.setSpacing(9)
        self.stat_cards = {
            "received": StatCard("statPurple", line_icon_pixmap("mail", 28, PURPLE), "收取邮件", PURPLE),
            "saved": StatCard("statGreen", line_icon_pixmap("calendar", 28, SUCCESS), "保存文件", SUCCESS),
            "sent": StatCard("statBlue", line_icon_pixmap("send", 28, "#2394C8"), "发送邮件", "#2394C8"),
            "errors": StatCard("statRed", line_icon_pixmap("warning", 28, WARNING), "失败 / 错误", DANGER),
        }
        stats.addWidget(self.stat_cards["received"], 0, 0)
        stats.addWidget(self.stat_cards["saved"], 0, 1)
        stats.addWidget(self.stat_cards["sent"], 1, 0)
        stats.addWidget(self.stat_cards["errors"], 1, 1)
        layout.addLayout(stats)
        health_card = QFrame()
        health_card.setObjectName("card")
        health_layout = QVBoxLayout(health_card)
        health_layout.setContentsMargins(12, 11, 12, 11)
        health_title = QLabel("连接健康")
        health_title.setObjectName("minorTitle")
        self.health_summary_label = QLabel("5 项尚未检查")
        self.health_summary_label.setObjectName("hint")
        self.health_summary_label.setWordWrap(True)
        health_layout.addWidget(health_title)
        health_layout.addWidget(self.health_summary_label)
        self.health_rows: dict[str, HealthStatusRow] = {}
        health_specs = (
            ("Gmail 收件", "mail", "gmail"),
            ("QQ SMTP", "send", "qq"),
            ("Agent / MCP", "terminal", "agent"),
            ("凭据 / OAuth", "key", "credentials"),
            ("SQLite / 数据目录", "database", "files_data"),
        )
        for name, icon_kind, target in health_specs:
            row = HealthStatusRow(icon_kind, name, target)
            row.fix_requested.connect(self.go_to_health_target)
            self.health_rows[name] = row
            health_layout.addWidget(row)
        self.health_check_button = self._button(
            "一键检查全部",
            self.run_all_connection_diagnostics,
            primary=True,
            icon_kind="shield",
        )
        self.task_buttons.append(self.health_check_button)
        health_layout.addWidget(self.health_check_button)
        layout.addWidget(health_card)

        tips_title = QLabel("快捷提示")
        tips_title.setObjectName("sectionTitle")
        layout.addWidget(tips_title)
        layout.addWidget(TipRow(provider_icon("gmail"), "收件范围可通过“编辑偏好”调整。", PURPLE))
        layout.addWidget(TipRow(provider_icon("qq"), "QQ 发件固定发送到绑定的 Gmail。", "#329BC5"))
        help_button = self._button("查看帮助文档", self._show_help, text_only=True)
        layout.addWidget(help_button, 0, Qt.AlignmentFlag.AlignLeft)
        return panel

    def _standard_page(
        self,
        title: str,
        description: str,
        *,
        header_action_label: str | None = None,
    ) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("pageSurface")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(page, "#FFFFFF")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 22, 20)
        layout.setSpacing(12)
        heading_row = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        if header_action_label:
            page.header_action_button = self._button(
                header_action_label,
                outline=True,
                icon_kind="refresh",
            )
            heading_row.addWidget(page.header_action_button)
        hint = QLabel(description)
        hint.setObjectName("hint")
        layout.addLayout(heading_row)
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

    def _wire_config_change_tracking(self) -> None:
        """统一跟踪配置改动，避免用户误以为输入已自动生效。"""
        for combo in (self.network_combo,):
            combo.currentIndexChanged.connect(self._mark_config_dirty)
        for spin in (self.fetch_limit_spin, self.send_limit_spin):
            spin.valueChanged.connect(self._mark_config_dirty)
        for check in (self.startup_check,):
            check.toggled.connect(self._mark_config_dirty)

    def _mark_config_dirty(self, *_args) -> None:
        if self._loading_controls:
            return
        self._config_dirty = True
        self.unsaved_config_label.setText("有未保存的配置修改")
        self.unsaved_config_label.setObjectName("errorText")
        self.unsaved_config_label.style().unpolish(self.unsaved_config_label)
        self.unsaved_config_label.style().polish(self.unsaved_config_label)

    def _set_config_clean(self) -> None:
        self._config_dirty = False
        self.unsaved_config_label.setText("配置已保存")
        self.unsaved_config_label.setObjectName("successText")
        self.unsaved_config_label.style().unpolish(self.unsaved_config_label)
        self.unsaved_config_label.style().polish(self.unsaved_config_label)

    def _toggle_secret_visibility(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.gmail_password_edit.setEchoMode(mode)
        self.qq_auth_edit.setEchoMode(mode)
        self.show_message(
            "敏感字段已临时显示，请注意周围环境" if visible else "敏感字段已恢复隐藏",
            "normal",
        )

    def _button(
        self,
        label: str,
        callback: Callable | None = None,
        *,
        primary: bool = False,
        outline: bool = False,
        text_only: bool = False,
        icon_kind: str | None = None,
    ) -> QPushButton:
        button = QPushButton(label)
        if primary:
            button.setObjectName("primaryButton")
        elif outline:
            button.setObjectName("outlinePurple")
        elif text_only:
            button.setObjectName("textButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if icon_kind:
            button.setIcon(QIcon(line_icon_pixmap(icon_kind, 15, PURPLE)))
            button.setIconSize(QSize(15, 15))
        if callback is not None:
            button.clicked.connect(callback)
        return button

    def _configure_file_table(self, table: DataTable) -> None:
        header = table.horizontalHeader()
        header.setMinimumSectionSize(80)
        header.setStretchLastSection(False)
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 210)
        table.setColumnWidth(1, 88)
        table.setColumnWidth(2, 430)
        table.setColumnWidth(3, 150)
        table.setColumnWidth(4, 180)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

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
        target = name
        if target not in self.pages:
            target = "inbox"
        current_name = self._current_page_name()
        if (
            self._config_dirty
            and current_name in {"settings", "advanced"}
            and target != current_name
        ):
            choice = QMessageBox.question(
                self,
                "配置尚未保存",
                "当前配置有未保存修改，离开页面会保留输入但不会生效。仍要离开吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        self.page_stack.setCurrentWidget(self.pages[target])
        tab_target = "send" if target == "agent" else target
        self._set_exclusive_checked(self.tab_buttons, tab_target)
        nav_target = {
            "maintenance": "files_data",
            "advanced": "settings",
        }.get(target, target)
        self._set_exclusive_checked(self.nav_buttons, nav_target)

    def _current_page_name(self) -> str:
        if not hasattr(self, "page_stack"):
            return "inbox"
        current = self.page_stack.currentWidget()
        for name, page in self.pages.items():
            if page is current:
                return name
        return "inbox"

    @staticmethod
    def _set_exclusive_checked(buttons: dict[str, QPushButton], selected: str) -> None:
        """允许跨导航组页面清空旧选中态，再恢复互斥行为。"""
        for button in buttons.values():
            button.setAutoExclusive(False)
            button.setChecked(False)
        if selected in buttons:
            buttons[selected].setChecked(True)
        for button in buttons.values():
            button.setAutoExclusive(True)

    def open_add_account(self) -> None:
        open_add_account_dialog(self.service, self)
        self.show_message("已查看邮箱扩展说明；现有账号未被修改", "normal")

    def open_account(self, account_type: str) -> None:
        if open_account_dialog(self.service, account_type, self):
            self.refresh()
            self.show_message("邮箱账号配置已更新", "success")

    def save_receive_preferences(self) -> None:
        minutes = self._auto_minutes()
        try:
            save_env_values(
                {
                    "AUTO_RECEIVE_ONLY_SELF_MAIL": str(self.self_mail_check.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE": str(self.auto_switch.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE_INTERVAL_MINUTES": str(minutes),
                }
            )
        except OSError as exc:
            self.show_message(f"保存收件偏好失败：{exc}", "error")
            return
        self.service.cfg.auto_receive_only_self_mail = self.self_mail_check.isChecked()
        self._update_receive_preference_summary()
        self.show_message("收件偏好已保存", "success")

    def _update_receive_preference_summary(self) -> None:
        if not hasattr(self, "preference_summary_label"):
            return
        summary = (
            "仅本人邮件"
            if self.self_mail_check.isChecked()
            else "当前收件范围内的全部邮件"
        )
        self.preference_summary_label.setText(f"当前：{summary}")

    def open_receive_preferences_editor(self) -> None:
        """用小型对话框编辑 API 与 IMAP 共用的收件范围。"""
        dialog = QDialog(self)
        dialog.setObjectName("receivePreferencesDialog")
        dialog.setWindowTitle("编辑收件偏好")
        dialog.setModal(True)
        dialog.setMinimumWidth(480)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("选择要保存的邮件范围")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        only_self = QRadioButton("仅本人邮件（推荐）")
        only_self.setToolTip("只保存当前 Gmail 发给当前 Gmail 的可信邮件")
        all_scanned = QRadioButton("当前收件范围内的全部邮件")
        all_scanned.setToolTip("仍受查询范围、单次限制和去重规则约束")
        group = QButtonGroup(dialog)
        group.addButton(only_self)
        group.addButton(all_scanned)
        only_self.setChecked(self.self_mail_check.isChecked())
        all_scanned.setChecked(not self.self_mail_check.isChecked())
        layout.addWidget(only_self)
        layout.addWidget(all_scanned)
        note = QLabel("手动立即收取与自动收取、Gmail API 与 Gmail IMAP 均使用此设置。")
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        only_self_enabled = only_self.isChecked()
        if self.self_mail_check.isChecked() and not only_self_enabled:
            QMessageBox.information(
                self,
                "收件范围已扩大",
                "关闭“仅本人邮件”后，AgentMailBridge 会保存当前收件后端扫描到的其他邮件，请确认这符合你的使用需求。",
            )
        self.self_mail_check.setChecked(only_self_enabled)
        self.save_receive_preferences()

    def receive(self) -> None:
        if self.auto_switch.isChecked():
            self.show_message("自动收取已开启，手动收取已禁用", "normal")
            return
        sender = self.sender()
        button = sender if isinstance(sender, QPushButton) else None
        self._run_task(
            "正在连接 Gmail 并检查新邮件",
            self.service.receive,
            self._show_receive_result,
            button=button,
            working_text="收取中…",
        )

    def choose_and_send(self) -> None:
        if not self.choose_send_file():
            return
        self.select_page("send")

    def choose_send_file(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择待发送文件", str(Path.home()), "所有文件 (*.*)"
        )
        if not path:
            self.show_message("已取消选择文件")
            return False
        selected_path = Path(path)
        try:
            if not selected_path.is_file():
                raise OSError("文件不存在或不是普通文件")
            selection = SendFileSelection.capture(selected_path)
        except (OSError, SecurityError) as exc:
            self._clear_send_selection()
            self.show_message(f"无法选择该文件：{exc}", "error")
            return False
        self.send_selection = selection
        self.selected_send_path = str(selection.path)
        self.send_path_edit.setText(str(selection.path))
        self.send_path_edit.setToolTip(str(selection.path))
        self.send_file_name_value.setText(selection.path.name)
        self.send_file_name_value.setToolTip(selection.path.name)
        self.send_file_size_value.setText(format_size(selection.size))
        self.send_file_type_value.setText(selection.path.suffix.lower() or "无扩展名")
        self.send_file_modified_value.setText(
            datetime.fromtimestamp(selection.modified_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
        )
        for button in (
            self.copy_send_path_button,
            self.reveal_send_file_button,
            self.preview_send_file_button,
            self.send_action_button,
        ):
            button.setEnabled(True)
        self.send_status_label.setText("文件已锁定为本次选择；发送前还会检查是否被修改")
        self.show_message("文件已选择，请核对文件名、大小、路径和主题", "success")
        return True

    def send_selected_file(self) -> None:
        if self.task_active:
            self.show_message("已有后台任务正在运行，请等待完成后再发送", "working")
            return
        selection = self.send_selection
        if selection is None:
            self.show_message("请先选择待发送文件", "error")
            return
        if not selection.is_unchanged():
            self._clear_send_selection()
            self.show_message("文件已被删除、移动或修改，请重新选择后再发送", "error")
            return
        subject = self.subject_edit.text().strip() or None
        subject_text = subject or f"AgentMailBridge 文件：{selection.path.name}"
        confirmation = QMessageBox.question(
            self,
            "确认发送文件",
            "请确认本次真实发送内容：\n\n"
            f"附件：{selection.path.name}\n"
            f"大小：{format_size(selection.size)}\n"
            f"主题：{subject_text}\n"
            f"固定收件人：{self.recipient_edit.text().strip() or '未配置'}\n\n"
            "确认后才会连接 QQ SMTP。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            self.send_status_label.setText("已取消发送，当前文件仍保留供重新核对")
            self.show_message("已取消发送，没有连接邮件服务器", "normal")
            return
        self.send_status_label.setText("正在校验文件并连接 QQ SMTP")
        self.send_progress.show()
        self._run_task(
            "正在校验并发送文件，请勿重复点击",
            lambda: self._send_unchanged_selection(selection, subject),
            self._show_send_result,
            button=self.send_action_button,
            working_text="正在发送…",
        )

    def _send_unchanged_selection(
        self, selection: SendFileSelection, subject: str | None
    ) -> ServiceResult:
        """后台发送前再次校验快照，缩小确认后文件被替换的窗口。"""
        if not selection.is_unchanged():
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="file_changed",
                message="确认后文件发生变化，已阻止发送",
            )
        return self.service.send_user_selected_file(
            selection.path,
            subject=subject,
            expected_sha256=selection.sha256,
        )

    def _clear_send_selection(self) -> None:
        self.send_selection = None
        self.selected_send_path = ""
        self.send_path_edit.clear()
        self.send_path_edit.setToolTip("")
        self.send_file_name_value.setText("未选择")
        self.send_file_size_value.setText("—")
        self.send_file_type_value.setText("—")
        self.send_file_modified_value.setText("—")
        for button in (
            self.copy_send_path_button,
            self.reveal_send_file_button,
            self.preview_send_file_button,
            self.send_action_button,
        ):
            button.setEnabled(False)
        self.send_status_label.setText("请选择本次要发送的文件")

    def copy_selected_send_path(self) -> None:
        if self.send_selection is None:
            self.show_message("当前没有已选择文件", "error")
            return
        QApplication.clipboard().setText(str(self.send_selection.path))
        self.show_message("完整文件路径已复制", "success")

    def reveal_selected_send_file(self) -> None:
        if self.send_selection is None:
            self.show_message("当前没有已选择文件", "error")
            return
        self._reveal_file(self.send_selection.path)

    def preview_selected_send_file(self) -> None:
        if self.send_selection is None:
            self.show_message("当前没有已选择文件", "error")
            return
        self._preview_path(str(self.send_selection.path))

    def test_connection(self) -> None:
        sender = self.sender()
        button = sender if isinstance(sender, QPushButton) else None
        backend = self.service.cfg.gmail_receive_backend
        if backend == "gmail_api" or (backend == "auto" and self.service.cfg.gmail_api_configured):
            self._diagnose("正在测试 Gmail API 连接", self.service.diagnose_gmail_api, button)
        else:
            self._diagnose("正在测试 Gmail IMAP 连接", self.service.diagnose_imap, button)

    def _diagnose(
        self,
        title: str,
        operation: Callable[[], ServiceResult],
        button: QPushButton | None = None,
    ) -> None:
        self._run_task(title, operation, self._show_service_result, button=button, operation_name="诊断")

    def authorize_gmail_api(self) -> None:
        self._run_task(
            "正在进行 Gmail API 授权",
            self.service.authorize_gmail_api,
            self._show_service_result,
            button=getattr(self, "authorize_button", None),
            operation_name="授权",
        )

    def import_oauth_json(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "导入 Gmail OAuth 客户端配置",
            "",
            "JSON 文件 (*.json)",
        )
        if not source:
            return
        target = self.service.cfg.gmail_api_credentials_path
        replace = False
        if target.exists():
            replace = QMessageBox.question(
                self,
                "替换 OAuth 客户端配置",
                "受控 OAuth 目录中已存在配置，确认替换吗？现有 token 不会删除。",
            ) == QMessageBox.StandardButton.Yes
            if not replace:
                return
        result = self.service.import_oauth_credentials(Path(source), replace=replace)
        if not result.ok:
            self._show_service_result(result)
            return
        self.refresh()
        self.show_message("OAuth 客户端配置已复制到受控用户目录", "success")

    def save_basic_config(self) -> None:
        email = self.gmail_email_edit.text().strip()
        backend = str(self.backend_combo.currentData())
        password = self.gmail_password_edit.text()
        if not self._valid_email(email):
            self.show_message("请输入有效的 Gmail 地址", "error")
            return
        if backend == "imap" and not (password.strip() or self.service.cfg.gmail_app_password):
            self.show_message("IMAP 模式需要 Gmail 应用专用密码", "error")
            return
        minutes = int(self.interval_combo.currentData() or AUTO_RECEIVE_DEFAULT_MINUTES)
        previous_password = self.service.cfg.gmail_app_password
        if password.strip():
            credential_result = self.service.set_credential(GMAIL_IMAP_SECRET, password)
            if not credential_result.ok:
                self.show_message(f"保存 IMAP 凭据失败：{credential_result.message}", "error")
                return
        try:
            save_env_values(
                {
                    "GMAIL_ADDRESS": email,
                    "OWNER_GMAIL": email,
                    "GMAIL_APP_PASSWORD": "",
                    "GMAIL_RECEIVE_BACKEND": backend,
                    "AUTO_RECEIVE_ONLY_SELF_MAIL": str(self.self_mail_check.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE": str(self.auto_switch.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE_INTERVAL_MINUTES": str(minutes),
                }
            )
        except OSError as exc:
            if password.strip():
                if previous_password:
                    self.service.set_credential(GMAIL_IMAP_SECRET, previous_password)
                else:
                    self.service.delete_credential(GMAIL_IMAP_SECRET)
            self.show_message(f"保存配置失败：{exc}", "error")
            return
        self.service.cfg.gmail_address = email
        self.service.cfg.owner_gmail = email
        self.gmail_password_edit.clear()
        self.gmail_password_edit.setPlaceholderText("已配置；留空保持不变")
        self.service.cfg.gmail_receive_backend = backend
        self.service.cfg.auto_receive_only_self_mail = self.self_mail_check.isChecked()
        self.recipient_edit.setText(email)
        self.refresh()
        self._set_config_clean()
        self.show_message("配置已安全保存并在当前运行中生效", "success")

    def save_advanced_config(self) -> None:
        network_mode = str(self.network_combo.currentData())
        previous_startup = StartupManager.is_enabled()
        desired_startup = self.startup_check.isChecked()
        startup_changed = desired_startup != previous_startup
        try:
            if startup_changed:
                StartupManager.set_enabled(desired_startup)
        except OSError as exc:
            self.show_message(f"开机启动设置失败，其他配置未写入：{exc}", "error")
            return
        try:
            save_env_values(
                {
                    "GMAIL_NETWORK_MODE": network_mode,
                    "MAX_FETCH_LIMIT": str(self.fetch_limit_spin.value()),
                    "MAX_SEND_FILE_MB": str(self.send_limit_spin.value()),
                }
            )
        except OSError as exc:
            rollback_message = "开机启动状态已回滚"
            try:
                if startup_changed:
                    StartupManager.set_enabled(previous_startup)
            except OSError:
                rollback_message = "开机启动状态回滚失败，请手动检查"
            self.show_message(f"保存设置失败：{exc}；{rollback_message}", "error")
            return
        self.service.cfg.gmail_network_mode = network_mode
        self.service.cfg.max_fetch_limit = self.fetch_limit_spin.value()
        self.service.cfg.max_send_file_mb = self.send_limit_spin.value()
        self.refresh()
        self._set_config_clean()
        self.show_message("设置已安全保存", "success")

    def open_managed_directory(self, path: Path) -> None:
        """创建并打开应用受控目录，不要求用户复制或记忆内部路径。"""
        try:
            path.mkdir(parents=True, exist_ok=True)
            allowed = [get_runtime_paths().user_root, self.service.cfg.data_root_path]
            assert_within_allowed_roots(path, allowed)
            os.startfile(str(path))
        except (OSError, SecurityError) as exc:
            self.show_message(f"打开目录失败：{exc}", "error")
            return
        self.show_message("目录已打开", "success")

    def run_all_connection_diagnostics(self) -> None:
        self._run_task(
            "正在执行一键检查",
            self._collect_all_connection_diagnostics,
            self._show_health_check_result,
            button=self.health_check_button,
            operation_name="一键检查",
        )

    def _collect_all_connection_diagnostics(self) -> ServiceResult:
        backend = self.service.cfg.gmail_receive_backend
        receive_result = (
            self.service.diagnose_gmail_api()
            if backend == "gmail_api" or (backend == "auto" and self.service.cfg.gmail_api_configured)
            else self.service.diagnose_imap()
        )
        checks = [
            {
                "name": "Gmail 收件",
                "ok": receive_result.ok,
                "state": "normal" if receive_result.ok else "fault",
                "message": receive_result.message or receive_result.error_code,
                "target": "gmail",
            }
        ]
        qq_result = (
            self.service.diagnose_qq_smtp()
            if self.service.cfg.qq_email and self.service.cfg.qq_auth_code
            else ServiceResult(OperationStatus.FAILED, error_code="qq_not_configured", message="QQ SMTP 未配置")
        )
        checks.append({
            "name": "QQ SMTP",
            "ok": qq_result.ok,
            "state": "normal" if qq_result.ok else "partial" if qq_result.error_code == "qq_not_configured" else "fault",
            "message": qq_result.message,
            "target": "qq",
        })
        command, _ = mcp_launch()
        mcp_ok = not get_runtime_paths().frozen or Path(command).is_file()
        checks.append({
            "name": "Agent / MCP",
            "ok": mcp_ok and bool(self.service.cfg.owner_gmail) and bool(self.service.cfg.effective_allowed_send_roots),
            "state": "normal" if mcp_ok and self.service.cfg.owner_gmail and self.service.cfg.effective_allowed_send_roots else "partial" if mcp_ok else "fault",
            "message": "stdio 组件与安全边界正常" if mcp_ok and self.service.cfg.owner_gmail and self.service.cfg.effective_allowed_send_roots else "请检查固定收件人、允许目录或内部组件",
            "target": "agent",
        })
        status = self.service.get_config_and_connection_status().details
        active_auth_ok = (
            status.get("gmail_api", {}).get("state") in {"READY", "TOKEN_VALID", "TOKEN_EXPIRED_REFRESHABLE"}
            if backend == "gmail_api"
            else status.get("imap") == "configured"
        )
        checks.append({
            "name": "凭据 / OAuth",
            "ok": bool(active_auth_ok and status.get("qq_smtp") == "configured"),
            "state": "normal" if active_auth_ok and status.get("qq_smtp") == "configured" else "partial",
            "message": "当前凭据状态完整" if active_auth_ok else "当前收件认证未就绪",
            "target": "gmail" if not active_auth_ok else "qq",
        })
        maintenance = self.service.get_maintenance_status()
        database_ok = maintenance.ok and maintenance.details.get("integrity_check") == "ok"
        checks.append({
            "name": "SQLite / 数据目录",
            "ok": database_ok and self.service.cfg.data_root_path.exists(),
            "state": "normal" if database_ok and self.service.cfg.data_root_path.exists() else "fault",
            "message": "数据库与数据目录正常" if database_ok else maintenance.message or "数据库检查失败",
            "target": "files_data",
        })
        failed = [item for item in checks if not item["ok"]]
        if failed:
            status_value = OperationStatus.FAILED if len(failed) == len(checks) else OperationStatus.PARTIAL
            return ServiceResult(
                status_value,
                error_code="health_check_failed",
                message=f"{len(checks) - len(failed)}/{len(checks)} 项正常",
                details={"checks": checks, "target": failed[0]["target"]},
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"{len(checks)}/{len(checks)} 项检查通过",
            details={"checks": checks},
        )

    def _show_health_check_result(self, result: ServiceResult) -> None:
        checks = result.details.get("checks", [])
        checked_at = datetime.now().strftime("%H:%M:%S")
        passed = sum(1 for item in checks if item.get("ok"))
        self.health_summary_label.setText(
            f"最近检查 {checked_at} · {passed}/{len(checks)} 项正常"
            if checks else result.message
        )
        self.health_summary_label.setToolTip("\n".join(
            f"{item.get('name')}：{item.get('message')}" for item in checks
        ))
        for item in checks:
            row = self.health_rows.get(str(item.get("name")))
            if row is not None:
                row.target = str(item.get("target") or row.target)
                row.set_status(
                    str(item.get("state") or ("normal" if item.get("ok") else "fault")),
                    str(item.get("message") or "未提供检查说明"),
                    checked_at,
                )
        self.health_fix_target = str(result.details.get("target") or "")
        self._show_service_result(result)

    def go_to_health_issue(self) -> None:
        self.go_to_health_target(self.health_fix_target)

    def go_to_health_target(self, target: str) -> None:
        if target in {"gmail", "credentials"}:
            self.open_account(AccountTypeDialog.GMAIL)
        elif target == "qq":
            self.open_account(AccountTypeDialog.QQ)
        elif target in {"agent", "files_data"}:
            self.select_page(target)

    def run_mcp_self_check(self) -> None:
        command, _ = mcp_launch()
        component_ok = not get_runtime_paths().frozen or Path(command).is_file()
        boundaries_ok = bool(self.service.cfg.owner_gmail and self.service.cfg.effective_allowed_send_roots)
        if component_ok and boundaries_ok:
            self.show_message("MCP 自检通过：stdio 按需启动、固定收件人和允许目录均已就绪", "success")
        else:
            self.show_message("MCP 自检未通过：请检查内部组件、固定收件人或允许目录", "error")

    def import_legacy_configuration(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择旧版 .env",
            "",
            "环境配置 (.env *.env);;所有文件 (*)",
        )
        if not source:
            self.show_message("已取消配置迁移", "normal")
            return
        confirmation = QMessageBox.question(
            self,
            "导入旧版配置",
            "程序将迁移非敏感配置和 Windows 凭据，失败时回滚。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            self.show_message("已取消配置迁移", "normal")
            return
        try:
            result = import_legacy_env(Path(source))
        except Exception as exc:  # noqa: BLE001
            self.show_message(f"配置迁移失败：{exc}", "error")
            return
        self.show_message(f"配置迁移完成：已导入 {len(result.imported_keys)} 项", "success")

    def delete_credential(self, name: str) -> None:
        """明确确认后删除单项 Windows 凭据。"""
        confirmation = QMessageBox.question(
            self,
            "确认删除凭据",
            "删除后对应邮箱连接将不可用，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            self.show_message("已取消删除凭据", "normal")
            return
        result = self.service.delete_credential(name)
        if result.ok:
            if name == GMAIL_IMAP_SECRET and hasattr(self, "gmail_password_edit"):
                self.gmail_password_edit.setPlaceholderText("未配置")
            elif name == QQ_SMTP_SECRET and hasattr(self, "qq_auth_edit"):
                self.qq_auth_edit.setPlaceholderText("未配置")
        self._show_service_result(result)

    def refresh(self) -> None:
        if self.task_active:
            self.show_message("当前任务尚未完成", "working")
            return
        self._apply_refresh_result(self._collect_refresh_result())

    def request_refresh(self, button: QPushButton | None = None) -> None:
        """手动刷新在线程池执行，避免数据库或文件扫描阻塞界面。"""
        self._run_task(
            "正在刷新页面数据",
            self._collect_refresh_result,
            self._apply_refresh_result,
            button=button,
            working_text="刷新中…",
            refresh_on_finish=False,
        )

    def _collect_refresh_result(self) -> ServiceResult:
        try:
            status = self.service.get_config_and_connection_status().details
            files = self.service.get_today_files().details.get("files", [])
            logs = self.service.get_recent_logs(100).details.get("events", [])
            logs.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
            history = self.service.get_history(100).details
            mcp = self.service.get_mcp_history(100).details.get("calls", [])
            maintenance = self.service.get_maintenance_status()
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="refresh_failed",
                message=f"刷新界面失败：{exc}",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "status": status,
                "files": files,
                "logs": logs,
                "history": history,
                "mcp": mcp,
                "maintenance": maintenance.to_dict(),
            },
        )

    def _apply_refresh_result(self, result: ServiceResult) -> None:
        if not result.ok:
            self.last_error_details = self._redact_error_details(result.message)
            self.error_details_button.setEnabled(True)
            self.show_message(self._friendly_result_message(result), "error")
            return
        status = result.details.get("status", {})
        self.file_rows = result.details.get("files", [])
        self.log_rows = result.details.get("logs", [])
        self.history_rows = result.details.get("history", {"received": [], "sent": []})
        self.mcp_rows = result.details.get("mcp", [])
        self._apply_config_to_controls(status)
        self._populate_files(self.files_table, self.file_rows, actions=True)
        self._populate_logs(self.logs_table, self.log_rows[:30])
        self._populate_sent_history()
        if hasattr(self, "history_table"):
            self._populate_history()
        if hasattr(self, "managed_files_table"):
            self._rebuild_managed_files()
            self._filter_managed_files()
        if hasattr(self, "full_logs_table"):
            self._populate_full_logs()
        if hasattr(self, "mcp_table"):
            self._populate_mcp_history()
        maintenance = result.details.get("maintenance", {})
        maintenance_details = maintenance.get("details", {})
        if hasattr(self, "data_overview_label"):
            self.data_overview_label.setText(
                f"SQLite {maintenance_details.get('integrity_check', '—')} · "
                f"数据库 {format_size(maintenance_details.get('database_size_bytes', 0))} · "
                f"收件 {format_size(maintenance_details.get('received', {}).get('size_bytes', 0))} · "
                f"发送归档 {format_size(maintenance_details.get('sent', {}).get('size_bytes', 0))} · "
                f"备份 {format_size(maintenance_details.get('backups_size_bytes', 0))}"
            )
        self._update_right_panel(status)
        self.last_refresh_at = datetime.now()
        refresh_text = f"最后刷新 {self.last_refresh_at.strftime('%H:%M:%S')}"
        self.home_refresh_label.setText(refresh_text)
        self.logs_refresh_label.setText(refresh_text)
        self.dashboard_refresh_label.setText(refresh_text)
        self.show_message(f"状态已刷新，{self.last_refresh_at.strftime('%H:%M:%S')}", "success")

    def _apply_config_to_controls(self, status: dict) -> None:
        cfg = self.service.cfg
        self._loading_controls = True
        try:
            self.gmail_card.email_label.setText(cfg.gmail_address or "未配置")
            self.qq_card.email_label.setText(cfg.qq_email or "未配置")
            self.gmail_card.set_configured(bool(cfg.gmail_address))
            self.qq_card.set_configured(bool(cfg.qq_email))
            self.receive_account_label.setText(cfg.gmail_address or "尚未配置 Gmail 收件账号")
            self.self_mail_check.setChecked(cfg.auto_receive_only_self_mail)
            self._update_receive_preference_summary()
            self.recipient_edit.setText(cfg.owner_gmail or cfg.gmail_address)
            self._set_combo_data(self.network_combo, cfg.gmail_network_mode)
        finally:
            self._loading_controls = False
        masked = status.get("config", {})
        summary_lines = [
            f"收件后端：{status.get('receive_backend', '—')}",
            f"Gmail：{self._mask_email_for_display(str(masked.get('gmail_address') or ''))}",
            f"Gmail 密钥：{masked.get('gmail_app_password') or '未配置'}",
            f"Gmail API：{status.get('gmail_api', {}).get('state', '—')}",
            f"QQ 邮箱：{self._mask_email_for_display(str(masked.get('qq_email') or ''))}",
            f"QQ 授权码：{masked.get('qq_auth_code') or '未配置'}",
            f"网络模式：{masked.get('gmail_network_mode', '—')}",
            f"数据目录：{'可用' if cfg.data_root_path.exists() else '不存在'}（完整路径已隐藏）",
            f"允许发送目录数量：{len(masked.get('allowed_send_roots', []))}",
            f"Gmail API 权限：{masked.get('gmail_api_scopes', '—')}",
        ]
        self.config_summary.setPlainText("\n".join(summary_lines))

    def _update_right_panel(self, status: dict) -> None:
        backend = status.get("receive_backend", "—")
        oauth = status.get("gmail_api", {}).get("state", "—")
        qq = status.get("qq_smtp", "not_configured")
        backend_text = {
            "gmail_api": "Gmail API",
            "imap": "Gmail IMAP",
            "auto": "自动选择",
        }.get(str(backend), str(backend))
        oauth_key = str(oauth).upper()
        oauth_text = {
            "READY": "已授权",
            "TOKEN_VALID": "已授权",
            "TOKEN_EXPIRED_REFRESHABLE": "可安全刷新",
            "TOKEN_EXPIRED": "需重新授权",
            "NOT_CONFIGURED": "未配置",
            "CREDENTIALS_MISSING": "未配置",
            "TOKEN_MISSING": "未授权",
        }.get(oauth_key)
        if oauth_text is None:
            oauth_text = "未配置" if "MISSING" in oauth_key else "需重新授权" if "EXPIRED" in oauth_key else "状态待检查"
        qq_text_short = "已配置" if qq == "configured" else "未配置"
        self.service_rows["service"].set_value("● 运行中", success=True)
        self.service_rows["auto"].set_value("已开启" if self.auto_switch.isChecked() else "未开启", success=self.auto_switch.isChecked())
        qq_text = self.service.cfg.qq_email or "未配置"
        self.service_rows["qq"].set_value(qq_text, success=qq == "configured")
        receive_time = self._latest_event_time(("receive", "收件"))
        send_time = self._latest_event_time(("send", "sent", "发件", "发送"))
        self.service_rows["receive"].set_value(receive_time)
        self.service_rows["send"].set_value(send_time)
        self.title_bar.status_label.setText("服务已启动")
        self.title_bar.status_label.setToolTip(f"Gmail API：{oauth}")
        health_detail = (
            f"收件：{backend_text} · Gmail 授权：{oauth_text} · QQ 发件：{qq_text_short}"
        )
        self.dashboard_health_detail.setText(health_detail)
        self.dashboard_health_detail.setToolTip(health_detail)

        today = datetime.now().strftime("%Y-%m-%d")
        sent_today = sum(1 for row in self.history_rows.get("sent", []) if str(row.get("sent_at", "")).startswith(today) and row.get("status") in {"sent", "success"})
        error_today = sum(
            1
            for row in self.log_rows
            if str(row.get("created_at", "")).startswith(today)
            and (
                str(row.get("level", "")).upper() in {"ERROR", "FAILED"}
                or (
                    str(row.get("level", "")).upper() == "WARNING"
                    and str(row.get("event_type", "")).lower() == "receive"
                    and "失败" in str(row.get("message", ""))
                )
            )
        )
        received_message_ids = {
            str(row.get("message_id")) for row in self.file_rows if row.get("message_id")
        }
        self.stat_cards["received"].set_count(len(received_message_ids))
        self.stat_cards["saved"].set_count(sum(1 for row in self.file_rows if row.get("status") in {"saved", "ok", "normal"}))
        self.stat_cards["sent"].set_count(sent_today)
        self.stat_cards["errors"].set_count(error_today)

    def _populate_files(self, table: DataTable, rows: list[dict], *, actions: bool) -> None:
        table.setRowCount(0)
        for row_index, row in enumerate(rows):
            table.insertRow(row_index)
            path = str(row.get("path_display") or row.get("saved_path") or row.get("body_file_path") or "")
            values = [
                str(row.get("saved_filename") or Path(path).name or "未命名文件"),
                format_size(row.get("size_now") if row.get("size_now") is not None else row.get("size_bytes")),
                path,
                self._short_time(row.get("created_at") or row.get("received_at"), include_date=True),
                "" if actions else str(row.get("status") or "saved"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                if column in {0, 2, 3}:
                    item.setToolTip(value)
                table.setItem(row_index, column, item)
            if actions:
                action_widget = QWidget()
                action_widget.setObjectName("tableActions")
                action_layout = QHBoxLayout(action_widget)
                action_layout.setContentsMargins(4, 2, 4, 2)
                action_layout.setSpacing(5)
                open_button = self._button(
                    "打开",
                    lambda checked=False, value=path: self._open_received_file(value),
                    icon_kind="open",
                )
                open_button.setObjectName("compactButton")
                copy_button = self._button("复制路径", icon_kind="copy")
                copy_button.setObjectName("compactButton")
                copy_button.clicked.connect(
                    lambda checked=False, button=copy_button, value=path: self._copy_received_path(button, value)
                )
                action_layout.addWidget(open_button)
                action_layout.addWidget(copy_button)
                table.setCellWidget(row_index, 4, action_widget)
        if rows:
            table.resizeColumnToContents(0)
            table.resizeColumnToContents(1)
            table.resizeColumnToContents(2)
            table.resizeColumnToContents(3)
            table.setColumnWidth(0, max(180, table.columnWidth(0)))
            table.setColumnWidth(2, max(360, table.columnWidth(2)))
            table.setColumnWidth(3, max(145, table.columnWidth(3)))
            table.setColumnWidth(4, 180)

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
        keyword = self.log_search.text().strip().lower()
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("event_type", "")).lower()
                or keyword in str(row.get("message", "")).lower()
            ]
        rows = [row for row in rows if self._matches_time_filter(row.get("created_at"), self.log_time_filter.currentText())]
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
            path = str(row.get("sent_copy_path") or row.get("send_copy_path") or "")
            filename = str(row.get("original_filename") or Path(path).name or "—")
            origin = "用户手动选择" if row.get("source_origin") == "manual_gui" else "受控目录"
            values = [
                filename,
                format_size(int(row.get("size_bytes") or 0)),
                origin,
                str(row.get("request_id") or "—"),
                self._short_time(row.get("sent_at"), include_date=True),
                str(row.get("status") or "—"),
            ]
            for column, value in enumerate(values):
                self.sent_table.setItem(index, column, QTableWidgetItem(value))

    def _populate_history(self) -> None:
        received = [("收件", row) for row in self.history_rows.get("received", [])]
        sent = [("发件", row) for row in self.history_rows.get("sent", [])]
        agent = [("Agent / MCP", row) for row in self.mcp_rows]
        combined = received + sent + agent
        combined.sort(key=lambda pair: str(pair[1].get("created_at") or pair[1].get("sent_at") or pair[1].get("received_at") or ""), reverse=True)
        selected_type = self.history_type_filter.currentText()
        if selected_type != "全部类型":
            combined = [pair for pair in combined if pair[0] == selected_type]
        selected_status = self.history_status_filter.currentText()
        if selected_status != "全部状态":
            def status_group(row: dict) -> str:
                status = str(row.get("status") or "").lower()
                if status in {"sent", "success", "saved", "ok", "accepted"}:
                    return "成功"
                if status in {"failed", "error", "rejected"}:
                    return "失败"
                if status in {"duplicate", "duplicated"}:
                    return "重复"
                return "其他"
            combined = [pair for pair in combined if status_group(pair[1]) == selected_status]
        selected_time = self.history_time_filter.currentText()
        combined = [
            pair for pair in combined
            if self._matches_time_filter(
                pair[1].get("created_at") or pair[1].get("sent_at") or pair[1].get("received_at"),
                selected_time,
            )
        ]
        keyword = self.history_search.text().strip().lower()
        if keyword:
            combined = [
                pair for pair in combined
                if keyword in str(pair[1].get("subject") or "").lower()
                or keyword in str(pair[1].get("original_filename") or "").lower()
                or keyword in str(pair[1].get("file_path") or "").lower()
                or keyword in str(pair[1].get("request_id") or "").lower()
            ]
        self.history_table.setRowCount(0)
        for index, (direction, row) in enumerate(combined):
            self.history_table.insertRow(index)
            path = str(row.get("body_file_path") or row.get("sent_copy_path") or row.get("send_copy_path") or row.get("file_path") or row.get("source_path") or "")
            title = str(row.get("subject") or row.get("original_filename") or Path(path).name or "—")
            time_value = row.get("created_at") or row.get("sent_at") or row.get("received_at")
            values = [
                direction,
                title,
                str(row.get("request_id") or "—"),
                self._short_time(time_value, include_date=True),
                str(row.get("status") or "—"),
                path,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setData(Qt.ItemDataRole.UserRole + 1, row)
                self.history_table.setItem(index, column, item)

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
                or keyword in str(row.get("subject", "")).lower()
            ]
        self._populate_files(self.inbox_table, rows, actions=True)

    @staticmethod
    def _matches_time_filter(value, selected: str) -> bool:
        if selected == "全部时间":
            return True
        raw = str(value or "").replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            return False
        now = datetime.now()
        if selected == "今天":
            return moment.date() == now.date()
        days = 7 if selected == "最近 7 天" else 30
        return moment >= now.replace(microsecond=0) - timedelta(days=days)

    def _rebuild_managed_files(self) -> None:
        rows: list[dict] = []
        for row in self.history_rows.get("received", []):
            path = str(row.get("body_file_path") or "")
            if path:
                rows.append({
                    "type": "收件文件", "source": "Gmail", "path": path,
                    "name": Path(path).name, "size_bytes": row.get("size_bytes", 0),
                    "time": row.get("created_at") or row.get("received_at"),
                })
        for row in self.history_rows.get("sent", []):
            path = str(row.get("sent_copy_path") or row.get("send_copy_path") or "")
            if path:
                rows.append({
                    "type": "已发送归档",
                    "source": "手动发件" if row.get("source_origin") == "manual_gui" else "Agent / MCP",
                    "path": path,
                    "name": str(row.get("original_filename") or Path(path).name),
                    "size_bytes": row.get("size_bytes", 0),
                    "time": row.get("sent_at") or row.get("created_at"),
                })
        for row in self.mcp_rows:
            path = str(row.get("file_path") or "")
            if path:
                rows.append({
                    "type": "Agent 结果", "source": "Agent / MCP", "path": path,
                    "name": Path(path).name, "size_bytes": row.get("size_bytes", 0),
                    "time": row.get("created_at"),
                })
        rows.sort(key=lambda row: str(row.get("time") or ""), reverse=True)
        self.managed_file_rows = rows[:300]

    def _filter_managed_files(self, *_args) -> None:
        if not hasattr(self, "managed_files_table"):
            return
        keyword = self.file_data_search.text().strip().lower()
        selected_type = self.file_data_type_filter.currentText()
        selected_source = self.file_data_source_filter.currentText()
        selected_time = self.file_data_time_filter.currentText()
        rows = [
            row for row in self.managed_file_rows
            if (not keyword or keyword in str(row.get("name", "")).lower() or keyword in str(row.get("path", "")).lower())
            and (selected_type == "全部类型" or row.get("type") == selected_type)
            and (selected_source == "全部来源" or row.get("source") == selected_source)
            and self._matches_time_filter(row.get("time"), selected_time)
        ]
        self.managed_files_table.setRowCount(0)
        for index, row in enumerate(rows):
            self.managed_files_table.insertRow(index)
            values = [
                str(row.get("type")), str(row.get("source")), str(row.get("name")),
                format_size(row.get("size_bytes")), self._short_time(row.get("time"), include_date=True),
                str(row.get("path")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, str(row.get("path")))
                item.setToolTip(str(row.get("path")) if column in {2, 5} else value)
                self.managed_files_table.setItem(index, column, item)

    def _selected_path(self, table: DataTable) -> str:
        row = table.currentRow()
        if row < 0 or table.columnCount() == 0:
            return ""
        item = table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _preview_managed_file(self, row: int, _column: int) -> None:
        item = self.managed_files_table.item(row, 0)
        if item:
            self._preview_path(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def preview_selected_managed_file(self) -> None:
        path = self._selected_path(self.managed_files_table)
        self._preview_path(path) if path else self.show_message("请先选择文件", "warning")

    def reveal_selected_managed_file(self) -> None:
        path = self._selected_path(self.managed_files_table)
        self._reveal_file(Path(path)) if path else self.show_message("请先选择文件", "warning")

    def copy_selected_managed_file_path(self) -> None:
        path = self._selected_path(self.managed_files_table)
        if not path:
            self.show_message("请先选择文件", "warning")
            return
        QApplication.clipboard().setText(path)
        self.show_message("文件路径已复制", "success")

    def open_send_history(self) -> None:
        self.select_page("history")
        self.history_type_filter.setCurrentText("发件")

    def _show_history_detail(self, row: int, _column: int) -> None:
        item = self.history_table.item(row, 0)
        details = item.data(Qt.ItemDataRole.UserRole + 1) if item else {}
        if not isinstance(details, dict):
            details = {}
        safe = {
            key: value for key, value in details.items()
            if key not in {"body", "raw_message", "secret", "token"}
        }
        QMessageBox.information(self, "记录详情", "\n".join(f"{key}：{value}" for key, value in safe.items()) or "暂无详情")

    def show_selected_history_detail(self) -> None:
        row = self.history_table.currentRow()
        if row < 0:
            self.show_message("请先选择记录", "warning")
            return
        self._show_history_detail(row, 0)

    def reveal_selected_history_file(self) -> None:
        path = self._selected_path(self.history_table)
        self._reveal_file(Path(path)) if path else self.show_message("当前记录没有可定位文件", "warning")

    def _show_log_detail(self, row: int, _column: int) -> None:
        values = [self.full_logs_table.item(row, column).text() for column in range(self.full_logs_table.columnCount())]
        QMessageBox.information(self, "日志详情", "\n".join(values))

    def show_selected_log_detail(self) -> None:
        row = self.full_logs_table.currentRow()
        if row < 0:
            self.show_message("请先选择日志", "warning")
            return
        self._show_log_detail(row, 0)

    def open_log_folder(self) -> None:
        folder = self.service.cfg.data_root_path / "logs"
        self.open_managed_directory(folder)

    def _open_project_file(self, filename: str) -> None:
        root = get_runtime_paths().resource_root.parent if get_runtime_paths().frozen else Path(__file__).resolve().parents[2]
        path = root / filename
        if not path.is_file():
            self.show_message(f"未找到 {filename}", "error")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            self.show_message(f"打开文件失败：{exc}", "error")

    def _run_task(
        self,
        title: str,
        operation: Callable[[], ServiceResult],
        callback: Callable[[ServiceResult], None],
        *,
        button: QPushButton | None = None,
        operation_name: str | None = None,
        working_text: str | None = None,
        refresh_on_finish: bool = True,
    ) -> None:
        if not getattr(self, "accepting_tasks", True):
            self.error_var.set("程序正在退出，不再启动新任务")
            return
        if self.task_active:
            self.error_var.set("已有任务正在运行，请勿重复点击")
            return
        self.task_active = True
        self._task_refresh_on_finish = refresh_on_finish
        self.status_var.set(title)
        self._active_task_button = button
        if button is not None:
            self._active_task_button_text = button.text()
            button.setEnabled(False)
            button.setToolTip("正在执行，请稍候")
            button.setProperty("taskState", "running")
            if working_text:
                button.setText(working_text)
            button.style().unpolish(button)
            button.style().polish(button)
        runner = _TaskRunner(operation)
        if operation_name:
            self._task_callback = lambda result: self._show_operation_result(result, operation_name, callback)
        else:
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
        completed_button = self._active_task_button
        if completed_button is not None:
            completed_button.setText(self._active_task_button_text)
            completed_button.setEnabled(True)
            completed_button.setToolTip("")
            if result.status in {OperationStatus.PARTIAL, OperationStatus.DUPLICATE, OperationStatus.CANCELLED}:
                task_state = "warning"
            else:
                task_state = "success" if result.ok else "error"
            completed_button.setProperty("taskState", task_state)
            completed_button.style().unpolish(completed_button)
            completed_button.style().polish(completed_button)
            QTimer.singleShot(1200, lambda button=completed_button: self._reset_task_button_state(button))
        self._active_task_button = None
        self._active_task_button_text = ""
        if self._task_refresh_on_finish:
            self.refresh()
        self._task_refresh_on_finish = True
        if callback is not None:
            callback(result)
        self._sync_manual_receive_actions()
        if self.pending_quit:
            self.pending_quit = False
            QTimer.singleShot(0, self._finalize_quit)

    @staticmethod
    def _reset_task_button_state(button: QPushButton) -> None:
        button.setProperty("taskState", "idle")
        button.style().unpolish(button)
        button.style().polish(button)

    def _show_receive_result(self, result: ServiceResult) -> None:
        if isinstance(result, ReceiveResult):
            if result.status in {OperationStatus.FAILED, OperationStatus.AUTH_REQUIRED}:
                reason = result.message or (result.errors[0] if result.errors else result.error_code)
                message = f"收件失败：{reason or '原因未知'}"
            elif result.status == OperationStatus.PARTIAL:
                reason = result.message or (result.errors[0] if result.errors else "部分邮件处理失败")
                message = (
                    f"收件部分完成：已保存 {result.saved} 封邮件，"
                    f"但 {result.failed} 项处理失败；{reason}"
                )
            elif result.status == OperationStatus.NO_CHANGES or (
                result.status == OperationStatus.SUCCESS
                and result.saved == 0
                and result.failed == 0
            ):
                message = "检查完成，暂时没有新邮件"
            else:
                file_count = len(result.saved_files) or result.saved + result.attachments
                message = f"收取完成：新增 {result.saved} 封邮件，保存 {file_count} 个文件"
        else:
            message = result.message or result.status.value
        kind = (
            "warning"
            if result.status == OperationStatus.PARTIAL
            else "normal"
            if result.status == OperationStatus.NO_CHANGES
            or (
                isinstance(result, ReceiveResult)
                and result.status == OperationStatus.SUCCESS
                and result.saved == 0
                and result.failed == 0
            )
            else "success"
            if result.ok
            else "error"
        )
        self.show_message(message, kind)
        if not result.ok:
            self.last_error_details = self._redact_error_details(message)
            self.error_details_button.setEnabled(True)
        if isinstance(result, ReceiveResult) and result.saved:
            self.notify("收到新邮件", f"已安全保存 {result.saved} 封邮件中的文件", "receive-success")
        elif isinstance(result, ReceiveResult) and not result.ok:
            title = "需要重新授权" if result.needs_auth else "自动收取失败"
            self.notify(title, "请打开主窗口查看具体原因", "receive-failure", 300)

    def _show_send_result(self, result: ServiceResult) -> None:
        self.send_progress.hide()
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
        if result.status in {OperationStatus.PARTIAL, OperationStatus.DUPLICATE} or getattr(result, "send_status", "") in {"sent_archive_failed", "duplicate"}:
            kind = "warning"
        else:
            kind = "success" if result.ok else "error"
        self.show_message(message, kind)
        if kind == "error":
            self.last_error_details = self._redact_error_details(message)
            self.error_details_button.setEnabled(True)
        self.send_status_label.setText(message)
        if isinstance(result, SendResult) and result.send_status in {
            "sent", "sent_archive_failed", "duplicate"
        }:
            self._clear_send_selection()
            self.subject_edit.clear()
            self.send_status_label.setText(message + "；已清空本次选择")

    def _show_service_result(self, result: ServiceResult) -> None:
        message = self._friendly_result_message(result)
        if not result.ok:
            self.last_error_details = self._redact_error_details(
                result.message or result.error_code or "无详细信息"
            )
            self.error_details_button.setEnabled(True)
        if result.status in {OperationStatus.PARTIAL, OperationStatus.DUPLICATE, OperationStatus.CANCELLED}:
            kind = "warning"
        elif result.status == OperationStatus.NO_CHANGES:
            kind = "normal"
        else:
            kind = "success" if result.ok else "error"
        self.show_message(message, kind)

    def show_last_error_details(self) -> None:
        if not self.last_error_details:
            self.show_message("当前没有可查看的错误详情", "normal")
            return
        QMessageBox.warning(self, "最近错误详情（已脱敏）", self.last_error_details)

    def _redact_error_details(self, value: str) -> str:
        text = str(value)
        replacements = {
            self.service.cfg.gmail_app_password: "<Gmail 密钥已隐藏>",
            self.service.cfg.qq_auth_code: "<QQ 授权码已隐藏>",
            str(self.service.cfg.gmail_api_credentials_path): "<credentials 路径已隐藏>",
            str(self.service.cfg.gmail_api_token_path): "<token 路径已隐藏>",
            str(self.service.cfg.data_root_path): "<数据目录已隐藏>",
        }
        for original, replacement in replacements.items():
            if original:
                text = text.replace(original, replacement)
        for email in (self.service.cfg.gmail_address, self.service.cfg.qq_email):
            if email:
                text = text.replace(email, self._mask_email_for_display(email))
        return text

    @staticmethod
    def _mask_email_for_display(value: str) -> str:
        local, separator, domain = value.partition("@")
        if not local or not separator or not domain:
            return "未配置"
        return f"{local[:1]}***@{domain}"

    @staticmethod
    def _friendly_result_message(result: ServiceResult) -> str:
        if result.ok:
            return result.message or "操作完成"
        messages = {
            "oauth_failed": "Gmail 授权失败，请检查网络、credentials.json 后重试。",
            "gmail_api_diagnose_failed": "Gmail API 当前不可用，请检查授权是否失效。",
            "imap_diagnose_failed": "当前网络无法连接 Gmail IMAP 993，建议使用 Gmail API。",
            "qq_smtp_diagnose_failed": "QQ SMTP 连接失败，请检查网络和 QQ 邮箱授权码。",
            "file_changed": "文件在确认后发生变化，已阻止发送，请重新选择。",
            "path_not_allowed": "该文件不在允许发送目录中。",
            "receive_busy": "已有收件任务正在运行，请等待完成。",
        }
        return messages.get(result.error_code or "", result.message or "操作失败，请查看日志。")

    def export_diagnostic_report(self, button: QPushButton | None = None) -> None:
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存脱敏诊断报告",
            str(self.service.cfg.data_root_path / "diagnostic-report.md"),
            "Markdown 文件 (*.md)",
        )
        if not destination:
            self.show_message("已取消导出诊断报告", "normal")
            return
        self._run_task(
            "正在生成脱敏诊断报告",
            lambda: self.service.export_diagnostic_report(Path(destination)),
            self._show_service_result,
            button=button or self.export_diagnosis_button,
            operation_name="导出诊断报告",
            working_text="正在导出…",
        )

    def _show_operation_result(
        self,
        result: ServiceResult,
        operation_name: str,
        callback: Callable[[ServiceResult], None],
    ) -> None:
        callback(result)
        state = "完成" if result.ok else "失败"
        detail = result.message or result.status.value
        self.show_message(f"{operation_name}{state}，按钮已恢复可用：{detail}", "success" if result.ok else "error")

    def show_message(self, text: str, kind: str = "normal") -> None:
        if hasattr(self, "message_bar"):
            self.message_bar.set_message(text, kind)

    def apply_theme(self, theme: str) -> None:
        """在不重建界面的情况下应用主题。"""
        self.theme_mode = "dark" if theme == "dark" else "light"
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self.theme_mode))
        if hasattr(self, "title_bar"):
            self.title_bar.set_theme(self.theme_mode)
        if hasattr(self, "theme_value_label"):
            self.theme_value_label.setText("深色模式" if self.theme_mode == "dark" else "浅色模式")

    def toggle_theme(self) -> None:
        next_theme = "dark" if self.theme_mode == "light" else "light"
        self.apply_theme(next_theme)
        try:
            save_env_values({"GUI_THEME": next_theme})
        except OSError as exc:
            self.show_message(f"已切换主题，但无法保存下次启动设置：{exc}", "error")
            return
        theme_name = "深色模式" if next_theme == "dark" else "浅色模式"
        self.show_message(f"已切换为{theme_name}", "success")

    def _sync_manual_receive_actions(self) -> None:
        """自动收取期间禁止所有入口重复手动收取。"""
        enabled = not self.auto_switch.isChecked()
        hint = "自动收取已开启，关闭后可手动收取" if not enabled else ""
        for button in self.manual_receive_buttons:
            button.setEnabled(enabled)
            button.setToolTip(hint)

    def _load_auto_receive_preferences(self) -> None:
        minutes_text = os.getenv("GUI_AUTO_RECEIVE_INTERVAL_MINUTES", str(AUTO_RECEIVE_DEFAULT_MINUTES))
        try:
            minutes = max(1, int(minutes_text))
        except ValueError:
            minutes = AUTO_RECEIVE_DEFAULT_MINUTES
        self._set_combo_data(self.interval_combo, minutes)
        enabled = os.getenv("GUI_AUTO_RECEIVE", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.auto_switch.setChecked(enabled)
        self._sync_manual_receive_actions()
        self._reschedule_auto_receive()

    def _toggle_auto_receive(self, enabled: bool) -> None:
        self._sync_manual_receive_actions()
        if enabled:
            self.auto_failures = 0
            self._schedule_auto_receive()
            self.show_message(f"自动收件已开启，每 {self._auto_minutes()} 分钟检查一次", "success")
        else:
            self.auto_timer.stop()
            self.show_message("自动收件已关闭", "normal")
        if hasattr(self, "service_rows"):
            self.service_rows["auto"].set_value("已开启" if enabled else "未开启", success=enabled)

    def _reschedule_auto_receive(self) -> None:
        if hasattr(self, "auto_switch") and self.auto_switch.isChecked():
            self._schedule_auto_receive()

    def _automatic_receive(self) -> None:
        if self.task_active:
            self._schedule_auto_receive(1)
            return
        self._run_task("自动收件正在运行", self.service.receive, self._finish_auto_receive)

    def _auto_minutes(self) -> int:
        return int(self.interval_combo.currentData() or AUTO_RECEIVE_DEFAULT_MINUTES)

    def _schedule_auto_receive(self, minutes: int | None = None) -> None:
        if not self.auto_switch.isChecked():
            return
        delay = minutes if minutes is not None else self._auto_minutes()
        self.auto_timer.start(max(1, delay) * 60 * 1000)

    def _finish_auto_receive(self, result: ServiceResult) -> None:
        self._show_receive_result(result)
        if not self.auto_switch.isChecked():
            return
        if result.needs_auth:
            self.service_rows["auto"].set_value("需重新授权", danger=True)
            return
        if not result.ok:
            self.auto_failures += 1
            delay = min(self._auto_minutes() * (2 ** min(self.auto_failures, 3)), 60)
            self._schedule_auto_receive(delay)
            return
        self.auto_failures = 0
        self._schedule_auto_receive()

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
        """兼容旧调用；文件操作现由单元格内真实按钮完成。"""
        del row, column

    def _open_received_file(self, raw_path: str) -> None:
        path = Path(raw_path)
        try:
            assert_within_allowed_roots(path, [self.service.cfg.data_root_path])
        except SecurityError:
            self.show_message("已阻止打开 DATA_ROOT 之外的文件", "error")
            return
        if not path.exists() or not path.is_file():
            self.show_message("文件不存在或已移动，无法打开", "error")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            self.show_message(f"打开文件失败：{exc}", "error")
            return
        self.show_message("已使用 Windows 默认程序打开文件", "success")

    def _copy_received_path(self, button: QPushButton, path: str) -> None:
        if not path:
            self.show_message("文件路径不可用", "error")
            return
        QApplication.clipboard().setText(path)
        original = button.text()
        button.setText("已复制")
        button.setEnabled(False)
        self.show_message("完整文件路径已复制", "success")

        def restore() -> None:
            if not self.closed:
                button.setText(original)
                button.setEnabled(True)

        QTimer.singleShot(1200, restore)

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
            "codex": mcp_client_command("codex"),
            "claude": mcp_client_command("claude"),
            "json": generic_mcp_json(),
        }
        command = commands.get(target)
        if command is None:
            self.show_message("未知的 Agent 配置类型", "error")
            return
        QApplication.clipboard().setText(command)
        self.show_message("MCP 配置命令已复制", "success")

    def _build_tray(self) -> None:
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_icon: QSystemTrayIcon | None = None
        if not self.tray_available:
            return
        tray_brand_icon = brand_icon()
        if tray_brand_icon.isNull():
            tray_brand_icon = self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
        self.tray_icon = QSystemTrayIcon(tray_brand_icon, self)
        menu = QMenu(self)
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show_from_tray)
        receive_action = QAction("手动检查新邮件", self)
        receive_action.triggered.connect(self.receive)
        status_action = QAction("刷新最近状态", self)
        status_action.triggered.connect(lambda: self.request_refresh())
        quit_action = QAction("退出程序", self)
        quit_action.triggered.connect(self.request_quit)
        for action in (show_action, receive_action, status_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(lambda reason: self.show_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def notify(self, title: str, message: str, key: str, cooldown_seconds: int = 120) -> None:
        now = datetime.now().timestamp()
        if now - self._notification_times.get(key, 0) < cooldown_seconds:
            return
        self._notification_times[key] = now
        if self.tray_icon is not None:
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def minimize_to_tray(self) -> None:
        if self.tray_icon is None:
            self.showMinimized()
            return
        self.hide()
        self.notify("Agent 邮箱桥接工具仍在运行", "可从系统托盘打开窗口或正常退出", "tray-hidden", 3600)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        self.accepting_tasks = False
        self.auto_timer.stop()
        if self.task_active:
            self.pending_quit = True
            self.show_message("正在等待当前任务安全结束", "working")
            return
        self._finalize_quit()

    def _finalize_quit(self) -> None:
        self.quitting = True
        self.thread_pool.waitForDone(1000)
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/last_page", self._current_page_name())
        self.settings.setValue("runtime/clean_exit", True)
        self.settings.sync()
        close_connection()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.close()
        QApplication.quit()

    def _show_help(self) -> None:
        QMessageBox.information(self, "帮助", "详细配置、诊断和安全说明请查看项目 README.md。")

    def _show_about(self) -> None:
        logo_state = "最终 Logo 已接入" if find_brand_asset() else "Logo 接入结构已就绪，等待最终素材"
        QMessageBox.information(
            self,
            "关于 AgentMailBridge",
            f"AgentMailBridge v{__version__}\n\n本地优先、单用户的邮箱桥接工具。\n"
            f"{logo_state}。\n正式界面使用 PySide6，核心能力复用 ApplicationService。",
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
        if not self.quitting and self.tray_icon is not None:
            self.minimize_to_tray()
            event.ignore()
            return
        self.closed = True
        self.auto_timer.stop()
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/last_page", self._current_page_name())
        if self.quitting or self.tray_icon is None:
            self.settings.setValue("runtime/clean_exit", True)
        self.settings.sync()
        event.accept()

    def resizeEvent(self, event) -> None:
        if hasattr(self, "size_grip"):
            self.size_grip.move(self.width() - 16, self.height() - 16)
        super().resizeEvent(event)
