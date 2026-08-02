from __future__ import annotations

import hashlib
from email.message import EmailMessage

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.consistency_scan import scan_mail_consistency
from agent_mail_bridge.database import get_connection, upsert_mailboxes
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.mail_threading import record_sent_mapping
from agent_mail_bridge.send_reconciliation import find_sent_candidate
from agent_mail_bridge.send_recovery import reconcile_send_request_locally
from agent_mail_bridge.send_requests import create_send_request, get_send_request


RECONCILIATION_WINDOW_SECONDS = 7 * 24 * 60 * 60


def _account_context(tmp_cfg, *, provider: str = "qq"):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    assert service.synchronize_mail_accounts().ok
    account = next(
        row
        for row in service.list_mail_accounts().details["accounts"]
        if row["provider"] == provider
    )
    account_id = str(account["account_id"])
    mailboxes = upsert_mailboxes(
        tmp_cfg.db_path,
        account_id,
        [
            {
                "external_ref": "INBOX",
                "raw_name": "INBOX",
                "display_name": "Inbox",
                "mailbox_role": "inbox",
                "role_source": "special_use",
            },
            {
                "external_ref": "Sent",
                "raw_name": "Sent",
                "display_name": "Sent",
                "mailbox_role": "sent",
                "role_source": "special_use",
            },
        ],
    )
    return service, account, {str(row["external_ref"]): row for row in mailboxes}


def _fingerprint(
    *,
    subject: str,
    body: str,
    to_emails: tuple[str, ...],
    cc_emails: tuple[str, ...] = (),
    attachments: tuple[tuple[str, bytes], ...] = (),
) -> str:
    attachment_parts = sorted(
        f"{name.casefold()}:{len(content)}:{hashlib.sha256(content).hexdigest()}"
        for name, content in attachments
    )
    material = "\n".join(
        (
            subject.strip(),
            body.replace("\r\n", "\n").strip(),
            ",".join(sorted({*(item.casefold() for item in to_emails), *(item.casefold() for item in cc_emails)})),
            "|".join(attachment_parts),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _insert_package(
    tmp_cfg,
    *,
    package_id: str,
    account_id: str,
    mailbox_id: str,
    message_id: str,
    raw_sha256: str,
    subject: str = "audit subject",
    from_email: str = "sender@example.com",
    to_emails: tuple[str, ...] = ("receiver@example.com",),
    cc_emails: tuple[str, ...] = (),
    content_fingerprint: str = "",
    outbound_id: str = "",
    sent_at: str = "2026-08-01 12:00:00",
) -> None:
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        """
        INSERT INTO mail_packages
            (package_id, account_ref, account_id, mailbox_ref, mailbox_id,
             backend, message_id, direction, content_fingerprint, subject,
             from_email, to_emails, cc_emails, outbound_id, local_outbound,
             sent_at, package_root, raw_eml_sha256, raw_eml_status,
             archive_status, parse_status, created_at, updated_at)
        VALUES (?, ?, ?, 'Sent', ?, 'smtp', ?, 'outbound', ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, 'available', 'ready', 'ready', ?, ?)
        """,
        (
            package_id,
            f"qq:{from_email.casefold()}",
            account_id,
            mailbox_id,
            message_id,
            content_fingerprint or None,
            subject,
            from_email,
            ", ".join(to_emails),
            ", ".join(cc_emails),
            outbound_id or None,
            int(bool(outbound_id)),
            sent_at,
            str(tmp_cfg.data_root_path / package_id),
            raw_sha256,
            sent_at,
            sent_at,
        ),
    )
    connection.commit()


def _find(
    tmp_cfg,
    *,
    account_id: str,
    message_id: str = "",
    raw_sha256: str = "",
    provider_message_id: str = "",
    outbound_id: str = "",
    content_fingerprint: str = "",
    from_header: str = "sender@example.com",
    to_header: str = "receiver@example.com",
    cc_header: str = "",
    subject: str = "audit subject",
    observed_at: str = "2026-08-01 12:30:00",
):
    return find_sent_candidate(
        tmp_cfg.db_path,
        account_id=account_id,
        provider_message_id=provider_message_id,
        message_id=message_id,
        outbound_id=outbound_id,
        raw_sha256=raw_sha256,
        content_fingerprint=content_fingerprint,
        from_header=from_header,
        to_header=to_header,
        cc_header=cc_header,
        subject=subject,
        observed_at=observed_at,
    )


def _create_delivery_unknown_request(
    tmp_cfg,
    *,
    suffix: str,
    message_id: str,
    subject: str = "audit subject",
    body: str = "current body",
    to_email: str = "receiver@example.com",
):
    service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    created = service.create_agent_client(
        client_type="codex",
        display_name=f"v1.7.2 red team {suffix}",
        capabilities=["mail.send", "send.status"],
        account_ids=[account_id],
        mailbox_ids=[],
        send_account_ids=[account_id],
        attachment_workspace_ids=[],
        send_mode="autonomous",
    )
    assert created.ok
    client_id = str(created.details["client"]["client_id"])
    request, was_created = create_send_request(
        tmp_cfg.db_path,
        send_request_id=f"send-v172-{suffix}",
        client_id=client_id,
        idempotency_key=f"idem-v172-{suffix}",
        operation="new",
        sender_account_id=account_id,
        source_package_id=None,
        reply_to_package_id=None,
        forward_from_package_id=None,
        send_mode="autonomous",
        subject=subject,
        body_text=body,
        body_html="",
        status="ready_to_send",
        expires_at=None,
        message_id=message_id,
        recipients=[
            {"recipient_type": "to", "email_address": to_email},
        ],
        attachments=[],
    )
    assert was_created
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        """
        UPDATE send_requests
        SET status='delivery_unknown', delivery_status='delivery_unknown',
            current_stage='smtp_data_started', recovery_required=1
        WHERE send_request_id=?
        """,
        (request["send_request_id"],),
    )
    connection.commit()
    return service, account, mailboxes, get_send_request(
        tmp_cfg.db_path, str(request["send_request_id"])
    )


