"""基于 PySide6 的 AgentMailBridge 正式桌面主窗口。"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QDate, QEvent, QObject, QPoint, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import close_connection, log_event
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
from agent_mail_bridge.managed_files import localize_status
from agent_mail_bridge.mail_summaries import (
    MAIL_LIST_ROW_HEIGHT,
    build_mail_list_summary,
    build_mail_list_tooltip,
    build_outbound_list_summary,
)
from agent_mail_bridge.mail_send import normalize_manual_recipient
from agent_mail_bridge.receive_rules import (
    ALL_SCANNED,
    CUSTOM,
    SELF_ONLY,
    normalize_sender_rules,
    normalize_subject_keywords,
    parse_rule_items,
    serialize_rule_items,
    validate_rule_settings,
)
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

AUTO_RECEIVE_DEFAULT_SECONDS = 60
AUTO_RECEIVE_MIN_SECONDS = 30
AUTO_RECEIVE_DEFAULT_MINUTES = 1  # 兼容旧调用方；新调度统一按秒计算。
AUTO_RECEIVE_BACKOFF_SECONDS = (30, 60, 120, 300, 900)
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


class _HistoryRescanSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)


class _HistoryRescanRunner(QRunnable):
    """执行可取消的历史补扫，并把紧凑统计安全送回 GUI 线程。"""

    _PROGRESS_KEYS = (
        "fetched", "matched", "saved", "duplicates", "rule_skipped", "failed",
    )

    def __init__(
        self,
        service: ApplicationService,
        *,
        date_from: datetime,
        date_to: datetime,
        apply_receive_rule: bool,
    ):
        super().__init__()
        self.service = service
        self.date_from = date_from
        self.date_to = date_to
        self.apply_receive_rule = apply_receive_rule
        self.cancel_event = threading.Event()
        self.signals = _HistoryRescanSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        def publish(payload: dict) -> None:
            self.signals.progress.emit(
                {key: int(payload.get(key) or 0) for key in self._PROGRESS_KEYS}
            )

        try:
            result = self.service.historical_rescan(
                date_from=self.date_from,
                date_to=self.date_to,
                apply_receive_rule=self.apply_receive_rule,
                cancel_event=self.cancel_event,
                progress_callback=publish,
            )
        except Exception as exc:  # noqa: BLE001
            result = ReceiveResult(
                OperationStatus.FAILED,
                error_code="history_rescan_failed",
                message=str(exc),
                failed=1,
                errors=[str(exc)],
            )
        self.signals.finished.emit(result)


class _VerticalResizeHandle(QWidget):
    """无边框窗口底边的垂直拉伸手柄。"""

    def __init__(self, window: QMainWindow, parent: QWidget):
        super().__init__(parent)
        self.window_ref = window
        self.start_global_y = 0.0
        self.start_height = 0
        self.setObjectName("verticalResizeHandle")
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setFixedHeight(6)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_global_y = event.globalPosition().y()
            self.start_height = self.window_ref.height()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            delta = int(event.globalPosition().y() - self.start_global_y)
            self.window_ref.resize(
                self.window_ref.width(),
                max(self.window_ref.minimumHeight(), self.start_height + delta),
            )
            event.accept()


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

        self.minimize_button = QPushButton()
        self.maximize_button = QPushButton()
        self.close_button = QPushButton()
        for button in (self.minimize_button, self.maximize_button):
            button.setObjectName("titleButton")
            button.setFixedSize(42, 38)
        self.close_button.setObjectName("closeButton")
        self.close_button.setFixedSize(42, 38)
        self.minimize_button.clicked.connect(window.minimize_to_tray)
        self.maximize_button.clicked.connect(self._toggle_maximized)
        self.close_button.clicked.connect(window.close)
        self.minimize_button.setToolTip("最小化到托盘")
        self.close_button.setToolTip("关闭到托盘")
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
        self.sync_window_state()

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
        if not hasattr(self, "minimize_button"):
            return
        color = "#D8DBE8" if theme == "dark" else "#555B69"
        self.minimize_button.setIcon(QIcon(line_icon_pixmap("minimize", 16, color)))
        self.close_button.setIcon(QIcon(line_icon_pixmap("close", 16, color)))
        self.minimize_button.setIconSize(QSize(16, 16))
        self.close_button.setIconSize(QSize(16, 16))
        self.sync_window_state(color=color)

    def sync_window_state(self, *, color: str | None = None) -> None:
        """窗口状态变化时同步最大化/还原图标与提示。"""
        if not hasattr(self, "maximize_button"):
            return
        if color is None:
            color = "#D8DBE8" if self.window_ref.theme_mode == "dark" else "#555B69"
        maximized = self.window_ref.isMaximized()
        kind = "restore" if maximized else "maximize"
        self.maximize_button.setIcon(QIcon(line_icon_pixmap(kind, 16, color)))
        self.maximize_button.setIconSize(QSize(16, 16))
        self.maximize_button.setToolTip("还原" if maximized else "最大化")

    def _toggle_maximized(self) -> None:
        if self.window_ref.isMaximized():
            self.window_ref.showNormal()
        else:
            self.window_ref.showMaximized()
        self.sync_window_state()

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
        self._active_runner: _TaskRunner | _HistoryRescanRunner | None = None
        self._task_callback: Callable[[ServiceResult], None] | None = None
        self.closed = False
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)
        self.accepting_tasks = True
        self.quitting = False
        self.pending_quit = False
        self.instance_guard = None
        self._notification_times: dict[str, float] = {}
        self._hidden_window_was_maximized = False
        self.task_buttons: list[QPushButton] = []
        self.manual_receive_buttons: list[QPushButton] = []
        self._active_task_button: QPushButton | None = None
        self._active_task_button_text = ""
        self._task_refresh_on_finish = True
        saved_theme = os.getenv("GUI_THEME", "light").strip().lower()
        self.theme_mode = saved_theme if saved_theme in {"light", "dark"} else "light"
        self.file_rows: list[dict] = []
        self.mail_rows: list[dict] = []
        self.log_rows: list[dict] = []
        self.history_rows: dict[str, list[dict]] = {"received": [], "sent": []}
        self.mcp_rows: list[dict] = []
        self.managed_file_rows: list[dict] = []
        self.health_fix_target = ""
        self.selected_send_path = ""
        self.send_selection: SendFileSelection | None = None
        self.send_selections: list[SendFileSelection] = []
        self.send_links: list[dict[str, str]] = []
        self._detail_return_page = "inbox"
        self._mail_detail_return_widget: QWidget | None = None
        self._mail_detail_splitter_sizes = [380, 260]
        self._history_rescan_runner: _HistoryRescanRunner | None = None
        self._history_rescan_dialog: QDialog | None = None
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
        self.auto_watchdog = QTimer(self)
        self.auto_watchdog.setInterval(15_000)
        self.auto_watchdog.timeout.connect(self._watchdog_auto_receive)
        self.auto_watchdog.start()
        self.auto_failures = 0
        self._loading_auto_receive = False
        self._loading_mcp_read_access = False
        self.setWindowTitle("Agent 邮箱桥接工具")
        self.setWindowIcon(brand_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1400, 960)
        self.setMinimumSize(1320, 660)
        self._build()
        self.apply_theme(self.theme_mode)
        self._build_tray()
        self._load_auto_receive_preferences()
        self._wire_config_change_tracking()
        saved_geometry = self.settings.value("window/normal_geometry")
        if saved_geometry:
            try:
                self.setGeometry(saved_geometry)
            except TypeError:
                pass
        self._fit_to_available_screen()
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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """紧凑窗口中让文件表格滚轮优先移动页面，以保证日志可达。"""
        if (
            watched is getattr(self, "_inbox_page_wheel_source", None)
            and event.type() == QEvent.Type.Wheel
            and hasattr(self, "inbox_page_scroll")
        ):
            bar = self.inbox_page_scroll.verticalScrollBar()
            if bar.maximum() > 0:
                pixel_delta = event.pixelDelta().y()
                page_delta = pixel_delta or int(event.angleDelta().y() / 120 * 72)
                if page_delta:
                    bar.setValue(bar.value() - page_delta)
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

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
        self.vertical_resize_handle = _VerticalResizeHandle(self, root)
        self.vertical_resize_handle.raise_()

    def _fit_to_available_screen(self) -> None:
        """限制恢复后的旧窗口几何，确保 150% DPI 下不超出可用桌面。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.setMinimumSize(
            min(1320, available.width()),
            min(660, available.height()),
        )
        target_width = min(max(self.width(), self.minimumWidth()), available.width())
        preferred_height = min(1020, available.height())
        target_height = min(
            max(self.height(), preferred_height, self.minimumHeight()),
            available.height(),
        )
        self.resize(target_width, target_height)
        frame = self.frameGeometry()
        x = min(max(frame.x(), available.left()), available.right() - frame.width() + 1)
        y = min(max(frame.y(), available.top()), available.bottom() - frame.height() + 1)
        self.move(x, y)

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidebar")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(panel, "#FFFFFF")
        panel.setFixedWidth(248)
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
        nav_card = QFrame()
        nav_card.setObjectName("navCard")
        nav_layout = QVBoxLayout(nav_card)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)

        self.agent_nav_button = NavButton(
            QIcon(line_icon_pixmap("terminal", 17, "#6F7585")), "Agent / MCP"
        )
        self.agent_nav_button.clicked.connect(
            lambda checked=False: self.select_page("agent")
        )
        self.nav_buttons["agent"] = self.agent_nav_button
        nav_layout.addWidget(self.agent_nav_button)
        nav_layout.addWidget(horizontal_line())
        nav_specs = (
            ("history", "clock", "历史记录"),
            ("files_data", "database", "文件与数据"),
            ("settings", "settings", "设置"),
            ("about", "info", "关于"),
        )
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
        panel.setMinimumWidth(720)
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
        self.global_refresh_label = QLabel("刷新本地页面数据")
        self.global_refresh_label.setObjectName("hint")
        self.global_refresh_button = self._button(
            "刷新", icon_kind="refresh", outline=True
        )
        self.global_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.global_refresh_button)
        )
        tab_layout.addWidget(self.global_refresh_label)
        tab_layout.addWidget(self.global_refresh_button)
        tab_layout.addSpacing(18)
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
        # 邮件、会话和发件详情是二级工作面，不增加新的顶级路由。
        self.mail_detail_page = self._build_mail_detail_page()
        self.mail_thread_page = self._build_mail_thread_page()
        self.outbound_detail_page = self._build_outbound_detail_page()
        for page in (
            self.mail_detail_page,
            self.mail_thread_page,
            self.outbound_detail_page,
        ):
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
        for label, seconds in (
            ("每 30 秒", 30), ("每 1 分钟", 60), ("每 3 分钟", 180),
            ("每 5 分钟", 300), ("每 10 分钟", 600),
        ):
            self.interval_combo.addItem(label, seconds)
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

        self.files_table = DataTable(["文件名", "大小", "收取时间", "操作"])
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
        )
        # 高内容页在当前屏幕可用高度足够时整页显示，较矮窗口保留滚动兜底。
        layout.setContentsMargins(24, 20, 22, 24)
        layout.setSpacing(12)
        self.inbox_refresh_button = self.global_refresh_button
        tools = QHBoxLayout()
        tools.setSpacing(9)
        tools.addWidget(QLabel("自动收取"))
        self.auto_switch = ToggleSwitch()
        self.auto_switch.toggled.connect(self._toggle_auto_receive)
        tools.addWidget(self.auto_switch)
        tools.addSpacing(5)
        tools.addWidget(QLabel("检查间隔"))
        self.interval_combo = QComboBox()
        for label, seconds in (
            ("每 30 秒", 30), ("每 1 分钟", 60), ("每 3 分钟", 180),
            ("每 5 分钟", 300), ("每 10 分钟", 600),
        ):
            self.interval_combo.addItem(label, seconds)
        self.interval_combo.setFixedWidth(145)
        self.interval_combo.currentIndexChanged.connect(self._reschedule_auto_receive)
        tools.addWidget(self.interval_combo)
        tools.addStretch(1)
        self.inbox_test_button = self._button("测试当前连接", self.test_connection)
        receive = self._button("立即收取", self.receive, primary=True, icon_kind="mail")
        receive.setToolTip("检查当前增量范围；如需找回较早邮件，请使用历史补扫")
        self.history_rescan_button = self._button(
            "历史补扫", self.open_history_rescan_dialog, outline=True, icon_kind="clock"
        )
        self.history_rescan_button.setToolTip("按 24 小时、7 天、30 天或自定义日期重新扫描")
        self.receive_button = receive
        self.task_buttons.extend((self.inbox_test_button, self.history_rescan_button, receive))
        self.manual_receive_buttons.extend((self.history_rescan_button, receive))
        tools.addWidget(self.inbox_test_button)
        tools.addWidget(self.history_rescan_button)
        tools.addWidget(receive)
        layout.addLayout(tools)

        auto_state_card = QFrame()
        auto_state_card.setObjectName("card")
        auto_state_layout = QGridLayout(auto_state_card)
        auto_state_layout.setContentsMargins(16, 10, 16, 10)
        auto_state_layout.setHorizontalSpacing(14)
        auto_state_layout.setVerticalSpacing(5)
        self.auto_state_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate((
            ("state", "自动收取"), ("last_check", "上次检查"),
            ("last_success", "上次成功"), ("next_check", "下次检查"),
            ("last_result", "最近结果"), ("retries", "待重试邮件"),
        )):
            caption = QLabel(title)
            caption.setObjectName("hint")
            value = QLabel("—")
            value.setObjectName("fieldLabel")
            self.auto_state_values[key] = value
            row, column = divmod(index, 3)
            auto_state_layout.addWidget(caption, row * 2, column)
            auto_state_layout.addWidget(value, row * 2 + 1, column)
        layout.addWidget(auto_state_card)

        self.self_mail_check = QCheckBox(page)
        self.self_mail_check.setChecked(self.service.cfg.auto_receive_only_self_mail)
        self.self_mail_check.hide()
        preference_card = QFrame()
        preference_card.setObjectName("card")
        preference_row = QHBoxLayout(preference_card)
        preference_row.setContentsMargins(16, 12, 16, 12)
        preference_text = QVBoxLayout()
        preference_text.setSpacing(4)
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
        file_title = QLabel("今日收到邮件")
        file_title.setObjectName("sectionTitle")
        self.inbox_search = QLineEdit()
        self.inbox_search.setObjectName("inboxSearch")
        self.inbox_search.setPlaceholderText("搜索主题、联系人、正文、附件或链接")
        self.inbox_search.setClearButtonEnabled(True)
        self.inbox_search.addAction(
            QIcon(line_icon_pixmap("search", 17, TEXT_MUTED)),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.inbox_search.setMinimumWidth(260)
        self.inbox_search.setMaximumWidth(420)
        self.inbox_search_timer = QTimer(self)
        self.inbox_search_timer.setSingleShot(True)
        self.inbox_search_timer.setInterval(250)
        self.inbox_search_timer.timeout.connect(self._filter_inbox)
        self.inbox_search.textChanged.connect(self._schedule_inbox_filter)
        open_button = self._button("打开今日邮件归档", self.open_today_folder, icon_kind="file")
        file_header.addWidget(file_title)
        file_header.addStretch(1)
        file_header.addWidget(self.inbox_search)
        file_header.addWidget(open_button)
        layout.addLayout(file_header)
        self.files_table = DataTable(
            ["主题", "发件人", "内容", "收取时间", "状态", "操作"]
        )
        self.files_table.setMinimumHeight(220)
        self.files_table.cellDoubleClicked.connect(self._open_inbox_mail)
        self._configure_inbox_table(self.files_table)
        self.inbox_table = self.files_table
        layout.addWidget(self.files_table, 2)

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
        self.logs_table.setMinimumHeight(180)
        self._configure_log_table(self.logs_table)
        self.logs_refresh_label = self.home_refresh_label
        self.dashboard_refresh_label = self.home_refresh_label
        layout.addWidget(self.logs_table, 1)
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self.inbox_page_scroll = scroll
        self._inbox_page_wheel_source = self.files_table.viewport()
        self._inbox_page_wheel_source.installEventFilter(self)
        return scroll

    def _build_send_page(self) -> QWidget:
        page, layout = self._standard_page(
            "发邮件",
            "主题、正文、多个附件和多个链接会组成一封邮件；用户可明确填写一个合法收件人。",
        )

        # 旧版单文件控件继续作为程序兼容接口存在，但不再占用正式界面。
        self.send_path_edit = QLineEdit(page)
        self.send_path_edit.setReadOnly(True)
        self.send_file_name_value = QLabel("未选择", page)
        self.send_file_size_value = QLabel("—", page)
        self.send_file_type_value = QLabel("—", page)
        self.send_file_modified_value = QLabel("—", page)
        self.copy_send_path_button = self._button("复制路径", self.copy_selected_send_path)
        self.reveal_send_file_button = self._button("打开所在文件夹", self.reveal_selected_send_file)
        self.preview_send_file_button = self._button("安全预览", self.preview_selected_send_file)
        for widget in (
            self.send_path_edit,
            self.send_file_name_value,
            self.send_file_size_value,
            self.send_file_type_value,
            self.send_file_modified_value,
            self.copy_send_path_button,
            self.reveal_send_file_button,
            self.preview_send_file_button,
        ):
            widget.hide()

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        card = QFrame()
        card.setObjectName("card")
        form = QVBoxLayout(card)
        form.setContentsMargins(18, 14, 18, 14)
        form.setSpacing(8)

        recipient_row = QHBoxLayout()
        recipient_row.addWidget(QLabel("收件人 To"))
        self.recipient_edit = QLineEdit(
            self.service.cfg.owner_gmail or self.service.cfg.gmail_address
        )
        self.recipient_edit.setPlaceholderText("name@example.com")
        self.recipient_edit.setToolTip(
            "GUI 手动发件可填写一个合法邮箱；Agent/MCP 仍固定发送到 OWNER_GMAIL"
        )
        self._recipient_user_edited = False
        self.recipient_edit.textEdited.connect(self._mark_recipient_edited)
        self.recipient_edit.textChanged.connect(self._update_send_action_state)
        recipient_row.addWidget(self.recipient_edit, 1)
        form.addLayout(recipient_row)

        self.subject_edit = QLineEdit()
        self.subject_edit.setPlaceholderText("邮件主题（可单独发送）")
        self.subject_edit.textChanged.connect(self._update_send_action_state)
        form.addWidget(self.subject_edit)

        self.send_body_edit = QTextEdit()
        self.send_body_edit.setPlaceholderText("邮件正文（纯文本；可单独发送）")
        self.send_body_edit.setMinimumHeight(82)
        self.send_body_edit.setMaximumHeight(130)
        self.send_body_edit.textChanged.connect(self._update_send_action_state)
        form.addWidget(self.send_body_edit)

        resource_row = QHBoxLayout()
        resource_row.setSpacing(10)
        attachment_box = QFrame()
        attachment_box.setObjectName("accountPanel")
        attachment_layout = QVBoxLayout(attachment_box)
        attachment_layout.setContentsMargins(10, 8, 10, 8)
        attachment_header = QHBoxLayout()
        attachment_title = QLabel("附件 0 个")
        attachment_title.setObjectName("fieldLabel")
        self.send_attachment_title = attachment_title
        attachment_header.addWidget(attachment_title)
        attachment_header.addStretch(1)
        attachment_header.addWidget(
            self._button("添加附件", self.choose_send_attachments, outline=True, icon_kind="file")
        )
        attachment_layout.addLayout(attachment_header)
        self.send_attachment_table = DataTable(["文件名", "大小", "操作"])
        self.send_attachment_table.setObjectName("compactResourceTable")
        self.send_attachment_table.setAlternatingRowColors(False)
        self.send_attachment_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.send_attachment_table.setMinimumHeight(102)
        self.send_attachment_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.send_attachment_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.send_attachment_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.send_attachment_table.setColumnWidth(2, 72)
        self.send_attachment_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        attachment_layout.addWidget(self.send_attachment_table)
        resource_row.addWidget(attachment_box, 1)

        link_box = QFrame()
        link_box.setObjectName("accountPanel")
        link_layout = QVBoxLayout(link_box)
        link_layout.setContentsMargins(10, 8, 10, 8)
        link_title = QLabel("相关链接 0 个")
        link_title.setObjectName("fieldLabel")
        self.send_link_title = link_title
        link_layout.addWidget(link_title)
        link_add_row = QHBoxLayout()
        self.send_link_edit = QLineEdit()
        self.send_link_edit.setPlaceholderText("https://example.com")
        self.send_link_edit.returnPressed.connect(self.add_send_link)
        link_add_row.addWidget(self.send_link_edit, 1)
        link_add_row.addWidget(self._button("添加", self.add_send_link, outline=True))
        link_layout.addLayout(link_add_row)
        self.send_link_table = DataTable(["链接", "操作"])
        self.send_link_table.setObjectName("compactResourceTable")
        self.send_link_table.setAlternatingRowColors(False)
        self.send_link_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.send_link_table.setMinimumHeight(72)
        self.send_link_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.send_link_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.send_link_table.setColumnWidth(1, 72)
        self.send_link_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        link_layout.addWidget(self.send_link_table)
        resource_row.addWidget(link_box, 1)
        form.addLayout(resource_row)

        action_row = QHBoxLayout()
        self.send_action_button = self._button(
            "发送这一封邮件", self.send_composed_mail, primary=True, icon_kind="send"
        )
        self.send_action_button.setEnabled(False)
        self.task_buttons.append(self.send_action_button)
        self.send_progress = QProgressBar()
        self.send_progress.setRange(0, 0)
        self.send_progress.setTextVisible(False)
        self.send_progress.setFixedHeight(4)
        self.send_progress.hide()
        self.send_status_label = QLabel("填写主题、正文，或添加附件、链接后即可发送")
        self.send_status_label.setObjectName("hint")
        action_row.addWidget(self.send_action_button)
        action_row.addWidget(self.send_status_label, 1)
        form.addLayout(action_row)
        form.addWidget(self.send_progress)
        splitter.addWidget(card)

        history_panel = QWidget()
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(8)
        history_header = QHBoxLayout()
        history_title = QLabel("最近发送记录")
        history_title.setObjectName("sectionTitle")
        history_header.addWidget(history_title)
        history_header.addStretch(1)
        history_header.addWidget(self._button("管理记录", self.open_send_history, text_only=True))
        history_layout.addLayout(history_header)
        self.sent_table = DataTable(
            ["主题", "内容", "来源", "发送时间", "状态", "操作"]
        )
        self.sent_table.setObjectName("mailRecordTable")
        self.sent_table.setAlternatingRowColors(False)
        self.sent_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.sent_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sent_table.cellDoubleClicked.connect(self._open_sent_record)
        sent_header = self.sent_table.horizontalHeader()
        sent_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        sent_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        sent_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        sent_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        sent_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        sent_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.sent_table.setColumnWidth(5, 92)
        self.sent_table.setWordWrap(True)
        self.sent_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.sent_table.verticalHeader().setDefaultSectionSize(MAIL_LIST_ROW_HEIGHT)
        history_layout.addWidget(self.sent_table, 1)
        splitter.addWidget(history_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
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

        self.managed_files_table = DataTable(
            ["类型", "来源", "文件名", "大小", "时间", "状态", "操作"]
        )
        self.managed_files_table.cellDoubleClicked.connect(self._preview_managed_file)
        header = self.managed_files_table.horizontalHeader()
        for column in (0, 1, 3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 82), (1, 82), (3, 92), (4, 136), (5, 82), (6, 166)):
            self.managed_files_table.setColumnWidth(column, width)
        self.managed_files_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.managed_files_table.setWordWrap(True)
        self.managed_files_table.verticalHeader().setDefaultSectionSize(48)
        self.managed_files_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        layout.addWidget(self.managed_files_table, 2)

        actions = QHBoxLayout()
        actions.addWidget(self._button("安全预览", self.preview_selected_managed_file))
        actions.addWidget(self._button("打开", self.open_selected_managed_file))
        actions.addWidget(self._button("打开所在目录", self.reveal_selected_managed_file))
        actions.addWidget(self._button("复制路径", self.copy_selected_managed_file_path, text_only=True))
        actions.addWidget(self._button("文件详情", self.show_selected_managed_file_detail, text_only=True))
        actions.addStretch(1)
        layout.addLayout(actions)

        overview = QFrame()
        overview.setObjectName("card")
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(16, 12, 16, 12)
        overview_header = QHBoxLayout()
        overview_title = QLabel("数据概览")
        overview_title.setObjectName("minorTitle")
        overview_header.addWidget(overview_title)
        overview_header.addStretch(1)
        overview_header.addWidget(
            self._button("数据维护与备份", lambda: self.select_page("maintenance"), outline=True)
        )
        overview_layout.addLayout(overview_header)
        overview_grid = QGridLayout()
        overview_grid.setSpacing(8)
        self.data_overview_values: dict[str, QLabel] = {}
        metrics = (
            ("database", "数据库状态"),
            ("database_size", "数据库大小"),
            ("received", "收件文件占用"),
            ("sent", "已发送归档占用"),
            ("agent", "Agent 结果占用"),
            ("backups", "备份占用"),
        )
        for index, (key, title) in enumerate(metrics):
            metric = QFrame()
            metric.setObjectName("overviewMetric")
            metric_layout = QVBoxLayout(metric)
            metric_layout.setContentsMargins(10, 8, 10, 8)
            metric_layout.setSpacing(2)
            caption = QLabel(title)
            caption.setObjectName("hint")
            value = QLabel("—")
            value.setObjectName("overviewValue")
            metric_layout.addWidget(caption)
            metric_layout.addWidget(value)
            self.data_overview_values[key] = value
            overview_grid.addWidget(metric, index // 3, index % 3)
        overview_layout.addLayout(overview_grid)
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
        self.history_status_filter.addItems(
            ["全部状态", "已保存", "已发送", "成功", "失败", "重复", "部分完成", "处理中", "其他"]
        )
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
        self.history_table = DataTable(["类型", "摘要", "时间", "状态", "操作"])
        self.history_table.cellDoubleClicked.connect(self._show_history_detail)
        history_header = self.history_table.horizontalHeader()
        history_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        history_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        history_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        history_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        history_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(4, 175)
        self.history_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.history_table.setWordWrap(True)
        self.history_table.verticalHeader().setDefaultSectionSize(48)
        layout.addWidget(self.history_table, 1)
        return page

    def _build_logs_page(self) -> QWidget:
        page, layout = self._standard_page("日志管理", "筛选和查看脱敏技术事件；不会显示密码或 OAuth token。")
        self.log_overview_label = QLabel("正在读取技术日志概览…")
        self.log_overview_label.setObjectName("hint")
        self.log_overview_label.setWordWrap(True)
        layout.addWidget(self.log_overview_label)

        tools = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜索事件或消息")
        self.log_search_timer = QTimer(self)
        self.log_search_timer.setSingleShot(True)
        self.log_search_timer.setInterval(250)
        self.log_search_timer.timeout.connect(self._populate_full_logs)
        self.log_search.textChanged.connect(lambda _text: self.log_search_timer.start())
        self.log_filter = QComboBox()
        self.log_filter.addItems(["全部级别", "INFO", "SUCCESS", "WARNING", "ERROR"])
        self.log_filter.currentTextChanged.connect(self._populate_full_logs)
        self.log_type_filter = QComboBox()
        self.log_type_filter.addItems([
            "全部事件", "收件", "发件", "Agent / MCP", "配置", "数据库与文件", "系统与诊断",
        ])
        self.log_type_filter.currentTextChanged.connect(self._populate_full_logs)
        self.log_time_filter = QComboBox()
        self.log_time_filter.addItems(["全部时间", "今天", "最近 7 天", "最近 30 天"])
        self.log_time_filter.currentTextChanged.connect(self._populate_full_logs)
        self.log_daily_check = QCheckBox("显示日常自动检查")
        self.log_daily_check.setChecked(False)
        self.log_daily_check.toggled.connect(self._populate_full_logs)
        self.logs_refresh_button = self._button("刷新")
        self.logs_refresh_button.clicked.connect(
            lambda: self.request_refresh(self.logs_refresh_button)
        )
        self.logs_refresh_label = QLabel("尚未刷新")
        self.logs_refresh_label.setObjectName("hint")
        tools.addWidget(self.log_search, 1)
        tools.addWidget(self.log_filter)
        tools.addWidget(self.log_type_filter)
        tools.addWidget(self.log_time_filter)
        tools.addWidget(self.log_daily_check)
        tools.addWidget(self.logs_refresh_label)
        tools.addWidget(self.logs_refresh_button)
        layout.addLayout(tools)

        retention = QHBoxLayout()
        retention.addWidget(QLabel("普通日志保留"))
        self.log_normal_retention = QComboBox()
        for days in (7, 30, 90):
            self.log_normal_retention.addItem(f"{days} 天", days)
        self._set_combo_data(
            self.log_normal_retention, self.service.cfg.normal_log_retention_days
        )
        retention.addWidget(self.log_normal_retention)
        retention.addWidget(QLabel("错误日志保留"))
        self.log_error_retention = QComboBox()
        for days in (30, 90, 180):
            self.log_error_retention.addItem(f"{days} 天", days)
        self._set_combo_data(
            self.log_error_retention,
            self.service.cfg.warning_error_log_retention_days,
        )
        retention.addWidget(self.log_error_retention)
        retention.addWidget(QLabel("最多记录"))
        self.log_max_count = QComboBox()
        for count in (5_000, 10_000, 20_000):
            self.log_max_count.addItem(f"{count:,} 条", count)
        self._set_combo_data(self.log_max_count, self.service.cfg.app_event_max_count)
        retention.addWidget(self.log_max_count)
        retention.addWidget(self._button("保存保留设置", self.save_log_retention_settings, outline=True))
        retention.addStretch(1)
        layout.addLayout(retention)

        self.full_logs_table = DataTable(["时间", "级别", "事件", "消息"])
        self._configure_log_table(self.full_logs_table, full=True)
        self.full_logs_table.cellDoubleClicked.connect(self._show_log_detail)
        layout.addWidget(self.full_logs_table, 1)
        self.full_log_rows: list[dict] = []
        self.log_page_size = 150
        self.log_query_total = 0
        self.log_load_more_button = self._button("加载更多", self._load_more_logs, outline=True)

        log_actions = QHBoxLayout()
        log_actions.addWidget(self._button("查看详情", self.show_selected_log_detail))
        self.log_export_filtered_button = self._button("导出当前筛选日志")
        self.log_export_filtered_button.clicked.connect(self.export_current_log_filter)
        log_actions.addWidget(self.log_export_filtered_button)
        self.log_export_button = self._button("导出脱敏诊断信息（完整）")
        self.log_export_button.clicked.connect(
            lambda: self.export_diagnostic_report(self.log_export_button)
        )
        log_actions.addWidget(self.log_export_button)
        log_actions.addWidget(self.log_load_more_button)
        log_actions.addWidget(self._button("打开日志目录", self.open_log_folder))
        log_actions.addStretch(1)
        layout.addLayout(log_actions)
        maintenance_actions = QHBoxLayout()
        maintenance_actions.addWidget(self._button("立即清理过期日志", self.prune_technical_logs))
        maintenance_actions.addWidget(self._button("清除日常检查", self.clear_daily_check_logs))
        maintenance_actions.addWidget(self._button("清空全部技术日志", self.clear_all_technical_logs, text_only=True))
        maintenance_actions.addStretch(1)
        maintenance_actions.addWidget(
            self._button("← 返回收件", lambda: self.select_page("inbox"), text_only=True)
        )
        layout.addLayout(maintenance_actions)
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
            "Agent / MCP",
            "统一管理本机 stdio MCP 的邮件读取、资源准备、结果发送、工作区和调用审计。",
        )

        status_card = QFrame()
        status_card.setObjectName("card")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(18, 14, 18, 14)
        status_header = QHBoxLayout()
        status_title = QLabel("MCP 基本状态")
        status_title.setObjectName("sectionTitle")
        status_header.addWidget(status_title)
        status_header.addStretch(1)
        mcp_command, _ = mcp_launch()
        packaged_mcp_ready = not get_runtime_paths().frozen or Path(mcp_command).is_file()
        self.mcp_status_label = QLabel(
            "可用 · 本地 stdio" if packaged_mcp_ready else "内部 MCP 组件缺失"
        )
        self.mcp_status_label.setStyleSheet(f"color: {SUCCESS}; font-weight: 700;")
        status_header.addWidget(self.mcp_status_label)
        status_layout.addLayout(status_header)
        capability = QLabel(
            f"版本 {__version__}　支持搜索邮件、读取正文与附件、准备资源到工作区，以及发送最终结果。"
        )
        capability.setObjectName("hint")
        capability.setWordWrap(True)
        status_layout.addWidget(capability)
        self.mcp_recipient_label = QLabel(self.service.cfg.owner_gmail or "未配置")
        self.mcp_recipient_label.hide()
        self.mcp_roots_label = QLabel(
            "\n".join(str(path) for path in self.service.cfg.effective_allowed_send_roots)
        )
        self.mcp_roots_label.setWordWrap(True)
        self.mcp_roots_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        roots_caption = QLabel("允许目录")
        roots_caption.setObjectName("fieldLabel")
        status_layout.addWidget(roots_caption)
        status_layout.addWidget(self.mcp_roots_label)
        layout.addWidget(status_card)

        read_card = QFrame()
        read_card.setObjectName("card")
        read_layout = QVBoxLayout(read_card)
        read_layout.setContentsMargins(18, 14, 18, 14)
        read_header = QHBoxLayout()
        read_title = QLabel("邮件读取")
        read_title.setObjectName("sectionTitle")
        read_header.addWidget(read_title)
        read_header.addStretch(1)
        self.mcp_read_status_label = QLabel()
        self.mcp_read_status_label.setObjectName("hint")
        read_header.addWidget(self.mcp_read_status_label)
        self.mcp_read_switch = ToggleSwitch()
        self._loading_mcp_read_access = True
        self.mcp_read_switch.setChecked(bool(self.service.cfg.mcp_mail_read_enabled))
        self._loading_mcp_read_access = False
        self.mcp_read_switch.toggled.connect(self._toggle_mcp_read_access)
        read_header.addWidget(self.mcp_read_switch)
        read_layout.addLayout(read_header)
        read_hint = QLabel(
            "启用后，能启动这份 MCP 配置的本机进程可按工具边界读取本地邮件归档。"
            "不读取凭据，不修改或删除邮件，不逐封授权；可随时关闭。"
        )
        read_hint.setObjectName("hint")
        read_hint.setWordWrap(True)
        read_layout.addWidget(read_hint)
        sync_row = QHBoxLayout()
        sync_row.addWidget(QLabel("同步状态"))
        self.mcp_sync_status_label = QLabel("尚未刷新")
        self.mcp_sync_status_label.setObjectName("fieldLabel")
        sync_row.addWidget(self.mcp_sync_status_label, 1)
        read_layout.addLayout(sync_row)
        layout.addWidget(read_card)
        self._update_mcp_read_status()

        config_card = QFrame()
        config_card.setObjectName("card")
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 14, 18, 14)
        config_title = QLabel("统一配置")
        config_title.setObjectName("sectionTitle")
        config_layout.addWidget(config_title)
        config_hint = QLabel("所有兼容本地 stdio MCP 的 Agent 共用同一个服务、配置和工具。")
        config_hint.setObjectName("hint")
        config_hint.setWordWrap(True)
        config_layout.addWidget(config_hint)
        config_actions = QHBoxLayout()
        config_actions.addWidget(
            self._button("复制 MCP 配置", lambda: self._copy_mcp_config("json"), primary=True)
        )
        config_actions.addWidget(self._button("查看接入说明", self._show_mcp_setup_guide))
        config_actions.addWidget(self._button("MCP 自检", self.run_mcp_self_check))
        config_actions.addStretch(1)
        config_layout.addLayout(config_actions)
        layout.addWidget(config_card)

        example_card = QFrame()
        example_card.setObjectName("card")
        example_layout = QHBoxLayout(example_card)
        example_layout.setContentsMargins(18, 14, 18, 14)
        example_title = QLabel("示例指令")
        example_title.setObjectName("sectionTitle")
        example_layout.addWidget(example_title)
        example_layout.addStretch(1)
        example_layout.addWidget(
            self._button("复制收件示例指令", lambda: self._copy_mcp_example("receive"))
        )
        example_layout.addWidget(
            self._button("复制发件示例指令", lambda: self._copy_mcp_example("send"))
        )
        layout.addWidget(example_card)

        workspace_card = QFrame()
        workspace_card.setObjectName("card")
        workspace_layout = QVBoxLayout(workspace_card)
        workspace_layout.setContentsMargins(16, 12, 16, 12)
        workspace_header = QHBoxLayout()
        workspace_title = QLabel("Agent 工作区授权")
        workspace_title.setObjectName("sectionTitle")
        workspace_header.addWidget(workspace_title)
        workspace_header.addStretch(1)
        workspace_header.addWidget(
            self._button("添加工作区", self.add_agent_workspace_from_dialog, outline=True)
        )
        workspace_layout.addLayout(workspace_header)
        workspace_hint = QLabel(
            "工作区只影响发件源路径和邮件资源准备目标，不限制本地邮件事实查询。"
            "驱动器根目录、用户目录、AppData 和产品数据目录会被拒绝。"
        )
        workspace_hint.setObjectName("hint")
        workspace_hint.setWordWrap(True)
        workspace_layout.addWidget(workspace_hint)
        self.agent_workspace_table = DataTable(["已授权工作区", "操作"])
        self.agent_workspace_table.setMinimumHeight(116)
        workspace_table_header = self.agent_workspace_table.horizontalHeader()
        workspace_table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        workspace_table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.agent_workspace_table.setColumnWidth(1, 154)
        self.agent_workspace_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        workspace_layout.addWidget(self.agent_workspace_table)
        layout.addWidget(workspace_card)

        calls_title = QLabel("最近 MCP 调用")
        calls_title.setObjectName("sectionTitle")
        layout.addWidget(calls_title)
        self.mcp_table = DataTable(
            ["调用时间", "操作", "目标", "状态", "详情"]
        )
        self.mcp_table.setObjectName("mailRecordTable")
        self.mcp_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.mcp_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mcp_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.mcp_table.setWordWrap(True)
        self.mcp_table.cellDoubleClicked.connect(self._open_mcp_call_detail)
        self.mcp_table.setMinimumHeight(220)
        header = self.mcp_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.mcp_table.setColumnWidth(4, 92)
        layout.addWidget(self.mcp_table, 1)
        security = QLabel(
            "安全边界：Agent 不能指定收件人、读取凭据、修改或删除邮件，也不能扫描任意目录。"
        )
        security.setObjectName("hint")
        security.setWordWrap(True)
        layout.addWidget(security)
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _build_mail_detail_page(self) -> QWidget:
        page, layout = self._standard_page(
            "邮件详情", "查看一封邮件的正文、图片、附件、链接与归档状态。"
        )
        navigation = QHBoxLayout()
        navigation.addWidget(
            self._button("← 返回", self._return_from_mail_detail, text_only=True)
        )
        navigation.addStretch(1)
        self.mail_detail_thread_button = self._button(
            "查看邮件会话", self.open_current_mail_thread, outline=True
        )
        self.mail_detail_archive_button = self._button(
            "打开邮件归档", self.open_current_mail_archive, outline=True, icon_kind="file"
        )
        navigation.addWidget(self.mail_detail_thread_button)
        navigation.addWidget(self.mail_detail_archive_button)
        layout.addLayout(navigation)

        header_card = QFrame()
        header_card.setObjectName("card")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 14, 18, 14)
        self.mail_detail_subject = QLabel("无主题邮件")
        self.mail_detail_subject.setObjectName("sectionTitle")
        self.mail_detail_subject.setWordWrap(True)
        self.mail_detail_meta = QLabel("—")
        self.mail_detail_meta.setObjectName("hint")
        self.mail_detail_meta.setWordWrap(True)
        self.mail_detail_counts = QLabel("—")
        self.mail_detail_counts.setObjectName("minorTitle")
        header_layout.addWidget(self.mail_detail_subject)
        header_layout.addWidget(self.mail_detail_meta)
        header_layout.addWidget(self.mail_detail_counts)
        layout.addWidget(header_card)

        self.mail_detail_splitter = QSplitter(Qt.Orientation.Vertical)
        self.mail_detail_splitter.setObjectName("mailDetailSplitter")
        self.mail_detail_splitter.setChildrenCollapsible(False)

        body_pane = QFrame()
        body_pane.setObjectName("card")
        body_layout = QVBoxLayout(body_pane)
        body_layout.setContentsMargins(14, 10, 14, 12)
        body_layout.setSpacing(7)
        body_title = QLabel("邮件正文")
        body_title.setObjectName("sectionTitle")
        body_layout.addWidget(body_title)
        self.mail_detail_body = QTextEdit()
        self.mail_detail_body.setObjectName("mailDetailBody")
        self.mail_detail_body.setReadOnly(True)
        self.mail_detail_body.setMinimumHeight(240)
        body_layout.addWidget(self.mail_detail_body, 1)
        body_pane.setMinimumHeight(270)
        self.mail_detail_splitter.addWidget(body_pane)

        resource_pane = QFrame()
        resource_pane.setObjectName("mailDetailResourcesPane")
        resource_pane_layout = QVBoxLayout(resource_pane)
        resource_pane_layout.setContentsMargins(0, 0, 0, 0)
        resource_pane_layout.setSpacing(0)
        resource_scroll = QScrollArea()
        resource_scroll.setObjectName("mailDetailResourcesScroll")
        resource_scroll.setWidgetResizable(True)
        resource_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mail_detail_resource_widget = QWidget()
        self.mail_detail_resource_widget.setObjectName("mailDetailResourcesContent")
        self.mail_detail_resource_layout = QVBoxLayout(self.mail_detail_resource_widget)
        self.mail_detail_resource_layout.setContentsMargins(2, 8, 8, 8)
        self.mail_detail_resource_layout.setSpacing(8)
        resource_scroll.setWidget(self.mail_detail_resource_widget)
        resource_pane_layout.addWidget(resource_scroll)
        resource_pane.setMinimumHeight(96)
        self.mail_detail_resource_scroll = resource_scroll
        self.mail_detail_splitter.addWidget(resource_pane)
        self.mail_detail_splitter.setStretchFactor(0, 3)
        self.mail_detail_splitter.setStretchFactor(1, 2)
        self.mail_detail_splitter.setSizes(self._mail_detail_splitter_sizes)
        self.mail_detail_splitter.splitterMoved.connect(
            lambda _position, _index: self._remember_mail_detail_splitter()
        )
        layout.addWidget(self.mail_detail_splitter, 1)
        return page

    def _build_mail_thread_page(self) -> QWidget:
        page, layout = self._standard_page(
            "邮件会话", "按时间顺序查看属于同一会话的邮件。"
        )
        back = QHBoxLayout()
        back.addWidget(
            self._button("← 返回邮件", self._return_from_mail_thread, text_only=True)
        )
        back.addStretch(1)
        layout.addLayout(back)
        self.mail_thread_summary = QLabel("—")
        self.mail_thread_summary.setObjectName("sectionTitle")
        self.mail_thread_summary.setWordWrap(True)
        layout.addWidget(self.mail_thread_summary)
        self.mail_thread_cards_widget = QWidget()
        self.mail_thread_cards_layout = QVBoxLayout(self.mail_thread_cards_widget)
        self.mail_thread_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.mail_thread_cards_layout.setSpacing(10)
        layout.addWidget(self.mail_thread_cards_widget)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _build_outbound_detail_page(self) -> QWidget:
        page, layout = self._standard_page(
            "发送邮件详情", "查看一次完整发送的正文、附件、链接和审计状态。"
        )
        back = QHBoxLayout()
        back.addWidget(
            self._button("← 返回", self._return_from_outbound_detail, text_only=True)
        )
        back.addStretch(1)
        layout.addLayout(back)
        header_card = QFrame()
        header_card.setObjectName("card")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 14, 18, 14)
        self.outbound_detail_subject = QLabel("无主题邮件")
        self.outbound_detail_subject.setObjectName("sectionTitle")
        self.outbound_detail_subject.setWordWrap(True)
        self.outbound_detail_meta = QLabel("—")
        self.outbound_detail_meta.setObjectName("hint")
        self.outbound_detail_meta.setWordWrap(True)
        header_layout.addWidget(self.outbound_detail_subject)
        header_layout.addWidget(self.outbound_detail_meta)
        layout.addWidget(header_card)
        body_title = QLabel("邮件正文")
        body_title.setObjectName("sectionTitle")
        layout.addWidget(body_title)
        self.outbound_detail_body = QTextEdit()
        self.outbound_detail_body.setReadOnly(True)
        self.outbound_detail_body.setMinimumHeight(150)
        layout.addWidget(self.outbound_detail_body)
        resource_title = QLabel("附件与链接")
        resource_title.setObjectName("sectionTitle")
        layout.addWidget(resource_title)
        self.outbound_detail_resource_widget = QWidget()
        self.outbound_detail_resource_layout = QVBoxLayout(
            self.outbound_detail_resource_widget
        )
        self.outbound_detail_resource_layout.setContentsMargins(0, 0, 0, 0)
        self.outbound_detail_resource_layout.setSpacing(8)
        layout.addWidget(self.outbound_detail_resource_widget)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _build_about_page(self) -> QWidget:
        page, layout = self._standard_page("关于", f"AgentMailBridge v{__version__} 产品与构建信息。")
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
        scroll = QScrollArea()
        scroll.setObjectName("rightPanel")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFixedWidth(315)
        panel = QWidget()
        panel.setObjectName("rightPanelContent")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _fill_background(panel, "#FCFCFE")
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
            "saved": StatCard("statGreen", line_icon_pixmap("calendar", 28, SUCCESS), "保存内容", SUCCESS),
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
        layout.addWidget(TipRow(provider_icon("qq"), "GUI 手动发件可填写一个明确收件人。", "#329BC5"))
        help_button = self._button("查看帮助文档", self._show_help, text_only=True)
        layout.addWidget(help_button, 0, Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(panel)
        return scroll

    def _standard_page(
        self,
        title: str,
        description: str,
        *,
        header_action_label: str | None = None,
        header_action_icon: str = "refresh",
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
                icon_kind=header_action_icon,
            )
            heading_row.addWidget(page.header_action_button)
        hint = QLabel(description)
        hint.setObjectName("hint")
        separator = horizontal_line()
        page.description_label = hint
        page.header_separator = separator
        layout.addLayout(heading_row)
        layout.addWidget(hint)
        layout.addWidget(separator)
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
        header.setMinimumSectionSize(55)
        header.setStretchLastSection(False)
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, 86)
        table.setColumnWidth(3, 170)
        table.setWordWrap(True)
        table.verticalHeader().setDefaultSectionSize(48)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def _configure_inbox_table(self, table: DataTable) -> None:
        table.setObjectName("mailRecordTable")
        table.setAlternatingRowColors(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(
            ["主题", "发件人", "内容", "收取时间", "状态", "操作"]
        )
        header = table.horizontalHeader()
        header.setMinimumSectionSize(58)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(5, 92)
        table.setWordWrap(True)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.verticalHeader().setDefaultSectionSize(MAIL_LIST_ROW_HEIGHT)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    @staticmethod
    def _wrapped_row_height(
        table: DataTable,
        values: tuple[tuple[int, str], ...],
        *,
        minimum: int = 48,
    ) -> int:
        metrics = table.fontMetrics()
        max_lines = 1
        for column, text in values:
            width = max(48, table.columnWidth(column) - 16)
            lines = 0
            for segment in str(text or "").splitlines() or [""]:
                pixel_width = max(1, metrics.horizontalAdvance(segment))
                lines += max(1, (pixel_width + width - 1) // width)
            max_lines = max(max_lines, lines)
        return max(minimum, min(260, max_lines * metrics.lineSpacing() + 18))

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
        if hasattr(self, "right_panel"):
            self.right_panel.setVisible(target in {"inbox", "send", "agent"})
        tab_target = target if target in {"inbox", "send"} else ""
        self._set_exclusive_checked(self.tab_buttons, tab_target)
        if hasattr(self, "global_refresh_button"):
            visible = target in {"inbox", "send"}
            self.global_refresh_button.setVisible(visible)
            self.global_refresh_label.setVisible(visible)
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

    def save_receive_preferences(
        self,
        mode: str | None = None,
        senders: tuple[str, ...] | None = None,
        subject_keywords: tuple[str, ...] | None = None,
        require_attachment: bool | None = None,
    ) -> bool:
        """原子保存新规则；写入失败时不覆盖当前有效配置。"""
        cfg = self.service.cfg
        target_mode = mode or (
            SELF_ONLY if self.self_mail_check.isChecked() else ALL_SCANNED
        )
        target_senders = normalize_sender_rules(
            cfg.receive_rule_senders if senders is None else senders
        )
        target_keywords = normalize_subject_keywords(
            cfg.receive_rule_subject_keywords
            if subject_keywords is None
            else subject_keywords
        )
        target_attachment = (
            cfg.receive_rule_require_attachment
            if require_attachment is None
            else bool(require_attachment)
        )
        errors = validate_rule_settings(
            target_mode, target_senders, target_keywords, target_attachment
        )
        if errors:
            self.show_message(errors[0], "error")
            return False
        seconds = self._auto_seconds()
        try:
            save_env_values(
                {
                    "RECEIVE_RULE_MODE": target_mode,
                    "RECEIVE_RULE_SENDERS": serialize_rule_items(target_senders),
                    "RECEIVE_RULE_SUBJECT_KEYWORDS": serialize_rule_items(target_keywords),
                    "RECEIVE_RULE_REQUIRE_ATTACHMENT": str(target_attachment).lower(),
                    "AUTO_RECEIVE_ONLY_SELF_MAIL": str(target_mode == SELF_ONLY).lower(),
                    "RECEIVE_RULE_CONFIG_VERSION": "2",
                    "RECEIVE_RULE_MODE_SOURCE": "user_explicit",
                    "GUI_AUTO_RECEIVE": str(self.auto_switch.isChecked()).lower(),
                    "GUI_AUTO_RECEIVE_INTERVAL_SECONDS": str(seconds),
                    "GUI_AUTO_RECEIVE_INTERVAL_MINUTES": str(max(1, seconds // 60)),
                }
            )
        except OSError as exc:
            self.show_message(f"保存收件偏好失败：{exc}", "error")
            return False
        cfg.receive_rule_mode = target_mode
        cfg.receive_rule_senders = target_senders
        cfg.receive_rule_subject_keywords = target_keywords
        cfg.receive_rule_require_attachment = target_attachment
        cfg.auto_receive_only_self_mail = target_mode == SELF_ONLY
        cfg.receive_rule_config_version = 2
        cfg.receive_rule_mode_source = "user_explicit"
        cfg.receive_rule_migration_needed = False
        self.self_mail_check.setChecked(cfg.auto_receive_only_self_mail)
        self._update_receive_preference_summary()
        self.show_message("收件规则已保存", "success")
        return True

    def _update_receive_preference_summary(self) -> None:
        if not hasattr(self, "preference_summary_label"):
            return
        cfg = self.service.cfg
        if cfg.receive_rule_mode == SELF_ONLY:
            summary = "当前：仅 Gmail 自发自收邮件（高级）"
        elif cfg.receive_rule_mode == ALL_SCANNED:
            summary = "当前：当前扫描范围内全部邮件"
        else:
            parts = []
            if cfg.receive_rule_senders:
                parts.append(f"发件人 {len(cfg.receive_rule_senders)} 条")
            if cfg.receive_rule_subject_keywords:
                parts.append(f"关键词 {len(cfg.receive_rule_subject_keywords)} 个")
            if cfg.receive_rule_require_attachment:
                parts.append("仅含附件")
            summary = "当前：自定义规则"
            if parts:
                summary += "\n" + " · ".join(parts)
        self.preference_summary_label.setText(summary)

    def open_receive_preferences_editor(self) -> None:
        """编辑 API、IMAP、手动和自动收件共用的正式规则。"""
        dialog = QDialog(self)
        dialog.setObjectName("receivePreferencesDialog")
        dialog.setWindowTitle("编辑收件偏好")
        dialog.setModal(True)
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("收件模式")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        only_self = QRadioButton("仅 Gmail 自发自收邮件（高级）")
        only_self.setToolTip("仅接收由当前 Gmail 自己发送并投递回同一 Gmail 的邮件")
        all_scanned = QRadioButton("当前扫描范围内全部邮件（默认）")
        all_scanned.setToolTip("仍受查询范围、单次限制和去重规则约束")
        custom = QRadioButton("自定义规则")
        group = QButtonGroup(dialog)
        group.addButton(only_self, 0)
        group.addButton(all_scanned, 1)
        group.addButton(custom, 2)
        only_self.setChecked(self.service.cfg.receive_rule_mode == SELF_ONLY)
        all_scanned.setChecked(self.service.cfg.receive_rule_mode == ALL_SCANNED)
        custom.setChecked(self.service.cfg.receive_rule_mode == CUSTOM)
        layout.addWidget(only_self)
        layout.addWidget(all_scanned)
        layout.addWidget(custom)

        custom_panel = QFrame()
        custom_panel.setObjectName("accountPanel")
        custom_layout = QFormLayout(custom_panel)
        custom_layout.setContentsMargins(14, 12, 14, 12)
        custom_layout.setSpacing(8)
        sender_edit = QTextEdit()
        sender_edit.setObjectName("receiveSenderRules")
        sender_edit.setFixedHeight(72)
        sender_edit.setPlaceholderText("每行一项，例如 a@example.com 或 @example.org")
        sender_edit.setPlainText("\n".join(self.service.cfg.receive_rule_senders))
        keyword_edit = QTextEdit()
        keyword_edit.setObjectName("receiveSubjectKeywords")
        keyword_edit.setFixedHeight(72)
        keyword_edit.setPlaceholderText("每行一个关键词，例如 report 或 project")
        keyword_edit.setPlainText(
            "\n".join(self.service.cfg.receive_rule_subject_keywords)
        )
        attachment_check = QCheckBox("仅保存含附件的邮件")
        attachment_check.setChecked(
            self.service.cfg.receive_rule_require_attachment
        )
        custom_layout.addRow("指定发件人 / 域名", sender_edit)
        custom_layout.addRow("主题关键词", keyword_edit)
        custom_layout.addRow("附件条件", attachment_check)
        custom_panel.setVisible(custom.isChecked())
        custom.toggled.connect(custom_panel.setVisible)
        layout.addWidget(custom_panel)

        note = QLabel(
            "仅 Gmail 自发自收是高级显式规则，不用于防止发件回流。不同分类之间同时满足，"
            "同一分类内任一命中即可；手动、自动、Gmail API 与 Gmail IMAP 共用此规则。"
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def persist() -> None:
            mode = SELF_ONLY if only_self.isChecked() else ALL_SCANNED if all_scanned.isChecked() else CUSTOM
            senders = parse_rule_items(sender_edit.toPlainText())
            keywords = parse_rule_items(keyword_edit.toPlainText())
            errors = validate_rule_settings(
                mode, senders, keywords, attachment_check.isChecked()
            )
            if errors:
                QMessageBox.warning(dialog, "规则校验失败", "\n".join(errors))
                return
            if self.save_receive_preferences(
                mode,
                normalize_sender_rules(senders),
                normalize_subject_keywords(keywords),
                attachment_check.isChecked(),
            ):
                dialog.accept()

        buttons.accepted.connect(persist)
        dialog.exec()

    def receive(self) -> None:
        sender = self.sender()
        button = sender if isinstance(sender, QPushButton) else None
        self._run_task(
            "正在连接 Gmail 并检查当前增量范围",
            self.service.receive,
            self._show_receive_result,
            button=button,
            working_text="收取中…",
        )

    def open_history_rescan_dialog(self) -> None:
        """选择明确历史范围并启动可取消的后台补扫。"""
        dialog = QDialog(self)
        dialog.setObjectName("historyRescanDialog")
        dialog.setWindowTitle("历史补扫")
        dialog.setModal(True)
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("重新扫描历史邮件")
        title.setObjectName("sectionTitle")
        description = QLabel(
            "历史补扫会直接查询指定范围，不受日常增量回看窗口限制；已归档邮件只计为重复，不会再次保存。"
        )
        description.setObjectName("hint")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        form = QFormLayout()
        range_combo = QComboBox()
        range_combo.setObjectName("historyRescanRange")
        range_combo.addItem("最近 24 小时", "24h")
        range_combo.addItem("最近 7 天", "7d")
        range_combo.addItem("最近 30 天", "30d")
        range_combo.addItem("自定义日期范围", "custom")
        form.addRow("扫描范围", range_combo)
        layout.addLayout(form)

        custom_panel = QFrame()
        custom_panel.setObjectName("accountPanel")
        custom_layout = QFormLayout(custom_panel)
        custom_layout.setContentsMargins(14, 10, 14, 10)
        start_date = QDateEdit(QDate.currentDate().addDays(-7))
        start_date.setObjectName("historyRescanStartDate")
        start_date.setCalendarPopup(True)
        start_date.setDisplayFormat("yyyy-MM-dd")
        end_date = QDateEdit(QDate.currentDate())
        end_date.setObjectName("historyRescanEndDate")
        end_date.setCalendarPopup(True)
        end_date.setDisplayFormat("yyyy-MM-dd")
        custom_layout.addRow("开始日期", start_date)
        custom_layout.addRow("结束日期", end_date)
        custom_panel.hide()
        range_combo.currentIndexChanged.connect(
            lambda _index: custom_panel.setVisible(range_combo.currentData() == "custom")
        )
        layout.addWidget(custom_panel)

        apply_rule = QCheckBox("使用当前收件规则重新判断")
        apply_rule.setObjectName("historyRescanApplyRule")
        apply_rule.setChecked(True)
        apply_rule.setToolTip("关闭后会接收指定扫描范围内全部候选邮件；本地去重仍然生效")
        layout.addWidget(apply_rule)

        progress = QProgressBar()
        progress.setObjectName("historyRescanProgress")
        progress.setRange(0, 1)
        progress.setValue(0)
        progress.hide()
        stats = QLabel("尚未开始")
        stats.setObjectName("historyRescanStats")
        stats.setWordWrap(True)
        layout.addWidget(progress)
        layout.addWidget(stats)

        safety = QLabel(
            "补扫按页处理并可取消，不删除 Gmail 邮件；Gmail API 保持只读，IMAP 使用 BODY.PEEK 不标记已读。"
        )
        safety.setObjectName("hint")
        safety.setWordWrap(True)
        layout.addWidget(safety)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_button = self._button("关闭", dialog.reject, outline=True)
        cancel_button.setObjectName("historyRescanCancel")
        start_button = self._button("开始补扫", primary=True)
        start_button.setObjectName("historyRescanStart")
        actions.addWidget(cancel_button)
        actions.addWidget(start_button)
        layout.addLayout(actions)

        self._history_rescan_dialog = dialog
        self._history_rescan_progress = progress
        self._history_rescan_stats = stats
        self._history_rescan_cancel_button = cancel_button
        self._history_rescan_start_button = start_button

        def selected_range() -> tuple[datetime, datetime]:
            now = datetime.now()
            preset = str(range_combo.currentData() or "24h")
            if preset == "24h":
                return now - timedelta(hours=24), now
            if preset == "7d":
                return now - timedelta(days=7), now
            if preset == "30d":
                return now - timedelta(days=30), now
            start = datetime.strptime(start_date.date().toString("yyyy-MM-dd"), "%Y-%m-%d")
            end = datetime.strptime(end_date.date().toString("yyyy-MM-dd"), "%Y-%m-%d")
            return start, end.replace(hour=23, minute=59, second=59, microsecond=999999)

        def start_scan() -> None:
            try:
                range_start, range_end = selected_range()
            except ValueError as exc:
                QMessageBox.warning(dialog, "日期范围无效", str(exc))
                return
            if range_start > range_end:
                QMessageBox.warning(dialog, "日期范围无效", "开始日期不能晚于结束日期")
                return
            self._start_history_rescan(
                range_start,
                range_end,
                apply_receive_rule=apply_rule.isChecked(),
            )

        start_button.clicked.connect(start_scan)
        cancel_button.clicked.disconnect()
        cancel_button.clicked.connect(
            lambda _checked=False: self._cancel_history_rescan()
        )
        dialog.finished.connect(lambda _code: self._cancel_history_rescan(close_dialog=False))
        dialog.exec()

    def _start_history_rescan(
        self,
        date_from: datetime,
        date_to: datetime,
        *,
        apply_receive_rule: bool,
    ) -> None:
        if not self.accepting_tasks:
            self.show_message("程序正在退出，不再启动新任务", "error")
            return
        if self.task_active:
            self.show_message("已有任务正在运行，请等待完成后再补扫", "working")
            return
        self.task_active = True
        self._sync_manual_receive_actions()
        self._task_refresh_on_finish = True
        self.status_var.set("正在分页重新扫描历史邮件")
        self._active_task_button = self._history_rescan_start_button
        self._active_task_button_text = self._history_rescan_start_button.text()
        self._history_rescan_start_button.setEnabled(False)
        self._history_rescan_start_button.setText("补扫中…")
        self._history_rescan_cancel_button.setText("取消补扫")
        self._history_rescan_cancel_button.setEnabled(True)
        self._history_rescan_progress.setRange(0, 0)
        self._history_rescan_progress.show()
        self._history_rescan_stats.setText("正在查询指定历史范围…")
        runner = _HistoryRescanRunner(
            self.service,
            date_from=date_from,
            date_to=date_to,
            apply_receive_rule=apply_receive_rule,
        )
        runner.signals.progress.connect(self._update_history_rescan_progress)
        runner.signals.finished.connect(self._finish_task)
        self._task_callback = self._show_history_rescan_result
        self._history_rescan_runner = runner
        self._active_runner = runner
        self.thread_pool.start(runner)

    @Slot(object)
    def _update_history_rescan_progress(self, progress: dict) -> None:
        if self.closed or not hasattr(self, "_history_rescan_stats"):
            return
        self._history_rescan_stats.setText(
            "扫描 {fetched} 封，匹配 {matched} 封，新增 {saved} 封，"
            "已归档 {duplicates} 封，规则不匹配 {rule_skipped} 封，失败 {failed} 封".format(
                **{key: int(progress.get(key) or 0) for key in _HistoryRescanRunner._PROGRESS_KEYS}
            )
        )

    def _cancel_history_rescan(self, *, close_dialog: bool = True) -> None:
        runner = self._history_rescan_runner
        if runner is not None and self.task_active:
            runner.cancel()
            if hasattr(self, "_history_rescan_cancel_button"):
                self._history_rescan_cancel_button.setText("正在取消…")
                self._history_rescan_cancel_button.setEnabled(False)
            if hasattr(self, "_history_rescan_stats"):
                self._history_rescan_stats.setText("已请求取消，正在安全结束当前邮件…")
            return
        if close_dialog and self._history_rescan_dialog is not None:
            self._history_rescan_dialog.reject()

    def _show_history_rescan_result(self, result: ServiceResult) -> None:
        self._history_rescan_runner = None
        if isinstance(result, ReceiveResult):
            self._update_history_rescan_progress(
                {
                    "fetched": result.scanned,
                    "matched": result.matched,
                    "saved": result.saved,
                    "duplicates": result.duplicates,
                    "rule_skipped": result.rule_skipped,
                    "failed": result.failed,
                }
            )
        if hasattr(self, "_history_rescan_progress"):
            self._history_rescan_progress.setRange(0, 1)
            self._history_rescan_progress.setValue(1)
        if hasattr(self, "_history_rescan_cancel_button"):
            self._history_rescan_cancel_button.setText("关闭")
            self._history_rescan_cancel_button.setEnabled(True)
        if hasattr(self, "_history_rescan_start_button"):
            self._history_rescan_start_button.setText("再次补扫")
        if result.status == OperationStatus.CANCELLED:
            kind = "warning"
        elif result.status == OperationStatus.NO_CHANGES:
            kind = "normal"
        elif result.status == OperationStatus.PARTIAL:
            kind = "warning"
        else:
            kind = "success" if result.ok else "error"
        self.show_message(result.message or "历史补扫已结束", kind)

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
        self._append_send_selection(selection)
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

    def choose_send_attachments(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择邮件附件", str(Path.home()), "所有文件 (*.*)"
        )
        if not paths:
            self.show_message("已取消添加附件", "normal")
            return
        added = 0
        for raw_path in paths:
            try:
                selection = SendFileSelection.capture(Path(raw_path))
            except (OSError, SecurityError) as exc:
                self.show_message(f"无法添加附件：{exc}", "error")
                continue
            if self._append_send_selection(selection):
                added += 1
        self.show_message(f"已添加 {added} 个附件", "success" if added else "normal")

    def _append_send_selection(self, selection: SendFileSelection) -> bool:
        key = str(selection.path).casefold()
        if any(str(item.path).casefold() == key for item in self.send_selections):
            return False
        self.send_selections.append(selection)
        if self.send_selection is None:
            self.send_selection = selection
        self._populate_send_attachments()
        self._update_send_action_state()
        return True

    def _populate_send_attachments(self) -> None:
        if not hasattr(self, "send_attachment_table"):
            return
        self.send_attachment_table.setRowCount(0)
        for index, selection in enumerate(self.send_selections):
            self.send_attachment_table.insertRow(index)
            name_item = QTableWidgetItem(selection.path.name)
            name_item.setToolTip(str(selection.path))
            name_item.setData(Qt.ItemDataRole.UserRole, str(selection.path))
            self.send_attachment_table.setItem(index, 0, name_item)
            self.send_attachment_table.setItem(
                index, 1, QTableWidgetItem(format_size(selection.size))
            )
            remove = self._button(
                "移除",
                lambda checked=False, value=str(selection.path): self.remove_send_attachment(value),
                text_only=True,
            )
            remove.setObjectName("compactButton")
            self.send_attachment_table.setCellWidget(index, 2, remove)
            self.send_attachment_table.setRowHeight(index, 42)
        self.send_attachment_title.setText(f"附件 {len(self.send_selections)} 个")

    def remove_send_attachment(self, raw_path: str) -> None:
        key = str(Path(raw_path)).casefold()
        self.send_selections = [
            item for item in self.send_selections if str(item.path).casefold() != key
        ]
        if self.send_selection and str(self.send_selection.path).casefold() == key:
            self.send_selection = self.send_selections[0] if self.send_selections else None
        self._sync_compatibility_send_selection()
        self._populate_send_attachments()
        self._update_send_action_state()

    def add_send_link(self) -> None:
        raw = self.send_link_edit.text().strip()
        parsed = QUrl(raw)
        if not raw or parsed.scheme().lower() not in {"http", "https"} or not parsed.host():
            self.show_message("链接必须是完整的 HTTP 或 HTTPS 地址", "error")
            return
        normalized = parsed.toString()
        if any(item["url"].casefold() == normalized.casefold() for item in self.send_links):
            self.show_message("该链接已在当前邮件中", "normal")
            return
        self.send_links.append({"url": normalized, "display_text": ""})
        self.send_link_edit.clear()
        self._populate_send_links()
        self._update_send_action_state()

    def _populate_send_links(self) -> None:
        self.send_link_table.setRowCount(0)
        for index, link in enumerate(self.send_links):
            self.send_link_table.insertRow(index)
            item = QTableWidgetItem(link["url"])
            item.setToolTip(link["url"])
            self.send_link_table.setItem(index, 0, item)
            remove = self._button(
                "移除",
                lambda checked=False, value=link["url"]: self.remove_send_link(value),
                text_only=True,
            )
            remove.setObjectName("compactButton")
            self.send_link_table.setCellWidget(index, 1, remove)
            self.send_link_table.setRowHeight(index, 42)
        self.send_link_title.setText(f"相关链接 {len(self.send_links)} 个")

    def remove_send_link(self, url: str) -> None:
        self.send_links = [item for item in self.send_links if item["url"] != url]
        self._populate_send_links()
        self._update_send_action_state()

    def _update_send_action_state(self, *_args) -> None:
        if not hasattr(self, "send_action_button"):
            return
        has_content = bool(
            self.subject_edit.text().strip()
            or self.send_body_edit.toPlainText().strip()
            or self.send_selections
            or self.send_links
        )
        self.send_action_button.setEnabled(has_content and not self.task_active)

    def _mark_recipient_edited(self, _value: str) -> None:
        self._recipient_user_edited = True

    def send_composed_mail(self) -> None:
        if self.task_active:
            self.show_message("已有后台任务正在运行，请等待完成后再发送", "working")
            return
        subject = self.subject_edit.text().strip()
        body = self.send_body_edit.toPlainText()
        if not (subject or body.strip() or self.send_selections or self.send_links):
            self.show_message("邮件主题、正文、附件和链接不能同时为空", "error")
            return
        try:
            recipient = normalize_manual_recipient(self.recipient_edit.text())
        except ValueError as exc:
            self.show_message(str(exc), "error")
            self.recipient_edit.setFocus()
            return
        changed = [item.path.name for item in self.send_selections if not item.is_unchanged()]
        if changed:
            self.show_message(
                f"附件已被删除、移动或修改，请重新添加：{changed[0]}", "error"
            )
            return
        total_size = sum(item.size for item in self.send_selections)
        confirmation = QMessageBox.question(
            self,
            "确认发送邮件",
            "请确认本次真实发送内容：\n\n"
            f"主题：{subject or '自动生成'}\n"
            f"正文：{'有' if body.strip() else '无'}\n"
            f"附件：{len(self.send_selections)} 个，共 {format_size(total_size)}\n"
            f"链接：{len(self.send_links)} 个\n"
            f"实际收件人：{recipient}\n\n"
            "确认后才会连接 QQ SMTP。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            self.send_status_label.setText("已取消发送，当前邮件内容仍保留")
            self.show_message("已取消发送，没有连接邮件服务器", "normal")
            return
        snapshots = list(self.send_selections)
        links = [dict(item) for item in self.send_links]
        self.send_status_label.setText("正在校验整封邮件并连接 QQ SMTP")
        self.send_progress.show()
        self._run_task(
            "正在校验并发送这一封邮件，请勿重复点击",
            lambda: self._send_composition_if_unchanged(
                recipient, subject, body, snapshots, links
            ),
            self._show_send_result,
            button=self.send_action_button,
            working_text="正在发送…",
        )

    def _send_composition_if_unchanged(
        self,
        recipient: str,
        subject: str,
        body: str,
        snapshots: list[SendFileSelection],
        links: list[dict[str, str]],
    ) -> ServiceResult:
        if any(not item.is_unchanged() for item in snapshots):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="file_changed",
                message="确认后附件发生变化，已阻止发送",
            )
        return self.service.send_user_selected_mail(
            recipient=recipient,
            subject=subject or None,
            body_text=body,
            attachment_paths=[item.path for item in snapshots],
            links=links,
        )

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
        self.send_selections = []
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
        ):
            button.setEnabled(False)
        self._populate_send_attachments()
        self._update_send_action_state()
        self.send_status_label.setText("填写主题、正文，或添加附件、链接后即可发送")

    def _sync_compatibility_send_selection(self) -> None:
        selection = self.send_selection
        if selection is None:
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
            ):
                button.setEnabled(False)
            return
        self.selected_send_path = str(selection.path)
        self.send_path_edit.setText(str(selection.path))
        self.send_path_edit.setToolTip(str(selection.path))
        self.send_file_name_value.setText(selection.path.name)
        self.send_file_size_value.setText(format_size(selection.size))
        self.send_file_type_value.setText(selection.path.suffix.lower() or "无扩展名")
        self.send_file_modified_value.setText(
            datetime.fromtimestamp(selection.modified_ns / 1_000_000_000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )
        for button in (
            self.copy_send_path_button,
            self.reveal_send_file_button,
            self.preview_send_file_button,
        ):
            button.setEnabled(True)

    def _clear_send_composition(self) -> None:
        self.send_links = []
        if hasattr(self, "send_link_edit"):
            self.send_link_edit.clear()
        if hasattr(self, "send_body_edit"):
            self.send_body_edit.clear()
        self.subject_edit.clear()
        self._recipient_user_edited = False
        self.recipient_edit.setText(
            self.service.cfg.owner_gmail or self.service.cfg.gmail_address
        )
        self._populate_send_links()
        self._clear_send_selection()

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
        seconds = self._auto_seconds()
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
                    "GUI_AUTO_RECEIVE_INTERVAL_SECONDS": str(seconds),
                    "GUI_AUTO_RECEIVE_INTERVAL_MINUTES": str(max(1, seconds // 60)),
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
            today = datetime.now().strftime("%Y-%m-%d")
            messages = self.service.list_mail_messages(
                date_from=today,
                date_to=f"{today}\uffff",
                limit=500,
            ).details.get("messages", [])
            logs = self.service.get_recent_logs(100).details.get("events", [])
            logs.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
            history = self.service.get_history(100).details
            mcp = self.service.get_mcp_history(100).details.get("calls", [])
            managed_result = self.service.get_managed_files(500)
            if not managed_result.ok:
                raise RuntimeError(managed_result.message)
            maintenance = self.service.get_maintenance_status()
            auto_receive = self.service.get_auto_receive_state().details
            mail_sync = self.service.get_mail_sync_status().details
            workspaces = self.service.list_agent_workspaces().details.get(
                "workspaces", []
            )
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
                "messages": messages,
                "logs": logs,
                "history": history,
                "mcp": mcp,
                "managed_files": managed_result.details.get("files", []),
                "maintenance": maintenance.to_dict(),
                "auto_receive": auto_receive,
                "mail_sync": mail_sync,
                "workspaces": workspaces,
            },
        )

    def _apply_refresh_result(self, result: ServiceResult) -> None:
        if not result.ok:
            self.last_error_details = self._redact_error_details(result.message)
            self.error_details_button.setEnabled(True)
            self.show_message(self._friendly_result_message(result), "error")
            return
        status = result.details.get("status", {})
        self.mail_rows = result.details.get("messages", [])
        # 兼容旧界面测试和扩展读取名称；正式收件表的数据粒度已是邮件。
        self.file_rows = self.mail_rows
        self.log_rows = result.details.get("logs", [])
        self.history_rows = result.details.get("history", {"received": [], "sent": []})
        self.mcp_rows = result.details.get("mcp", [])
        self.managed_file_rows = result.details.get("managed_files", [])
        self._update_auto_receive_status(result.details.get("auto_receive", {}))
        self._update_mcp_read_status(result.details.get("mail_sync", {}))
        self._apply_config_to_controls(status)
        if self.inbox_search.text().strip():
            self._filter_inbox()
        else:
            self._populate_inbox_messages(self.mail_rows)
        self._populate_logs(self.logs_table, self.log_rows[:30])
        self._populate_sent_history()
        if hasattr(self, "history_table"):
            self._populate_history()
        if hasattr(self, "managed_files_table"):
            self._filter_managed_files()
        if hasattr(self, "full_logs_table"):
            self._populate_full_logs()
        if hasattr(self, "mcp_table"):
            self._populate_mcp_history()
        if hasattr(self, "agent_workspace_table"):
            self._populate_agent_workspaces(result.details.get("workspaces", []))
        maintenance = result.details.get("maintenance", {})
        maintenance_details = maintenance.get("details", {})
        if hasattr(self, "data_overview_values"):
            existing = [row for row in self.managed_file_rows if row.get("exists")]
            sent_size = sum(
                int(row.get("size_bytes") or 0)
                for row in existing if row.get("category") == "已发送归档"
            )
            agent_size = sum(
                int(row.get("size_bytes") or 0)
                for row in existing if row.get("category") == "Agent 结果"
            )
            overview_values = {
                "database": "正常" if maintenance_details.get("integrity_check") == "ok" else "异常",
                "database_size": format_size(maintenance_details.get("database_size_bytes")),
                "received": format_size(maintenance_details.get("received", {}).get("size_bytes")),
                "sent": format_size(sent_size),
                "agent": format_size(agent_size),
                "backups": format_size(maintenance_details.get("backups_size_bytes")),
            }
            for key, value in overview_values.items():
                self.data_overview_values[key].setText(value)
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
            if hasattr(self, "receive_account_label"):
                self.receive_account_label.setText(cfg.gmail_address or "尚未配置 Gmail 收件账号")
            self.self_mail_check.setChecked(cfg.auto_receive_only_self_mail)
            self._update_receive_preference_summary()
            if not getattr(self, "_recipient_user_edited", False):
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
        auto_state = getattr(self, "_auto_state", {})
        auto_failures = int(auto_state.get("consecutive_global_failures") or 0)
        auto_text = (
            "连接退避" if self.auto_switch.isChecked() and auto_failures
            else "正常运行" if self.auto_switch.isChecked()
            else "未开启"
        )
        self.service_rows["auto"].set_value(
            auto_text,
            success=self.auto_switch.isChecked() and not auto_failures,
            danger=bool(auto_failures),
        )
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
        if hasattr(self, "dashboard_health_detail"):
            self.dashboard_health_detail.setText(health_detail)
            self.dashboard_health_detail.setToolTip(health_detail)

        today = datetime.now().strftime("%Y-%m-%d")
        sent_today = sum(
            1
            for row in self.history_rows.get("sent", [])
            if str(row.get("sent_at") or row.get("created_at") or "").startswith(today)
            and row.get("status") in {"sent", "success", "sent_archive_failed"}
        )
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
        self.stat_cards["received"].set_count(len(self.mail_rows))
        self.stat_cards["saved"].set_count(
            sum(int(row.get("counts", {}).get("resources") or 0) for row in self.mail_rows)
        )
        self.stat_cards["sent"].set_count(sent_today)
        self.stat_cards["errors"].set_count(error_today)

    def _populate_inbox_messages(self, rows: list[dict]) -> None:
        table = self.inbox_table
        self._configure_inbox_table(table)
        table.setRowCount(0)
        for index, row in enumerate(rows):
            table.insertRow(index)
            package_id = str(row.get("package_id") or "")
            subject = str(row.get("subject") or "无主题邮件")
            sender = str(row.get("from") or "发件人未知")
            counts = row.get("counts") or {}
            body = str(row.get("body_summary") or "")
            summary = build_mail_list_summary(
                body,
                attachment_count=int(counts.get("attachments") or 0),
                inline_image_count=int(counts.get("inline_images") or 0),
                link_count=int(counts.get("links") or 0),
                downloaded_count=int(counts.get("downloads") or 0),
                archive_status=str(row.get("archive_status") or ""),
                parse_status=str(row.get("parse_status") or ""),
            )
            status = self._mail_archive_status_text(row)
            tooltip = build_mail_list_tooltip(
                subject=subject,
                sender=sender,
                body=body,
                attachment_count=int(counts.get("attachments") or 0),
                inline_image_count=int(counts.get("inline_images") or 0),
                link_count=int(counts.get("links") or 0),
                downloaded_count=int(counts.get("downloads") or 0),
            )
            values = [
                subject,
                sender,
                summary,
                self._short_time(row.get("received_at") or row.get("saved_at")),
                status,
                "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, package_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, row)
                item.setToolTip(tooltip)
                table.setItem(index, column, item)
            action = self._button(
                "查看邮件",
                lambda checked=False, value=package_id: self.show_mail_detail(value, "inbox"),
                outline=True,
            )
            action.setObjectName("compactButton")
            action.setFixedHeight(30)
            table.setCellWidget(index, 5, action)
            table.setRowHeight(index, MAIL_LIST_ROW_HEIGHT)

    @staticmethod
    def _mail_archive_status_text(row: dict) -> str:
        status = str(row.get("archive_status") or "").strip().lower()
        if row.get("legacy"):
            return "旧记录（内容有限）"
        return {
            "ready": "已归档",
            "saved": "已归档",
            "normal": "已归档",
            "partial": "部分完成",
            "failed": "处理失败",
            "needs_attention": "需要处理",
            "staging": "处理中",
        }.get(status, "已归档" if status else "状态未知")

    def _populate_files(self, table: DataTable, rows: list[dict], *, actions: bool) -> None:
        # 旧版单文件渲染入口仍保持原有四列契约；正式收件页不再调用它。
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["文件名", "大小", "收取时间", "操作"])
        self._configure_file_table(table)
        table.setRowCount(0)
        for row_index, row in enumerate(rows):
            table.insertRow(row_index)
            path = str(row.get("path_display") or row.get("saved_path") or row.get("body_file_path") or "")
            name = str(row.get("saved_filename") or Path(path).name or "未命名文件")
            if row.get("exists_now") is False:
                size_text = "文件已不存在"
            else:
                size_value = row.get("size_now")
                if size_value is None:
                    size_value = row.get("size_bytes")
                size_text = format_size(size_value)
            values = [
                name,
                size_text,
                self._short_time(row.get("created_at") or row.get("received_at")),
                "" if actions else localize_status(row.get("status") or "saved"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                if column in {0, 2}:
                    item.setToolTip(value)
                table.setItem(row_index, column, item)
            table.setRowHeight(row_index, 62 if len(name) > 38 else 48)
            if actions:
                action_widget = QWidget()
                action_widget.setObjectName("tableActions")
                action_layout = QHBoxLayout(action_widget)
                action_layout.setContentsMargins(4, 0, 4, 0)
                action_layout.setSpacing(5)
                action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                open_button = self._button(
                    "打开",
                    lambda checked=False, value=path: self._open_received_file(value),
                    icon_kind="open",
                )
                open_button.setObjectName("compactButton")
                open_button.setFixedHeight(30)
                copy_button = self._button("复制路径", icon_kind="copy")
                copy_button.setObjectName("compactButton")
                copy_button.setFixedHeight(30)
                copy_button.clicked.connect(
                    lambda checked=False, button=copy_button, value=path: self._copy_received_path(button, value)
                )
                action_layout.addWidget(open_button)
                action_layout.addWidget(copy_button)
                table.setCellWidget(row_index, 3, action_widget)
        if rows:
            table.setColumnWidth(2, 86)
            table.setColumnWidth(3, 170)

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

    def _populate_full_logs(self, *_args, append: bool = False) -> None:
        if not hasattr(self, "full_logs_table"):
            return
        offset = len(self.full_log_rows) if append else 0
        result = self.service.query_logs(
            level=self.log_filter.currentText(),
            category=(
                "" if self.log_type_filter.currentText() == "全部事件"
                else self.log_type_filter.currentText()
            ),
            date_from=self._log_filter_date_from(),
            search=self.log_search.text().strip(),
            include_daily_checks=self.log_daily_check.isChecked(),
            limit=self.log_page_size,
            offset=offset,
        )
        if not result.ok:
            self.show_message(result.message or "读取技术日志失败", "error")
            return
        page_rows = result.details.get("events", [])
        self.log_query_total = int(result.details.get("total") or 0)
        self.full_log_rows = (
            self.full_log_rows + page_rows if append else list(page_rows)
        )
        self.full_logs_table.setRowCount(0)
        for index, row in enumerate(self.full_log_rows):
            self.full_logs_table.insertRow(index)
            level = str(row.get("level", "INFO")).upper()
            values = [
                self._short_time(row.get("created_at"), include_date=True),
                level,
                str(row.get("category") or "系统与诊断"),
                str(row.get("message", "")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole + 1, row)
                if column == 1:
                    item.setForeground(QColor(self._level_color(level)))
                self.full_logs_table.setItem(index, column, item)
        displayed = len(self.full_log_rows)
        self.log_load_more_button.setText(
            f"加载更多（已显示 {displayed} / {self.log_query_total}）"
        )
        self.log_load_more_button.setEnabled(displayed < self.log_query_total)
        self._update_log_overview()

    def _load_more_logs(self) -> None:
        self._populate_full_logs(append=True)

    def _log_filter_date_from(self) -> str | None:
        selected = self.log_time_filter.currentText()
        now = datetime.now()
        if selected == "今天":
            return now.strftime("%Y-%m-%d")
        if selected == "最近 7 天":
            return (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        if selected == "最近 30 天":
            return (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        return None

    def _update_log_overview(self) -> None:
        overview = self.service.get_log_overview()
        if not overview.ok:
            self.log_overview_label.setText("技术日志概览暂不可用")
            return
        details = overview.details
        last_cleanup = self._short_time(
            details.get("last_cleanup_at"), include_date=True
        )
        self.log_overview_label.setText(
            f"当前技术事件 {int(details.get('total') or 0)} 条 · "
            f"今日警告/错误 {int(details.get('today_errors') or 0)} 条 · "
            f"自动保留：普通 {details.get('normal_days')} 天 / "
            f"错误 {details.get('error_days')} 天 / 最多 {int(details.get('max_count') or 0):,} 条 · "
            f"当前过期 {int(details.get('expired') or 0)} 条 · 上次清理 {last_cleanup}"
        )

    def _populate_sent_history(self) -> None:
        rows = self.history_rows.get("sent", [])
        self.sent_table.setRowCount(0)
        for index, row in enumerate(rows):
            self.sent_table.insertRow(index)
            path = str(row.get("sent_copy_path") or row.get("send_copy_path") or "")
            subject = str(
                row.get("subject")
                or row.get("original_filename")
                or Path(path).name
                or "无主题邮件"
            )
            body = str(row.get("body_text") or "")
            origin = {
                "manual_gui": "手动发件",
                "agent_mcp": "Agent 发送",
                "legacy_sent_file": "旧发送记录",
            }.get(str(row.get("source_origin") or ""), "受控发送")
            outbound_id = str(row.get("outbound_id") or "")
            summary = build_outbound_list_summary(
                body,
                attachment_count=int(row.get("attachment_count") or 0),
                link_count=int(row.get("link_count") or 0),
                source_origin=str(row.get("source_origin") or ""),
            )
            tooltip = (
                f"主题：{subject}\n来源：{origin}\n内容：{summary}\n"
                "双击查看完整发送详情。"
            )
            values = [
                subject,
                summary,
                origin,
                self._short_time(
                    row.get("sent_at") or row.get("created_at"), include_date=True
                ),
                localize_status(row.get("status")),
                "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, outbound_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, row)
                item.setToolTip(tooltip)
                self.sent_table.setItem(index, column, item)
            detail = self._button(
                "查看详情",
                lambda checked=False, value=outbound_id, data=dict(row): (
                    self.show_outbound_detail(value, "send")
                    if value else self._show_history_detail_data(data)
                ),
                outline=True,
            )
            detail.setObjectName("compactButton")
            self.sent_table.setCellWidget(index, 5, detail)
            self.sent_table.setRowHeight(index, MAIL_LIST_ROW_HEIGHT)

    def _populate_history(self) -> None:
        received = [
            ("收到邮件" if row.get("package_id") else "收件", row)
            for row in self.history_rows.get("received", [])
        ]
        sent = [
            (
                "Agent 发送"
                if row.get("outbound_id") and row.get("source_origin") == "agent_mcp"
                else "发送邮件" if row.get("outbound_id") else "发件",
                row,
            )
            for row in self.history_rows.get("sent", [])
        ]
        outbound_request_ids = {
            str(row.get("request_id") or "").casefold()
            for row in self.history_rows.get("sent", [])
            if row.get("request_id")
        }
        agent = [
            ("Agent / MCP", row)
            for row in self.mcp_rows
            if not row.get("request_id")
            or str(row.get("request_id")).casefold() not in outbound_request_ids
        ]
        combined = received + sent + agent
        combined.sort(key=lambda pair: str(pair[1].get("created_at") or pair[1].get("sent_at") or pair[1].get("received_at") or ""), reverse=True)
        selected_type = self.history_type_filter.currentText()
        if selected_type != "全部类型":
            combined = [
                pair for pair in combined
                if self._history_type_matches_filter(pair[0], selected_type)
            ]
        selected_status = self.history_status_filter.currentText()
        if selected_status != "全部状态":
            known_statuses = {
                self.history_status_filter.itemText(index)
                for index in range(1, self.history_status_filter.count() - 1)
            }
            combined = [
                pair for pair in combined
                if (
                    self._history_status_display(pair[0], pair[1]) == selected_status
                    or selected_status == "其他"
                    and self._history_status_display(pair[0], pair[1]) not in known_statuses
                )
            ]
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
                if keyword in self._history_summary(pair[0], pair[1]).lower()
                or keyword in str(pair[1].get("file_path") or "").lower()
                or keyword in str(pair[1].get("request_id") or "").lower()
                or keyword in str(pair[1].get("from") or "").lower()
                or keyword in str(pair[1].get("body_summary") or pair[1].get("body_text") or "").lower()
            ]
        self.history_table.setRowCount(0)
        for index, (direction, row) in enumerate(combined):
            self.history_table.insertRow(index)
            path = str(row.get("body_file_path") or row.get("sent_copy_path") or row.get("send_copy_path") or row.get("file_path") or row.get("source_path") or "")
            summary = self._history_summary(direction, row)
            time_value = row.get("created_at") or row.get("sent_at") or row.get("received_at")
            detail = dict(row)
            detail.update({
                "_direction": direction,
                "_summary": summary,
                "_path": path,
                "_time": str(time_value or ""),
                "_status_display": self._history_status_display(direction, row),
            })
            values = [
                direction,
                summary,
                self._short_time(time_value, include_date=True),
                self._history_status_display(direction, row),
                "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setData(Qt.ItemDataRole.UserRole + 1, detail)
                self.history_table.setItem(index, column, item)
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(3, 0, 3, 0)
            action_layout.setSpacing(4)
            action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            detail_button = self._button(
                "查看邮件" if row.get("package_id") else "查看详情",
                lambda checked=False, value=detail: self._open_history_detail(value),
            )
            file_button = self._button(
                "关联文件",
                lambda checked=False, value=path: self._reveal_file(Path(value)),
            )
            for button in (detail_button, file_button):
                button.setObjectName("compactButton")
                button.setFixedHeight(28)
                action_layout.addWidget(button)
            file_button.setEnabled(bool(path))
            self.history_table.setCellWidget(index, 4, action_widget)
            self.history_table.resizeRowToContents(index)
            self.history_table.setRowHeight(
                index,
                max(
                    self.history_table.rowHeight(index),
                    self._wrapped_row_height(
                        self.history_table, ((1, summary),), minimum=48
                    ),
                ),
            )

    @staticmethod
    def _history_type_matches_filter(direction: str, selected: str) -> bool:
        if selected == "收件":
            return direction in {"收件", "收到邮件"}
        if selected == "发件":
            return direction in {"发件", "发送邮件"}
        if selected == "Agent / MCP":
            return direction in {"Agent / MCP", "Agent 发送"}
        return direction == selected

    def _history_status_display(self, direction: str, row: dict) -> str:
        if direction in {"收件", "收到邮件"} and row.get("package_id"):
            return self._mail_archive_status_text(row)
        return localize_status(row.get("status"))

    def _open_history_detail(self, details: dict) -> None:
        if details.get("package_id"):
            self.show_mail_detail(str(details["package_id"]), "history")
        elif details.get("outbound_id"):
            self.show_outbound_detail(str(details["outbound_id"]), "history")
        else:
            self._show_history_detail_data(details)

    @staticmethod
    def _history_summary(direction: str, row: dict) -> str:
        path = str(
            row.get("body_file_path")
            or row.get("sent_copy_path")
            or row.get("send_copy_path")
            or row.get("file_path")
            or row.get("source_path")
            or ""
        )
        if direction in {"收件", "收到邮件"}:
            return str(row.get("subject") or "无主题邮件")
        if direction in {"发件", "发送邮件", "Agent 发送"}:
            return str(
                row.get("subject")
                or row.get("original_filename")
                or Path(path).name
                or "发送邮件"
            )
        return str(
            row.get("title")
            or Path(path).name
            or row.get("request_id")
            or "Agent / MCP 记录"
        )

    def _populate_mcp_history(self) -> None:
        if not hasattr(self, "mcp_table"):
            return
        self.mcp_recipient_label.setText(self.service.cfg.owner_gmail or "未配置")
        self.mcp_roots_label.setText(
            "\n".join(str(path) for path in self.service.cfg.effective_allowed_send_roots)
        )
        self.mcp_table.setRowCount(0)
        for index, row in enumerate(self.mcp_rows):
            self.mcp_table.insertRow(index)
            tool_name = str(row.get("tool_name") or "submit_result")
            values = [
                self._short_time(row.get("called_at") or row.get("created_at"), include_date=True),
                self._mcp_operation_text(tool_name),
                self._mcp_target_text(row),
                self._mcp_status_text(str(row.get("status") or "")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row)
                if column == 2:
                    full_path = str(
                        row.get("prepared_path")
                        or row.get("source_path")
                        or row.get("file_path")
                        or ""
                    )
                    item.setToolTip(full_path or value)
                self.mcp_table.setItem(index, column, item)
            detail = self._button(
                "查看详情",
                lambda checked=False, value=row: self._show_mcp_call_detail(value),
                text_only=True,
            )
            detail.setObjectName("compactButton")
            self.mcp_table.setCellWidget(index, 4, detail)
            self.mcp_table.setRowHeight(
                index,
                self._wrapped_row_height(self.mcp_table, ((2, values[2]),), minimum=48),
            )

    @staticmethod
    def _mcp_operation_text(tool_name: str) -> str:
        return {
            "search_mails": "搜索邮件",
            "get_mail": "读取邮件",
            "read_mail_resource": "读取附件",
            "prepare_mail_resources": "准备资源",
            "list_agent_workspaces": "查询工作区",
            "get_mail_sync_status": "查询同步",
            "submit_result": "发送结果",
        }.get(tool_name, "MCP 调用")

    @staticmethod
    def _mcp_status_text(status: str) -> str:
        return {
            "success": "成功",
            "no_changes": "成功",
            "partial": "部分完成",
            "duplicate": "已完成",
            "sent": "成功",
            "read_access_disabled": "读取已关闭",
            "failed": "失败",
        }.get(status, status or "未知")

    @staticmethod
    def _mcp_target_text(row: dict) -> str:
        target = str(row.get("target_summary") or "").strip()
        if target:
            return target
        path = str(row.get("file_path") or row.get("source_path") or "")
        if path:
            parent = Path(path).parent.name
            return f"文件：{Path(path).name}" + (f"（{parent}）" if parent else "")
        return str(row.get("mail_id") or row.get("resource_id") or row.get("request_id") or "MCP 调用")

    def _open_mcp_call_detail(self, row_index: int, _column: int) -> None:
        if row_index < 0 or row_index >= len(self.mcp_rows):
            return
        self._show_mcp_call_detail(self.mcp_rows[row_index])

    def _show_mcp_call_detail(self, row: dict) -> None:
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        hashes = details.get("hashes") or []
        hash_text = "\n".join(
            f"{item.get('filename') or item.get('resource_id')}  {item.get('sha256') or ''}"
            for item in hashes
        )
        if not hash_text and details.get("source_sha256"):
            hash_text = str(details.get("source_sha256"))
        self._show_structured_detail(
            "MCP 调用详情",
            (
                ("调用时间", row.get("called_at") or row.get("created_at")),
                ("操作", self._mcp_operation_text(str(row.get("tool_name") or "submit_result"))),
                ("工具名", row.get("tool_name") or "submit_result"),
                ("状态", self._mcp_status_text(str(row.get("status") or ""))),
                ("错误代码", row.get("error_code")),
                ("查询条件", row.get("query_summary")),
                ("目标", self._mcp_target_text(row)),
                ("request_id", row.get("request_id")),
                ("mail_id", row.get("mail_id")),
                ("resource_id", row.get("resource_id")),
                ("源路径", row.get("source_path") or row.get("file_path")),
                ("准备后路径", row.get("prepared_path") or row.get("staged_path")),
                ("结果数量", row.get("result_count")),
                ("返回字节", row.get("bytes_returned")),
                ("耗时", f"{int(row.get('duration_ms') or 0)} ms"),
                ("触发同步", "是" if row.get("sync_triggered") else "否"),
                ("使用缓存", "是" if row.get("cached") else "否"),
                ("资源 Hash", hash_text),
            ),
        )

    def _schedule_inbox_filter(self, _text: str = "") -> None:
        self.inbox_search_timer.start()

    def _filter_inbox(self, text: str | None = None) -> None:
        keyword = (
            str(text) if text is not None else self.inbox_search.text()
        ).strip()
        if not keyword:
            rows = self.mail_rows
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            result = self.service.search_mail_facts(
                keyword,
                date_from=today,
                date_to=f"{today}\uffff",
                limit=500,
            )
            if not result.ok:
                self.show_message(result.message or "邮件搜索失败", "error")
                rows = []
            else:
                rows = result.details.get("messages", [])
        self._populate_inbox_messages(rows)

    def _open_sent_record(self, row: int, column: int) -> None:
        if column == 5:
            return
        item = self.sent_table.item(row, 0)
        if item is None:
            return
        outbound_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        details = item.data(Qt.ItemDataRole.UserRole + 1) or {}
        if outbound_id:
            self.show_outbound_detail(outbound_id, "send")
        elif details:
            self._show_history_detail_data(dict(details))

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

    def _filter_managed_files(self, *_args) -> None:
        if not hasattr(self, "managed_files_table"):
            return
        keyword = self.file_data_search.text().strip().lower()
        selected_type = self.file_data_type_filter.currentText()
        selected_source = self.file_data_source_filter.currentText()
        selected_time = self.file_data_time_filter.currentText()
        rows = [
            row for row in self.managed_file_rows
            if (not keyword or keyword in str(row.get("display_name", "")).lower() or keyword in str(row.get("path", "")).lower())
            and (selected_type == "全部类型" or row.get("category") == selected_type)
            and (
                selected_source == "全部来源"
                or row.get("source") == selected_source
                or selected_source == "Gmail" and str(row.get("source", "")).startswith("Gmail")
            )
            and self._matches_time_filter(row.get("time"), selected_time)
        ]
        show_mail_owner = any("mail_subject" in row for row in rows)
        headers = (
            ["类型", "来源", "所属邮件", "文件名", "大小", "时间", "状态", "操作"]
            if show_mail_owner
            else ["类型", "来源", "文件名", "大小", "时间", "状态", "操作"]
        )
        self.managed_files_table.setColumnCount(len(headers))
        self.managed_files_table.setHorizontalHeaderLabels(headers)
        header = self.managed_files_table.horizontalHeader()
        if show_mail_owner:
            for column in (0, 1, 4, 5, 6, 7):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            for column, width in ((0, 82), (1, 82), (4, 82), (5, 132), (6, 82), (7, 184)):
                self.managed_files_table.setColumnWidth(column, width)
            action_column = 7
        else:
            for column in (0, 1, 3, 4, 5, 6):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            for column, width in ((0, 82), (1, 82), (3, 92), (4, 136), (5, 82), (6, 166)):
                self.managed_files_table.setColumnWidth(column, width)
            action_column = 6
        self.managed_files_table.setRowCount(0)
        for index, row in enumerate(rows):
            self.managed_files_table.insertRow(index)
            path = str(row.get("path") or "")
            values = [str(row.get("display_type") or row.get("category")), str(row.get("source"))]
            if show_mail_owner:
                values.append(str(row.get("mail_subject") or row.get("subject") or "—"))
            values.extend([
                str(row.get("display_name")),
                self._managed_size_text(row),
                self._short_time(row.get("time"), include_date=True),
                str(row.get("status_display") or localize_status(row.get("status"))),
                "",
            ])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setData(Qt.ItemDataRole.UserRole + 1, row)
                item.setToolTip(value)
                self.managed_files_table.setItem(index, column, item)
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(3, 0, 3, 0)
            action_layout.setSpacing(3)
            action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            buttons = [
                self._button("预览", lambda checked=False, value=path: self._preview_path(value)),
                self._button("打开", lambda checked=False, value=path: self._open_managed_file(value)),
            ]
            if show_mail_owner:
                buttons.append(
                    self._button(
                        "邮件",
                        lambda checked=False, value=str(row.get("package_id") or ""), data=dict(row): (
                            self.show_mail_detail(value, "files_data")
                            if value else self._show_managed_file_detail_data(data)
                        ),
                    )
                )
            else:
                buttons.append(
                    self._button("复制", lambda checked=False, value=path: self._copy_managed_path(value))
                )
            for button in buttons:
                button.setObjectName("compactButton")
                button.setFixedHeight(28)
                action_layout.addWidget(button)
            for button in buttons[:2]:
                button.setEnabled(bool(path and row.get("exists")))
            buttons[2].setEnabled(bool(row.get("package_id")) if show_mail_owner else bool(path))
            self.managed_files_table.setCellWidget(index, action_column, action_widget)
            self.managed_files_table.resizeRowToContents(index)
            self.managed_files_table.setRowHeight(
                index,
                max(
                    self.managed_files_table.rowHeight(index),
                    self._wrapped_row_height(
                        self.managed_files_table,
                        (
                            ((2, str(row.get("mail_subject") or "")), (3, str(row.get("display_name") or "")))
                            if show_mail_owner
                            else ((2, str(row.get("display_name") or "")),)
                        ),
                        minimum=48,
                    ),
                ),
            )

    @staticmethod
    def _managed_size_text(row: dict) -> str:
        if not row.get("path"):
            return "—"
        if not row.get("exists"):
            return "文件已不存在"
        if not row.get("size_known"):
            return "—"
        return format_size(row.get("size_bytes"))

    def _selected_path(self, table: DataTable) -> str:
        row = table.currentRow()
        if row < 0 or table.columnCount() == 0:
            return ""
        item = table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _selected_managed_file(self) -> dict:
        row = self.managed_files_table.currentRow()
        item = self.managed_files_table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.ItemDataRole.UserRole + 1) if item else {}
        return value if isinstance(value, dict) else {}

    def _preview_managed_file(self, row: int, column: int) -> None:
        item = self.managed_files_table.item(row, 0)
        if item:
            details = item.data(Qt.ItemDataRole.UserRole + 1)
            if (
                self.managed_files_table.columnCount() == 8
                and column == 2
                and isinstance(details, dict)
                and details.get("package_id")
            ):
                self.show_mail_detail(str(details["package_id"]), "files_data")
            else:
                self._preview_path(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def preview_selected_managed_file(self) -> None:
        path = self._selected_path(self.managed_files_table)
        self._preview_path(path) if path else self.show_message("请先选择文件", "warning")

    def open_selected_managed_file(self) -> None:
        path = self._selected_path(self.managed_files_table)
        self._open_managed_file(path) if path else self.show_message("请先选择文件", "warning")

    def reveal_selected_managed_file(self) -> None:
        path = self._selected_path(self.managed_files_table)
        self._reveal_file(Path(path)) if path else self.show_message("请先选择文件", "warning")

    def copy_selected_managed_file_path(self) -> None:
        path = self._selected_path(self.managed_files_table)
        if not path:
            self.show_message("请先选择文件", "warning")
            return
        self._copy_managed_path(path)

    def _copy_managed_path(self, path: str) -> None:
        if not path:
            self.show_message("文件路径不可用", "error")
            return
        QApplication.clipboard().setText(path)
        self.show_message("完整文件路径已复制", "success")

    def _open_managed_file(self, raw_path: str) -> None:
        path = Path(raw_path)
        try:
            assert_within_allowed_roots(
                path, self.service.cfg.effective_allowed_send_roots
            )
        except SecurityError:
            self.show_message("已阻止打开允许目录之外的文件", "error")
            return
        if not path.is_file():
            self.show_message("文件已不存在", "error")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            self.show_message(f"打开文件失败：{exc}", "error")

    def show_selected_managed_file_detail(self) -> None:
        row = self._selected_managed_file()
        if not row:
            self.show_message("请先选择文件", "warning")
            return
        self._show_managed_file_detail_data(row)

    def _show_managed_file_detail_data(self, row: dict) -> None:
        labels = {
            "body": "邮件正文",
            "attachment": "邮件附件",
            "sent_archive": "发送归档",
            "agent_source": "Agent 源文件",
        }
        fields = (
            ("文件名", row.get("display_name") or "—"),
            ("类型", labels.get(str(row.get("file_type")), row.get("category") or "—")),
            ("来源", row.get("source") or "—"),
            ("大小", self._managed_size_text(row)),
            ("完整路径", row.get("path") or "路径不可用"),
            ("时间", row.get("time") or "—"),
            ("状态", row.get("status_display") or localize_status(row.get("status"))),
            ("是否存在", "是" if row.get("exists") else "否"),
            ("MIME 类型", row.get("mime_type") or "—"),
            ("request_id", row.get("request_id") or "—"),
            ("SHA-256", row.get("sha256") or "—"),
        )
        self._show_structured_detail("文件详情", fields)

    def open_send_history(self) -> None:
        self.select_page("history")
        self.history_type_filter.setCurrentText("发件")

    def _show_history_detail(self, row: int, _column: int) -> None:
        item = self.history_table.item(row, 0)
        details = item.data(Qt.ItemDataRole.UserRole + 1) if item else {}
        if not isinstance(details, dict):
            details = {}
        self._open_history_detail(details)

    def _show_history_detail_data(self, details: dict) -> None:
        path = str(details.get("_path") or "")
        fields = (
            ("类型", details.get("_direction") or "—"),
            ("摘要", details.get("_summary") or "—"),
            ("完整时间", details.get("_time") or "—"),
            ("状态", details.get("_status_display") or localize_status(details.get("status"))),
            ("原始状态", details.get("status") or "—"),
            ("request_id", details.get("request_id") or "—"),
            ("关联文件", Path(path).name if path else "无关联文件"),
            ("完整路径", path or "—"),
            ("错误详情", details.get("error_message") or details.get("message") or "—"),
            ("source", details.get("source") or "—"),
            ("backend", details.get("backend") or "—"),
        )
        self._show_structured_detail("历史记录详情", fields)

    def _show_secondary_page(self, page: QWidget, *, return_page: str) -> None:
        self._detail_return_page = return_page if return_page in self.pages else "inbox"
        self.page_stack.setCurrentWidget(page)
        if hasattr(self, "right_panel"):
            self.right_panel.setVisible(False)
        tab_target = "send" if return_page in {"send", "agent"} else "inbox"
        self._set_exclusive_checked(self.tab_buttons, tab_target)
        nav_target = return_page if return_page in self.nav_buttons else ""
        self._set_exclusive_checked(self.nav_buttons, nav_target)

    @staticmethod
    def _clear_dynamic_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child_layout = item.layout()
            if child_layout is not None:
                BridgeWindow._clear_dynamic_layout(child_layout)

    def _open_inbox_mail(self, row: int, _column: int) -> None:
        item = self.inbox_table.item(row, 0)
        package_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        if package_id:
            self.show_mail_detail(package_id, "inbox")

    def _remember_mail_detail_splitter(self) -> None:
        sizes = self.mail_detail_splitter.sizes()
        if len(sizes) == 2 and all(size > 0 for size in sizes):
            self._mail_detail_splitter_sizes = [int(sizes[0]), int(sizes[1])]

    def show_mail_detail(self, package_id: str, return_page: str = "inbox") -> None:
        result = self.service.get_mail_message(package_id)
        if not result.ok:
            self._show_service_result(result)
            return
        details = result.details.get("message", {})
        self._current_mail_details = details
        self._mail_detail_return_widget = (
            self.mail_thread_page if return_page == "thread" else None
        )
        subject = str(details.get("subject") or "无主题邮件")
        sender = str(details.get("from") or "发件人未知")
        recipients = self._format_contact_items(
            details.get("to_addresses"), details.get("to")
        ) or "未记录"
        cc = self._format_contact_items(details.get("cc_addresses"), details.get("cc"))
        reply_to = self._format_contact_items(details.get("reply_to"), None)
        received_at = self._short_time(
            details.get("received_at") or details.get("saved_at"), include_date=True
        )
        backend = {
            "gmail_api": "Gmail API",
            "imap": "Gmail IMAP",
        }.get(str(details.get("backend") or ""), "Gmail")
        self.mail_detail_subject.setText(subject)
        meta_lines = [f"发件人：{sender}", f"收件人：{recipients}"]
        if cc:
            meta_lines.append(f"抄送：{cc}")
        if reply_to:
            meta_lines.append(f"回复至：{reply_to}")
        outbound = details.get("outbound_origin") or {}
        if isinstance(outbound, dict) and outbound.get("is_local"):
            meta_lines.append("来源：本机 AgentMailBridge 发件回流（已精确标记）")
        meta_lines.append(f"收取时间：{received_at} · {backend}")
        self.mail_detail_meta.setText("\n".join(meta_lines))
        counts = details.get("counts") or {}
        self.mail_detail_counts.setText(
            f"附件 {int(counts.get('attachments') or 0)} 个 · "
            f"图片 {int(counts.get('inline_images') or 0)} 张 · "
            f"链接 {int(counts.get('links') or 0)} 个 · "
            f"下载文件 {int(counts.get('downloads') or 0)} 个"
        )
        self.mail_detail_body.setPlainText(self._mail_body_text(details))
        self.mail_detail_thread_button.setEnabled(bool(details.get("thread_ref")))
        self.mail_detail_archive_button.setEnabled(bool(details.get("package_root")))
        self._populate_mail_detail_resources(details.get("resources") or [])
        self.mail_detail_splitter.setSizes(self._mail_detail_splitter_sizes)
        self.mail_detail_resource_scroll.verticalScrollBar().setValue(0)
        self._show_secondary_page(
            self.mail_detail_page,
            return_page=(self._detail_return_page if return_page == "thread" else return_page),
        )

    @staticmethod
    def _format_contact_items(items, fallback) -> str:
        readable: list[str] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            display = str(item.get("display_name") or "").strip()
            address = str(item.get("address") or "").strip()
            text = f"{display} <{address}>" if display and address else address or display
            if text:
                readable.append(text)
        if readable:
            return "；".join(readable)
        fallback_values = [fallback] if isinstance(fallback, str) else (fallback or [])
        return "；".join(
            str(value) for value in fallback_values if str(value).strip()
        )

    def _mail_body_text(self, details: dict) -> str:
        body = details.get("body") or {}
        for key in ("plain_absolute_path", "readable_absolute_path"):
            raw_path = str(body.get(key) or "")
            if not raw_path:
                continue
            path = Path(raw_path)
            try:
                assert_within_allowed_roots(path, [self.service.cfg.data_root_path])
                if path.is_file():
                    return path.read_bytes()[: PREVIEW_MAX_BYTES * 4].decode(
                        "utf-8", errors="replace"
                    )
            except (OSError, SecurityError):
                continue
        summary = str(details.get("body_summary") or "").strip()
        if summary:
            return summary
        return "此邮件没有可显示的纯文本正文。"

    def _populate_mail_detail_resources(self, resources: list[dict]) -> None:
        self._clear_dynamic_layout(self.mail_detail_resource_layout)
        visible = [
            resource
            for resource in resources
            if not str(resource.get("internal_type") or "").startswith("body_")
        ]
        if not visible:
            empty = QLabel("此邮件没有图片、附件或链接。")
            empty.setObjectName("hint")
            empty.setWordWrap(True)
            self.mail_detail_resource_layout.addWidget(empty)
            return
        groups = (
            ("邮件中的图片", [item for item in visible if item.get("internal_type") == "inline_image"]),
            ("附件", [item for item in visible if item.get("internal_type") == "attachment"]),
            (
                "链接与下载",
                [
                    item for item in visible
                    if item.get("internal_type") not in {"inline_image", "attachment"}
                ],
            ),
        )
        for section_name, section_resources in groups:
            if not section_resources:
                continue
            title = QLabel(section_name)
            title.setObjectName("sectionTitle")
            title.setProperty("resourceSection", section_name)
            self.mail_detail_resource_layout.addWidget(title)
            for resource in section_resources:
                self.mail_detail_resource_layout.addWidget(
                    self._mail_resource_card(resource)
                )

    def _mail_resource_card(self, resource: dict) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        internal_type = str(resource.get("internal_type") or "")
        path = str(resource.get("absolute_path") or "")
        url = str(resource.get("url") or "")
        if internal_type == "inline_image" and path:
            preview = QLabel()
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                preview.setPixmap(
                    pixmap.scaled(
                        132,
                        86,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                preview.setFixedSize(142, 94)
                preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(preview)
        text = QVBoxLayout()
        title = QLabel(
            str(resource.get("display_name") or resource.get("kind_display") or "邮件内容")
        )
        title.setObjectName("minorTitle")
        title.setWordWrap(True)
        title.setToolTip(url or str(resource.get("display_name") or resource.get("original_name") or ""))
        detail_parts = [str(resource.get("kind_display") or "邮件内容")]
        mime_type = str(resource.get("mime_type") or "").strip()
        if mime_type:
            detail_parts.append(mime_type)
        if resource.get("size_bytes") is not None:
            detail_parts.append(format_size(int(resource.get("size_bytes") or 0)))
        hostname = str(resource.get("hostname") or "").strip()
        if hostname:
            detail_parts.append(hostname)
        detail_parts.append(str(resource.get("status_display") or "已识别"))
        detail = QLabel(" · ".join(detail_parts))
        detail.setObjectName("hint")
        detail.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(detail)
        if resource.get("error"):
            error = QLabel(str(resource["error"]))
            error.setObjectName("errorText")
            error.setWordWrap(True)
            text.addWidget(error)
        layout.addLayout(text, 1)
        if path:
            layout.addWidget(
                self._button(
                    "安全预览",
                    lambda checked=False, value=path: self._preview_path(value),
                    outline=True,
                )
            )
            layout.addWidget(
                self._button(
                    "打开",
                    lambda checked=False, value=path: self._open_received_file(value),
                )
            )
        if url:
            copy_button = self._button(
                "复制 URL",
                lambda checked=False, value=url: self._copy_mail_url(value),
                text_only=True,
            )
            copy_button.setToolTip(url)
            layout.addWidget(copy_button)
            layout.addWidget(
                self._button(
                    "打开链接",
                    lambda checked=False, value=url: self._open_external_link(value),
                    outline=True,
                )
            )
        return card

    def _copy_mail_url(self, url: str) -> None:
        QApplication.clipboard().setText(url)
        self.show_message("完整 URL 已复制", "success")

    def _open_external_link(self, url: str) -> None:
        parsed = QUrl(url)
        if parsed.scheme().lower() != "https" or not parsed.host():
            self.show_message("仅允许显式打开有效的 HTTPS 链接", "error")
            return
        if not QDesktopServices.openUrl(parsed):
            self.show_message("无法使用默认浏览器打开链接", "error")

    def open_current_mail_archive(self) -> None:
        details = getattr(self, "_current_mail_details", {})
        raw_path = str(details.get("package_root") or "")
        if not raw_path:
            self.show_message("当前邮件没有可打开的归档目录", "normal")
            return
        path = Path(raw_path)
        try:
            assert_within_allowed_roots(path, [self.service.cfg.data_root_path])
            if not path.is_dir():
                raise OSError("归档目录已不存在")
            os.startfile(str(path))
        except (OSError, SecurityError) as exc:
            self.show_message(f"打开邮件归档失败：{exc}", "error")

    def _return_from_mail_detail(self) -> None:
        if self._mail_detail_return_widget is self.mail_thread_page:
            self.page_stack.setCurrentWidget(self.mail_thread_page)
            self._mail_detail_return_widget = None
            return
        self.select_page(self._detail_return_page)

    def open_current_mail_thread(self) -> None:
        details = getattr(self, "_current_mail_details", {})
        thread_ref = str(details.get("thread_ref") or "")
        if not thread_ref:
            self.show_message("当前邮件没有可用的会话信息", "normal")
            return
        result = self.service.get_mail_thread(
            thread_ref,
            account_ref=str(details.get("account_ref") or "") or None,
        )
        if not result.ok:
            self._show_service_result(result)
            return
        thread = result.details.get("thread", {})
        messages = thread.get("messages") or []
        self._current_thread = thread
        self.mail_thread_summary.setText(f"本会话共 {len(messages)} 封邮件")
        self._clear_dynamic_layout(self.mail_thread_cards_layout)
        for index, message in enumerate(messages, 1):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            header = QHBoxLayout()
            title = QLabel(f"{index}. {message.get('subject') or '无主题邮件'}")
            title.setObjectName("minorTitle")
            title.setWordWrap(True)
            header.addWidget(title, 1)
            header.addWidget(
                self._button(
                    "查看此邮件",
                    lambda checked=False, value=str(message.get("package_id") or ""): self.show_mail_detail(value, "thread"),
                    outline=True,
                )
            )
            card_layout.addLayout(header)
            meta = QLabel(
                f"{message.get('from') or '发件人未知'} · "
                f"{self._short_time(message.get('received_at') or message.get('saved_at'), include_date=True)}"
            )
            meta.setObjectName("hint")
            card_layout.addWidget(meta)
            summary = QLabel(" ".join(str(message.get("body_summary") or "无正文").split())[:500])
            summary.setWordWrap(True)
            card_layout.addWidget(summary)
            self.mail_thread_cards_layout.addWidget(card)
        self._show_secondary_page(self.mail_thread_page, return_page=self._detail_return_page)

    def _return_from_mail_thread(self) -> None:
        self.page_stack.setCurrentWidget(self.mail_detail_page)

    def show_outbound_detail(self, outbound_id: str, return_page: str = "send") -> None:
        result = self.service.get_outbound_message(outbound_id)
        if not result.ok:
            self._show_service_result(result)
            return
        details = result.details.get("message", {})
        self._current_outbound_details = details
        subject = str(details.get("subject") or "无主题邮件")
        source = {
            "manual_gui": "手动发件",
            "agent_mcp": "Agent 发送",
            "legacy_sent_file": "旧发送记录",
        }.get(str(details.get("source_origin") or ""), "受控发送")
        recipients = "；".join(details.get("to") or []) or "固定收件人"
        status = localize_status(details.get("status"))
        sent_at = self._short_time(
            details.get("sent_at") or details.get("created_at"), include_date=True
        )
        limited = " · 旧记录内容有限" if details.get("legacy_limited") else ""
        self.outbound_detail_subject.setText(subject)
        self.outbound_detail_meta.setText(
            f"来源：{source} · 收件人：{recipients}\n发送时间：{sent_at} · 状态：{status}{limited}"
        )
        body = str(details.get("body_text") or "")
        self.outbound_detail_body.setPlainText(
            body if body else "此发送记录没有可显示的正文。"
        )
        self._clear_dynamic_layout(self.outbound_detail_resource_layout)
        resources = details.get("resources") or []
        links = details.get("links") or []
        if not resources and not links:
            empty = QLabel("此发送记录没有附件或链接。")
            empty.setObjectName("hint")
            self.outbound_detail_resource_layout.addWidget(empty)
        for resource in resources:
            self.outbound_detail_resource_layout.addWidget(
                self._outbound_resource_card(resource)
            )
        for link in links:
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            label = QLabel(str(link.get("display_text") or link.get("url") or "链接"))
            label.setWordWrap(True)
            row.addWidget(label, 1)
            row.addWidget(
                self._button(
                    "打开链接",
                    lambda checked=False, value=str(link.get("url") or ""): self._open_external_link(value),
                    outline=True,
                )
            )
            self.outbound_detail_resource_layout.addWidget(card)
        self._show_secondary_page(self.outbound_detail_page, return_page=return_page)

    def _outbound_resource_card(self, resource: dict) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        path = str(
            resource.get("sent_archive_path")
            or resource.get("staged_path")
            or resource.get("source_path")
            or ""
        )
        title = QLabel(str(resource.get("display_name") or "附件"))
        title.setObjectName("minorTitle")
        title.setWordWrap(True)
        layout.addWidget(title, 1)
        size = resource.get("size_bytes")
        layout.addWidget(QLabel(format_size(size) if size is not None else "—"))
        if path:
            layout.addWidget(
                self._button(
                    "安全预览",
                    lambda checked=False, value=path: self._preview_path(value),
                    outline=True,
                )
            )
            layout.addWidget(
                self._button(
                    "打开",
                    lambda checked=False, value=path: self._open_managed_file(value),
                )
            )
        return card

    def _return_from_outbound_detail(self) -> None:
        self.select_page(self._detail_return_page)

    def _toggle_mcp_read_access(self, enabled: bool) -> None:
        if self._loading_mcp_read_access:
            return
        if enabled:
            confirmation = QMessageBox.question(
                self,
                "启用本机 Agent 邮件读取",
                "启用后，能启动当前 MCP 配置的本机进程可以按工具边界搜索和读取本地邮件归档。\n\n"
                "该授权不是逐封分享；不会开放凭据、任意文件路径或邮件修改能力。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirmation != QMessageBox.StandardButton.Yes:
                self._loading_mcp_read_access = True
                self.mcp_read_switch.setChecked(False)
                self._loading_mcp_read_access = False
                self._update_mcp_read_status()
                return
        result = self.service.set_mcp_mail_read_access(enabled)
        if not result.ok:
            self._loading_mcp_read_access = True
            self.mcp_read_switch.setChecked(not enabled)
            self._loading_mcp_read_access = False
        self._update_mcp_read_status()
        self._show_service_result(result)

    def _update_mcp_read_status(self, sync: dict | None = None) -> None:
        if not hasattr(self, "mcp_read_status_label"):
            return
        enabled = bool(self.service.cfg.mcp_mail_read_enabled)
        self.mcp_read_status_label.setText("读取已启用" if enabled else "读取已关闭")
        self.mcp_read_status_label.setStyleSheet(
            f"color: {SUCCESS if enabled else TEXT_MUTED}; font-weight: 700;"
        )
        if sync is None:
            sync = self.service.get_mail_sync_status().details
        freshness = {
            "fresh": "数据新鲜",
            "stale": "本地数据较旧",
            "unknown": "尚无成功同步时间",
        }.get(str(sync.get("freshness") or ""), "状态未知")
        running = "同步中" if sync.get("is_syncing") else "后台已开启" if sync.get("enabled") else "后台未开启"
        last_success = self._short_time(sync.get("last_success_at"), include_date=True)
        self.mcp_sync_status_label.setText(f"{running}　{freshness}　上次成功 {last_success}")

    def _show_mcp_setup_guide(self) -> None:
        command, args = mcp_launch()
        self._show_structured_detail(
            "MCP 接入说明",
            (
                ("服务名称", "agent-mail-bridge"),
                ("传输方式", "本机 stdio，按需启动，会话结束退出"),
                ("启动程序", subprocess.list2cmdline([command, *args])),
                ("通用配置", generic_mcp_json()),
                ("Codex 命令", mcp_client_command("codex")),
                ("Claude Code 命令", mcp_client_command("claude")),
            ),
        )

    def _copy_mcp_example(self, kind: str) -> None:
        examples = {
            "receive": "请调用 AgentMailBridge MCP，根据当前任务需要搜索并读取相关邮件及附件。",
            "send": "请调用 AgentMailBridge MCP，将当前任务最终结果发送到我的邮箱。",
        }
        QApplication.clipboard().setText(examples[kind])
        self.show_message("示例指令已复制", "success")

    def _populate_agent_workspaces(self, workspaces: list[str] | None = None) -> None:
        values = workspaces
        if values is None:
            values = self.service.list_agent_workspaces().details.get("workspaces", [])
        self.agent_workspace_table.setRowCount(0)
        for index, raw_path in enumerate(values):
            self.agent_workspace_table.insertRow(index)
            item = QTableWidgetItem(str(raw_path))
            item.setToolTip(str(raw_path))
            item.setData(Qt.ItemDataRole.UserRole, str(raw_path))
            self.agent_workspace_table.setItem(index, 0, item)
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(4, 3, 4, 3)
            actions_layout.setSpacing(5)
            copy_button = self._button(
                "复制路径",
                lambda checked=False, value=str(raw_path): self._copy_agent_workspace_path(value),
                text_only=True,
            )
            remove = self._button(
                "移除",
                lambda checked=False, value=str(raw_path): self.remove_agent_workspace(value),
                text_only=True,
            )
            copy_button.setObjectName("compactButton")
            remove.setObjectName("compactButton")
            actions_layout.addWidget(copy_button)
            actions_layout.addWidget(remove)
            self.agent_workspace_table.setCellWidget(index, 1, actions)
            self.agent_workspace_table.setRowHeight(
                index,
                self._wrapped_row_height(
                    self.agent_workspace_table, ((0, str(raw_path)),), minimum=46
                ),
            )
        self.mcp_roots_label.setText(
            "\n".join(str(path) for path in self.service.cfg.effective_allowed_send_roots)
        )

    def _copy_agent_workspace_path(self, raw_path: str) -> None:
        QApplication.clipboard().setText(raw_path)
        self.show_message("工作区路径已复制", "success")

    def add_agent_workspace_from_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择要授权给 Agent 的项目工作区", str(Path.home())
        )
        if not path:
            self.show_message("已取消添加工作区", "normal")
            return
        result = self.service.add_agent_workspace(path)
        self._show_service_result(result)
        if result.ok:
            self._populate_agent_workspaces()

    def remove_agent_workspace(self, raw_path: str) -> None:
        confirmation = QMessageBox.question(
            self,
            "移除 Agent 工作区授权",
            f"移除后，下一次 MCP 会话将不能交付该目录中的文件。\n\n{raw_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        result = self.service.remove_agent_workspace(raw_path)
        self._show_service_result(result)
        if result.ok:
            self._populate_agent_workspaces()

    def _show_structured_detail(
        self, title: str, fields: tuple[tuple[str, object], ...]
    ) -> None:
        dialog = QDialog(self)
        dialog.setObjectName("structuredDetailDialog")
        dialog.setWindowTitle(title)
        dialog.resize(680, 460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(9)
        for name, value in fields:
            label = QLabel(str("—" if value is None or value == "" else value))
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow(name, label)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(
            self._button("关闭", dialog.accept, primary=True),
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        dialog.exec()

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
        item = self.full_logs_table.item(row, 0)
        details = item.data(Qt.ItemDataRole.UserRole + 1) if item else None
        if not isinstance(details, dict):
            return
        level = str(details.get("level") or "INFO").upper()
        advice = (
            "建议检查对应账号、网络或数据目录，并按需导出脱敏诊断信息。"
            if level in {"WARNING", "ERROR", "FAILED"}
            else "无需处理。"
        )
        message = self._redact_error_details(str(details.get("message") or ""))
        QMessageBox.information(
            self,
            "日志详情",
            "\n".join((
                f"时间：{details.get('created_at') or '—'}",
                f"级别：{level}",
                f"事件类型：{details.get('category') or '系统与诊断'}",
                f"消息：{message}",
                f"处理建议：{advice}",
            )),
        )

    def show_selected_log_detail(self) -> None:
        row = self.full_logs_table.currentRow()
        if row < 0:
            self.show_message("请先选择日志", "warning")
            return
        self._show_log_detail(row, 0)

    def open_log_folder(self) -> None:
        folder = self.service.cfg.data_root_path / "logs"
        self.open_managed_directory(folder)

    def save_log_retention_settings(self) -> None:
        normal_days = int(self.log_normal_retention.currentData())
        error_days = int(self.log_error_retention.currentData())
        max_count = int(self.log_max_count.currentData())
        result = self.service.set_log_retention(
            normal_days=normal_days,
            error_days=error_days,
            max_count=max_count,
        )
        if not result.ok:
            self._show_service_result(result)
            return
        try:
            save_env_values({
                "NORMAL_LOG_RETENTION_DAYS": str(normal_days),
                "WARNING_ERROR_LOG_RETENTION_DAYS": str(error_days),
                "APP_EVENT_MAX_COUNT": str(max_count),
            })
        except OSError as exc:
            self.show_message(f"日志保留设置未能持久化：{exc}", "error")
            return
        log_event(
            self.service.cfg.db_path,
            "SUCCESS",
            "config",
            f"日志保留设置已更新：普通 {normal_days} 天，错误 {error_days} 天，上限 {max_count} 条",
        )
        self._update_log_overview()
        self.show_message("日志保留设置已保存，重启后仍会生效", "success")

    def prune_technical_logs(self) -> None:
        self._run_task(
            "正在清理过期技术日志",
            self.service.prune_logs,
            self._after_log_maintenance,
            operation_name="技术日志清理",
            working_text="正在清理…",
        )

    def clear_daily_check_logs(self) -> None:
        self._run_task(
            "正在清除日常自动检查记录",
            self.service.clear_daily_check_logs,
            self._after_log_maintenance,
            operation_name="清除日常检查",
            working_text="正在清除…",
        )

    def clear_all_technical_logs(self) -> None:
        choice = QMessageBox.question(
            self,
            "清空全部技术日志",
            "只删除技术日志，不会删除邮件、附件、收发历史、Agent 交付记录或 MCP 审计。确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        self._run_task(
            "正在清空技术日志",
            self.service.clear_all_technical_logs,
            self._after_log_maintenance,
            operation_name="清空技术日志",
            working_text="正在清空…",
        )

    def _after_log_maintenance(self, result: ServiceResult) -> None:
        self._show_service_result(result)
        self._populate_full_logs()

    def export_current_log_filter(self) -> None:
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前筛选日志",
            str(self.service.cfg.data_root_path / "filtered-technical-logs.csv"),
            "CSV 文件 (*.csv)",
        )
        if not destination:
            return
        filters = {
            "level": self.log_filter.currentText(),
            "category": (
                "" if self.log_type_filter.currentText() == "全部事件"
                else self.log_type_filter.currentText()
            ),
            "date_from": self._log_filter_date_from(),
            "search": self.log_search.text().strip(),
            "include_daily_checks": self.log_daily_check.isChecked(),
        }
        self._run_task(
            "正在导出当前筛选日志",
            lambda: self.service.export_filtered_logs(destination, **filters),
            self._show_service_result,
            button=self.log_export_filtered_button,
            operation_name="筛选日志导出",
            working_text="正在导出…",
        )

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
        self._sync_manual_receive_actions()
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
            self._clear_send_composition()
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
        """自动任务执行中禁止重复启动；开启自动收取不禁用立即收取。"""
        enabled = not self.task_active
        hint = "当前已有检查任务正在运行" if not enabled else ""
        for button in self.manual_receive_buttons:
            button.setEnabled(enabled)
            button.setToolTip(hint)

    def _load_auto_receive_preferences(self) -> None:
        persisted = self.service.get_auto_receive_state().details
        seconds_text = os.getenv("GUI_AUTO_RECEIVE_INTERVAL_SECONDS", "").strip()
        try:
            if seconds_text:
                seconds = max(AUTO_RECEIVE_MIN_SECONDS, int(seconds_text))
            else:
                legacy_minutes = int(os.getenv("GUI_AUTO_RECEIVE_INTERVAL_MINUTES", "1"))
                seconds = max(AUTO_RECEIVE_MIN_SECONDS, legacy_minutes * 60)
        except ValueError:
            seconds = AUTO_RECEIVE_DEFAULT_SECONDS
        enabled = os.getenv("GUI_AUTO_RECEIVE", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if persisted.get("updated_at"):
            seconds = max(
                AUTO_RECEIVE_MIN_SECONDS,
                int(persisted.get("interval_seconds") or seconds),
            )
            enabled = bool(persisted.get("enabled"))
        self.auto_failures = int(persisted.get("consecutive_global_failures") or 0)
        self._loading_auto_receive = True
        self._set_combo_data(self.interval_combo, seconds)
        self.auto_switch.setChecked(enabled)
        self._loading_auto_receive = False
        self._sync_manual_receive_actions()
        self.service.save_auto_receive_state(
            enabled=enabled,
            interval_seconds=seconds,
        )
        if enabled:
            self._schedule_auto_receive(3)
        self._update_auto_receive_status(
            self.service.get_auto_receive_state().details
        )

    def _toggle_auto_receive(self, enabled: bool) -> None:
        if self._loading_auto_receive:
            return
        self._sync_manual_receive_actions()
        self.service.save_auto_receive_state(
            enabled=enabled,
            interval_seconds=self._auto_seconds(),
            consecutive_global_failures=0 if enabled else self.auto_failures,
            next_check_at=None if not enabled else None,
        )
        if enabled:
            self.auto_failures = 0
            self._schedule_auto_receive(3)
            self.show_message(
                f"自动收件已开启，每 {self._auto_seconds()} 秒检查一次，约 3 秒后首次检查",
                "success",
            )
        else:
            self.auto_timer.stop()
            self.show_message("自动收件已关闭", "normal")
        if hasattr(self, "service_rows"):
            self.service_rows["auto"].set_value("已开启" if enabled else "未开启", success=enabled)
        self._update_auto_receive_status(
            self.service.get_auto_receive_state().details
        )

    def _reschedule_auto_receive(self) -> None:
        if self._loading_auto_receive:
            return
        self.service.save_auto_receive_state(interval_seconds=self._auto_seconds())
        if hasattr(self, "auto_switch") and self.auto_switch.isChecked():
            self._schedule_auto_receive()

    def _automatic_receive(self) -> None:
        if not self.auto_switch.isChecked():
            return
        if self.task_active:
            self._schedule_auto_receive(5)
            return
        now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
        self.service.save_auto_receive_state(
            last_check_at=now_text,
            last_result="checking",
        )
        self._update_auto_receive_status(
            self.service.get_auto_receive_state().details
        )
        self._run_task(
            "自动收件正在运行",
            lambda: self.service.receive(automatic=True),
            self._finish_auto_receive,
        )

    def _watchdog_auto_receive(self) -> None:
        """睡眠、长暂停或事件循环阻塞恢复后尽快补偿检查。"""
        self.service.schedule_event_maintenance()
        if not self.auto_switch.isChecked() or self.task_active:
            return
        state = self.service.get_auto_receive_state().details
        next_check = state.get("next_check_at")
        if not next_check:
            self._schedule_auto_receive(1)
            return
        try:
            overdue = datetime.now() - datetime.fromisoformat(str(next_check))
        except ValueError:
            overdue = timedelta(seconds=1)
        if overdue.total_seconds() >= 0:
            self.auto_timer.start(1)

    def _auto_seconds(self) -> int:
        return max(
            AUTO_RECEIVE_MIN_SECONDS,
            int(self.interval_combo.currentData() or AUTO_RECEIVE_DEFAULT_SECONDS),
        )

    def _schedule_auto_receive(self, seconds: int | None = None) -> None:
        if not self.auto_switch.isChecked():
            return
        delay = max(1, seconds if seconds is not None else self._auto_seconds())
        self.auto_timer.start(delay * 1000)
        next_check = (datetime.now() + timedelta(seconds=delay)).isoformat(
            sep=" ", timespec="seconds"
        )
        self.service.save_auto_receive_state(
            enabled=True,
            interval_seconds=self._auto_seconds(),
            next_check_at=next_check,
        )
        self._update_auto_receive_status(
            self.service.get_auto_receive_state().details
        )

    def _finish_auto_receive(self, result: ServiceResult) -> None:
        self._show_receive_result(result)
        if not self.auto_switch.isChecked():
            return
        now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
        if result.status in {OperationStatus.FAILED, OperationStatus.AUTH_REQUIRED}:
            self.auto_failures += 1
            if self.auto_failures == 1:
                log_event(
                    self.service.cfg.db_path,
                    "WARNING",
                    "receive",
                    "自动收件进入连接退避："
                    f"{self._redact_error_details(result.message or result.error_code or '连接失败')}；"
                    "调度器将按策略自动重试",
                )
            delay = AUTO_RECEIVE_BACKOFF_SECONDS[
                min(self.auto_failures - 1, len(AUTO_RECEIVE_BACKOFF_SECONDS) - 1)
            ]
            self.service.save_auto_receive_state(
                last_check_at=now_text,
                last_result=result.status.value,
                last_error=result.message or result.error_code,
                consecutive_global_failures=self.auto_failures,
            )
            self._schedule_auto_receive(delay)
            return
        recovered = self.auto_failures > 0
        self.auto_failures = 0
        if recovered:
            log_event(
                self.service.cfg.db_path,
                "SUCCESS",
                "receive",
                "自动收件连接已恢复并退出全局退避",
            )
        result_text = {
            OperationStatus.NO_CHANGES: "暂无新邮件",
            OperationStatus.PARTIAL: "部分完成，失败项已隔离重试",
            OperationStatus.SUCCESS: "收件成功",
        }.get(result.status, result.message or result.status.value)
        self.service.save_auto_receive_state(
            last_check_at=now_text,
            last_success_at=now_text,
            last_result=result_text,
            last_error=None,
            consecutive_global_failures=0,
            checkpoint=now_text,
        )
        self._schedule_auto_receive()

    def _update_auto_receive_status(self, state: dict) -> None:
        if not hasattr(self, "auto_state_values"):
            return
        self._auto_state = dict(state)
        enabled = bool(state.get("enabled", self.auto_switch.isChecked()))
        failures = int(state.get("consecutive_global_failures") or 0)
        if not enabled:
            state_text = "未开启"
        elif self.task_active:
            state_text = "正在检查"
        elif failures:
            state_text = "连接退避"
        else:
            state_text = "正常运行"
        last_result = str(state.get("last_result") or "尚未检查")
        if failures and state.get("last_error"):
            last_result = f"连接失败：{state.get('last_error')}"
        pending = int(state.get("pending") or state.get("pending_retries") or 0)
        attention = int(state.get("needs_attention") or 0)
        retries = str(pending)
        if attention:
            retries += f"，需处理 {attention}"
        self.auto_state_values["state"].setText(state_text)
        self.auto_state_values["last_check"].setText(self._short_time(state.get("last_check_at")))
        self.auto_state_values["last_success"].setText(self._short_time(state.get("last_success_at")))
        self.auto_state_values["next_check"].setText(self._short_time(state.get("next_check_at")))
        self.auto_state_values["last_result"].setText(last_result)
        self.auto_state_values["last_result"].setToolTip(last_result)
        self.auto_state_values["retries"].setText(retries)

    def open_today_folder(self) -> None:
        now = datetime.now()
        today_folder = (
            self.service.cfg.received_dir
            / "mail"
            / f"{now.year:04d}"
            / f"{now.month:02d}"
            / f"{now.day:02d}"
        )
        mail_root = self.service.cfg.received_dir / "mail"
        target = today_folder if today_folder.exists() else mail_root if mail_root.exists() else self.service.cfg.received_dir
        try:
            assert_within_allowed_roots(target, [self.service.cfg.data_root_path])
            os.startfile(str(target))
        except (OSError, SecurityError) as exc:
            self.show_message(f"打开目录失败：{exc}", "error")

    def select_latest_file(self) -> None:
        if not self.mail_rows:
            self.show_message("今日暂未收到邮件")
            return
        self.files_table.selectRow(0)
        package_id = str(self.mail_rows[0].get("package_id") or "")
        if package_id:
            self.show_mail_detail(package_id, "inbox")

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
            assert_within_allowed_roots(
                path, self.service.cfg.effective_allowed_send_roots
            )
        except SecurityError:
            self.show_message("已阻止访问允许目录之外的路径", "error")
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
        self._hidden_window_was_maximized = self.isMaximized()
        self.hide()
        self.notify("Agent 邮箱桥接工具仍在运行", "可从系统托盘打开窗口或正常退出", "tray-hidden", 3600)

    def show_from_tray(self) -> None:
        if self._hidden_window_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        self.accepting_tasks = False
        self.auto_timer.stop()
        self.auto_watchdog.stop()
        if self.task_active:
            if self._history_rescan_runner is not None:
                self._history_rescan_runner.cancel()
            self.pending_quit = True
            self.show_message("正在等待当前任务安全结束", "working")
            return
        self._finalize_quit()

    def _finalize_quit(self) -> None:
        self.quitting = True
        self.thread_pool.waitForDone(1000)
        self.settings.setValue("window/normal_geometry", self.normalGeometry())
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
    def _compact_table_path(value: str, visible_ratio: float = 0.30) -> str:
        """表格仅显示约 30% 路径；完整值仍保留在数据、提示和操作中。"""
        text = str(value or "")
        if len(text) <= 30:
            return text
        keep = min(21, max(16, int(len(text) * visible_ratio)))
        left = max(8, int(keep * 0.55))
        right = max(6, keep - left)
        return f"{text[:left]}…{text[-right:]}"

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
        self.auto_watchdog.stop()
        self.settings.setValue("window/normal_geometry", self.normalGeometry())
        self.settings.setValue("window/last_page", self._current_page_name())
        if self.quitting or self.tray_icon is None:
            self.settings.setValue("runtime/clean_exit", True)
        self.settings.sync()
        event.accept()

    def resizeEvent(self, event) -> None:
        if hasattr(self, "size_grip"):
            self.size_grip.move(self.width() - 16, self.height() - 16)
        if hasattr(self, "vertical_resize_handle"):
            self.vertical_resize_handle.setGeometry(0, self.height() - 6, self.width() - 16, 6)
        super().resizeEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.sync_window_state()
        super().changeEvent(event)
