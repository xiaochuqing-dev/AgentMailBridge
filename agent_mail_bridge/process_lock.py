"""跨进程互斥锁；锁随进程退出由操作系统自动释放。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class ProcessLock:
    """使用一个锁文件字节协调 Windows/POSIX 本机进程。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream: BinaryIO | None = None

    def acquire(self, timeout: float = 0.0, poll_interval: float = 0.1) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            try:
                _lock_byte(stream)
                self._stream = stream
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    stream.close()
                    return False
                time.sleep(max(0.01, min(float(poll_interval), 0.5)))

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            _unlock_byte(stream)
        finally:
            stream.close()

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise BlockingIOError(f"锁已被占用：{self.path}")
        return self

    def __exit__(self, *_args) -> None:
        self.release()


def is_lock_available(path: str | Path) -> bool:
    """非阻塞探测锁状态，不保留锁。"""
    lock = ProcessLock(path)
    acquired = lock.acquire()
    if acquired:
        lock.release()
    return acquired


if os.name == "nt":
    import msvcrt

    def _lock_byte(stream: BinaryIO) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_byte(stream: BinaryIO) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover - Windows 是正式运行平台，POSIX 仅供兼容开发。
    import fcntl

    def _lock_byte(stream: BinaryIO) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_byte(stream: BinaryIO) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
