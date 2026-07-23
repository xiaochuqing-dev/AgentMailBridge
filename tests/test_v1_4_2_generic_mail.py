"""v1.4.2 Generic IMAP/SMTP、QQ 与 163 双向核心回归。"""

from __future__ import annotations

import json
import smtplib
import sqlite3
from dataclasses import replace
from email.message import EmailMessage

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import (
    IncomingRuntimeConfig,
    OutgoingRuntimeConfig,
)
from agent_mail_bridge.database import (
    get_auto_receive_state,
    get_outbound_message,
    query_mail_accounts,
)
from agent_mail_bridge.imap_sync import receive_imap_account
from agent_mail_bridge.mail_send import SmtpStageError, _smtp_send_with_stage


def _raw(uid: int, *, subject: str | None = None) -> bytes:
    return (
        f"From: sender{uid}@example.com\r\n"
        "To: receiver@example.com\r\n"
        f"Subject: {subject or f'message-{uid}'}\r\n"
        f"Message-ID: <message-{uid}@example.com>\r\n"
        "Date: Thu, 23 Jul 2026 10:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"body-{uid}"
    ).encode("utf-8")


class FakeImapClient:
    def __init__(
        self,
        messages: dict[int, bytes],
        *,
        uidvalidity: int = 1,
        missing: set[int] | None = None,
    ):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.missing = set(missing or ())
        self.commands: list[tuple] = []
        self.seen: list[int] = []

    def login(self, username, secret):
        self.commands.append(("login", username, secret))

    def list_folders(self):
        return [
            ((b"\\Inbox",), b"/", "INBOX"),
            ((b"\\Sent",), b"/", "Sent"),
            ((b"\\Trash",), b"/", "Trash"),
        ]

    def select_folder(self, mailbox, readonly=True):
        self.commands.append(("select", mailbox, readonly))
        return {
            b"UIDVALIDITY": self.uidvalidity,
            b"UIDNEXT": max(self.messages, default=0) + 1,
            b"HIGHESTMODSEQ": 0,
        }

    def search(self, criteria):
        self.commands.append(("search", criteria))
        return sorted(self.messages)

    def fetch(self, uids, _parts):
        self.commands.append(("fetch", tuple(uids)))
        return {
            uid: {b"BODY[]": self.messages[uid]}
            for uid in uids
            if uid in self.messages and uid not in self.missing
        }

    def add_flags(self, uids, flags, silent=True):
        self.seen.extend(uids)
        self.commands.append(("flags", tuple(uids), tuple(flags), silent))

    def logout(self):
        self.commands.append(("logout",))


def _generic_runtime(service: ApplicationService, account_id: str):
    return service._account_router.context(
        account_id, capability="receive"
    ).config


def test_generic_runtime_is_provider_neutral_and_does_not_reuse_legacy_fields(
    tmp_cfg,
):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    created = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address="person@example.net",
        provider_settings={
            "imap_host": "imap.example.net",
            "imap_port": 993,
            "imap_security": "ssl",
            "smtp_host": "smtp.example.net",
            "smtp_port": 587,
            "smtp_security": "starttls",
        },
        imap_secret="incoming-secret",
        smtp_secret="outgoing-secret",
    )
    assert created.ok
    account_id = created.details["account"]["account_id"]
    runtime = service._account_router.context(account_id).config

    assert runtime.gmail_address == tmp_cfg.gmail_address
    assert runtime.qq_email == tmp_cfg.qq_email
    assert runtime.incoming == IncomingRuntimeConfig(
        backend="imap",
        username="person@example.net",
        secret="incoming-secret",
        host="imap.example.net",
        port=993,
        security="ssl",
        connect_timeout=20,
        mailbox="INBOX",
        uid_overlap=10,
    )
    assert runtime.outgoing == OutgoingRuntimeConfig(
        backend="smtp",
        username="person@example.net",
        secret="outgoing-secret",
        host="smtp.example.net",
        port=587,
        security="starttls",
        connect_timeout=20,
    )


