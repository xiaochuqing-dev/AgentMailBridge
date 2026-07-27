"""对一个已配置 QQ/163 账号执行 2024 年真实历史导入并输出脱敏证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.database import get_history_import_run
from agent_mail_bridge.utils import sha256_of_file
from agent_mail_bridge.version import __version__


ALLOWED_PROVIDERS = {"qq", "163"}


def _write(evidence: dict[str, Any], output: Path) -> None:
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"2024 history evidence written: {target}")


def _package_count(db_path: Path, account_id: str) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM mail_packages
            WHERE account_id = ?
              AND received_at >= '2024-01-01'
              AND received_at < '2025-01-01'
            """,
            (account_id,),
        ).fetchone()
    return int(row[0] if row else 0)


def _verify_selected_mail(
    service: ApplicationService,
    account_id: str,
) -> dict[str, Any]:
    found = service.search_mail_facts(
        "",
        account_id=account_id,
        date_from="2024-01-01 00:00:00",
        date_to="2024-12-31 23:59:59",
        limit=1,
    )
    messages = list(found.details.get("messages") or []) if found.ok else []
    if not messages:
        return {
            "mail_found": False,
            "body_verified": False,
            "raw_verified": False,
            "resources_verified": False,
        }
    package_id = str(messages[0].get("package_id") or "")
    detail = service.get_mail_message(package_id)
    message = dict(detail.details.get("message") or {}) if detail.ok else {}
    package_root = Path(str(message.get("package_root") or ""))
    raw = dict(message.get("raw_eml") or {})
    raw_path = package_root / str(raw.get("path") or "")
    raw_verified = bool(
        raw.get("status") == "available"
        and raw.get("sha256")
        and raw_path.is_file()
        and sha256_of_file(raw_path) == raw.get("sha256")
    )
    body = dict(message.get("body") or {})
    body_path_text = str(
        body.get("readable_absolute_path")
        or body.get("plain_absolute_path")
        or ""
    )
    body_path = Path(body_path_text) if body_path_text else None
    body_verified = bool(body_path and body_path.is_file())
    resources = [
        item
        for item in message.get("resources") or []
        if item.get("absolute_path") and item.get("sha256")
    ]
    resources_verified = all(
        Path(str(item["absolute_path"])).is_file()
        and sha256_of_file(Path(str(item["absolute_path"])))
        == str(item["sha256"])
        for item in resources
    )
    return {
        "mail_found": bool(
            package_id and message.get("account_id") == account_id
        ),
        "mail_fingerprint": hashlib.sha256(
            package_id.encode("utf-8")
        ).hexdigest()[:12],
        "year": 2024,
        "body_verified": body_verified,
        "raw_verified": raw_verified,
        "resources_verified": resources_verified,
        "resource_count": len(message.get("resources") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("qq", "163"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-network", action="store_true")
    parser.add_argument("--scan-cap", type=int, default=10000)
    args = parser.parse_args()
    if not args.confirm_network:
        raise SystemExit(
            "Refusing real history import without --confirm-network"
        )

    cfg = load_config()
    service = ApplicationService(cfg)
    if not service.initialize().ok:
        raise SystemExit("AgentMailBridge initialization failed")
    candidates = [
        item
        for item in service.list_mail_accounts().details.get("accounts") or []
        if str(item.get("provider") or "") == args.provider
        and item.get("enabled")
        and item.get("receive_enabled")
    ]
    if len(candidates) != 1:
        raise SystemExit(
            "Provider must resolve to exactly one enabled receive account"
        )
    account_id = str(candidates[0]["account_id"])
    before_state = service.get_auto_receive_state(account_id).details
    before_count = _package_count(cfg.db_path, account_id)
    result = service.import_historical_mails(
        account_id=account_id,
        preset="custom",
        date_from="2024-01-01",
        date_to="2024-12-31",
        apply_receive_rule=True,
        page_size=100,
        scan_cap=max(100, min(int(args.scan_cap), 10000)),
    )
    after_state = service.get_auto_receive_state(account_id).details
    after_count = _package_count(cfg.db_path, account_id)
    run = get_history_import_run(cfg.db_path, result.scan_id) or {}
    selected = _verify_selected_mail(service, account_id)
    checkpoint_unchanged = (
        before_state.get("checkpoint") == after_state.get("checkpoint")
    )
    accepted_status = str(getattr(result.status, "value", result.status)) in {
        "success",
        "no_changes",
        "partial",
    }
    checks = {
        "network_history_import": accepted_status,
        "history_mail_exists": bool(selected.get("mail_found")),
        "raw_eml": bool(selected.get("raw_verified")),
        "body": bool(selected.get("body_verified")),
        "resource_hashes": bool(selected.get("resources_verified")),
        "account_ownership": bool(selected.get("mail_found")),
        "incremental_checkpoint_unchanged": checkpoint_unchanged,
        "segmented": int(run.get("total_segments") or 0) >= 4,
        "state_persisted": bool(run.get("run_id")),
    }
    evidence = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "provider": args.provider,
        "account_fingerprint": hashlib.sha256(
            account_id.encode("utf-8")
        ).hexdigest()[:12],
        "year": 2024,
        "counts": {
            "before": before_count,
            "after": after_count,
            "scanned": int(result.scanned or 0),
            "matched": int(result.matched or 0),
            "saved": int(result.saved or 0),
            "duplicates": int(result.duplicates or 0),
            "rule_skipped": int(result.rule_skipped or 0),
            "failed": int(result.failed or 0),
            "segments": int(run.get("total_segments") or 0),
        },
        "selected_mail": selected,
        "checks": {
            key: {"status": "PASS" if value else "FAIL"}
            for key, value in checks.items()
        },
    }
    evidence["overall"] = (
        "PASS" if all(checks.values()) else "FAIL"
    )
    _write(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
