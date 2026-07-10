"""安全校验模块。

职责：
1. 危险文件扩展名检测（.exe / .bat / .cmd / .ps1 / .sh / .msi 等）。
2. 允许的附件扩展名白名单。
3. 路径越权校验（防止 .. 跳出数据根目录）。
4. 文件大小校验。
"""

from __future__ import annotations

from pathlib import Path

# 危险扩展名：仅给出 warning，不自动执行 / 不删除
DANGEROUS_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".sh", ".msi",
    ".com", ".scr", ".vbs", ".js", ".jar", ".wsf", ".cpl",
}

# 收件允许保存的附件扩展名白名单
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".md", ".txt", ".pdf",
    ".docx", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp",
    ".json", ".csv", ".zip",
}

# 发件允许的扩展名（与附件一致，可放宽到任意非危险类型）
# 这里采用“非危险即允许”的策略


class SecurityError(Exception):
    """安全校验失败（路径越权、扩展名禁止等）。"""


def get_extension(filename: str) -> str:
    """获取小写扩展名（含点），无扩展名返回空串。"""
    return Path(filename).suffix.lower()


def is_dangerous(filename: str) -> bool:
    """判断文件是否为危险扩展名。"""
    return get_extension(filename) in DANGEROUS_EXTENSIONS


def is_attachment_allowed(filename: str) -> bool:
    """判断附件扩展名是否在白名单内。"""
    return get_extension(filename) in ALLOWED_ATTACHMENT_EXTENSIONS


def is_sendable(filename: str) -> bool:
    """判断是否允许发送该文件（非危险扩展名即可）。"""
    return not is_dangerous(filename)


def assert_within_root(path: Path, root: Path) -> None:
    """断言 path 解析后的绝对路径位于 root 之内，防止路径越权。"""
    try:
        path_resolved = Path(path).resolve()
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(f"路径无法解析：{path}") from exc
    root_resolved = Path(root).resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SecurityError(
            f"路径越权：{path_resolved} 不在根目录 {root_resolved} 之内"
        ) from exc


def check_size_ok(size_bytes: int, max_bytes: int) -> bool:
    """判断文件大小是否在限制内。"""
    return 0 <= size_bytes <= max_bytes
