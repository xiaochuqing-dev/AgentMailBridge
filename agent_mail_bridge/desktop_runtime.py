"""Windows 桌面运行时辅助：单实例与开机启动。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QLockFile, QStandardPaths
from PySide6.QtNetwork import QLocalServer, QLocalSocket

APP_RUN_VALUE = "AgentMailBridge"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class SingleInstanceGuard:
    """基于本机锁文件防止同一数据目录启动多个 GUI。"""

    def __init__(self, data_root: Path) -> None:
        digest = hashlib.sha256(str(data_root.resolve()).encode("utf-8")).hexdigest()[:16]
        temp_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation))
        self.lock = QLockFile(str(temp_dir / f"agent-mail-bridge-{digest}.lock"))
        self.lock.setStaleLockTime(30_000)  # 30 秒后允许回收异常退出的旧锁。
        self.server_name = f"agent-mail-bridge-{digest}"
        self.server: QLocalServer | None = None
        self.activation_handler: Callable[[], None] | None = None

    def acquire(self) -> bool:
        if not self.lock.tryLock(0):
            self._notify_existing()
            self._activate_existing_window()
            return False
        QLocalServer.removeServer(self.server_name)
        self.server = QLocalServer()
        if not self.server.listen(self.server_name):
            self.lock.unlock()
            self.server = None
            return False
        self.server.newConnection.connect(self._handle_activation)
        return True

    def set_activation_handler(self, handler: Callable[[], None]) -> None:
        self.activation_handler = handler

    def _notify_existing(self) -> None:
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        if socket.waitForConnected(1_000):
            socket.write(b"activate")
            socket.flush()
            socket.waitForBytesWritten(1_000)
            socket.disconnectFromServer()

    @staticmethod
    def _activate_existing_window() -> None:
        """Windows 下直接恢复已存在的隐藏主窗口，避免第二实例弹框停留。"""
        if sys.platform != "win32":
            return
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.FindWindowW(None, "Agent 邮箱桥接工具")
        if handle:
            user32.ShowWindow(handle, 9)  # SW_RESTORE
            user32.SetForegroundWindow(handle)

    def _handle_activation(self) -> None:
        if self.server is None:
            return
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.waitForReadyRead(200)
            if bytes(connection.readAll()).startswith(b"activate") and self.activation_handler:
                self.activation_handler()
            connection.disconnectFromServer()
            connection.deleteLater()

    def release(self) -> None:
        if self.server is not None:
            self.server.close()
            QLocalServer.removeServer(self.server_name)
            self.server = None
        if self.lock.isLocked():
            self.lock.unlock()


class StartupManager:
    """管理当前 Windows 用户的开机启动项。"""

    @staticmethod
    def command() -> str:
        if getattr(sys, "frozen", False):
            return subprocess.list2cmdline([sys.executable, "--background"])
        return subprocess.list2cmdline([sys.executable, "-m", "agent_mail_bridge.gui", "--background"])

    @classmethod
    def is_enabled(cls) -> bool:
        if sys.platform != "win32":
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, APP_RUN_VALUE)
                return value == cls.command()
        except FileNotFoundError:
            return False

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        if sys.platform != "win32":
            raise OSError("开机启动仅支持 Windows")
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_RUN_VALUE, 0, winreg.REG_SZ, cls.command())
            else:
                try:
                    winreg.DeleteValue(key, APP_RUN_VALUE)
                except FileNotFoundError:
                    pass
