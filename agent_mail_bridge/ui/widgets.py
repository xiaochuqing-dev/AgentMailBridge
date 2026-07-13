"""正式界面的可复用控件。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from agent_mail_bridge.ui.theme import (
    BORDER,
    DANGER,
    PURPLE,
    PURPLE_SOFT,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    WARNING,
)


def clear_layout(layout) -> None:
    """清空布局中的控件。"""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)


def horizontal_line() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    line.setFixedHeight(1)
    return line


def format_size(size_bytes: int | str | None) -> str:
    """把字节数转为紧凑显示。"""
    try:
        if size_bytes is None or str(size_bytes).strip() == "":
            return "—"
        value = max(0, int(size_bytes))
    except (TypeError, ValueError):
        return "—"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def tinted_icon_pixmap(icon: QIcon, size: int, color: str) -> QPixmap:
    """将 Qt 系统图标统一为参考图使用的单色线性视觉。"""
    source = icon.pixmap(size, size)
    result = QPixmap(source.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(result.rect(), QColor(color))
    painter.end()
    return result


def line_icon_pixmap(kind: str, size: int = 20, color: str = PURPLE) -> QPixmap:
    """绘制参考图风格的轻量线性图标，不替代邮箱品牌 Logo。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), max(1.4, size / 12), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    pad = size * 0.16
    rect = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    if kind in {"mail", "envelope"}:
        painter.drawRoundedRect(rect, size * 0.09, size * 0.09)
        painter.drawLine(rect.topLeft(), QPointF(size / 2, size * 0.56))
        painter.drawLine(rect.topRight(), QPointF(size / 2, size * 0.56))
    elif kind == "calendar":
        painter.drawRoundedRect(rect, size * 0.09, size * 0.09)
        painter.drawLine(QPointF(rect.left(), size * 0.38), QPointF(rect.right(), size * 0.38))
        painter.drawLine(QPointF(size * 0.34, size * 0.1), QPointF(size * 0.34, size * 0.28))
        painter.drawLine(QPointF(size * 0.66, size * 0.1), QPointF(size * 0.66, size * 0.28))
        painter.drawLine(QPointF(size * 0.34, size * 0.59), QPointF(size * 0.46, size * 0.7))
        painter.drawLine(QPointF(size * 0.46, size * 0.7), QPointF(size * 0.7, size * 0.48))
    elif kind == "send":
        painter.drawPolygon(QPolygonF([
            QPointF(size * 0.12, size * 0.47), QPointF(size * 0.88, size * 0.14),
            QPointF(size * 0.64, size * 0.86), QPointF(size * 0.46, size * 0.58),
        ]))
        painter.drawLine(QPointF(size * 0.46, size * 0.58), QPointF(size * 0.88, size * 0.14))
    elif kind == "warning":
        painter.drawPolygon(QPolygonF([
            QPointF(size / 2, size * 0.1), QPointF(size * 0.9, size * 0.84), QPointF(size * 0.1, size * 0.84),
        ]))
        painter.drawLine(QPointF(size / 2, size * 0.36), QPointF(size / 2, size * 0.59))
        painter.drawPoint(QPointF(size / 2, size * 0.72))
    elif kind == "shield":
        path = QPainterPath(QPointF(size / 2, size * 0.08))
        path.lineTo(size * 0.84, size * 0.22)
        path.lineTo(size * 0.78, size * 0.66)
        path.quadTo(size / 2, size * 0.92, size * 0.22, size * 0.66)
        path.lineTo(size * 0.16, size * 0.22)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(size * 0.32, size * 0.48), QPointF(size * 0.45, size * 0.61))
        painter.drawLine(QPointF(size * 0.45, size * 0.61), QPointF(size * 0.69, size * 0.35))
    elif kind == "clock":
        painter.drawEllipse(rect)
        painter.drawLine(QPointF(size / 2, size / 2), QPointF(size / 2, size * 0.29))
        painter.drawLine(QPointF(size / 2, size / 2), QPointF(size * 0.67, size * 0.6))
    elif kind == "database":
        painter.drawEllipse(QRectF(pad, pad, size - 2 * pad, size * 0.28))
        painter.drawLine(QPointF(pad, size * 0.3), QPointF(pad, size * 0.73))
        painter.drawLine(QPointF(size - pad, size * 0.3), QPointF(size - pad, size * 0.73))
        painter.drawArc(QRectF(pad, size * 0.58, size - 2 * pad, size * 0.28), 180 * 16, 180 * 16)
    elif kind == "settings":
        painter.drawEllipse(QRectF(size * 0.35, size * 0.35, size * 0.3, size * 0.3))
        for x1, y1, x2, y2 in ((0.5, 0.12, 0.5, 0.28), (0.5, 0.72, 0.5, 0.88), (0.12, 0.5, 0.28, 0.5), (0.72, 0.5, 0.88, 0.5), (0.23, 0.23, 0.34, 0.34), (0.66, 0.66, 0.77, 0.77), (0.77, 0.23, 0.66, 0.34), (0.34, 0.66, 0.23, 0.77)):
            painter.drawLine(QPointF(size * x1, size * y1), QPointF(size * x2, size * y2))
    elif kind == "info":
        painter.drawEllipse(rect)
        painter.drawLine(QPointF(size / 2, size * 0.44), QPointF(size / 2, size * 0.72))
        painter.drawPoint(QPointF(size / 2, size * 0.3))
    elif kind == "search":
        painter.drawEllipse(QRectF(size * 0.16, size * 0.14, size * 0.5, size * 0.5))
        painter.drawLine(QPointF(size * 0.6, size * 0.6), QPointF(size * 0.86, size * 0.86))
    elif kind == "refresh":
        painter.drawArc(QRectF(size * 0.16, size * 0.16, size * 0.68, size * 0.68), 35 * 16, 280 * 16)
        painter.drawLine(QPointF(size * 0.72, size * 0.13), QPointF(size * 0.85, size * 0.18))
        painter.drawLine(QPointF(size * 0.85, size * 0.18), QPointF(size * 0.8, size * 0.32))
    elif kind == "file":
        path = QPainterPath(QPointF(size * 0.27, size * 0.1))
        path.lineTo(size * 0.62, size * 0.1)
        path.lineTo(size * 0.78, size * 0.27)
        path.lineTo(size * 0.78, size * 0.88)
        path.lineTo(size * 0.27, size * 0.88)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(size * 0.61, size * 0.1), QPointF(size * 0.61, size * 0.29))
        painter.drawLine(QPointF(size * 0.61, size * 0.29), QPointF(size * 0.78, size * 0.29))
    elif kind == "open":
        painter.drawRoundedRect(QRectF(size * 0.12, size * 0.24, size * 0.64, size * 0.62), size * 0.05, size * 0.05)
        painter.drawLine(QPointF(size * 0.46, size * 0.54), QPointF(size * 0.88, size * 0.12))
        painter.drawLine(QPointF(size * 0.62, size * 0.12), QPointF(size * 0.88, size * 0.12))
        painter.drawLine(QPointF(size * 0.88, size * 0.12), QPointF(size * 0.88, size * 0.38))
    elif kind == "copy":
        painter.drawRoundedRect(QRectF(size * 0.28, size * 0.26, size * 0.58, size * 0.6), size * 0.06, size * 0.06)
        painter.drawRoundedRect(QRectF(size * 0.12, size * 0.1, size * 0.58, size * 0.6), size * 0.06, size * 0.06)
    elif kind == "moon":
        path = QPainterPath(QPointF(size * 0.68, size * 0.12))
        path.cubicTo(size * 0.34, size * 0.18, size * 0.24, size * 0.63, size * 0.54, size * 0.82)
        path.cubicTo(size * 0.71, size * 0.93, size * 0.86, size * 0.84, size * 0.9, size * 0.76)
        path.cubicTo(size * 0.55, size * 0.78, size * 0.42, size * 0.35, size * 0.68, size * 0.12)
        painter.drawPath(path)
    elif kind == "sun":
        painter.drawEllipse(QRectF(size * 0.34, size * 0.34, size * 0.32, size * 0.32))
        for x1, y1, x2, y2 in ((0.5, 0.08, 0.5, 0.23), (0.5, 0.77, 0.5, 0.92), (0.08, 0.5, 0.23, 0.5), (0.77, 0.5, 0.92, 0.5), (0.2, 0.2, 0.3, 0.3), (0.7, 0.7, 0.8, 0.8), (0.8, 0.2, 0.7, 0.3), (0.3, 0.7, 0.2, 0.8)):
            painter.drawLine(QPointF(size * x1, size * y1), QPointF(size * x2, size * y2))
    elif kind == "key":
        painter.drawEllipse(QRectF(size * 0.1, size * 0.18, size * 0.42, size * 0.42))
        painter.drawLine(QPointF(size * 0.46, size * 0.52), QPointF(size * 0.88, size * 0.86))
        painter.drawLine(QPointF(size * 0.69, size * 0.7), QPointF(size * 0.77, size * 0.61))
    elif kind == "terminal":
        painter.drawRoundedRect(rect, size * 0.08, size * 0.08)
        painter.drawLine(QPointF(size * 0.28, size * 0.38), QPointF(size * 0.42, size * 0.5))
        painter.drawLine(QPointF(size * 0.42, size * 0.5), QPointF(size * 0.28, size * 0.62))
        painter.drawLine(QPointF(size * 0.5, size * 0.66), QPointF(size * 0.7, size * 0.66))
    painter.end()
    return pixmap


