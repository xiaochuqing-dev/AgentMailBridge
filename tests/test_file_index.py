"""测试文件索引与状态扫描（scan_file_status）。"""

from datetime import datetime
from pathlib import Path

from agent_mail_bridge.database import insert_received_file
from agent_mail_bridge.file_index import scan_file_status, list_received_files_for_date
from agent_mail_bridge.storage import write_bytes, write_text
from agent_mail_bridge.utils import sha256_of_file


DT = datetime(2026, 7, 9, 1, 31, 22)


def _make_file_record(tmp_cfg, name, content, file_type="attachment"):
    """在当天附件目录创建文件并写入数据库记录。"""
    from agent_mail_bridge.storage import received_attachments_dir, received_day_dir
    directory = received_attachments_dir(tmp_cfg, DT) if file_type == "attachment" else received_day_dir(tmp_cfg, DT)
    p = directory / name
    write_bytes(p, content if isinstance(content, bytes) else content.encode())
    sha = sha256_of_file(p)
    insert_received_file(
        tmp_cfg.db_path,
        message_id="<t@example.com>",
        file_type=file_type,
        original_filename=name,
        saved_filename=name,
        saved_path=str(p),
        sha256=sha,
        size_bytes=len(content if isinstance(content, bytes) else content.encode()),
        mime_type="application/octet-stream",
        saved_date="2026-07-09",
        status="normal",
    )
    return p, sha


class TestScanStatus:
    def test_normal_unchanged(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "ok.bin", b"data")
        changes = scan_file_status(tmp_cfg)
        # 初始为 normal，无变化
        assert not any(c["id"] for c in changes if c["new_status"] != "normal") or all(
            c["new_status"] == "normal" for c in changes
        )

    def test_missing_detected(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "gone.bin", b"data")
        # 删除文件
        p.unlink()
        changes = scan_file_status(tmp_cfg)
        missing = [c for c in changes if c["new_status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["original_filename"] == "gone.bin"

    def test_modified_detected(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "changed.bin", b"original")
        # 修改文件内容
        write_bytes(p, b"modified-content")
        changes = scan_file_status(tmp_cfg)
        modified = [c for c in changes if c["new_status"] == "modified"]
        assert len(modified) == 1
        assert modified[0]["original_filename"] == "changed.bin"

    def test_renamed_detected(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "oldname.bin", b"stable-content")
        # 改名（内容不变，hash 不变）
        new_p = p.with_name("newname.bin")
        p.rename(new_p)
        changes = scan_file_status(tmp_cfg)
        renamed = [c for c in changes if c["new_status"] == "renamed"]
        assert len(renamed) == 1
        assert renamed[0]["new_path"] == str(new_p)

    def test_recovered_to_normal(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "rec.bin", b"data")
        # 先删除触发 missing
        p.unlink()
        scan_file_status(tmp_cfg)
        # 再恢复文件
        write_bytes(p, b"data")
        changes = scan_file_status(tmp_cfg)
        recovered = [c for c in changes if c["new_status"] == "normal"]
        assert len(recovered) == 1


class TestListReceivedFilesForDate:
    def test_includes_filesystem_info(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "list.bin", b"hello", file_type="body")
        files = list_received_files_for_date(tmp_cfg, "2026-07-09")
        assert len(files) == 1
        f = files[0]
        assert f["exists_now"] is True
        assert f["size_now"] == 5
        assert f["path_display"] == str(p)

    def test_nonexistent_shown_as_missing_now(self, tmp_cfg):
        p, sha = _make_file_record(tmp_cfg, "ghost.bin", b"x")
        p.unlink()
        files = list_received_files_for_date(tmp_cfg, "2026-07-09")
        assert len(files) == 1
        assert files[0]["exists_now"] is False
        assert files[0]["size_now"] is None
