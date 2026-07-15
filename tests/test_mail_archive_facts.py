"""统一邮件归档、邮件事实、迁移和可信下载安全回归。"""

from __future__ import annotations

import json
import socket
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.database import (
    close_connection,
    get_connection,
    insert_received_file,
    insert_received_message,
    query_mail_resources,
)
from agent_mail_bridge.mail_archive import backfill_legacy_mail_packages
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.mail_processing import process_normalized_mail
from agent_mail_bridge.maintenance import scan_consistency
from agent_mail_bridge.receive_rules import ALL_SCANNED
from agent_mail_bridge.trusted_downloads import (
    download_trusted_url,
    is_host_trusted,
    normalize_trusted_domain,
    validate_public_https_target,
)
from agent_mail_bridge.utils import sha256_of_bytes


def _normalized(
    tmp_cfg,
    *,
    message_id: str = "<archive@test>",
    subject: str = "统一邮件归档测试",
    plain: str = "纯文本正文",
    html: str = "<p>HTML 正文</p>",
    attachment_count: int = 0,
    include_inline: bool = False,
    long_filename: str | None = None,
    backend: str = "imap",
    thread_id: str = "",
):
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "test@gmail.com"
    message["Cc"] = "copy@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Wed, 15 Jul 2026 10:00:00 +0800"
    if plain and html:
        message.set_content(plain)
        message.add_alternative(html, subtype="html")
    elif html:
        message.set_content(html, subtype="html")
    else:
        message.set_content(plain)
    if include_inline:
        if message.get_content_type() != "multipart/alternative":
            message.add_alternative('<img src="cid:logo-1">', subtype="html")
        html_part = message.get_payload()[-1]
        html_part.add_related(
            b"inline-image", maintype="image", subtype="png",
            cid="<logo-1>", filename="正文图片.png", disposition="inline",
        )
    for index in range(attachment_count):
        filename = long_filename if index == 0 and long_filename else f"附件 {index + 1}.txt"
        message.add_attachment(
            f"content-{index}".encode(), maintype="text", subtype="plain",
            filename=filename,
        )
    raw = message.as_bytes()
    normalized = normalized_mail_from_raw(
        raw,
        backend=backend,
        backend_message_id="provider-1" if backend == "gmail_api" else "",
        thread_id=thread_id,
        uid="101" if backend == "imap" else "",
        received_at="2026-07-15 10:00:00",
        saved_date="2026-07-15",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes,
        mailbox_ref="gmail:me/inbox" if backend == "gmail_api" else "imap:INBOX",
    )
    return normalized, raw


