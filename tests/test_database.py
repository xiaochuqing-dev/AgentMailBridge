"""测试数据库模块（建表、增查、去重）。"""

from agent_mail_bridge.database import (
    init_db,
    insert_received_message,
    message_id_exists,
    update_received_message_status,
    query_received_messages_by_date,
    insert_received_file,
    query_received_files_by_date,
    query_received_files_by_message,
    update_received_file_status,
    query_all_received_files,
    insert_sent_file,
    query_sent_files_by_date,
    log_event,
    query_recent_events,
)


class TestSchema:
    def test_init_creates_db(self, tmp_cfg):
        assert tmp_cfg.db_path.exists()

    def test_reinit_idempotent(self, tmp_cfg):
        # 再次初始化不应报错
        init_db(tmp_cfg.db_path)


class TestReceivedMessages:
    def test_insert_and_query(self, tmp_cfg):
        msg_id = "<abc@example.com>"
        rid = insert_received_message(
            tmp_cfg.db_path,
            message_id=msg_id,
            gmail_uid="123",
            subject="测试主题",
            from_email="test@gmail.com",
            to_email="test@gmail.com",
            received_at="2026-07-09 01:31:22",
            saved_date="2026-07-09",
            body_file_path="/tmp/body.md",
            body_sha256="abc123",
            has_attachments=False,
        )
        assert rid is not None
        assert message_id_exists(tmp_cfg.db_path, msg_id)

    def test_dedup_same_message_id(self, tmp_cfg):
        msg_id = "<dup@example.com>"
        rid1 = insert_received_message(
            tmp_cfg.db_path,
            message_id=msg_id,
            gmail_uid="1", subject="s", from_email="a@b.com",
            to_email="a@b.com", received_at="2026-07-09 00:00:00",
            saved_date="2026-07-09", body_file_path=None,
            body_sha256=None, has_attachments=False,
        )
        rid2 = insert_received_message(
            tmp_cfg.db_path,
            message_id=msg_id,
            gmail_uid="1", subject="s", from_email="a@b.com",
            to_email="a@b.com", received_at="2026-07-09 00:00:00",
            saved_date="2026-07-09", body_file_path=None,
            body_sha256=None, has_attachments=False,
        )
        assert rid1 is not None
        assert rid2 is None  # 第二次被忽略

    def test_update_status(self, tmp_cfg):
        msg_id = "<status@example.com>"
        insert_received_message(
            tmp_cfg.db_path,
            message_id=msg_id, gmail_uid="1", subject="s",
            from_email="a@b.com", to_email="a@b.com",
            received_at="2026-07-09 00:00:00", saved_date="2026-07-09",
            body_file_path=None, body_sha256=None, has_attachments=False,
        )
        update_received_message_status(tmp_cfg.db_path, msg_id, "file_missing")
        rows = query_received_messages_by_date(tmp_cfg.db_path, "2026-07-09")
        assert len(rows) == 1
        assert rows[0]["status"] == "file_missing"


class TestReceivedFiles:
    def test_insert_and_query_by_date(self, tmp_cfg):
        fid = insert_received_file(
            tmp_cfg.db_path,
            message_id="<f@example.com>", file_type="body",
            original_filename="主题", saved_filename="01-00-00_主题.md",
            saved_path="/tmp/01-00-00_主题.md", sha256="hash1",
            size_bytes=100, mime_type="text/markdown",
            saved_date="2026-07-09", status="normal",
        )
        assert fid is not None
        rows = query_received_files_by_date(tmp_cfg.db_path, "2026-07-09")
        assert len(rows) == 1
        assert rows[0]["sha256"] == "hash1"

    def test_query_by_message(self, tmp_cfg):
        msg_id = "<m@example.com>"
        insert_received_file(
            tmp_cfg.db_path, message_id=msg_id, file_type="body",
            original_filename="b", saved_filename="b.md",
            saved_path="/b.md", sha256="h", size_bytes=1,
            mime_type="text/markdown", saved_date="2026-07-09",
        )
        insert_received_file(
            tmp_cfg.db_path, message_id=msg_id, file_type="attachment",
            original_filename="a.png", saved_filename="a.png",
            saved_path="/a.png", sha256="h2", size_bytes=2,
            mime_type="image/png", saved_date="2026-07-09",
        )
        rows = query_received_files_by_message(tmp_cfg.db_path, msg_id)
        assert len(rows) == 2

    def test_update_file_status(self, tmp_cfg):
        fid = insert_received_file(
            tmp_cfg.db_path, message_id="<x@example.com>", file_type="body",
            original_filename="b", saved_filename="b.md",
            saved_path="/b.md", sha256="h", size_bytes=1,
            mime_type="text/markdown", saved_date="2026-07-09",
        )
        update_received_file_status(tmp_cfg.db_path, fid, "modified")
        rows = query_all_received_files(tmp_cfg.db_path)
        assert rows[0]["status"] == "modified"


class TestSentFiles:
    def test_insert_and_query(self, tmp_cfg):
        sid = insert_sent_file(
            tmp_cfg.db_path,
            source_path="/tmp/r.md", send_copy_path="/send/r.md",
            sent_copy_path="/sent/r.md", sha256="hash",
            subject="结果", from_email="q@qq.com", to_email="o@gmail.com",
            sent_at="2026-07-09 22:30:15", status="sent",
        )
        assert sid is not None
        rows = query_sent_files_by_date(tmp_cfg.db_path, "2026-07-09")
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"

    def test_query_no_match(self, tmp_cfg):
        rows = query_sent_files_by_date(tmp_cfg.db_path, "2026-01-01")
        assert rows == []


class TestAppEvents:
    def test_log_and_query(self, tmp_cfg):
        log_event(tmp_cfg.db_path, "INFO", "receive", "开始收取")
        log_event(tmp_cfg.db_path, "SUCCESS", "receive", "完成")
        rows = query_recent_events(tmp_cfg.db_path, limit=10)
        assert len(rows) == 2
        # 反转为时间正序，第一条是最早的
        assert rows[0]["message"] == "开始收取"
        assert rows[1]["message"] == "完成"