def test_single_reused_message_id_with_different_raw_is_unmatched(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message_id = "<reused-single-v172@example.com>"
    old_fingerprint = _fingerprint(
        subject="audit subject",
        body="old body",
        to_emails=("receiver@example.com",),
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-reused-old",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=message_id,
        raw_sha256="1" * 64,
        content_fingerprint=old_fingerprint,
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        raw_sha256="2" * 64,
        content_fingerprint=_fingerprint(
            subject="audit subject",
            body="new body",
            to_emails=("receiver@example.com",),
        ),
    )

    assert result["status"] == "unmatched"
    assert result["evidence_type"] == "weak_message_id_only"
    assert result["candidate_count"] == 1
    assert result["package_id"] is None


def test_same_message_id_with_different_recipient_is_unmatched(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message_id = "<recipient-conflict-v172@example.com>"
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-recipient-old",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=message_id,
        raw_sha256="3" * 64,
        to_emails=("old-recipient@example.com",),
        content_fingerprint=_fingerprint(
            subject="audit subject",
            body="same body",
            to_emails=("old-recipient@example.com",),
        ),
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        raw_sha256="4" * 64,
        to_header="new-recipient@example.com",
        content_fingerprint=_fingerprint(
            subject="audit subject",
            body="same body",
            to_emails=("new-recipient@example.com",),
        ),
    )

    assert result["status"] == "unmatched"
    assert result["package_id"] is None


def test_same_message_id_and_subject_with_different_attachment_is_unmatched(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message_id = "<attachment-conflict-v172@example.com>"
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-attachment-old",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=message_id,
        raw_sha256="5" * 64,
        content_fingerprint=_fingerprint(
            subject="audit subject",
            body="same body",
            to_emails=("receiver@example.com",),
            attachments=(("audit.txt", b"old attachment"),),
        ),
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        raw_sha256="6" * 64,
        content_fingerprint=_fingerprint(
            subject="audit subject",
            body="same body",
            to_emails=("receiver@example.com",),
            attachments=(("audit.txt", b"different attachment"),),
        ),
    )

    assert result["status"] == "unmatched"
    assert result["package_id"] is None


def test_exact_raw_hash_wins_over_one_conflicting_weak_message_id(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    observed_message_id = "<raw-conflict-observed-v172@example.com>"
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-raw-a",
        account_id=account_id,
        mailbox_id=mailbox_id,
        message_id="<raw-physical-a-v172@example.com>",
        raw_sha256="7" * 64,
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-message-b",
        account_id=account_id,
        mailbox_id=mailbox_id,
        message_id=observed_message_id,
        raw_sha256="8" * 64,
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=observed_message_id,
        raw_sha256="7" * 64,
    )

    assert result["status"] == "matched"
    assert result["package_id"] == "pkg-v172-raw-a"
    assert result["evidence_type"] == "exact_raw_hash"
    assert result["decision_reason"] == "strong_evidence_overrode_message_id"


def test_provider_mapping_and_raw_hash_conflict_is_ambiguous(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-provider-a",
        account_id=account_id,
        mailbox_id=mailbox_id,
        message_id="<provider-a-v172@example.com>",
        raw_sha256="9" * 64,
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-provider-b",
        account_id=account_id,
        mailbox_id=mailbox_id,
        message_id="<provider-b-v172@example.com>",
        raw_sha256="a" * 64,
    )
    record_sent_mapping(
        tmp_cfg.db_path,
        account_id=account_id,
        package_id="pkg-v172-provider-a",
        mailbox_id=mailbox_id,
        provider_message_id="provider-v172-conflict",
        uidvalidity=172,
        provider_uid="501",
        message_id="<provider-a-v172@example.com>",
        matched_by="exact_provider_id",
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        provider_message_id="provider-v172-conflict",
        raw_sha256="a" * 64,
    )

    assert result == {
        "status": "ambiguous",
        "evidence_type": "conflicting_strong_evidence",
        "confidence": "manual_review",
        "candidate_count": 2,
        "package_id": None,
    }


def test_unique_message_id_needs_full_composite_and_bounded_time(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message_id = "<composite-v172@example.com>"
    fingerprint = _fingerprint(
        subject="audit subject",
        body="current body",
        to_emails=("receiver@example.com",),
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-composite",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=message_id,
        raw_sha256="b" * 64,
        content_fingerprint=fingerprint,
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        raw_sha256="c" * 64,
        content_fingerprint=fingerprint,
    )

    assert result == {
        "status": "matched",
        "evidence_type": "deterministic_message_composite",
        "confidence": "high",
        "candidate_count": 1,
        "package_id": "pkg-v172-composite",
    }


def test_delivery_unknown_message_id_only_does_not_advance_or_send(tmp_cfg):
    _service, account, mailboxes, request = _create_delivery_unknown_request(
        tmp_cfg,
        suffix="weak-local",
        message_id="<weak-local-v172@example.com>",
    )
    account_id = str(account["account_id"])
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-unrelated-local",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=str(request["message_id"]),
        raw_sha256="d" * 64,
        subject=str(request["subject"]),
        from_email=str(account["email_address"]),
        to_emails=("other-recipient@example.com",),
        content_fingerprint=_fingerprint(
            subject=str(request["subject"]),
            body="unrelated body",
            to_emails=("other-recipient@example.com",),
        ),
    )
    before_attempts = int(request["smtp_attempt_count"])

    result = reconcile_send_request_locally(
        tmp_cfg.db_path, str(request["send_request_id"])
    )
    persisted = get_send_request(tmp_cfg.db_path, str(request["send_request_id"]))

    assert result["changed"] is False
    assert persisted["status"] == "delivery_unknown"
    assert persisted["package_id"] in {None, ""}
    assert int(persisted["smtp_attempt_count"]) == before_attempts


def test_local_recovery_accepts_exact_raw_hash_without_smtp_retry(tmp_cfg):
    _service, account, mailboxes, request = _create_delivery_unknown_request(
        tmp_cfg,
        suffix="raw-local",
        message_id="<raw-local-request-v172@example.com>",
    )
    request_id = str(request["send_request_id"])
    raw_hash = "e" * 64
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET raw_eml_sha256=? WHERE send_request_id=?",
        (raw_hash, request_id),
    )
    connection.commit()
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-local-raw",
        account_id=str(account["account_id"]),
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id="<different-local-package-v172@example.com>",
        raw_sha256=raw_hash,
    )

    result = reconcile_send_request_locally(tmp_cfg.db_path, request_id)
    persisted = get_send_request(tmp_cfg.db_path, request_id)

    assert result["changed"] is True
    assert persisted["status"] == "sent"
    assert persisted["package_id"] == "pkg-v172-local-raw"
    assert int(persisted["smtp_attempt_count"]) == 0


