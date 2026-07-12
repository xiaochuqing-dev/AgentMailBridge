"""显式确认后执行一次 packaged MCP 真实固定收件人发送验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.version import __version__


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("file", type=Path)
    parser.add_argument("--confirm-real-send", action="store_true")
    args = parser.parse_args()
    if not args.confirm_real_send:
        raise SystemExit("Refusing real send without --confirm-real-send")
    executable = args.executable.resolve()
    source = args.file.resolve()
    root = Path(__file__).resolve().parent.parent
    request_id = f"packaged-real-{uuid.uuid4().hex}"
    calls = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "submit_result", "arguments": {"file_path": str(source), "title": f"AgentMailBridge {__version__} packaged E2E", "request_id": request_id}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "submit_result", "arguments": {"file_path": str(source), "title": f"AgentMailBridge {__version__} packaged E2E", "request_id": request_id}},
        },
    ]
    env = os.environ.copy()
    env["AGENT_MAIL_BRIDGE_CONFIG"] = str(root / ".env")
    env.pop("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE", None)
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in calls) + "\n"
    completed = subprocess.run(
        [str(executable)],
        input=payload,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"MCP exit code {completed.returncode}")
    responses = {item["id"]: item for item in map(json.loads, completed.stdout.splitlines())}
    first = responses[2]["result"]["structuredContent"]
    second = responses[3]["result"]["structuredContent"]
    if first["status"] != "success" or second["status"] != "duplicate":
        raise SystemExit(f"Unexpected statuses: {first['status']}, {second['status']}")
    archived = Path(first["sent_copy_path"])
    source_hash = file_hash(source)
    if not archived.is_file() or file_hash(archived) != source_hash:
        raise SystemExit("Sent archive hash mismatch")
    print(f"packaged real send PASS; duplicate PASS; sha256={source_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
