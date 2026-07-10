"""Windows 桌面运行时辅助：单实例与开机启动。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths

APP_RUN_VALUE = "AgentMailBridge"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class SingleInstanceGuard:
    """基于本机锁文件防止同一数据目录启动多个 GUI。"""

    def __init__(self, data_root: Path) -> None:
        digest = hashlib.sha256(str(data_root.resolve()).encode("utf-8")).hexdigest()[:16]
        temp_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation))
        self.lock = QLockFile(str(temp_dir / f"agent-mail-bridge-{digest}.lock"))
        self.lock.setStaleLockTime(30_000)  # 30 秒后允许回收异常退出的旧锁。

    def acquire(self) -> bool:
        return self.lock.tryLock(0)

    def release(self) -> None:
        if self.lock.isLocked():
            self.lock.unlock()


class StartupManager:
    """管理当前 Windows 用户的开机启动项。"""

    @staticmethod
    def command() -> str:
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
