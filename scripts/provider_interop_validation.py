"""对两个已配置账号执行低频真实互发，并输出脱敏链路证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.version import __version__


ALLOWED_PROVIDERS = {"qq", "163"}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(evidence: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        print(payload)
        return
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(f"Provider interop evidence written: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one real message between two configured QQ/163 accounts."
    )
    parser.add_argument("--from-account-id", required=True)
    parser.add_argument("--to-account-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-network", action="store_true")
    parser.add_argument("--confirm-real-send", action="store_true")
    parser.add_argument("--poll-attempts", type=int, default=8)
    parser.add_argument("--poll-interval", type=int, default=10)
    args = parser.parse_args()
    if not args.confirm_network:
        raise SystemExit("Refusing Provider network validation without --confirm-network")
    if not args.confirm_real_send:
        raise SystemExit("Refusing real send without --confirm-real-send")

    service = ApplicationService(load_config())
    if not service.initialize().ok:
        raise SystemExit("AgentMailBridge initialization failed")
    rows = list(service.list_mail_accounts().details.get("accounts") or [])
    by_id = {str(item.get("account_id") or ""): item for item in rows}
    sender = by_id.get(args.from_account_id)
    recipient = by_id.get(args.to_account_id)
    if sender is None or recipient is None:
        raise SystemExit("Both account IDs must already be configured")
    if (
        str(sender.get("provider") or "") not in ALLOWED_PROVIDERS
        or str(recipient.get("provider") or "") not in ALLOWED_PROVIDERS
    ):
        raise SystemExit("Interop validation is limited to configured QQ and 163 accounts")

    sender_provider = str(sender["provider"])
    recipient_provider = str(recipient["provider"])
    marker = (
        f"[AMB-v1.4.4-E2E] {sender_provider}-to-{recipient_provider}-"
        f"{uuid.uuid4().hex}"
    )
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "from_account_id": args.from_account_id,
        "to_account_id": args.to_account_id,
        "from_provider": sender_provider,
        "to_provider": recipient_provider,
        "network_confirmed": True,
        "real_send_confirmed": True,
        "checks": {},
    }
    checks = evidence["checks"]
    with tempfile.TemporaryDirectory(prefix="amb-interop-e2e-") as temporary:
        root = Path(temporary)
        sources = [
            root / "中文附件.txt",
            root / "零字节附件.bin",
            root / "第二份数据.csv",
        ]
        sources[0].write_text(
            f"AgentMailBridge 真实互发验证\n{marker}\n",
            encoding="utf-8",
            newline="\n",
        )
        sources[1].write_bytes(b"")
        sources[2].write_text(
            "name,value\n中文,163\n",
            encoding="utf-8",
            newline="\n",
        )
        expected = {
            path.name: {"size": path.stat().st_size, "sha256": _hash(path)}
            for path in sources
        }
        sent = service.send_user_selected_mail(
            from_account_id=args.from_account_id,
            recipient=str(recipient.get("email_address") or ""),
            subject=marker,
            body_text=(
                "AgentMailBridge QQ/163 真实互发验证。\n"
                "包含中文主题、中文正文、中文附件名、多附件与零字节附件。"
            ),
            attachment_paths=sources,
            links=[{"url": "https://example.com/e2e", "display_text": "验证链接"}],
        )
        checks["smtp_send"] = {
            "status": "PASS" if sent.ok else "FAIL",
            "operation_status": str(getattr(sent.status, "value", sent.status)),
            "error_code": str(sent.error_code or ""),
            "attachment_count": int(sent.attachment_count or 0),
        }
        outbound = (
            service.get_outbound_message(sent.outbound_id)
            if sent.outbound_id
            else None
        )
        outbound_message = (
            dict(outbound.details.get("message") or {})
            if outbound is not None and outbound.ok
            else {}
        )
        resources = list(outbound_message.get("resources") or [])
        chain_ok = bool(sent.ok and len(resources) == len(sources))
        for item in resources:
            name = str(item.get("display_name") or "")
            facts = expected.get(name)
            hashes = {
                str(item.get("sha256") or ""),
                str(item.get("staged_sha256") or ""),
                str(item.get("sent_archive_sha256") or ""),
            }
            chain_ok = bool(
                chain_ok
                and facts is not None
                and int(item.get("size_bytes") or 0) == facts["size"]
                and hashes == {facts["sha256"]}
                and str(item.get("status") or "") == "sent"
            )
        checks["outbound_archive_hash"] = {
            "status": "PASS" if chain_ok else "FAIL",
            "resource_count": len(resources),
        }

    received_message: dict[str, Any] | None = None
    last_receive_error = ""
    attempts = max(1, min(int(args.poll_attempts), 20))
    interval = max(1, min(int(args.poll_interval), 30))
    if sent.ok:
        for attempt in range(attempts):
            if attempt:
                time.sleep(interval)
            received = service.receive(account_id=args.to_account_id)
            if not received.ok:
                last_receive_error = str(received.error_code or "")
            found = service.search_mail_facts(
                marker, account_id=args.to_account_id, limit=20
            )
            messages = list(found.details.get("messages") or []) if found.ok else []
            if messages:
                received_message = dict(messages[0])
                break
    checks["real_delivery"] = {
        "status": "PASS" if received_message else "FAIL",
        "poll_attempts": attempts,
        "receive_error_code": last_receive_error,
    }

    package_ok = False
    attachment_ok = False
    if received_message:
        package_id = str(received_message.get("package_id") or "")
        detail = service.get_mail_message(package_id)
        message = dict(detail.details.get("message") or {}) if detail.ok else {}
        raw = dict(message.get("raw_eml") or {})
        raw_path = (
            Path(str(message.get("package_root") or ""))
            / str(raw.get("path") or "")
        )
        package_ok = bool(
            package_id
            and message.get("account_id") == args.to_account_id
            and raw.get("status") == "available"
            and raw.get("sha256")
            and raw_path.is_file()
            and _hash(raw_path) == raw.get("sha256")
        )
        received_resources = list(message.get("resources") or [])
        received_attachments = [
            item
            for item in received_resources
            if item.get("internal_type") == "attachment"
        ]
        actual = {
            str(item.get("display_name") or ""): {
                "size": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or ""),
            }
            for item in received_attachments
        }
        attachment_ok = actual == expected
    checks["mail_package_raw_ownership"] = {
        "status": "PASS" if package_ok else "FAIL"
    }
    checks["received_attachment_hash"] = {
        "status": "PASS" if attachment_ok else "FAIL",
        "attachment_count": len(expected) if attachment_ok else 0,
    }

    statuses = [str(item["status"]) for item in checks.values()]
    evidence["overall"] = (
        "PASS"
        if statuses and all(status == "PASS" for status in statuses)
        else "FAIL"
    )
    _write(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
