"""品牌资源发现与 Qt 接入。

最终 Logo 由用户提供；资源缺失时保留既有字母占位，不伪造品牌素材。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QLabel


BRAND_DIR = Path(__file__).resolve().parents[1] / "resources" / "branding"
BRAND_CANDIDATES = (
    BRAND_DIR / "agentmailbridge.ico",
    BRAND_DIR / "agentmailbridge.png",
    BRAND_DIR / "logo.png",
)


def find_brand_asset() -> Path | None:
    """返回第一个可用的最终品牌资源。"""
    return next((path for path in BRAND_CANDIDATES if path.is_file()), None)


def brand_icon() -> QIcon:
    """构建窗口和托盘共用图标；没有素材时返回空图标。"""
    path = find_brand_asset()
    return QIcon(str(path)) if path is not None else QIcon()


def apply_brand_label(label: QLabel, fallback) -> bool:
    """将 Logo 放入品牌区域，返回是否已接入真实素材。"""
    path = find_brand_asset()
    if path is None:
        fallback(label)
        label.setToolTip("品牌素材待接入")
        return False
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        fallback(label)
        label.setToolTip("品牌素材无法读取")
        return False
    label.setFixedSize(32, 32)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setPixmap(
        pixmap.scaled(
            30,
            30,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    )
    label.setToolTip("AgentMailBridge")
    return True
