"""生成源码版或安装版的 MCP 客户端配置。"""

from __future__ import annotations

import json
import subprocess
import sys

from agent_mail_bridge.runtime_paths import get_runtime_paths


def mcp_launch() -> tuple[str, list[str]]:
    paths = get_runtime_paths()
    if paths.frozen:
        return str(paths.install_root / "AgentMailBridgeMCP.exe"), []
    return sys.executable, ["-m", "agent_mail_bridge.mcp_server"]


def mcp_client_command(client: str) -> str:
    command, args = mcp_launch()
    prefix = [client, "mcp", "add", "agent-mail-bridge", "--"]
    return subprocess.list2cmdline([*prefix, command, *args])


def generic_mcp_json() -> str:
    command, args = mcp_launch()
    return json.dumps(
        {"mcpServers": {"agent-mail-bridge": {"command": command, "args": args}}},
        ensure_ascii=False,
        indent=2,
    )