def test_local_recovery_accepts_complete_composite_inside_window(tmp_cfg):
    _service, account, mailboxes, request = _create_delivery_unknown_request(
        tmp_cfg,
        suffix="composite-local",
        message_id="<composite-local-v172@example.com>",
    )
    request_id = str(request["send_request_id"])
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-local-composite",
        account_id=str(account["account_id"]),
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=str(request["message_id"]),
        raw_sha256="3" * 64,
        subject=str(request["subject"]),
        from_email=str(account["email_address"]),
        content_fingerprint=_fingerprint(
            subject=str(request["subject"]),
            body=str(request["body_text"]),
            to_emails=("receiver@example.com",),
        ),
        sent_at=str(request["created_at"]),
    )

    result = reconcile_send_request_locally(tmp_cfg.db_path, request_id)
    persisted = get_send_request(tmp_cfg.db_path, request_id)

    assert result["changed"] is True
    assert result["evidence_type"] == "deterministic_message_composite"
    assert persisted["status"] == "sent"
    assert persisted["package_id"] == "pkg-v172-local-composite"
    assert int(persisted["smtp_attempt_count"]) == 0


def test_delivery_unknown_sent_composite_reconciles_without_smtp_retry(tmp_cfg):
    service, account, _mailboxes, request = _create_delivery_unknown_request(
        tmp_cfg,
        suffix="sent-composite",
        message_id="<sent-composite-v172@example.com>",
    )
    request_id = str(request["send_request_id"])
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE send_requests SET smtp_attempt_count=1 WHERE send_request_id=?",
        (request_id,),
    )
    connection.commit()
    message = EmailMessage()
    message["From"] = str(account["email_address"])
    message["To"] = "receiver@example.com"
    message["Subject"] = str(request["subject"])
    message["Message-ID"] = str(request["message_id"])
    message.set_content(str(request["body_text"]))
    runtime_cfg = service._account_router.context(
        str(account["account_id"]), capability="receive"
    ).config
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="provider-sent-composite-v172",
        thread_id="",
        uid="1721",
        uidvalidity=172,
        received_at=str(request["created_at"]),
        saved_date=str(request["created_at"])[:10],
        max_attachment_bytes=runtime_cfg.max_attachment_bytes,
        mailbox_ref="Sent",
        direction="outbound",
    )

    result = process_normalized_mail(runtime_cfg, normalized, apply_receive_rule=False)
    persisted = get_send_request(tmp_cfg.db_path, request_id)

    assert result["status"] == "saved"
    assert persisted["status"] == "sent_reconciled"
    assert persisted["package_id"] == result["package_id"]
    assert int(persisted["smtp_attempt_count"]) == 1


