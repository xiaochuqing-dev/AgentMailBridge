"""品牌资源发现与 Qt 接入。

最终 Logo 由用户提供；资源缺失时保留既有字母占位，不伪造品牌素材。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon, QIconEngine, QPixmap
from PySide6.QtWidgets import QLabel

from agent_mail_bridge.runtime_paths import get_runtime_paths


class _DeferredFileIconEngine(QIconEngine):
    """Keep icon lookup safe before Qt creates the GUI application."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = str(path)

    def key(self) -> str:
        return f"agent-mail-bridge-file:{self._path}"

    def clone(self) -> "_DeferredFileIconEngine":
        return _DeferredFileIconEngine(Path(self._path))

    def isNull(self) -> bool:  # noqa: N802 - Qt virtual method name
        return not Path(self._path).is_file()

    def actualSize(self, size, mode, state):  # noqa: N802 - Qt virtual method name
        if QGuiApplication.instance() is None:
            return size
        return QIcon(self._path).actualSize(size, mode, state)

    def pixmap(self, size, mode, state):
        if QGuiApplication.instance() is None:
            return QPixmap()
        return QIcon(self._path).pixmap(size, mode, state)

    def paint(self, painter, rect, mode, state):
        if QGuiApplication.instance() is not None:
            QIcon(self._path).paint(painter, rect, mode, state)


def _file_icon(path: Path) -> QIcon:
    if not path.is_file():
        return QIcon()
    if QGuiApplication.instance() is None:
        return QIcon(_DeferredFileIconEngine(path))
    return QIcon(str(path))


def brand_candidates() -> tuple[Path, ...]:
    brand_dir = get_runtime_paths().resource_root / "branding"
    return (
        brand_dir / "agentmailbridge.ico",
        brand_dir / "agentmailbridge.png",
        brand_dir / "logo.png",
    )


# 源码兼容常量；实际查找每次重新发现路径，以支持 frozen。
BRAND_CANDIDATES = brand_candidates()


def find_brand_asset() -> Path | None:
    """返回第一个可用的最终品牌资源。"""
    return next((path for path in brand_candidates() if path.is_file()), None)


def brand_icon() -> QIcon:
    """构建窗口和托盘共用图标；没有素材时返回空图标。"""
    path = find_brand_asset()
    return _file_icon(path) if path is not None else QIcon()


def provider_icon(name: str) -> QIcon:
    """返回用户明确提供的邮箱服务图标。"""
    filenames = {"gmail": "gmail.svg", "qq": "qqmail.webp"}
    filename = filenames.get(name.strip().lower())
    if not filename:
        return QIcon()
    path = get_runtime_paths().resource_root / "branding" / filename
    return _file_icon(path)


def agent_icon(name: str) -> QIcon:
    """返回来自官方 GitHub 组织或产品仓库的 Agent 品牌图标。"""
    filenames = {
        "codex": "agents/openai.png",
        "claude_code": "agents/anthropic.png",
        "claude_desktop": "agents/anthropic.png",
        "hermes": "agents/hermes.png",
    }
    filename = filenames.get(name.strip().lower())
    if not filename:
        return QIcon()
    path = get_runtime_paths().resource_root / "branding" / filename
    return _file_icon(path)


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