@pytest.mark.parametrize(
    ("provider", "address", "imap_host", "smtp_host"),
    [
        ("qq", "123456@qq.com", "imap.qq.com", "smtp.qq.com"),
        ("163", "person@163.com", "imap.163.com", "smtp.163.com"),
    ],
)
def test_qq_and_163_share_full_provider_profile_and_credentials(
    tmp_cfg, provider, address, imap_host, smtp_host
):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    created = service.create_mail_account(
        provider=provider,
        email_address=address,
        secret="shared-authorization-code",
    )
    assert created.ok
    account = created.details["account"]
    assert account["receive_enabled"]
    assert account["send_enabled"]
    assert {"receive", "send", "archive", "mail_facts"} <= set(
        account["capabilities"]
    )
    runtime = service._account_router.context(account["account_id"]).config
    assert runtime.incoming.host == imap_host
    assert runtime.outgoing.host == smtp_host
    assert runtime.incoming.secret == runtime.outgoing.secret


def test_imap_initial_incremental_uidvalidity_and_failure_isolation(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    created = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address="sync@example.net",
        provider_settings={
            "imap_host": "imap.example.net",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        secret="imap-secret",
    )
    account_id = created.details["account"]["account_id"]
    runtime = _generic_runtime(service, account_id)

    first_client = FakeImapClient(
        {1: _raw(1), 2: _raw(2), 3: _raw(3)}, missing={2}
    )
    first = receive_imap_account(
        runtime,
        limit=10,
        client_factory=lambda *_args, **_kwargs: first_client,
    )
    assert first["ok"]
    assert first["saved"] == 2
    assert first["failed"] == 1
    checkpoint = json.loads(
        get_auto_receive_state(
            tmp_cfg.db_path, account_id=account_id
        )["checkpoint"]
    )["mailboxes"]["INBOX"]
    assert checkpoint["uidvalidity"] == 1
    assert checkpoint["last_uid"] == 3

    second_client = FakeImapClient(
        {1: _raw(1), 2: _raw(2), 3: _raw(3), 4: _raw(4)}
    )
    second = receive_imap_account(
        runtime,
        limit=10,
        client_factory=lambda *_args, **_kwargs: second_client,
    )
    assert second["saved"] == 1
    assert second["duplicates"] >= 2
    checkpoint = json.loads(
        get_auto_receive_state(
            tmp_cfg.db_path, account_id=account_id
        )["checkpoint"]
    )["mailboxes"]["INBOX"]
    assert checkpoint["last_uid"] == 4

    reset_client = FakeImapClient(
        {1: _raw(1), 2: _raw(2), 3: _raw(3), 4: _raw(4)},
        uidvalidity=2,
    )
    reset = receive_imap_account(
        runtime,
        limit=10,
        client_factory=lambda *_args, **_kwargs: reset_client,
    )
    assert reset["uidvalidity_changed"]
    checkpoint = json.loads(
        get_auto_receive_state(
            tmp_cfg.db_path, account_id=account_id
        )["checkpoint"]
    )["mailboxes"]["INBOX"]
    assert checkpoint["uidvalidity"] == 2
    assert checkpoint["uidvalidity_reset_count"] == 1


def test_imap_scan_cap_pages_forward_without_loading_whole_mailbox(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    created = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address="large@example.net",
        provider_settings={
            "imap_host": "imap.example.net",
            "imap_security": "ssl",
            "uid_overlap": 0,
        },
        secret="imap-secret",
    )
    account_id = created.details["account"]["account_id"]
    runtime = _generic_runtime(service, account_id)
    client = FakeImapClient({uid: _raw(uid) for uid in range(1, 6)})

    first = receive_imap_account(
        runtime,
        limit=2,
        client_factory=lambda *_args, **_kwargs: client,
    )
    second = receive_imap_account(
        runtime,
        limit=2,
        client_factory=lambda *_args, **_kwargs: client,
    )
    third = receive_imap_account(
        runtime,
        limit=2,
        client_factory=lambda *_args, **_kwargs: client,
    )
    assert [first["saved"], second["saved"], third["saved"]] == [2, 2, 1]
    checkpoint = json.loads(
        get_auto_receive_state(
            tmp_cfg.db_path, account_id=account_id
        )["checkpoint"]
    )["mailboxes"]["INBOX"]
    assert checkpoint["last_uid"] == 5


class FakeSmtp:
    instances: list["FakeSmtp"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.commands: list[tuple] = []
        self.__class__.instances.append(self)

    def ehlo(self):
        self.commands.append(("ehlo",))

    def starttls(self, context):
        self.commands.append(("starttls", bool(context)))

    def login(self, username, secret):
        self.commands.append(("login", username, secret))

    def send_message(self, message, from_addr, to_addrs):
        self.commands.append(("send", message["From"], from_addr, tuple(to_addrs)))

    def quit(self):
        self.commands.append(("quit",))


def test_smtp_supports_ssl_and_starttls_with_provider_neutral_sender(
    tmp_cfg, monkeypatch
):
    FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    message = EmailMessage()
    message["From"] = "sender@example.net"
    message["To"] = "target@example.com"
    message.set_content("body")

    ssl_cfg = replace(
        tmp_cfg,
        runtime_account_id="acct_111111111111111111111111",
        runtime_provider="generic_imap_smtp",
        outgoing=OutgoingRuntimeConfig(
            backend="smtp",
            username="sender@example.net",
            secret="secret",
            host="smtp.example.net",
            port=465,
            security="ssl",
        ),
    )
    _smtp_send_with_stage(ssl_cfg, message)
    assert not any(
        command[0] == "starttls"
        for command in FakeSmtp.instances[-1].commands
    )

    starttls_cfg = replace(
        ssl_cfg,
        outgoing=replace(ssl_cfg.outgoing, port=587, security="starttls"),
    )
    _smtp_send_with_stage(starttls_cfg, message)
    assert any(
        command[0] == "starttls"
        for command in FakeSmtp.instances[-1].commands
    )


def test_smtp_recipient_rejection_is_classified(tmp_cfg, monkeypatch):
    class RejectingSmtp(FakeSmtp):
        def send_message(self, message, from_addr, to_addrs):
            raise smtplib.SMTPRecipientsRefused(
                {to_addrs[0]: (550, b"rejected")}
            )

    monkeypatch.setattr(smtplib, "SMTP_SSL", RejectingSmtp)
    cfg = replace(
        tmp_cfg,
        outgoing=OutgoingRuntimeConfig(
            backend="smtp",
            username="sender@example.net",
            secret="secret",
            host="smtp.example.net",
        ),
    )
    message = EmailMessage()
    message["From"] = "sender@example.net"
    message["To"] = "target@example.com"
    message.set_content("body")
    with pytest.raises(SmtpStageError) as captured:
        _smtp_send_with_stage(cfg, message)
    assert captured.value.stage == "recipient_rejected"


def test_163_gui_send_reuses_outbound_archive_and_account_ownership(
    tmp_cfg, monkeypatch
):
    service = ApplicationService(tmp_cfg)
    service.initialize()
    created = service.create_mail_account(
        provider="163",
        email_address="sender@163.com",
        secret="authorization-code",
    )
    account_id = created.details["account"]["account_id"]
    captured: dict[str, str] = {}

    def fake_send(_cfg, message):
        captured["from"] = str(message["From"])

    monkeypatch.setattr(
        "agent_mail_bridge.mail_send._smtp_send_with_stage", fake_send
    )
    sent = service.send_user_selected_mail(
        from_account_id=account_id,
        recipient="target@example.com",
        subject="163 ownership",
        body_text="body",
        attachment_paths=[],
        links=[],
    )
    assert sent.ok
    assert captured["from"] == "sender@163.com"
    outbound = get_outbound_message(tmp_cfg.db_path, sent.outbound_id)
    assert outbound["from_account_id"] == account_id
    assert outbound["sender_ref"] == "sender@163.com"


def test_v142_migration_enables_existing_qq_without_touching_user_files(
    tmp_cfg,
):
    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    qq_id = next(
        item["account_id"]
        for item in query_mail_accounts(tmp_cfg.db_path)
        if item["provider"] == "qq"
    )
    with sqlite3.connect(tmp_cfg.db_path) as connection:
        connection.execute(
            "UPDATE migration_metadata SET schema_version = 2 "
            "WHERE migration_key = 'multi_account_core_v1'"
        )
        connection.execute(
            "UPDATE mail_accounts SET receive_enabled = 0, "
            "capabilities_json = '[\"send\"]' WHERE account_id = ?",
            (qq_id,),
        )
        connection.commit()

    service = ApplicationService(tmp_cfg)
    assert service.initialize().ok
    qq = next(
        item
        for item in query_mail_accounts(tmp_cfg.db_path)
        if item["account_id"] == qq_id
    )
    assert qq["receive_enabled"]
    assert {"receive", "send", "imap", "smtp"} <= set(qq["capabilities"])
