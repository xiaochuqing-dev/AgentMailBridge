"""本地存储模块。

职责：
1. 创建日期归档目录结构（received / send / sent，每天一个子目录）。
2. 生成带时间戳前缀的文件名，处理重名。
3. 文件复制（send 副本 / sent 副本）。
4. 提供查询今日文件的辅助函数。

目录结构见 README 与需求文档：
    AgentMailBridgeData/
    ├── received/YYYY-MM-DD/[attachments/]
    ├── send/YYYY-MM-DD/
    ├── sent/YYYY-MM-DD/
    └── logs/app.log
"""

from __future__ import annotations

import shutil
import os
import uuid
from datetime import datetime
from pathlib import Path

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.security import assert_within_root
from agent_mail_bridge.utils import (
    fmt_date,
    fmt_time_for_filename,
    sanitize_filename,
    split_ext,
    unique_path,
)


def ensure_data_dirs(cfg: AppConfig) -> None:
    """确保数据根目录及各子目录存在。"""
    cfg.data_root_path.mkdir(parents=True, exist_ok=True)
    cfg.received_dir.mkdir(parents=True, exist_ok=True)
    cfg.send_dir.mkdir(parents=True, exist_ok=True)
    cfg.sent_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)


def received_day_dir(cfg: AppConfig, dt: datetime) -> Path:
    """当天 received 目录：received/YYYY-MM-DD/。"""
    d = cfg.received_dir / fmt_date(dt)
    d.mkdir(parents=True, exist_ok=True)
    return d


def received_attachments_dir(cfg: AppConfig, dt: datetime) -> Path:
    """当天附件目录：received/YYYY-MM-DD/attachments/。"""
    d = received_day_dir(cfg, dt) / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def send_day_dir(cfg: AppConfig, dt: datetime) -> Path:
    """当天 send 目录：send/YYYY-MM-DD/。"""
    d = cfg.send_dir / fmt_date(dt)
    d.mkdir(parents=True, exist_ok=True)
    return d


def sent_day_dir(cfg: AppConfig, dt: datetime) -> Path:
    """当天 sent 目录：sent/YYYY-MM-DD/。"""
    d = cfg.sent_dir / fmt_date(dt)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# 文件名生成
# ============================================================

def build_body_filename(subject: str, dt: datetime) -> str:
    """生成正文文件名：HH-MM-SS_清洗标题.md。"""
    time_prefix = fmt_time_for_filename(dt)
    clean_subject = sanitize_filename(subject if subject else "无标题邮件")
    # 正文文件名长度限制：时间前缀 + 下划线 + 标题
    # 给标题留出合理空间
    return f"{time_prefix}_{clean_subject}.md"


def build_body_path(cfg: AppConfig, subject: str, dt: datetime) -> Path:
    """生成正文文件保存路径（处理重名）。"""
    directory = received_day_dir(cfg, dt)
    filename = build_body_filename(subject, dt)
    stem, ext = split_ext(filename)
    return unique_path(directory, stem, ext)


def build_attachment_filename(original_filename: str, dt: datetime) -> str:
    """生成附件文件名：HH-MM-SS_原名（保留原扩展名）。"""
    time_prefix = fmt_time_for_filename(dt)
    stem, ext = split_ext(original_filename)
    clean_stem = sanitize_filename(stem)
    return f"{time_prefix}_{clean_stem}{ext}"


def build_attachment_path(
    cfg: AppConfig, original_filename: str, dt: datetime
) -> Path:
    """生成附件保存路径（处理重名）。"""
    directory = received_attachments_dir(cfg, dt)
    filename = build_attachment_filename(original_filename, dt)
    stem, ext = split_ext(filename)
    return unique_path(directory, stem, ext)


def build_send_copy_filename(source_path: Path | str, dt: datetime) -> str:
    """生成发送副本文件名：HH-MM-SS_原名（保留扩展名）。"""
    source_path = Path(source_path)
    time_prefix = fmt_time_for_filename(dt)
    stem, ext = split_ext(source_path.name)
    clean_stem = sanitize_filename(stem)
    return f"{time_prefix}_{clean_stem}{ext}"


def build_send_copy_path(
    cfg: AppConfig, source_path: Path | str, dt: datetime
) -> Path:
    """生成 send 副本路径（处理重名）。"""
    directory = send_day_dir(cfg, dt)
    filename = build_send_copy_filename(source_path, dt)
    stem, ext = split_ext(filename)
    return unique_path(directory, stem, ext)


def build_sent_copy_path(
    cfg: AppConfig, source_path: Path | str, dt: datetime
) -> Path:
    """生成 sent 副本路径（处理重名）。

    为避免与 send 副本同名导致混淆，sent 副本使用相同命名规则但位于 sent 目录。
    """
    directory = sent_day_dir(cfg, dt)
    filename = build_send_copy_filename(source_path, dt)
    stem, ext = split_ext(filename)
    return unique_path(directory, stem, ext)


# ============================================================
# 文件写入 / 复制
# ============================================================

def write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """写入文本文件，自动创建父目录。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)


def write_bytes(path: Path, data: bytes) -> None:
    """写入二进制文件，自动创建父目录。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def copy_file(src: Path | str, dst: Path | str) -> Path:
    """复制文件（覆盖目标）。返回目标路径。"""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 校验目标位于数据根目录内（防御性）
    # 注：这里不做强校验，因为调用方已控制路径；越权校验在 security 层
    shutil.copy2(src, dst)
    return dst


def atomic_copy_file(src: Path | str, dst: Path | str) -> Path:
    """在目标目录内复制到临时文件后原子替换，避免暴露半成品。"""
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 临时名与用户文件名解耦，避免长 Unicode 文件名叠加 UUID 后触发
    # 未启用 Win32 长路径策略的 MAX_PATH 限制。
    temporary = target.parent / f".amb-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target


# ============================================================
# 今日文件查询（为后续 GUI 预留）
# ============================================================

def list_files_in_day_dir(day_dir: Path) -> list[Path]:
    """列出某天目录下的文件（不含 attachments 子目录里的文件，按名称排序）。

    返回该目录直接子文件，attachments 子目录单独通过
    list_files_in_attachments 处理。
    """
    day_dir = Path(day_dir)
    if not day_dir.exists():
        return []
    return sorted(
        p for p in day_dir.iterdir() if p.is_file()
    )


def list_attachments(day_dir: Path) -> list[Path]:
    """列出某天 attachments 子目录下的附件文件。"""
    day_dir = Path(day_dir)
    att_dir = day_dir / "attachments"
    if not att_dir.exists():
        return []
    return sorted(
        p for p in att_dir.iterdir() if p.is_file()
    )


def assert_path_safe(path: Path, root: Path) -> None:
    """对外暴露的路径越权校验（封装 security 模块）。"""
    assert_within_root(path, root)
