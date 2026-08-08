"""Windows GUI 子进程隐藏策略回归。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_mail_bridge import windows_process


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only startup flags")
def test_hidden_process_options_set_both_console_and_window_flags() -> None:
    options = windows_process.hidden_subprocess_kwargs(
        {"creationflags": 0x20, "encoding": "utf-8"}
    )

    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert options["creationflags"] & 0x20
    assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert options["startupinfo"].wShowWindow == subprocess.SW_HIDE
    assert options["encoding"] == "utf-8"


def test_non_windows_process_options_are_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(windows_process.sys, "platform", "linux")
    original = {"capture_output": True, "timeout": 5}

    assert windows_process.hidden_subprocess_kwargs(original) == original


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only startup flags")
def test_visible_gui_child_keeps_no_console_without_sw_hide() -> None:
    options = windows_process.hidden_subprocess_kwargs(
        {"text": True}, hide_window=False
    )

    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert "startupinfo" not in options


def test_gui_runtime_uses_shared_process_wrapper_and_mcp_stays_console() -> None:
    root = Path(__file__).resolve().parents[1]
    app_service = (root / "agent_mail_bridge" / "application_service.py").read_text(
        encoding="utf-8"
    )
    client_config = (root / "agent_mail_bridge" / "mcp_client_config.py").read_text(
        encoding="utf-8"
    )
    main_window = (root / "agent_mail_bridge" / "ui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    spec = (root / "packaging" / "windows" / "AgentMailBridge.spec").read_text(
        encoding="utf-8"
    )

    assert "completed = run_hidden(" in app_service
    assert "completed = run_hidden(" in client_config
    assert "popen_hidden(" in main_window
    mcp_block = spec.split("mcp_exe = EXE(", 1)[1].split("dist = COLLECT", 1)[0]
    assert "console=True" in mcp_block
