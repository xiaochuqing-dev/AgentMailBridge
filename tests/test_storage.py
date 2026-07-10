"""测试存储模块（日期目录、文件名生成、复制）。"""

from datetime import datetime
from pathlib import Path

from agent_mail_bridge.storage import (
    build_body_filename,
    build_body_path,
    build_attachment_filename,
    build_attachment_path,
    build_send_copy_path,
    build_sent_copy_path,
    copy_file,
    write_text,
    write_bytes,
    list_files_in_day_dir,
    list_attachments,
    received_day_dir,
    received_attachments_dir,
)
from agent_mail_bridge.utils import sanitize_filename


DT = datetime(2026, 7, 9, 1, 31, 22)


class TestDirStructure:
    def test_received_day_dir(self, tmp_cfg):
        d = received_day_dir(tmp_cfg, DT)
        assert d == tmp_cfg.received_dir / "2026-07-09"
        assert d.exists()

    def test_attachments_dir(self, tmp_cfg):
        d = received_attachments_dir(tmp_cfg, DT)
        assert d == tmp_cfg.received_dir / "2026-07-09" / "attachments"
        assert d.exists()


class TestBodyFilename:
    def test_basic(self):
        name = build_body_filename("Agent Mail Bridge 测试邮件", DT)
        assert name == "01-31-22_Agent Mail Bridge 测试邮件.md"

    def test_empty_subject(self):
        name = build_body_filename("", DT)
        assert name == "01-31-22_无标题邮件.md"

    def test_sanitized_subject(self):
        name = build_body_filename("a/b:c", DT)
        assert "/" not in name
        assert ":" not in name
        assert name.startswith("01-31-22_")
        assert name.endswith(".md")

    def test_body_path_unique(self, tmp_cfg):
        p1 = build_body_path(tmp_cfg, "主题", DT)
        write_text(p1, "x")
        p2 = build_body_path(tmp_cfg, "主题", DT)
        assert p1 != p2
        assert "_1" in p2.name


class TestAttachmentFilename:
    def test_preserves_extension(self):
        name = build_attachment_filename("界面截图.png", DT)
        assert name == "01-31-22_界面截图.png"

    def test_uppercase_ext_lowercased(self):
        name = build_attachment_filename("doc.PDF", DT)
        assert name.endswith(".pdf")

    def test_attachment_path(self, tmp_cfg):
        p = build_attachment_path(tmp_cfg, "doc.pdf", DT)
        assert p.parent == tmp_cfg.received_dir / "2026-07-09" / "attachments"
        assert p.name == "01-31-22_doc.pdf"


class TestSendSentCopyPaths:
    def test_send_copy_path(self, tmp_cfg):
        p = build_send_copy_path(tmp_cfg, "/tmp/result.md", DT)
        assert p.parent == tmp_cfg.send_dir / "2026-07-09"
        assert p.name == "01-31-22_result.md"

    def test_sent_copy_path(self, tmp_cfg):
        p = build_sent_copy_path(tmp_cfg, "/tmp/result.md", DT)
        assert p.parent == tmp_cfg.sent_dir / "2026-07-09"
        assert p.name == "01-31-22_result.md"

    def test_send_copy_unique_on_conflict(self, tmp_cfg):
        p1 = build_send_copy_path(tmp_cfg, "/tmp/result.md", DT)
        write_text(p1, "x")
        p2 = build_send_copy_path(tmp_cfg, "/tmp/result.md", DT)
        assert p1 != p2


class TestFileOps:
    def test_write_text(self, tmp_cfg):
        p = tmp_cfg.received_dir / "x.md"
        write_text(p, "内容")
        assert p.read_text(encoding="utf-8") == "内容"

    def test_write_bytes(self, tmp_cfg):
        p = tmp_cfg.received_dir / "x.bin"
        write_bytes(p, b"\x00\x01")
        assert p.read_bytes() == b"\x00\x01"

    def test_copy_file(self, tmp_cfg):
        src = tmp_cfg.received_dir / "src.txt"
        write_text(src, "hello")
        dst = tmp_cfg.send_dir / "dst.txt"
        copy_file(src, dst)
        assert dst.read_text(encoding="utf-8") == "hello"


class TestListing:
    def test_list_files_and_attachments(self, tmp_cfg):
        day_dir = received_day_dir(tmp_cfg, DT)
        write_text(day_dir / "01-00-00_a.md", "x")
        write_text(day_dir / "02-00-00_b.md", "y")
        att_dir = received_attachments_dir(tmp_cfg, DT)
        write_bytes(att_dir / "01-00-00_img.png", b"png")

        files = list_files_in_day_dir(day_dir)
        assert len(files) == 2
        atts = list_attachments(day_dir)
        assert len(atts) == 1
        assert atts[0].name == "01-00-00_img.png"

    def test_list_empty_dir(self, tmp_cfg):
        day_dir = received_day_dir(tmp_cfg, DT)
        assert list_files_in_day_dir(day_dir) == []
        assert list_attachments(day_dir) == []