def test_multiple_message_id_candidates_remain_ambiguous(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    message_id = "<multiple-v172@example.com>"
    for suffix in ("a", "b"):
        _insert_package(
            tmp_cfg,
            package_id=f"pkg-v172-multiple-{suffix}",
            account_id=account_id,
            mailbox_id=mailbox_id,
            message_id=message_id,
            raw_sha256=suffix * 64,
        )

    result = _find(tmp_cfg, account_id=account_id, message_id=message_id)

    assert result["status"] == "ambiguous"
    assert result["evidence_type"] == "ambiguous_message_id_candidates"
    assert result["candidate_count"] == 2
    assert result["package_id"] is None


def test_ambiguous_record_survives_consistency_scan_without_merging(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    message_id = "<repair-ambiguous-v172@example.com>"
    for suffix in ("a", "b"):
        _insert_package(
            tmp_cfg,
            package_id=f"pkg-v172-repair-{suffix}",
            account_id=account_id,
            mailbox_id=mailbox_id,
            message_id=message_id,
            raw_sha256=("4" if suffix == "a" else "5") * 64,
        )
    result = _find(tmp_cfg, account_id=account_id, message_id=message_id)
    connection = get_connection(tmp_cfg.db_path)
    before = connection.execute(
        "SELECT COUNT(*) FROM mail_packages WHERE account_id=? AND message_id=?",
        (account_id, message_id),
    ).fetchone()[0]

    scan = scan_mail_consistency(tmp_cfg)
    after = connection.execute(
        "SELECT COUNT(*) FROM mail_packages WHERE account_id=? AND message_id=?",
        (account_id, message_id),
    ).fetchone()[0]
    unresolved = connection.execute(
        "SELECT status, candidate_count FROM reconciliation_records "
        "WHERE account_id=? AND evidence_type='ambiguous_message_id_candidates'",
        (account_id,),
    ).fetchone()

    assert result["status"] == "ambiguous"
    assert before == after == 2
    assert tuple(unresolved) == ("ambiguous", 2)
    assert scan["summary"]["reconciliation_unresolved"] >= 1


def test_content_fingerprint_without_message_id_is_not_physical_identity(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    fingerprint = _fingerprint(
        subject="identical repeated send",
        body="identical body",
        to_emails=("receiver@example.com",),
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-content-only",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id="<content-only-old-v172@example.com>",
        raw_sha256="6" * 64,
        subject="identical repeated send",
        content_fingerprint=fingerprint,
    )

    result = _find(
        tmp_cfg,
        account_id=account_id,
        message_id="<content-only-new-v172@example.com>",
        raw_sha256="7" * 64,
        subject="identical repeated send",
        content_fingerprint=fingerprint,
    )

    assert result["status"] == "unmatched"
    assert result["candidate_count"] == 0
    assert result["package_id"] is None


def test_external_outbound_reused_message_id_stays_a_separate_fact(tmp_cfg):
    service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message_id = "<external-reused-v172@example.com>"
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-existing-local",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=message_id,
        raw_sha256="f" * 64,
        subject="external reused",
        from_email=str(account["email_address"]),
        content_fingerprint=_fingerprint(
            subject="external reused",
            body="old local body",
            to_emails=("receiver@example.com",),
        ),
    )
    message = EmailMessage()
    message["From"] = str(account["email_address"])
    message["To"] = "receiver@example.com"
    message["Subject"] = "external reused"
    message["Message-ID"] = message_id
    message.set_content("new external body")
    runtime_cfg = service._account_router.context(
        account_id, capability="receive"
    ).config
    normalized = normalized_mail_from_raw(
        message.as_bytes(),
        backend="imap",
        backend_message_id="provider-external-v172",
        thread_id="",
        uid="1720",
        uidvalidity=172,
        received_at="2026-08-01 12:30:00",
        saved_date="2026-08-01",
        max_attachment_bytes=runtime_cfg.max_attachment_bytes,
        mailbox_ref="Sent",
        direction="outbound",
    )

    first = process_normalized_mail(runtime_cfg, normalized, apply_receive_rule=False)
    second = process_normalized_mail(runtime_cfg, normalized, apply_receive_rule=False)
    connection = get_connection(tmp_cfg.db_path)

    assert first["status"] == "saved"
    assert first["package_id"] != "pkg-v172-existing-local"
    assert second["status"] == "duplicate"
    assert second["package_id"] == first["package_id"]
    assert connection.execute(
        "SELECT COUNT(*) FROM mail_packages WHERE account_id=? AND message_id=?",
        (account_id, message_id),
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM reconciliation_records "
        "WHERE package_id=? AND status='external_fact_created'",
        (first["package_id"],),
    ).fetchone()[0] == 1


def test_account_boundary_blocks_same_message_and_raw_hash(tmp_cfg):
    service, qq_account, mailboxes = _account_context(tmp_cfg)
    accounts = service.list_mail_accounts().details["accounts"]
    other_account = next(
        item for item in accounts if item["account_id"] != qq_account["account_id"]
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-account-a",
        account_id=str(qq_account["account_id"]),
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id="<account-boundary-v172@example.com>",
        raw_sha256="0" * 64,
    )

    result = _find(
        tmp_cfg,
        account_id=str(other_account["account_id"]),
        message_id="<account-boundary-v172@example.com>",
        raw_sha256="0" * 64,
    )

    assert result["status"] == "unmatched"
    assert result["candidate_count"] == 0
    assert result["package_id"] is None


def test_composite_time_window_is_absolute_and_bounded(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    message_id = "<time-window-v172@example.com>"
    fingerprint = _fingerprint(
        subject="audit subject",
        body="current body",
        to_emails=("receiver@example.com",),
    )
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-time-window",
        account_id=account_id,
        mailbox_id=str(mailboxes["Sent"]["mailbox_id"]),
        message_id=message_id,
        raw_sha256="1" * 64,
        content_fingerprint=fingerprint,
        sent_at="2026-08-10 12:00:00",
    )

    inside = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        content_fingerprint=fingerprint,
        observed_at="2026-08-17 12:00:00",
    )
    after = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        content_fingerprint=fingerprint,
        observed_at="2026-08-17 12:00:01",
    )
    before = _find(
        tmp_cfg,
        account_id=account_id,
        message_id=message_id,
        content_fingerprint=fingerprint,
        observed_at="2026-08-03 11:59:59",
    )

    assert RECONCILIATION_WINDOW_SECONDS == 604800
    assert inside["status"] == "matched"
    assert after["status"] == "unmatched"
    assert before["status"] == "unmatched"


