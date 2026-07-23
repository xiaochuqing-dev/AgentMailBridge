"""发送一封受控富 MIME 邮件，并验证目标账号的统一归档事实。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import effective_outgoing_runtime, load_config
from agent_mail_bridge.mail_send import _smtp_send_with_stage
from agent_mail_bridge.version import __version__


ALLOWED_PROVIDERS = {"qq", "163"}
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/wcAAgAB/ax3pAAAAABJRU5ErkJggg=="
)


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
    print(f"Provider MIME evidence written: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate real HTML, inline image, Unicode, and attachment receive."
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
    accounts = {
        str(item.get("account_id") or ""): item
        for item in service.list_mail_accounts().details.get("accounts") or []
    }
    sender = accounts.get(args.from_account_id)
    recipient = accounts.get(args.to_account_id)
    if sender is None or recipient is None:
        raise SystemExit("Both account IDs must already be configured")
    sender_provider = str(sender.get("provider") or "")
    recipient_provider = str(recipient.get("provider") or "")
    if sender_provider not in ALLOWED_PROVIDERS or recipient_provider not in ALLOWED_PROVIDERS:
        raise SystemExit("MIME validation is limited to configured QQ and 163 accounts")

    runtime_cfg = service._account_router.context(
        args.from_account_id, capability="send"
    ).config
    outgoing = effective_outgoing_runtime(runtime_cfg)
    marker = (
        f"[AMB-v1.4.4-E2E] MIME-{sender_provider}-to-"
        f"{recipient_provider}-{uuid.uuid4().hex}"
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
    with tempfile.TemporaryDirectory(prefix="amb-mime-e2e-") as temporary:
        root = Path(temporary)
        attachments = [
            root / "中文附件.txt",
            root / "零字节附件.bin",
            root / "第二份附件.csv",
        ]
        attachments[0].write_text(
            f"中文附件内容\n{marker}\n", encoding="utf-8", newline="\n"
        )
        attachments[1].write_bytes(b"")
        attachments[2].write_text(
            "项目,状态\nAgentMailBridge,通过\n",
            encoding="utf-8",
            newline="\n",
        )
        expected_attachments = {
            path.name: {"size": path.stat().st_size, "sha256": _hash(path)}
            for path in attachments
        }

        message = EmailMessage()
        message["From"] = outgoing.username
        message["To"] = str(recipient.get("email_address") or "")
        message["Subject"] = marker
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="agentmailbridge.local")
        message.set_content(
            "AgentMailBridge 富 MIME 真实验证：中文、HTML、inline image、"
            "多附件、中文文件名、零字节附件。"
        )
        message.add_alternative(
            '<html><body><h1>中文 HTML 正文</h1>'
            '<p>AgentMailBridge 富 MIME 验证。</p>'
            '<img src="cid:amb-inline-image"></body></html>',
            subtype="html",
        )
        message.get_payload()[-1].add_related(
            PNG_1X1,
            maintype="image",
            subtype="png",
            cid="<amb-inline-image>",
            filename="正文图片.png",
            disposition="inline",
        )
        for path in attachments:
            subtype = "csv" if path.suffix == ".csv" else "octet-stream"
            maintype = "text" if path.suffix in {".txt", ".csv"} else "application"
            message.add_attachment(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )
        try:
            _smtp_send_with_stage(runtime_cfg, message)
            checks["smtp_send"] = {"status": "PASS"}
        except Exception as exc:
            checks["smtp_send"] = {
                "status": "FAIL",
                "error_type": type(exc).__name__,
            }

    received_message: dict[str, Any] | None = None
    last_receive_error = ""
    attempts = max(1, min(int(args.poll_attempts), 20))
    interval = max(1, min(int(args.poll_interval), 30))
    if checks["smtp_send"]["status"] == "PASS":
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

    raw_ok = False
    body_ok = False
    inline_ok = False
    attachments_ok = False
    if received_message:
        detail = service.get_mail_message(str(received_message["package_id"]))
        archived = dict(detail.details.get("message") or {}) if detail.ok else {}
        raw = dict(archived.get("raw_eml") or {})
        raw_path = (
            Path(str(archived.get("package_root") or ""))
            / str(raw.get("path") or "")
        )
        raw_ok = bool(
            archived.get("account_id") == args.to_account_id
            and raw.get("status") == "available"
            and raw.get("sha256")
            and raw_path.is_file()
            and _hash(raw_path) == raw.get("sha256")
        )
        body = dict(archived.get("body") or {})
        html_path = (
            Path(str(archived.get("package_root") or ""))
            / str(body.get("html_path") or "")
        )
        body_ok = bool(body.get("html_path") and html_path.is_file())
        resources = list(archived.get("resources") or [])
        inline = [
            item
            for item in resources
            if item.get("internal_type") == "inline_image"
        ]
        inline_ok = bool(
            len(inline) == 1
            and inline[0].get("content_id") == "amb-inline-image"
            and int(inline[0].get("size_bytes") or 0) == len(PNG_1X1)
            and inline[0].get("sha256") == hashlib.sha256(PNG_1X1).hexdigest()
        )
        actual_attachments = {
            str(item.get("display_name") or ""): {
                "size": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or ""),
            }
            for item in resources
            if item.get("internal_type") == "attachment"
        }
        attachments_ok = actual_attachments == expected_attachments
    checks["raw_package_ownership"] = {"status": "PASS" if raw_ok else "FAIL"}
    checks["html_body"] = {"status": "PASS" if body_ok else "FAIL"}
    checks["inline_image"] = {"status": "PASS" if inline_ok else "FAIL"}
    checks["attachments"] = {
        "status": "PASS" if attachments_ok else "FAIL",
        "expected_count": len(expected_attachments),
    }
    evidence["overall"] = (
        "PASS"
        if all(item["status"] == "PASS" for item in checks.values())
        else "FAIL"
    )
    _write(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
