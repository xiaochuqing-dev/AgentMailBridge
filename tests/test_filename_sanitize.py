"""测试文件名清洗逻辑（utils.sanitize_filename 等）。"""

from agent_mail_bridge.utils import (
    sanitize_filename,
    split_ext,
    unique_path,
    decode_mime_header,
)


class TestSanitizeFilename:
    def test_normal(self):
        assert sanitize_filename("Agent Mail Bridge 测试邮件") == "Agent Mail Bridge 测试邮件"

    def test_invalid_chars(self):
        # 包含 Windows 非法字符
        result = sanitize_filename('a/b\\c:d*e?f<g>h|i"j')
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result
        assert "*" not in result
        assert "?" not in result
        assert '"' not in result
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result

    def test_leading_trailing_dots(self):
        assert sanitize_filename("...report...") == "report"

    def test_whitespace_collapse(self):
        assert sanitize_filename("  hello   world  ") == "hello world"

    def test_empty_returns_untitled(self):
        assert sanitize_filename("") == "untitled"
        assert sanitize_filename("   ") == "untitled"
        assert sanitize_filename("///") == "untitled"

    def test_truncate(self):
        long_name = "A" * 200
        result = sanitize_filename(long_name, max_len=80)
        assert len(result) <= 80

    def test_control_chars(self):
        assert sanitize_filename("a\x00b\x01c") == "a b c"

    def test_none(self):
        assert sanitize_filename(None) == "untitled"


class TestSplitExt:
    def test_with_ext(self):
        assert split_ext("报告.pdf") == ("报告", ".pdf")

    def test_uppercase_ext(self):
        assert split_ext("report.PDF") == ("report", ".pdf")

    def test_no_ext(self):
        assert split_ext("README") == ("README", "")

    def test_multiple_dots(self):
        assert split_ext("archive.tar.gz") == ("archive.tar", ".gz")


class TestUniquePath:
    def test_no_conflict(self, tmp_path):
        p = unique_path(tmp_path, "report", ".md")
        assert p == tmp_path / "report.md"

    def test_with_conflict(self, tmp_path):
        (tmp_path / "report.md").touch()
        p = unique_path(tmp_path, "report", ".md")
        assert p == tmp_path / "report_1.md"

    def test_multiple_conflicts(self, tmp_path):
        (tmp_path / "report.md").touch()
        (tmp_path / "report_1.md").touch()
        p = unique_path(tmp_path, "report", ".md")
        assert p == tmp_path / "report_2.md"


class TestDecodeMimeHeader:
    def test_plain(self):
        assert decode_mime_header("Hello") == "Hello"

    def test_none(self):
        assert decode_mime_header(None) == ""

    def test_encoded_utf8(self):
        # =?utf-8?b?5rWL6K+V?=
        result = decode_mime_header("=?utf-8?b?5rWL6K+V?=")
        assert "测试" in result
