"""v1.4.3 Provider Validation & Hardening 定向回归。"""

from __future__ import annotations

import smtplib

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import query_receive_retries
from agent_mail_bridge.imap_sync import receive_imap_account
from agent_mail_bridge.mail_send import _classify_smtp_error
from agent_mail_bridge.provider_foundation import (
    ProviderFoundationError,
    classify_protocol_error,
    discover_imap_mailboxes,
    mailbox_text,
    test_smtp_connection as verify_smtp_connection,
)


def _raw(uid: int) -> bytes:
    return (
        "From: sender@example.com\r\n"
        "To: receiver@example.com\r\n"
        f"Subject: generation-{uid}\r\n"
        f"Message-ID: <generation-{uid}@example.com>\r\n"
        "Date: Thu, 23 Jul 2026 10:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"body-{uid}"
    ).encode("utf-8")


class GenerationImapClient:
    def __init__(
        self,
        *,
        uidvalidity: int,
        raw: bytes | None,
        failure: Exception | None = None,
    ):
        self.uidvalidity = uidvalidity
        self.raw = raw
        self.failure = failure

    def login(self, _username, _secret):
        return None

    def list_folders(self):
        return [((b"\\Inbox",), b"/", b"INBOX")]

    def select_folder(self, _mailbox, readonly=True):
        return {
            b"UIDVALIDITY": self.uidvalidity,
            b"UIDNEXT": 2,
            b"HIGHESTMODSEQ": 0,
        }

    def search(self, _criteria):
        return [1]

    def fetch(self, _uids, _parts):
        if self.failure is not None:
            raise self.failure
        return {1: {b"BODY[]": self.raw}}

    def logout(self):
        return None


class ByteMailboxClient:
    def __init__(self, *_args, **_kwargs):
        pass

    def login(self, _username, _secret):
        return None

    def capabilities(self):
        return (b"IMAP4rev1", b"SPECIAL-USE")

    def list_folders(self):
        return [
            ((b"\\Inbox",), b"/", b"INBOX"),
            ((b"\\Sent",), b"/", b"&XfJT0ZAB-"),
        ]

    def select_folder(self, _mailbox, readonly=True):
        return {b"UIDVALIDITY": 7, b"UIDNEXT": 9}

    def logout(self):
        return None


def _generic_runtime(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    created = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address="validation@example.net",
        provider_settings={
            "imap_host": "imap.example.net",
            "imap_security": "ssl",
        },
        secret="stored-secret",
    )
    assert created.ok
    account_id = created.details["account"]["account_id"]
    runtime = service._account_router.context(
        account_id, capability="receive"
    ).config
    return account_id, runtime


def test_mailbox_bytes_are_decoded_and_roles_remain_provider_neutral():
    assert mailbox_text(b"INBOX") == "INBOX"
    result = discover_imap_mailboxes(
        settings={"imap_host": "imap.example.net"},
        username="user@example.net",
        secret="secret",
        client_factory=ByteMailboxClient,
    )
    assert [item["mailbox_role"] for item in result["mailboxes"]] == [
        "inbox",
        "sent",
    ]
    assert all(
        not item["display_name"].startswith("b'")
        for item in result["mailboxes"]
    )


def test_connection_errors_are_classified_without_server_response_or_secret():
    def imap_failure(*_args, **_kwargs):
        raise RuntimeError(
            "AUTHENTICATIONFAILED authorization-code-sensitive-value"
        )

    with pytest.raises(ProviderFoundationError) as imap_error:
        discover_imap_mailboxes(
            settings={"imap_host": "imap.example.net"},
            username="user@example.net",
            secret="authorization-code-sensitive-value",
            client_factory=imap_failure,
        )
    assert imap_error.value.error_code == "imap_auth_failed"
    assert "sensitive-value" not in str(imap_error.value)

    def smtp_failure(*_args, **_kwargs):
        raise smtplib.SMTPAuthenticationError(
            535, b"authorization-code-sensitive-value"
        )

    with pytest.raises(ProviderFoundationError) as smtp_error:
        verify_smtp_connection(
            settings={"smtp_host": "smtp.example.net"},
            username="user@example.net",
            secret="authorization-code-sensitive-value",
            smtp_ssl_factory=smtp_failure,
        )
    assert smtp_error.value.error_code == "smtp_auth_failed"
    assert "sensitive-value" not in str(smtp_error.value)


def test_connection_reset_is_classified_as_disconnect():
    assert classify_protocol_error(
        "imap", ConnectionResetError("connection reset")
    ) == ("imap_disconnected", "IMAP 连接已断开")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            smtplib.SMTPAuthenticationError(454, b"4.7.0 temporary auth"),
            "temporary",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {"target@example.com": (450, b"4.2.0 mailbox busy")}
            ),
            "temporary",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {"target@example.com": (550, b"5.1.1 rejected")}
            ),
            "recipient_rejected",
        ),
        (
            smtplib.SMTPDataError(552, b"5.3.4 message too large"),
            "message_too_large",
        ),
        (
            smtplib.SMTPDataError(554, b"5.7.1 policy rejected"),
            "permanent",
        ),
    ],
)
def test_smtp_reply_classification_distinguishes_retryability(error, expected):
    assert _classify_smtp_error(error, default_stage="send") == expected


def test_uidvalidity_reset_retires_old_generation_retry_and_sanitizes_error(
    tmp_cfg,
):
    account_id, runtime = _generic_runtime(tmp_cfg)
    first_client = GenerationImapClient(
        uidvalidity=11,
        raw=None,
        failure=RuntimeError("server detail authorization-code-sensitive-value"),
    )
    first = receive_imap_account(
        runtime,
        client_factory=lambda *_args, **_kwargs: first_client,
    )
    assert first["failed"] == 1
    retries = query_receive_retries(
        tmp_cfg.db_path, "imap", account_id=account_id
    )
    assert len(retries) == 1
    assert retries[0]["resource_id"] == "INBOX:11:1"
    assert retries[0]["last_error"] == "processing_RuntimeError"
    assert "sensitive-value" not in retries[0]["last_error"]

    second_client = GenerationImapClient(uidvalidity=12, raw=_raw(1))
    second = receive_imap_account(
        runtime,
        client_factory=lambda *_args, **_kwargs: second_client,
    )
    assert second["uidvalidity_changed"]
    assert second["stale_retries_retired"] == 1
    assert second["saved"] == 1
    assert (
        query_receive_retries(
            tmp_cfg.db_path, "imap", account_id=account_id
        )
        == []
    )
