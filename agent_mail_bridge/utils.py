"""通用工具函数。

职责：
1. 邮件标题 / 文件名清洗（跨平台安全、长度截断、去重）。
2. 时间格式化。
3. sha256 计算。
4. 邮件头解码（中文标题等）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path

# Windows / macOS / Linux 均不适合作文件名的字符
_INVALID_FILENAME_CHARS = r'<>:"/\\|?*\x00'
# 文件名最大长度（含扩展名），超出截断
MAX_FILENAME_LEN = 80


def now_local() -> datetime:
    """当前本地时间。"""
    return datetime.now()


def fmt_datetime(dt: datetime) -> str:
    """格式化为 YYYY-MM-DD HH:MM:SS。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_date(dt: datetime) -> str:
    """格式化为 YYYY-MM-DD（用于日期文件夹）。"""
    return dt.strftime("%Y-%m-%d")


def fmt_time_for_filename(dt: datetime) -> str:
    """格式化为 HH-MM-SS（用于文件名前缀）。"""
    return dt.strftime("%H-%M-%S")


def sanitize_filename(name: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """清洗文件名（不含路径）。

    规则：
    1. 去掉 Windows/macOS/Linux 不适合的字符。
    2. 去掉首尾空白和点。
    3. 折叠连续空白为单个空格。
    4. 标题过长时截断到 max_len。
    5. 空字符串回退为 "untitled"。
    """
    if name is None:
        return "untitled"
    # 1. 替换非法字符为空格
    cleaned = name
    for ch in _INVALID_FILENAME_CHARS:
        cleaned = cleaned.replace(ch, " ")
    # 2. 控制字符也替换
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
    # 3. 折叠连续空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 4. 去掉首尾的点（Windows 下不允许）
    cleaned = cleaned.strip(". ")
    # 5. 截断
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    if not cleaned:
        cleaned = "untitled"
    return cleaned


def split_ext(filename: str) -> tuple[str, str]:
    """分离文件名与扩展名（扩展名含点，小写）。

    Returns:
        (stem, ext) 例如 ("报告", ".pdf")；无扩展名时 ext 为 ""。
    """
    p = Path(filename)
    ext = p.suffix.lower()
    stem = p.stem if ext else filename
    return stem, ext


def unique_path(directory: Path, stem: str, ext: str) -> Path:
    """在 directory 下生成不重名的路径。

    若 stem+ext 已存在，则追加 _1 / _2 ... 直至不冲突。
    ext 应含前导点（如 ".md"），或为空字符串。
    """
    directory = Path(directory)
    candidate = directory / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        candidate = directory / f"{stem}_{idx}{ext}"
        if not candidate.exists():
            return candidate
        idx += 1


def decode_mime_header(value: str | None) -> str:
    """解码 MIME 编码的邮件头（处理中文等非 ASCII 标题）。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        # 解码失败时尽量保留原文
        return value


def sha256_of_file(path: Path | str, chunk_size: int = 64 * 1024) -> str:
    """计算文件 sha256，分块读取避免大文件占用内存。"""
    path = Path(path)
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    """计算字节串的 sha256。"""
    return hashlib.sha256(data).hexdigest()