def test_exact_outbound_provider_and_raw_paths_stay_supported(tmp_cfg):
    _service, account, mailboxes = _account_context(tmp_cfg)
    account_id = str(account["account_id"])
    mailbox_id = str(mailboxes["Sent"]["mailbox_id"])
    _insert_package(
        tmp_cfg,
        package_id="pkg-v172-exact-paths",
        account_id=account_id,
        mailbox_id=mailbox_id,
        message_id="<exact-paths-v172@example.com>",
        raw_sha256="2" * 64,
        outbound_id="out-v172-exact",
    )
    record_sent_mapping(
        tmp_cfg.db_path,
        account_id=account_id,
        package_id="pkg-v172-exact-paths",
        mailbox_id=mailbox_id,
        provider_message_id="provider-v172-exact",
        uidvalidity=172,
        provider_uid="777",
        message_id="<exact-paths-v172@example.com>",
        matched_by="exact_raw_hash",
    )

    outbound = _find(
        tmp_cfg, account_id=account_id, outbound_id="out-v172-exact"
    )
    provider = _find(
        tmp_cfg,
        account_id=account_id,
        provider_message_id="provider-v172-exact",
    )
    raw = _find(tmp_cfg, account_id=account_id, raw_sha256="2" * 64)

    assert outbound["evidence_type"] == "exact_outbound_id"
    assert provider["evidence_type"] == "exact_provider_id"
    assert raw["evidence_type"] == "exact_raw_hash"
    assert {outbound["package_id"], provider["package_id"], raw["package_id"]} == {
        "pkg-v172-exact-paths"
    }
