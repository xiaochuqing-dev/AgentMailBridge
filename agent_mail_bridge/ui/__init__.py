"""AgentMailBridge 正式桌面界面。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_mail_bridge.ui.main_window import BridgeWindow

__all__ = ["BridgeWindow"]


def __getattr__(name: str):
    """延迟加载 Qt 界面，避免无界面的 MCP 进程仅导入配置保存器就依赖 PySide6。"""
    if name == "BridgeWindow":
        from agent_mail_bridge.ui.main_window import BridgeWindow

        return BridgeWindow
    raise AttributeError(name)
