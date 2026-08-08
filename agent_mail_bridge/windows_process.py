"""Windows GUI 进程启动子进程时的统一无控制台策略。"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Mapping


def hidden_subprocess_kwargs(
    values: Mapping[str, Any] | None = None,
    *,
    hide_window: bool = True,
) -> dict[str, Any]:
    """返回可直接传给 subprocess 的参数，不改变 stdin/stdout/stderr 语义。"""

    options = dict(values or {})
    if sys.platform != "win32":
        return options

    create_no_window = int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    options["creationflags"] = int(options.get("creationflags") or 0) | create_no_window
    if not hide_window:
        return options

    startupinfo = options.get("startupinfo")
    if startupinfo is None:
        startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_factory is None:  # pragma: no cover - win32 应始终提供。
            return options
        startupinfo = startupinfo_factory()
    startupinfo.dwFlags |= int(
        getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    )
    startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
    options["startupinfo"] = startupinfo
    return options


def run_hidden(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """运行并等待一个不会创建可见控制台窗口的子进程。"""

    return subprocess.run(
        *popenargs,
        **hidden_subprocess_kwargs(kwargs),
    )


def popen_hidden(
    *popenargs: Any,
    hide_window: bool = True,
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    """启动无控制台子进程；显式 GUI 工具可保留自己的窗口。"""

    return subprocess.Popen(
        *popenargs,
        **hidden_subprocess_kwargs(kwargs, hide_window=hide_window),
    )
