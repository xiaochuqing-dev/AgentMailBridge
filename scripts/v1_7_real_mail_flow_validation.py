"""执行 v1.7 通用 Agent 邮件收发、线程和 Sent 去重真实验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
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

from agent_mail_bridge.agent_integration import AgentAccessError
from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.database import (
    close_connection,
    get_connection,
    query_mailboxes,
    set_mailbox_sync_enabled,
)
from agent_mail_bridge.mail_threading import query_sent_mappings
from agent_mail_bridge.version import __version__


ALLOWED_PROVIDERS = {"qq", "163"}
CAPABILITIES = [
    "mail.search",
    "mail.get",
    "resource.read",
    "resource.prepare",
    "sync.status",
    "workspace.list",
    "mail.accounts.list",
    "mailboxes.list",
    "mail.send",
    "send.status",
]


def _write_evidence(evidence: dict[str, Any], output: Path) -> None:
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"v1.7 real mail-flow evidence written: {target}")


def _status(ok: bool, **facts: Any) -> dict[str, Any]:
    return {"status": "PASS" if ok else "FAIL", **facts}


def _wait_for_mail(
    service: ApplicationService,
    *,
    account_id: str,
    marker: str,
    attempts: int,
    interval: int,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        found = service.search_mail_facts(
            marker, account_id=account_id, limit=10
        )
        messages = list(found.details.get("messages") or []) if found.ok else []
        if messages:
            return dict(messages[0])
        received = service.receive(
            account_id=account_id,
            limit=100,
            unseen_only=False,
            mark_seen=False,
            wait_for_process_lock=15,
        )
        if received.error_code not in {None, "", "sync_in_progress"}:
            raise RuntimeError(str(received.error_code))
        found = service.search_mail_facts(
            marker, account_id=account_id, limit=10
        )
        messages = list(found.details.get("messages") or []) if found.ok else []
        if messages:
            return dict(messages[0])
        if attempt + 1 < attempts:
            time.sleep(interval)
    return None


def _package_raw_matches(
    service: ApplicationService, package_id: str, expected_hash: str
) -> bool:
    detail = service.get_mail_message(package_id)
    if not detail.ok:
        return False
    message = dict(detail.details.get("message") or {})
    raw = dict(message.get("raw_eml") or {})
    path = Path(str(message.get("package_root") or "")) / str(
        raw.get("path") or ""
    )
    return bool(
        message.get("direction") == "outbound"
        and raw.get("status") == "available"
        and raw.get("sha256") == expected_hash
        and path.is_file()
        and hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
        and expected_hash
    )


def _attachment_facts(
    service: ApplicationService, package_id: str
) -> dict[str, tuple[int, str]]:
    detail = service.get_mail_message(package_id)
    if not detail.ok:
        return {}
    message = dict(detail.details.get("message") or {})
    return {
        str(row.get("display_name") or ""): (
            int(row.get("size_bytes") or 0),
            str(row.get("sha256") or ""),
        )
        for row in message.get("resources") or []
        if row.get("internal_type") == "attachment"
    }


def _thread_relation_exists(
    db_path: Path,
    package_id: str,
    source_package_id: str,
    relation_type: str,
) -> bool:
    row = get_connection(db_path).execute(
        """
        SELECT 1
        FROM mail_thread_relations
        WHERE package_id = ? AND related_package_id = ?
              AND relation_type = ?
        LIMIT 1
        """,
        (package_id, source_package_id, relation_type),
    ).fetchone()
    return row is not None


def _package_message_id_count(
    db_path: Path, account_id: str, message_id: str
) -> int:
    row = get_connection(db_path).execute(
        """
        SELECT COUNT(*)
        FROM mail_packages
        WHERE account_id = ? AND message_id = ? COLLATE NOCASE
        """,
        (account_id, message_id),
    ).fetchone()
    return int(row[0] if row else 0)


def _set_send_mode(
    service: ApplicationService,
    *,
    client_id: str,
    account_ids: list[str],
    workspace_ids: list[str],
    send_account_ids: list[str],
    attachment_workspace_ids: list[str],
    send_mode: str,
) -> None:
    updated = service.set_agent_client_permissions(
        client_id,
        capabilities=CAPABILITIES,
        account_ids=account_ids,
        workspace_ids=workspace_ids,
        send_account_ids=send_account_ids,
        attachment_workspace_ids=attachment_workspace_ids,
        account_scope_mode="selected",
        mailbox_scope_mode="all",
        send_account_scope_mode="selected",
        workspace_scope_mode="selected",
        attachment_scope_mode="selected",
        send_mode=send_mode,
    )
    if not updated.ok:
        raise RuntimeError(str(updated.error_code or "permission_update_failed"))


def _send_with_known_failure_retry(
    service: ApplicationService,
    identity,
    *,
    request_id: str,
    max_attempts: int = 3,
    **kwargs: Any,
):
    result = None
    for attempt in range(max(1, max_attempts)):
        result = service.send_agent_mail(
            identity,
            request_id=(
                request_id if attempt == 0 else f"{request_id}-retry-{attempt}"
            ),
            **kwargs,
        )
        status = str(
            (result.details.get("send_request") or {}).get("status") or ""
        )
        if status == "sent" or status == "delivery_unknown":
            break
        if status != "failed":
            break
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-account-id", required=True)
    parser.add_argument("--to-account-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--poll-attempts", type=int, default=10)
    parser.add_argument("--poll-interval", type=int, default=8)
    parser.add_argument("--confirm-network", action="store_true")
    parser.add_argument("--confirm-real-send", action="store_true")
    args = parser.parse_args()
    if not args.confirm_network or not args.confirm_real_send:
        raise SystemExit("Real network and send confirmation are both required")

    cfg = load_config()
    service = ApplicationService(cfg)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "product_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "providers": [],
        "checks": {},
    }
    checks = evidence["checks"]
    original_read_gate = bool(cfg.mcp_mail_read_enabled)
    original_mailbox_sync: dict[str, bool] = {}
    token = ""
    client_id = ""
    try:
        if not service.initialize().ok:
            raise RuntimeError("initialization_failed")
        if not original_read_gate:
            enabled = service.set_mcp_mail_read_access(True)
            if not enabled.ok:
                raise RuntimeError("read_gate_enable_failed")

        accounts = {
            str(row["account_id"]): row
            for row in service.list_mail_accounts().details.get("accounts", [])
        }
        sender = accounts.get(args.from_account_id)
        recipient = accounts.get(args.to_account_id)
        if sender is None or recipient is None:
            raise RuntimeError("account_not_found")
        providers = [
            str(sender.get("provider") or ""),
            str(recipient.get("provider") or ""),
        ]
        if set(providers) != ALLOWED_PROVIDERS:
            raise RuntimeError("provider_scope_invalid")
        evidence["providers"] = sorted(providers)
        for account_id in (args.from_account_id, args.to_account_id):
            discovered = service.discover_mail_account_mailboxes(account_id)
            if not discovered.ok:
                raise RuntimeError(
                    str(discovered.error_code or "folder_discovery_failed")
                )
        mailbox_rows = query_mailboxes(cfg.db_path, enabled_only=True)
        mailbox_facts: dict[str, set[str]] = {
            args.from_account_id: set(),
            args.to_account_id: set(),
        }
        for row in mailbox_rows:
            account_id = str(row.get("account_id") or "")
            if account_id in mailbox_facts:
                mailbox_facts[account_id].add(
                    str(row.get("mailbox_role") or "other")
                )
        checks["real_mailbox_discovery"] = _status(
            all(
                {"inbox", "sent", "other"}.issubset(roles)
                for roles in mailbox_facts.values()
            ),
            account_count=len(mailbox_facts),
            inbox=True,
            sent=True,
            custom=True,
        )
        for row in mailbox_rows:
            if (
                str(row.get("account_id") or "")
                in {args.from_account_id, args.to_account_id}
                and str(row.get("mailbox_role") or "")
                in {"inbox", "sent"}
                and not str(row.get("external_ref") or "").startswith(
                    "local:sent:"
                )
            ):
                mailbox_id = str(row["mailbox_id"])
                original_mailbox_sync[mailbox_id] = bool(
                    row.get("sync_enabled")
                )
                set_mailbox_sync_enabled(cfg.db_path, mailbox_id, True)

        workspace = service.list_agent_workspaces().details[
            "workspace_details"
        ][0]
        workspace_id = str(workspace["workspace_id"])
        workspace_root = Path(str(workspace["display_path"])).resolve()
        created = service.create_agent_client(
            client_type="custom",
            display_name=f"v1.7 real mail flow {uuid.uuid4().hex[:8]}",
            config_mode="manual",
            capabilities=CAPABILITIES,
            account_ids=[args.from_account_id, args.to_account_id],
            workspace_ids=[workspace_id],
            send_account_ids=[args.from_account_id, args.to_account_id],
            attachment_workspace_ids=[workspace_id],
            account_scope_mode="selected",
            mailbox_scope_mode="all",
            send_account_scope_mode="selected",
            workspace_scope_mode="selected",
            attachment_scope_mode="selected",
            send_mode="confirm",
        )
        if not created.ok:
            raise RuntimeError(
                str(created.error_code or "client_create_failed")
            )
        client_id = str(created.details["client"]["client_id"])
        token = str(created.details["scoped_token"])
        if not service.set_agent_client_state(
            client_id, "active", enabled=True
        ).ok:
            raise RuntimeError("client_activation_failed")
        identity = service.resolve_agent_identity(client_id, token)

        attempts = max(1, min(int(args.poll_attempts), 20))
        interval = max(1, min(int(args.poll_interval), 30))
        suffix = uuid.uuid4().hex
        markers = {
            "confirm": f"[AMB-v{__version__}-CONFIRM-{suffix}]",
            "reply": f"[AMB-v{__version__}-REPLY-{suffix}]",
            "reply_all": f"[AMB-v{__version__}-REPLY-ALL-{suffix}]",
            "forward": f"[AMB-v{__version__}-FORWARD-{suffix}]",
            "autonomous": f"[AMB-v{__version__}-AUTO-{suffix}]",
        }
        sent_packages: list[tuple[str, str, str]] = []
        with tempfile.TemporaryDirectory(
            prefix="amb-v17-real-", dir=workspace_root
        ) as raw_temp:
            attachment_root = Path(raw_temp)
            attachments = [
                attachment_root / "Agent附件一.txt",
                attachment_root / "Agent附件二.csv",
            ]
            attachments[0].write_text(
                "AgentMailBridge real flow attachment one\n",
                encoding="utf-8",
                newline="\n",
            )
            attachments[1].write_text(
                "name,value\nreal,1\n",
                encoding="utf-8",
                newline="\n",
            )
            expected_attachments = {
                path.name: (
                    path.stat().st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in attachments
            }
            public_cc = str(cfg.owner_gmail or "").strip()
            sender_address = str(sender.get("email_address") or "")
            recipient_address = str(recipient.get("email_address") or "")
            cc = (
                [public_cc]
                if public_cc.casefold()
                not in {sender_address.casefold(), recipient_address.casefold()}
                else []
            )
            pending = service.send_agent_mail(
                identity,
                request_id=f"confirm-{suffix}",
                operation="new",
                sender_account_id=args.from_account_id,
                to=[recipient_address],
                cc=cc,
                bcc=[sender_address],
                subject=markers["confirm"],
                body_text="AgentMailBridge v1.7 confirm real flow.",
                body_html="<p>AgentMailBridge v1.7 confirm real flow.</p>",
                attachments=[{"path": str(path)} for path in attachments],
            )
            pending_row = dict(pending.details.get("send_request") or {})
            before_receive = service.receive(
                account_id=args.to_account_id,
                limit=100,
                unseen_only=False,
                mark_seen=False,
                wait_for_process_lock=15,
            )
            before_search = service.search_mail_facts(
                markers["confirm"],
                account_id=args.to_account_id,
                limit=1,
            )
            checks["confirm_before_send"] = _status(
                pending.ok
                and pending_row.get("status") == "pending_confirmation"
                and int(pending_row.get("smtp_attempt_count") or 0) == 0
                and not pending_row.get("package_id")
                and before_receive.error_code
                in {None, "", "sync_in_progress"}
                and not before_search.details.get("messages"),
                smtp_attempts=int(
                    pending_row.get("smtp_attempt_count") or 0
                ),
                delivery_observed=False,
            )
            send_request_id = str(pending_row["send_request_id"])
            confirmed = service.confirm_agent_send_request(send_request_id)
            confirmed_again = service.confirm_agent_send_request(
                send_request_id
            )
            confirmed_row = dict(
                confirmed_again.details.get("send_request") or {}
            )
            checks["confirm_send_once"] = _status(
                confirmed.ok
                and confirmed_again.ok
                and confirmed_row.get("status") == "sent"
                and int(confirmed_row.get("smtp_attempt_count") or 0) == 1,
                smtp_attempts=int(
                    confirmed_row.get("smtp_attempt_count") or 0
                ),
            )
            confirm_package_id = str(confirmed_row.get("package_id") or "")
            checks["confirm_outbound_raw"] = _status(
                _package_raw_matches(
                    service,
                    confirm_package_id,
                    str(confirmed_row.get("raw_eml_sha256") or ""),
                )
            )
            sent_packages.append(
                (
                    args.from_account_id,
                    confirm_package_id,
                    str(confirmed_row.get("message_id") or ""),
                )
            )
            source = _wait_for_mail(
                service,
                account_id=args.to_account_id,
                marker=markers["confirm"],
                attempts=attempts,
                interval=interval,
            )
            if source is None:
                raise RuntimeError("confirm_delivery_not_observed")
            source_package_id = str(source["package_id"])
            checks["confirm_real_delivery"] = _status(
                _attachment_facts(service, source_package_id)
                == expected_attachments,
                attachment_count=len(expected_attachments),
            )

            _set_send_mode(
                service,
                client_id=client_id,
                account_ids=[args.from_account_id, args.to_account_id],
                workspace_ids=[workspace_id],
                send_account_ids=[
                    args.from_account_id,
                    args.to_account_id,
                ],
                attachment_workspace_ids=[workspace_id],
                send_mode="autonomous",
            )
            identity = service.resolve_agent_identity(client_id, token)

            operations = [
                (
                    "reply",
                    args.to_account_id,
                    args.from_account_id,
                    [],
                ),
                (
                    "reply_all",
                    args.to_account_id,
                    args.from_account_id,
                    [],
                ),
            ]
            for operation, from_id, receive_id, attachments_arg in operations:
                result = _send_with_known_failure_retry(
                    service,
                    identity,
                    request_id=f"{operation}-{suffix}",
                    operation=operation,
                    sender_account_id=from_id,
                    source_package_id=source_package_id,
                    subject=markers[operation],
                    body_text=f"AgentMailBridge v1.7 {operation} real flow.",
                    attachments=attachments_arg,
                )
                row = dict(result.details.get("send_request") or {})
                package_id = str(row.get("package_id") or "")
                sent_packages.append(
                    (
                        from_id,
                        package_id,
                        str(row.get("message_id") or ""),
                    )
                )
                delivered = _wait_for_mail(
                    service,
                    account_id=receive_id,
                    marker=markers[operation],
                    attempts=attempts,
                    interval=interval,
                )
                checks[f"{operation}_real"] = _status(
                    result.ok
                    and row.get("status") == "sent"
                    and delivered is not None
                    and _thread_relation_exists(
                        cfg.db_path,
                        package_id,
                        source_package_id,
                        "reply",
                    )
                )

            source_attachments = _attachment_facts(
                service, source_package_id
            )
            source_detail = service.get_mail_message(
                source_package_id
            ).details["message"]
            source_resource = next(
                row
                for row in source_detail.get("resources") or []
                if row.get("internal_type") == "attachment"
            )
            forwarded = _send_with_known_failure_retry(
                service,
                identity,
                request_id=f"forward-{suffix}",
                operation="forward",
                sender_account_id=args.to_account_id,
                source_package_id=source_package_id,
                to=[sender_address],
                subject=markers["forward"],
                body_text="AgentMailBridge v1.7 forward real flow.",
                attachments=[
                    {
                        "source_package_id": source_package_id,
                        "resource_id": source_resource["resource_id"],
                    }
                ],
            )
            forward_row = dict(
                forwarded.details.get("send_request") or {}
            )
            forward_package_id = str(
                forward_row.get("package_id") or ""
            )
            sent_packages.append(
                (
                    args.to_account_id,
                    forward_package_id,
                    str(forward_row.get("message_id") or ""),
                )
            )
            forward_delivered = _wait_for_mail(
                service,
                account_id=args.from_account_id,
                marker=markers["forward"],
                attempts=attempts,
                interval=interval,
            )
            forwarded_attachments = (
                _attachment_facts(
                    service, str(forward_delivered["package_id"])
                )
                if forward_delivered
                else {}
            )
            checks["forward_real"] = _status(
                forwarded.ok
                and forward_row.get("status") == "sent"
                and forward_delivered is not None
                and len(forwarded_attachments) == 1
                and set(forwarded_attachments.values()).issubset(
                    set(source_attachments.values())
                )
                and _thread_relation_exists(
                    cfg.db_path,
                    forward_package_id,
                    source_package_id,
                    "forward",
                )
            )

            autonomous = service.send_agent_mail(
                identity,
                request_id=f"autonomous-{suffix}",
                operation="new",
                sender_account_id=args.from_account_id,
                to=[recipient_address],
                subject=markers["autonomous"],
                body_text="AgentMailBridge v1.7 autonomous real flow.",
            )
            autonomous_retry = service.send_agent_mail(
                identity,
                request_id=f"autonomous-{suffix}",
                operation="new",
                sender_account_id=args.from_account_id,
                to=[recipient_address],
                subject="must not be sent",
                body_text="idempotency retry",
            )
            auto_row = dict(
                autonomous_retry.details.get("send_request") or {}
            )
            auto_package_id = str(auto_row.get("package_id") or "")
            sent_packages.append(
                (
                    args.from_account_id,
                    auto_package_id,
                    str(auto_row.get("message_id") or ""),
                )
            )
            auto_delivered = _wait_for_mail(
                service,
                account_id=args.to_account_id,
                marker=markers["autonomous"],
                attempts=attempts,
                interval=interval,
            )
            checks["autonomous_idempotent_real"] = _status(
                autonomous.ok
                and autonomous_retry.status.value == "duplicate"
                and auto_row.get("status") == "sent"
                and int(auto_row.get("smtp_attempt_count") or 0) == 1
                and auto_delivered is not None,
                smtp_attempts=int(auto_row.get("smtp_attempt_count") or 0),
            )

        for account_id in (args.from_account_id, args.to_account_id):
            service.discover_mail_account_mailboxes(account_id)
        mapped: set[str] = set()
        for attempt in range(attempts):
            for account_id in (args.from_account_id, args.to_account_id):
                service.receive(
                    account_id=account_id,
                    limit=100,
                    unseen_only=False,
                    mark_seen=False,
                    wait_for_process_lock=15,
                )
            mapped = {
                package_id
                for _account_id, package_id, _message_id in sent_packages
                if package_id
                if query_sent_mappings(
                    cfg.db_path, package_id=package_id
                )
            }
            if len(mapped) == len(sent_packages):
                break
            if attempt + 1 < attempts:
                time.sleep(interval)
        successful_sent_packages = [
            row for row in sent_packages if row[1] and row[2]
        ]
        duplicate_free = bool(successful_sent_packages) and all(
            _package_message_id_count(cfg.db_path, account_id, message_id) == 1
            for account_id, _package_id, message_id in sent_packages
            if _package_id and message_id
        )
        checks["sent_mapping_and_dedup"] = _status(
            len(mapped) == len(successful_sent_packages) and duplicate_free,
            outbound_count=len(successful_sent_packages),
            mapped_count=len(mapped),
            duplicate_prevented=duplicate_free,
        )

        paused = service.set_agent_client_state(
            client_id, "paused", enabled=False
        )
        paused_code = ""
        try:
            service.resolve_agent_identity(client_id, token)
        except AgentAccessError as exc:
            paused_code = exc.code
        resumed = service.set_agent_client_state(
            client_id, "active", enabled=True
        )
        resumed_ok = bool(
            resumed.ok
            and service.resolve_agent_identity(client_id, token).client_id
            == client_id
        )
        revoked = service.set_agent_client_state(
            client_id, "revoked", enabled=False
        )
        revoked_code = ""
        try:
            service.resolve_agent_identity(client_id, token)
        except AgentAccessError as exc:
            revoked_code = exc.code
        checks["pause_resume_revoke"] = _status(
            paused.ok
            and paused_code == "client_disabled"
            and resumed_ok
            and revoked.ok
            and revoked_code == "client_revoked"
        )
        checks["secret_not_in_evidence"] = _status(
            token
            not in json.dumps(
                evidence, ensure_ascii=False, sort_keys=True
            )
        )
    except (
        AgentAccessError,
        IndexError,
        KeyError,
        OSError,
        RuntimeError,
        StopIteration,
        ValueError,
    ) as exc:
        evidence["failure_type"] = type(exc).__name__
        evidence["failure_code"] = str(
            getattr(exc, "code", "") or getattr(exc, "args", [""])[0]
        )[:120]
    finally:
        for mailbox_id, enabled in original_mailbox_sync.items():
            try:
                set_mailbox_sync_enabled(
                    cfg.db_path, mailbox_id, enabled
                )
            except Exception:
                pass
        if client_id:
            try:
                service.set_agent_client_state(
                    client_id, "revoked", enabled=False
                )
            except Exception:
                pass
        if not original_read_gate:
            restored = service.set_mcp_mail_read_access(False)
            checks["read_gate_restored"] = _status(restored.ok)
        close_connection()
        logging.shutdown()

    evidence["overall"] = (
        "PASS"
        if checks
        and all(
            isinstance(row, dict) and row.get("status") == "PASS"
            for row in checks.values()
        )
        and not evidence.get("failure_type")
        else "FAIL"
    )
    _write_evidence(evidence, args.output)
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