def test_one_mail_has_one_complete_package_with_real_raw_and_relative_manifest(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    mail, raw = _normalized(
        tmp_cfg,
        plain="正文 https://example.com/report.pdf",
        html='<p>HTML</p><img src="cid:logo-1"><a href="https://docs.google.com/document/d/1">云文档</a>',
        attachment_count=7,
        include_inline=True,
        backend="gmail_api",
        thread_id="gmail-thread-1",
    )
    result = process_normalized_mail(tmp_cfg, mail)
    assert result["status"] == "saved"
    service = ApplicationService(tmp_cfg)
    message = service.get_mail_message(result["package_id"]).details["message"]
    root = Path(message["package_root"])
    assert (root / "raw.eml").read_bytes() == raw
    assert message["raw_eml"]["sha256"] == sha256_of_bytes(raw)
    assert (root / "body" / "body.txt").is_file()
    assert (root / "body" / "body.html").is_file()
    assert (root / "body" / "body.md").is_file()
    assert message["counts"]["attachments"] == 7
    assert message["counts"]["inline_images"] == 1
    resources = message["resources"]
    inline = next(item for item in resources if item["internal_type"] == "inline_image")
    assert inline["content_id"] == "logo-1"
    assert (root / inline["path"]).is_file()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["package_id"] == result["package_id"]
    assert manifest["raw_eml"]["path"] == "raw.eml"
    assert all(
        not Path(item["path"]).is_absolute()
        for item in manifest["resources"] if item.get("path")
    )


@pytest.mark.parametrize(
    "plain,html,expected",
    [
        ("plain only", "", {"body_plain", "body_readable"}),
        ("", "<p>html only</p><script>bad()</script>", {"body_html", "body_readable"}),
        ("plain", "<p>html</p>", {"body_plain", "body_html", "body_readable"}),
        ("", "", {"body_readable"}),
    ],
)
def test_body_variants_are_layered_and_readable_html_ignores_scripts(
    tmp_cfg, plain, html, expected
):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    mail, _ = _normalized(
        tmp_cfg, message_id=f"<body-{len(plain)}-{len(html)}@test>",
        plain=plain, html=html,
    )
    result = process_normalized_mail(tmp_cfg, mail)
    facts = ApplicationService(tmp_cfg).get_mail_message(result["package_id"]).details["message"]
    body_types = {
        item["internal_type"] for item in facts["resources"]
        if item["internal_type"].startswith("body_")
    }
    assert body_types == expected
    readable = Path(facts["package_root"], facts["body"]["readable_path"]).read_text(encoding="utf-8")
    assert "bad()" not in readable


def test_cid_image_and_normal_image_attachment_are_not_conflated(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "test@gmail.com"
    message["Message-ID"] = "<images@test>"
    message.set_content("fallback")
    message.add_alternative('<img src="cid:hero">', subtype="html")
    message.get_payload()[-1].add_related(
        b"inline", maintype="image", subtype="png", cid="<hero>",
        filename="hero.png", disposition="inline",
    )
    message.add_attachment(
        b"attachment", maintype="image", subtype="png", filename="photo.png"
    )
    raw = message.as_bytes()
    mail = normalized_mail_from_raw(
        raw, backend="imap", backend_message_id="", thread_id="", uid="1",
        received_at="2026-07-15 10:00:00", saved_date="2026-07-15",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes, mailbox_ref="imap:INBOX",
    )
    result = process_normalized_mail(tmp_cfg, mail)
    resources = query_mail_resources(tmp_cfg.db_path, result["package_id"])
    assert sum(item["resource_type"] == "inline_image" for item in resources) == 1
    assert sum(item["resource_type"] == "attachment" for item in resources) == 1


def test_duplicate_and_traversal_attachment_names_are_safely_isolated(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "test@gmail.com"
    message["Message-ID"] = "<duplicate-files@test>"
    message.set_content("body")
    for content in (b"one", b"two"):
        message.add_attachment(
            content, maintype="application", subtype="octet-stream",
            filename="../../same.txt",
        )
    raw = message.as_bytes()
    mail = normalized_mail_from_raw(
        raw, backend="imap", backend_message_id="", thread_id="", uid="1",
        received_at="2026-07-15 10:00:00", saved_date="2026-07-15",
        max_attachment_bytes=tmp_cfg.max_attachment_bytes, mailbox_ref="imap:INBOX",
    )
    result = process_normalized_mail(tmp_cfg, mail)
    resources = [
        item for item in query_mail_resources(tmp_cfg.db_path, result["package_id"])
        if item["resource_type"] == "attachment"
    ]
    assert len({item["local_path"] for item in resources}) == 2
    assert all(".." not in Path(item["local_path"]).parts for item in resources)


def test_package_id_is_stable_and_cross_backend_duplicate_is_not_recreated(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    first_mail, _ = _normalized(tmp_cfg, backend="imap")
    first = process_normalized_mail(tmp_cfg, first_mail)
    second_mail, _ = _normalized(tmp_cfg, backend="gmail_api", thread_id="thread-1")
    second = process_normalized_mail(tmp_cfg, second_mail)
    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert second["package_id"] == first["package_id"]
    assert len(ApplicationService(tmp_cfg).list_mail_messages().details["messages"]) == 1


def test_links_are_static_only_when_trusted_domains_are_empty(tmp_cfg, monkeypatch):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    mail, _ = _normalized(
        tmp_cfg,
        plain="网页 https://example.com/page 文件 https://example.com/a.zip",
        html='<img src="https://example.com/p.png"><a href="https://notion.so/work">知识页</a>',
    )
    monkeypatch.setattr(
        "agent_mail_bridge.mail_archive.download_trusted_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应触网")),
    )
    result = process_normalized_mail(tmp_cfg, mail)
    facts = ApplicationService(tmp_cfg).get_mail_message(result["package_id"]).details["message"]
    assert facts["counts"]["links"] == 4
    assert facts["counts"]["downloads"] == 0
    assert ApplicationService(tmp_cfg).list_trusted_domains().details["domains"] == []


def test_full_display_name_is_preserved_while_saved_name_is_controlled(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    original = "非常长的中文附件名称_" + "内容" * 80 + ".txt"
    mail, _ = _normalized(tmp_cfg, attachment_count=1, long_filename=original)
    result = process_normalized_mail(tmp_cfg, mail)
    resources = query_mail_resources(tmp_cfg.db_path, result["package_id"])
    attachment = next(item for item in resources if item["resource_type"] == "attachment")
    assert attachment["display_name"] == original
    assert "..." not in attachment["display_name"]
    assert len(Path(attachment["local_path"]).name) <= 80


def test_attachment_failure_is_partial_and_retry_updates_same_package(tmp_cfg, monkeypatch):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    mail, _ = _normalized(tmp_cfg, attachment_count=1)
    from agent_mail_bridge import mail_archive
    original = mail_archive._write_atomic
    failed = {"once": False}

    def fail_once(path: Path, data: bytes):
        if path.parent.name == "attachments" and not failed["once"]:
            failed["once"] = True
            raise OSError("simulated")
        return original(path, data)

    monkeypatch.setattr(mail_archive, "_write_atomic", fail_once)
    first = process_normalized_mail(tmp_cfg, mail)
    assert first["status"] == "partial"
    root = Path(ApplicationService(tmp_cfg).get_mail_message(first["package_id"]).details["message"]["package_root"])
    monkeypatch.setattr(mail_archive, "_write_atomic", original)
    second = process_normalized_mail(tmp_cfg, mail)
    assert second["status"] == "saved"
    assert second["package_id"] == first["package_id"]
    assert len(list(root.parent.glob(f"{first['package_id']}*"))) == 1


def test_mail_facts_filters_threads_and_search_all_required_fields(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    first_mail, _ = _normalized(
        tmp_cfg, message_id="<thread-root@test>", subject="Alpha project",
        plain="search-body-token", attachment_count=1,
        backend="gmail_api", thread_id="thread-42",
    )
    first = process_normalized_mail(tmp_cfg, first_mail)
    second_mail, _ = _normalized(
        tmp_cfg, message_id="<thread-reply@test>", subject="Re: Alpha project",
        plain="reply", backend="gmail_api", thread_id="thread-42",
    )
    process_normalized_mail(tmp_cfg, second_mail)
    service = ApplicationService(tmp_cfg)
    account_ref = service.get_mail_message(first["package_id"]).details["message"]["account_ref"]
    assert len(service.list_mail_messages(account_ref=account_ref, has_attachments=True).details["messages"]) == 1
    assert len(service.search_mail_facts("search-body-token").details["messages"]) == 1
    assert len(service.search_mail_facts("附件 1.txt").details["messages"]) == 1
    threads = service.list_mail_threads(account_ref=account_ref).details["threads"]
    assert threads[0]["message_count"] == 2
    thread = service.get_mail_thread("gmail:thread-42", account_ref=account_ref).details["thread"]
    assert [item["message_id"] for item in thread["messages"]] == [
        "<thread-root@test>", "<thread-reply@test>",
    ]


def test_legacy_backfill_is_idempotent_preserves_files_and_never_fabricates_raw(tmp_cfg):
    body = tmp_cfg.received_dir / "2026-07-01" / "old.md"
    attachment = tmp_cfg.received_dir / "2026-07-01" / "attachments" / "old.txt"
    body.parent.mkdir(parents=True, exist_ok=True)
    attachment.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("legacy body", encoding="utf-8")
    attachment.write_text("legacy attachment", encoding="utf-8")
    insert_received_message(
        tmp_cfg.db_path, message_id="<legacy@test>", gmail_uid="1", subject="旧邮件",
        from_email="test@gmail.com", to_email="test@gmail.com",
        received_at="2026-07-01 01:02:03", saved_date="2026-07-01",
        body_file_path=str(body), body_sha256=None, has_attachments=True,
    )
    insert_received_file(
        tmp_cfg.db_path, message_id="<legacy@test>", file_type="body",
        original_filename="旧邮件", saved_filename=body.name, saved_path=str(body),
        sha256=None, size_bytes=body.stat().st_size, mime_type="text/markdown",
        saved_date="2026-07-01",
    )
    insert_received_file(
        tmp_cfg.db_path, message_id="<legacy@test>", file_type="attachment",
        original_filename="old.txt", saved_filename=attachment.name,
        saved_path=str(attachment), sha256=None, size_bytes=attachment.stat().st_size,
        mime_type="text/plain", saved_date="2026-07-01",
    )
    first = backfill_legacy_mail_packages(tmp_cfg)
    second = backfill_legacy_mail_packages(tmp_cfg)
    assert first["migrated"] == 1
    assert second["status"] == "no_changes"
    facts = ApplicationService(tmp_cfg).search_mail_facts("旧邮件").details["messages"][0]
    assert facts["raw_eml"] == {"status": "legacy_missing", "path": None, "sha256": None}
    assert not (Path(facts["package_root"]) / "raw.eml").exists()
    assert body.is_file() and attachment.is_file()


def test_consistency_scan_rejects_package_relative_traversal(tmp_cfg):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    mail, _ = _normalized(tmp_cfg, attachment_count=1)
    result = process_normalized_mail(tmp_cfg, mail)
    connection = get_connection(tmp_cfg.db_path)
    connection.execute(
        "UPDATE mail_resources SET local_path = '../outside.txt' "
        "WHERE package_id = ? AND resource_type = 'attachment'",
        (result["package_id"],),
    )
    connection.commit()
    scan = scan_consistency(tmp_cfg)
    assert scan["summary"]["unsafe_path"] >= 1


def test_trusted_domain_persistence_and_subdomain_semantics(tmp_cfg):
    service = ApplicationService(tmp_cfg)
    assert service.list_trusted_domains().details["domains"] == []
    assert service.set_trusted_domain("Example.COM", include_subdomains=True).ok
    rows = service.list_trusted_domains().details["domains"]
    assert rows[0]["domain"] == "example.com"
    assert is_host_trusted("example.com", rows)
    assert is_host_trusted("files.example.com", rows)
    assert not is_host_trusted("badexample.com", rows)
    assert normalize_trusted_domain("*.Example.COM") == "example.com"


def test_trusted_direct_download_stays_inside_the_same_mail_package(tmp_cfg, monkeypatch):
    tmp_cfg.receive_rule_mode = ALL_SCANNED
    service = ApplicationService(tmp_cfg)
    service.set_trusted_domain("files.example.com")

    def fake_download(url, downloads_dir, **kwargs):
        path = Path(downloads_dir) / "report.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"zip-data")
        return {
            "url": url, "saved_path": str(path), "saved_filename": path.name,
            "original_filename": "report.zip", "mime_type": "application/zip",
            "size_bytes": 8, "sha256": sha256_of_bytes(b"zip-data"),
            "redirects": 0, "status": "downloaded",
        }

    monkeypatch.setattr("agent_mail_bridge.mail_archive.download_trusted_url", fake_download)
    mail, _ = _normalized(
        tmp_cfg, plain="https://files.example.com/report.zip", html=""
    )
    result = process_normalized_mail(tmp_cfg, mail)
    facts = service.get_mail_message(result["package_id"]).details["message"]
    downloaded = next(
        item for item in facts["resources"] if item["internal_type"] == "downloaded_file"
    )
    assert downloaded["package_id"] == result["package_id"]
    assert Path(downloaded["path"]).parts[0] == "downloads"
    assert (Path(facts["package_root"]) / downloaded["path"]).read_bytes() == b"zip-data"


@pytest.mark.parametrize(
    "url,addresses",
    [
        ("http://example.com/a.zip", ["93.184.216.34"]),
        ("https://localhost/a.zip", ["127.0.0.1"]),
        ("https://example.com/a.zip", ["10.0.0.1"]),
        ("https://example.com/a.zip", ["169.254.169.254"]),
        ("https://example.com/a.zip", ["224.0.0.1"]),
        ("https://example.com/a.zip", ["192.0.2.1"]),
    ],
)
def test_trusted_download_rejects_non_https_and_non_public_targets(url, addresses):
    def resolver(host, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, port)) for value in addresses]

    with pytest.raises(ValueError):
        validate_public_https_target(url, resolver=resolver)


def test_trusted_download_streams_checks_hash_and_cleans_filename(tmp_path):
    response = _FakeResponse(
        status=200,
        headers={
            "Content-Length": "7",
            "Content-Type": "text/plain",
            "Content-Disposition": 'attachment; filename="../../safe name.txt"',
        },
        body=b"content",
    )
    factory = _ConnectionFactory([response])
    result = download_trusted_url(
        "https://files.example.com/report.txt", tmp_path / "downloads",
        max_bytes=100, timeout_seconds=3,
        resolver=_public_resolver, connection_factory=factory,
    )
    path = Path(result["saved_path"])
    assert path.parent == (tmp_path / "downloads").resolve()
    assert path.name == "safe name.txt"
    assert path.read_bytes() == b"content"
    assert result["sha256"] == sha256_of_bytes(b"content")


def test_trusted_download_revalidates_redirect_and_blocks_private_ip(tmp_path):
    response = _FakeResponse(
        status=302, headers={"Location": "https://private.example/a.zip"}, body=b""
    )

    def resolver(host, port, type):
        address = "10.0.0.2" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    with pytest.raises(ValueError, match="非公网"):
        download_trusted_url(
            "https://files.example.com/a.zip", tmp_path / "downloads",
            max_bytes=100, timeout_seconds=3, resolver=resolver,
            connection_factory=_ConnectionFactory([response]),
        )


@pytest.mark.parametrize(
    "headers,body,error",
    [
        ({"Content-Length": "101", "Content-Type": "application/zip"}, b"", "大小限制"),
        ({"Content-Type": "application/zip"}, b"x" * 101, "下载流"),
        ({"Content-Type": "text/html"}, b"<html></html>", "网页内容"),
    ],
)
def test_trusted_download_enforces_declared_stream_and_mime_limits(
    tmp_path, headers, body, error
):
    with pytest.raises(ValueError, match=error):
        download_trusted_url(
            "https://files.example.com/a.zip", tmp_path / "downloads",
            max_bytes=100, timeout_seconds=3, resolver=_public_resolver,
            connection_factory=_ConnectionFactory([_FakeResponse(200, headers, body)]),
        )


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body
        self.offset = 0

    def getheader(self, name: str):
        return self.headers.get(name)

    def read(self, size: int) -> bytes:
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        return None


class _FakeConnection:
    def __init__(self, response: _FakeResponse):
        self.response = response

    def request(self, method, path, headers):
        assert method == "GET"

    def getresponse(self):
        return self.response

    def close(self):
        return None


class _ConnectionFactory:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)

    def __call__(self, host: str, port: int, address: str, timeout: int):
        return _FakeConnection(self.responses.pop(0))


def _public_resolver(host, port, type):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
