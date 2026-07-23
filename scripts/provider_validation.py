"""对已配置账号执行受控 Provider 验收，输出不含凭据和邮件正文的 JSON 证据。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.version import __version__


def _status(result: Any) -> str:
    status = getattr(result, "status", "")
    return str(getattr(status, "value", status))


def _check(result: Any, *, counts: dict[str, int] | None = None) -> dict[str, Any]:
    operation_status = _status(result)
    item: dict[str, Any] = {
        "status": (
            "PARTIAL"
            if operation_status == "partial"
            else "PASS"
            if bool(getattr(result, "ok", False))
            else "FAIL"
        ),
        "operation_status": operation_status,
        "error_code": str(getattr(result, "error_code", "") or ""),
    }
    if counts:
        item["counts"] = counts
    return item


def _receive_counts(result: Any) -> dict[str, int]:
    return {
        key: int(getattr(result, key, 0) or 0)
        for key in (
            "scanned",
            "saved",
            "duplicates",
            "failed",
            "attachments",
            "pending_retries",
            "needs_attention",
        )
    }


def _write_evidence(evidence: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        sys.stdout.write(payload + "\n")
        return
    resolved = output.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(f"Provider validation evidence written: {resolved}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate one configured AgentMailBridge mail account."
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-network", action="store_true")
    parser.add_argument("--confirm-real-send", action="store_true")
    parser.add_argument("--poll-attempts", type=int, default=3)
    parser.add_argument("--poll-interval", type=int, default=5)
    args = parser.parse_args()
    if not args.confirm_network:
        raise SystemExit("Refusing Provider network validation without --confirm-network")
    if args.confirm_real_send and not args.confirm_network:
        raise SystemExit("Real send also requires --confirm-network")

    service = ApplicationService(load_config())
    initialized = service.initialize()
    if not initialized.ok:
        raise SystemExit("AgentMailBridge initialization failed")
    accounts_result = service.list_mail_accounts()
    accounts = list(accounts_result.details.get("accounts") or [])
    account = next(
        (
            item
            for item in accounts
            if str(item.get("account_id") or "") == args.account_id
        ),
        None,
    )
    if account is None:
        raise SystemExit("Configured account_id was not found")
    provider = str(account.get("provider") or "")
    if provider not in {"qq", "163", "generic_imap_smtp"}:
        raise SystemExit(
            "This validator is limited to QQ, 163, and Generic IMAP/SMTP"
        )

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "account_id": args.account_id,
        "provider": provider,
        "network_confirmed": True,
        "real_send_confirmed": bool(args.confirm_real_send),
        "checks": {},
    }
    checks = evidence["checks"]
    checks["configuration"] = {
        "status": "PASS"
        if account.get("enabled") and account.get("credential_configured")
        else "FAIL",
        "enabled": bool(account.get("enabled")),
        "credential_configured": bool(account.get("credential_configured")),
        "receive_enabled": bool(account.get("receive_enabled")),
        "send_enabled": bool(account.get("send_enabled")),
    }

    connection = service.test_mail_account_connection(args.account_id)
    checks["login"] = _check(connection)
    folders = service.discover_mail_account_mailboxes(args.account_id)
    folder_rows = list(folders.details.get("mailboxes") or [])
    roles = Counter(str(item.get("mailbox_role") or "other") for item in folder_rows)
    checks["folder_discovery"] = _check(
        folders,
        counts={
            "mailboxes": len(folder_rows),
            **{f"role_{key}": value for key, value in sorted(roles.items())},
        },
    )

    first_receive = service.receive(account_id=args.account_id)
    second_receive = service.receive(account_id=args.account_id)
    checks["receive"] = _check(
        first_receive, counts=_receive_counts(first_receive)
    )
    checks["incremental"] = _check(
        second_receive, counts=_receive_counts(second_receive)
    )

    checks["send"] = {"status": "NOT_RUN"}
    checks["attachment"] = {"status": "NOT_RUN"}
    checks["receive_back"] = {"status": "NOT_RUN"}
    if args.confirm_real_send:
        if not account.get("send_enabled"):
            checks["send"] = {
                "status": "FAIL",
                "error_code": "send_not_enabled",
            }
        else:
            marker = f"AMB-E2E-{uuid.uuid4().hex}"
            recipient = str(account.get("email_address") or "")
            with tempfile.TemporaryDirectory(prefix="amb-provider-e2e-") as temporary:
                attachment = Path(temporary) / "中文附件.txt"
                attachment.write_text(
                    f"AgentMailBridge Provider validation {marker}\n",
                    encoding="utf-8",
                    newline="\n",
                )
                sent = service.send_user_selected_mail(
                    from_account_id=args.account_id,
                    recipient=recipient,
                    subject=marker,
                    body_text=f"AgentMailBridge v{__version__} loopback validation.",
                    attachment_paths=[attachment],
                    links=[],
                )
            checks["send"] = _check(sent)
            checks["attachment"] = {
                "status": checks["send"]["status"],
                "count": int(getattr(sent, "attachment_count", 0) or 0),
            }
            receive_back = False
            attempts = max(1, min(int(args.poll_attempts), 20))
            interval = max(1, min(int(args.poll_interval), 30))
            if sent.ok:
                for attempt in range(attempts):
                    if attempt:
                        time.sleep(interval)
                    service.receive(account_id=args.account_id)
                    found = service.search_mail_facts(
                        marker, account_id=args.account_id, limit=20
                    )
                    if found.ok and found.details.get("messages"):
                        receive_back = True
                        break
            checks["receive_back"] = {
                "status": "PASS" if receive_back else "FAIL",
                "poll_attempts": attempts,
            }

    required = (
        "configuration",
        "login",
        "folder_discovery",
        "receive",
        "incremental",
        "send",
        "attachment",
        "receive_back",
    )
    statuses = [str(checks[name]["status"]) for name in required]
    evidence["overall"] = (
        "PASS"
        if statuses and all(status == "PASS" for status in statuses)
        else "FAIL"
        if "FAIL" in statuses
        else "PARTIAL"
    )
    _write_evidence(evidence, args.output)
    return 0 if evidence["overall"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
