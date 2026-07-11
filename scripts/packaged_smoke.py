"""对最终 MCP EXE 执行 stdio 生命周期和安全拒绝 smoke。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise SystemExit(f"MCP EXE 不存在：{executable}")
    with tempfile.TemporaryDirectory(prefix="amb packaged 中文 ") as temporary:
        root = Path(temporary)
        data_root = root / "Data Root"
        outside = root / "outside" / "result.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("packaged smoke", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "AGENT_MAIL_BRIDGE_HOME": str(root / "User Home"),
                "AGENT_MAIL_BRIDGE_DISABLE_DOTENV": "1",
                "AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE": "1",
                "DATA_ROOT": str(data_root),
                "GMAIL_ADDRESS": "user@example.com",
                "OWNER_GMAIL": "owner@example.com",
                "QQ_EMAIL": "sender@example.com",
            }
        )
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "submit_result",
                    "arguments": {
                        "file_path": str(outside),
                        "request_id": "packaged-smoke-001",
                    },
                },
            },
        ]
        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n"
        completed = subprocess.run(
            [str(executable)],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(f"MCP 返回码异常：{completed.returncode}；stderr={completed.stderr[-500:]}")
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        by_id = {item.get("id"): item for item in responses}
        if set(by_id) != {1, 2, 3, 4}:
            raise SystemExit("MCP stdout 含缺失或额外协议输出")
        if by_id[1]["result"]["serverInfo"]["version"] != "0.9.0":
            raise SystemExit("MCP 版本不一致")
        tools = by_id[3]["result"]["tools"]
        if [item["name"] for item in tools] != ["submit_result"]:
            raise SystemExit("MCP 工具列表异常")
        result = by_id[4]["result"]["structuredContent"]
        if result["status"] != "path_not_allowed":
            raise SystemExit(f"MCP 路径边界异常：{result['status']}")
        if "Traceback" in completed.stderr:
            raise SystemExit("MCP stderr 出现异常回溯")
    print("packaged MCP smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