class ToggleSwitch(QAbstractButton):
    """带动画感的轻量开关。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 22)

    def sizeHint(self) -> QSize:
        return QSize(38, 22)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = QColor(PURPLE)
        inactive = QColor("#CDD0D9")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(active if self.isChecked() else inactive)
        painter.drawRoundedRect(QRectF(0, 2, 38, 18), 9, 9)
        center_x = 28 if self.isChecked() else 10
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QPointF(center_x, 11), 7, 7)


class AccountCard(QFrame):
    """左侧邮箱账号卡片。"""

    clicked = Signal()

    def __init__(self, symbol: QIcon | str, title: str, email: str, description: str, color: str):
        super().__init__()
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(122)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 13, 12, 12)
        layout.setSpacing(10)

        icon = QLabel(symbol if isinstance(symbol, str) else "")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(40, 40)
        if isinstance(symbol, QIcon) and not symbol.isNull():
            icon.setPixmap(symbol.pixmap(36, 36))
            icon.setStyleSheet("background: transparent; border: none;")
        else:
            icon.setStyleSheet(
                f"color: {color}; background: #FFFFFF; border: 1px solid {BORDER};"
                "border-radius: 8px; font-size: 17px; font-weight: 800;"
            )
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text_area = QVBoxLayout()
        text_area.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("minorTitle")
        self.title_label.setWordWrap(False)
        self.status_tag = QLabel("未配置")
        self.status_tag.setObjectName("tag")
        self.status_tag.setProperty("configured", False)
        self.email_label = QLabel(email or "未配置")
        self.email_label.setObjectName("muted")
        self.email_label.setWordWrap(True)
        self.email_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.detail_label = QLabel(description)
        self.detail_label.setObjectName("hint")
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(5)
        bottom_row.addWidget(self.detail_label)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self.status_tag)
        text_area.addWidget(self.title_label)
        text_area.addWidget(self.email_label)
        text_area.addLayout(bottom_row)
        layout.addLayout(text_area, 1)

    def set_configured(self, configured: bool) -> None:
        self.status_tag.setText("已配置" if configured else "未配置")
        self.status_tag.setProperty("configured", configured)
        self.status_tag.style().unpolish(self.status_tag)
        self.status_tag.style().polish(self.status_tag)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class NavButton(QPushButton):
    """左侧导航按钮。"""

    def __init__(self, icon: QIcon | str, text: str):
        super().__init__(text if isinstance(icon, QIcon) else f"{icon}   {text}")
        if isinstance(icon, QIcon):
            self.setIcon(icon)
            self.setIconSize(QSize(15, 15))
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(46)


class StatusRow(QWidget):
    """右侧服务状态行。"""

    def __init__(self, icon: QIcon | QPixmap | str, label: str, value: str = "—"):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(8)
        icon_label = QLabel(icon if isinstance(icon, str) else "")
        if isinstance(icon, QPixmap) and not icon.isNull():
            icon_label.setPixmap(icon)
        elif isinstance(icon, QIcon) and not icon.isNull():
            icon_label.setPixmap(tinted_icon_pixmap(icon, 16, PURPLE))
        else:
            icon_label.setStyleSheet(f"color: {PURPLE}; font-size: 14px;")
        icon_label.setFixedWidth(18)
        name = QLabel(label)
        name.setObjectName("statusName")
        name.setStyleSheet("font-size: 10px;")
        self.value_label = QLabel(value)
        # 126 像素可容纳完整日期和常见邮箱，避免高 DPI 下左侧截断。
        self.value_label.setMinimumWidth(126)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value_label.setObjectName("statusValue")
        self.value_label.setStyleSheet("font-size: 10px;")
        layout.addWidget(icon_label)
        layout.addWidget(name)
        layout.addStretch()
        layout.addWidget(self.value_label)

    def set_value(self, value: str, *, success: bool = False, danger: bool = False) -> None:
        self.value_label.setText(value)
        state = "success" if success else "danger" if danger else "normal"
        self.value_label.setProperty("statusState", state)
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)


class HealthStatusRow(QFrame):
    """可扫描、可跳转的单项连接健康状态。"""

    fix_requested = Signal(str)

    def __init__(self, icon_kind: str, title: str, target: str):
        super().__init__()
        self.setObjectName("healthItem")
        self.icon_kind = icon_kind
        self.target = target
        self.setMinimumHeight(54)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 7, 8, 7)
        layout.setSpacing(9)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(22, 22)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setSpacing(1)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("healthName")
        self.state_label = QLabel("未检查")
        self.state_label.setObjectName("healthState")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        title_row.addWidget(self.state_label)
        content.addLayout(title_row)
        self.detail_label = QLabel("尚未运行检查")
        self.detail_label.setObjectName("healthDetail")
        content.addWidget(self.detail_label)
        self.checked_label = QLabel("最近检查：—")
        self.checked_label.setObjectName("healthChecked")
        self.checked_label.hide()
        layout.addLayout(content, 1)

        self.fix_button = QPushButton("去处理")
        self.fix_button.setObjectName("compactButton")
        self.fix_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fix_button.clicked.connect(lambda: self.fix_requested.emit(self.target))
        self.fix_button.hide()
        layout.addWidget(self.fix_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_status("unchecked", "尚未运行检查")

    def set_status(self, state: str, detail: str, checked_at: str = "—") -> None:
        normalized = state if state in {"normal", "partial", "fault", "unchecked"} else "unchecked"
        labels = {
            "normal": ("正常", SUCCESS),
            "partial": ("部分异常", WARNING),
            "fault": ("故障", DANGER),
            "unchecked": ("未检查", TEXT_MUTED),
        }
        text, color = labels[normalized]
        self.state_label.setText(text)
        self.state_label.setProperty("healthState", normalized)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        detail_text = detail or "未提供检查说明"
        recent_text = checked_at or "—"
        self.detail_label.setText(f"{detail_text} · 最近检查 {recent_text}")
        self.detail_label.setToolTip(detail or "")
        self.checked_label.setText(f"最近检查：{recent_text}")
        self.icon_label.setPixmap(line_icon_pixmap(self.icon_kind, 18, color))
        self.fix_button.setVisible(normalized in {"partial", "fault"})


class StatCard(QFrame):
    """今日统计卡片。"""

    def __init__(self, object_name: str, icon: QIcon | QPixmap | str, title: str, color: str):
        super().__init__()
        self.setObjectName(object_name)
        self.setMinimumSize(116, 82)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 12, 10)
        layout.setSpacing(4)
        number_row = QHBoxLayout()
        icon_label = QLabel(icon if isinstance(icon, str) else "")
        if isinstance(icon, QPixmap) and not icon.isNull():
            icon_label.setPixmap(icon)
        elif isinstance(icon, QIcon) and not icon.isNull():
            icon_label.setPixmap(tinted_icon_pixmap(icon, 25, color))
        else:
            icon_label.setStyleSheet(f"color: {color}; font-size: 21px;")
        self.number = QLabel("0")
        self.number.setObjectName("statNumber")
        self.number.setStyleSheet("font-size: 24px; font-weight: 400;")
        number_row.addWidget(icon_label)
        number_row.addSpacing(6)
        number_row.addWidget(self.number)
        number_row.addStretch()
        caption = QLabel(title)
        caption.setObjectName("statCaption")
        caption.setStyleSheet("font-size: 10px;")
        layout.addLayout(number_row)
        layout.addWidget(caption)

    def set_count(self, value: int) -> None:
        self.number.setText(str(max(0, value)))


class TipRow(QWidget):
    """右侧快捷提示。"""

    def __init__(self, icon: QIcon | str, text: str, color: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(9)
        icon_label = QLabel(icon if isinstance(icon, str) else "")
        icon_label.setFixedWidth(19)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon_label.setPixmap(icon.pixmap(15, 15))
        else:
            icon_label.setStyleSheet(f"color: {color}; font-size: 15px;")
        label = QLabel(text)
        label.setObjectName("tipText")
        label.setWordWrap(True)
        label.setStyleSheet("font-size: 10px;")
        layout.addWidget(icon_label)
        layout.addWidget(label, 1)


class DataTable(QTableWidget):
    """统一表格外观和行为。"""

    def __init__(self, headers: list[str]):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.verticalHeader().setDefaultSectionSize(36)
        self.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.rowCount() != 0:
            return
        painter = QPainter(self.viewport())
        painter.setPen(self.palette().color(self.foregroundRole()).lighter(155))
        painter.drawText(
            self.viewport().rect(),
            Qt.AlignmentFlag.AlignCenter,
            "暂无数据",
        )


class MessageBar(QFrame):
    """展示任务结果和错误。"""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        self.label = QLabel("就绪")
        self.label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.label, 1)
        self.set_message("就绪")

    def set_message(self, text: str, kind: str = "normal") -> None:
        colors = {
            "normal": ("#F7F8FB", TEXT_MUTED),
            "success": ("#EFFAF3", SUCCESS),
            "error": ("#FFF1F2", DANGER),
            "warning": ("#FFF8E8", "#A76500"),
            "working": (PURPLE_SOFT, PURPLE),
        }
        background, foreground = colors.get(kind, colors["normal"])
        self.setStyleSheet(
            f"QFrame {{ background: {background}; border: 1px solid {BORDER}; border-radius: 5px; }}"
        )
        self.label.setStyleSheet(f"color: {foreground}; font-size: 10px; font-weight: 700;")
        self.label.setText(text)
        self.label.setToolTip(text)


def paint_app_icon(widget: QLabel) -> None:
    """设置紫色邮件应用图标。"""
    widget.setText("M")
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    widget.setFixedSize(30, 30)
    widget.setFont(QFont("Segoe UI Symbol", 15, QFont.Weight.Bold))
    widget.setStyleSheet(
        f"color: #FFFFFF; background: {PURPLE}; border-radius: 7px;"
        f"border: 1px solid {PURPLE};"
    )


def draw_status_dot(color: str = SUCCESS) -> QLabel:
    label = QLabel("●")
    label.setStyleSheet(f"color: {color}; font-size: 10px;")
    label.setFixedWidth(12)
    return label


def configure_table_pen(table: DataTable) -> None:
    """保留 Qt 高分屏下的细线效果。"""
    palette = table.palette()
    palette.setColor(table.foregroundRole(), QColor(TEXT))
    table.setPalette(palette)


def thin_pen(color: str = BORDER) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(1.0)
    return pen
