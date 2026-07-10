"""命令行入口模块。

支持命令：
    init                 初始化目录与数据库
    receive              收取 Gmail 邮件（IMAP / Gmail API，按 GMAIL_RECEIVE_BACKEND）
    send --file PATH     发送本地文件到 OWNER_GMAIL
    list-received        列出某天收到的文件
    list-sent            列出某天发送的文件
    scan-status          扫描文件删除/修改/改名状态
    show-config          显示当前配置（脱敏）
    diagnose-gmail       诊断 Gmail IMAP 连接（分步骤输出）
    diagnose-gmail-api   诊断 Gmail API 收件后端
    diagnose-network     诊断整体网络环境（Python/PySocks/direct/socks5/QQ SMTP）
    gmail-api-auth       Gmail API OAuth 授权（首次浏览器授权 / token 刷新）
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import (
    ConfigError,
    load_config,
    require_receive_config,
    require_send_config,
)
from agent_mail_bridge.database import (
    init_db,
    log_event,
    query_sent_files_by_date,
    query_received_messages_by_date,
)
from agent_mail_bridge.file_index import (
    list_received_files_for_date,
    scan_file_status,
)
from agent_mail_bridge.logging_setup import get_logger, setup_logging
from agent_mail_bridge.mail_receive import receive_mails
from agent_mail_bridge.mail_send import send_file_to_owner_gmail
from agent_mail_bridge.storage import ensure_data_dirs
from agent_mail_bridge.utils import fmt_date


def _setup(cfg) -> None:
    """初始化日志 + 数据目录 + 数据库（命令前置步骤）。"""
    ensure_data_dirs(cfg)
    init_db(cfg.db_path)
    setup_logging(cfg.logs_dir, cfg.log_level)


def _resolve_date(date_str: str | None) -> str:
    """解析 --date 参数：today / yesterday / YYYY-MM-DD。"""
    if not date_str or date_str.lower() == "today":
        return fmt_date(datetime.now())
    if date_str.lower() == "yesterday":
        from datetime import timedelta
        return fmt_date(datetime.now() - timedelta(days=1))
    return date_str


# ============================================================
# 各子命令
# ============================================================

def cmd_init(args, cfg) -> int:
    _setup(cfg)
    logger = get_logger("cli")
    logger.info("初始化完成：%s", cfg.data_root_path)
    log_event(cfg.db_path, "SUCCESS", "config",
              f"初始化目录与数据库：{cfg.data_root_path}")
    print(f"[OK] 已初始化数据目录：{cfg.data_root_path}")
    print(f"     - received/: {cfg.received_dir}")
    print(f"     - send/:     {cfg.send_dir}")
    print(f"     - sent/:     {cfg.sent_dir}")
    print(f"     - logs/:     {cfg.logs_dir}")
    print(f"     - db:        {cfg.db_path}")
    return 0


def cmd_receive(args, cfg) -> int:
    _setup(cfg)
    try:
        require_receive_config(cfg)
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    result = receive_mails(
        cfg,
        limit=args.limit,
        unseen_only=args.unseen_only,
        mark_seen=args.mark_seen,
    )
    backend = result.get("backend", "?")
    print(f"收件后端：{backend}")
    print(f"扫描邮件数：{result['fetched']}")
    print(f"新保存邮件：{result['saved']}")
    print(f"跳过(已记录)：{result['skipped']}")
    print(f"保存附件数：{result['attachments']}")
    if result["errors"]:
        print("错误：", file=sys.stderr)
        for e in result["errors"]:
            print(f"  - {e}", file=sys.stderr)
        return 1 if not result["ok"] else 0
    return 0


def cmd_send(args, cfg) -> int:
    _setup(cfg)
    try:
        require_send_config(cfg)
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    result = send_file_to_owner_gmail(args.file, subject=args.subject, cfg=cfg)
    if result["ok"]:
        print("[OK] 发送成功")
        print(f"  主题:    {result['subject']}")
        print(f"  源文件:  {result['source_path']}")
        print(f"  send副本:{result['send_copy_path']}")
        print(f"  sent副本:{result['sent_copy_path']}")
        print(f"  收件人:  {result['to']}")
        print(f"  发送时间:{result['sent_at']}")
        return 0
    else:
        print(f"[ERROR] 发送失败：{result['error']}", file=sys.stderr)
        return 1


def cmd_list_received(args, cfg) -> int:
    _setup(cfg)
    date_str = _resolve_date(args.date)
    files = list_received_files_for_date(cfg, date_str)
    print(f"=== {date_str} 收到的文件（共 {len(files)} 个）===")
    if not files:
        print("（无）")
        return 0
    print()
    for i, f in enumerate(files, 1):
        status = f["status"]
        kind = f["file_type"]
        name = f["saved_filename"]
        path = f["path_display"]
        size = f["size_now"]
        size_str = _human_size(size) if size is not None else "—"
        exists = "存在" if f["exists_now"] else "缺失"
        print(f"{i:>2}. [{kind}] {name}")
        print(f"     路径:   {path}")
        print(f"     大小:   {size_str}   文件{exists}   状态: {status}")
        print(f"     (复制路径: {path})")
        print()
    return 0


def cmd_list_sent(args, cfg) -> int:
    _setup(cfg)
    date_str = _resolve_date(args.date)
    rows = query_sent_files_by_date(cfg.db_path, date_str)
    print(f"=== {date_str} 发送的文件（共 {len(rows)} 个）===")
    if not rows:
        print("（无）")
        return 0
    print()
    for i, r in enumerate(rows, 1):
        print(f"{i:>2}. {r['subject']}")
        print(f"     状态:   {r['status']}")
        print(f"     源文件: {r['source_path']}")
        print(f"     sent副本:{r['sent_copy_path'] or '—'}")
        print(f"     收件人: {r['to_email']}")
        print(f"     发送时间:{r['sent_at'] or '—'}")
        if r["error_message"]:
            print(f"     错误:   {r['error_message']}")
        print()
    return 0


def cmd_scan_status(args, cfg) -> int:
    _setup(cfg)
    changes = scan_file_status(cfg)
    print(f"=== 文件状态扫描完成（变化 {len(changes)} 处）===")
    if not changes:
        print("所有文件状态正常。")
        return 0
    print()
    for c in changes:
        print(f"- {c['original_filename']}: {c['old_status']} -> {c['new_status']}")
        if c.get("new_path"):
            print(f"    新路径: {c['new_path']}")
    return 0


def cmd_show_config(args, cfg) -> int:
    _setup(cfg)
    print("=== 当前配置（脱敏）===")
    for k, v in cfg.mask().items():
        print(f"  {k}: {v}")
    return 0


def cmd_diagnose_gmail(args, cfg) -> int:
    # 诊断命令不写数据库、不建目录，只做网络诊断输出
    from agent_mail_bridge.diagnose import run_diagnose_gmail
    return run_diagnose_gmail(cfg)


def cmd_diagnose_network(args, cfg) -> int:
    from agent_mail_bridge.diagnose import run_diagnose_network
    return run_diagnose_network(cfg)


def cmd_gmail_api_auth(args, cfg) -> int:
    """Gmail API OAuth 授权命令。"""
    from agent_mail_bridge.gmail_api_auth import (
        CredentialsNotFoundError,
        GmailApiAuthError,
        get_gmail_api_service,
    )
    _setup(cfg)
    logger = get_logger("cli")

    print("[AgentMailBridge] Gmail API 授权")
    print()
    print(f"  GMAIL_API_CREDENTIALS_PATH = {cfg.gmail_api_credentials_path}")
    print(f"  GMAIL_API_TOKEN_PATH       = {cfg.gmail_api_token_path}")
    print(f"  GMAIL_API_SCOPES           = {cfg.gmail_api_scopes_str}")
    print()

    try:
        service = get_gmail_api_service(cfg)
    except CredentialsNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except GmailApiAuthError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # 验证 service 可用：获取 profile
    try:
        profile = service.users().getProfile(userId="me").execute()
        email_addr = profile.get("emailAddress", "(未知)")
        print(f"[OK] Gmail API 授权成功")
        print(f"     授权账号：{email_addr}")
        print(f"     token 已保存到：{cfg.gmail_api_token_path}")
        log_event(cfg.db_path, "SUCCESS", "config",
                  f"Gmail API 授权成功：{email_addr}")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gmail API profile 获取失败")
        print(f"[WARN] 授权已完成，但 profile 获取失败：{exc}", file=sys.stderr)
        print(f"       token 已保存到：{cfg.gmail_api_token_path}", file=sys.stderr)
        print("       可运行 diagnose-gmail-api 进一步排查。", file=sys.stderr)
        return 1


def cmd_diagnose_gmail_api(args, cfg) -> int:
    """Gmail API 诊断命令。"""
    from agent_mail_bridge.diagnose import run_diagnose_gmail_api
    return run_diagnose_gmail_api(cfg)


# ============================================================
# 工具
# ============================================================

def _human_size(size: int | None) -> str:
    if size is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0  # type: ignore[assignment]
    return f"{size:.1f} TB"


# ============================================================
# 主入口
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_mail_bridge",
        description="Agent Mail Bridge - 面向 AI Agent 的本地邮箱桥接工具（第一阶段 CLI）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="初始化目录与数据库")

    # receive
    p_recv = sub.add_parser("receive", help="收取 Gmail 邮件")
    p_recv.add_argument("--limit", type=int, default=None, help="单次最多抓取数量")
    p_recv.add_argument("--unseen-only", action="store_true", help="只收未读邮件")
    p_recv.add_argument("--mark-seen", action="store_true", help="收取后标记已读")

    # send
    p_send = sub.add_parser("send", help="发送本地文件到 OWNER_GMAIL")
    p_send.add_argument("--file", required=True, help="待发送的本地文件路径")
    p_send.add_argument("--subject", default=None, help="邮件主题")

    # list-received
    p_lr = sub.add_parser("list-received", help="列出某天收到的文件")
    p_lr.add_argument("--date", default="today", help="today / yesterday / YYYY-MM-DD")

    # list-sent
    p_ls = sub.add_parser("list-sent", help="列出某天发送的文件")
    p_ls.add_argument("--date", default="today", help="today / yesterday / YYYY-MM-DD")

    # scan-status
    sub.add_parser("scan-status", help="扫描文件删除/修改/改名状态")

    # show-config
    sub.add_parser("show-config", help="显示当前配置（脱敏）")

    # diagnose-gmail
    sub.add_parser("diagnose-gmail", help="诊断 Gmail IMAP 连接（分步骤输出）")

    # diagnose-gmail-api
    sub.add_parser("diagnose-gmail-api", help="诊断 Gmail API 收件后端")

    # gmail-api-auth
    sub.add_parser("gmail-api-auth", help="Gmail API OAuth 授权（首次浏览器授权 / token 刷新）")

    # diagnose-network
    sub.add_parser("diagnose-network", help="诊断整体网络环境")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config()
    # init 命令不需要预先 setup（它本身就是做 setup）
    if args.command == "init":
        return cmd_init(args, cfg)

    # diagnose-* 命令只做诊断，不写数据库、不建目录
    if args.command == "diagnose-gmail":
        return cmd_diagnose_gmail(args, cfg)
    if args.command == "diagnose-network":
        return cmd_diagnose_network(args, cfg)
    if args.command == "diagnose-gmail-api":
        return cmd_diagnose_gmail_api(args, cfg)

    _setup(cfg)

    handlers = {
        "receive": cmd_receive,
        "send": cmd_send,
        "list-received": cmd_list_received,
        "list-sent": cmd_list_sent,
        "scan-status": cmd_scan_status,
        "show-config": cmd_show_config,
        "gmail-api-auth": cmd_gmail_api_auth,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args, cfg)
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
