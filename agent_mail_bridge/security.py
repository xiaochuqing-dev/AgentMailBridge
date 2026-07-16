"""安全校验模块。

职责：
1. 危险文件扩展名检测（.exe / .bat / .cmd / .ps1 / .sh / .msi 等）。
2. 允许的附件扩展名白名单。
3. 路径越权校验（防止 .. 跳出数据根目录）。
4. 文件大小校验。
"""

from __future__ import annotations

import os
from pathlib import Path
import re

# 危险扩展名：仅给出 warning，不自动执行 / 不删除
DANGEROUS_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".sh", ".msi",
    ".com", ".scr", ".vbs", ".js", ".jar", ".wsf", ".cpl",
}

SENSITIVE_DELIVERY_FILENAMES = {
    ".env", "credentials.json", "token.json",
}

WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

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


def validate_local_filename(filename: str) -> None:
    """拒绝路径分隔符、Windows 保留名和非法字符。"""
    value = filename.strip().rstrip(". ")
    if not value or value in {".", ".."}:
        raise SecurityError("文件名为空或非法")
    if _INVALID_WINDOWS_CHARS.search(value):
        raise SecurityError(f"文件名包含非法字符：{filename}")
    if Path(value).stem.lower() in WINDOWS_RESERVED_NAMES:
        raise SecurityError(f"Windows 保留文件名不可使用：{filename}")


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


def assert_within_allowed_roots(path: Path, roots: list[Path]) -> Path:
    """验证路径位于任一明确白名单目录并返回解析后的路径。"""
    resolved = Path(path).resolve()
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return resolved
        except ValueError:
            continue
    allowed = "、".join(str(Path(root).resolve()) for root in roots)
    raise SecurityError(f"路径越权：{resolved} 不在允许目录 {allowed} 内")


def validate_agent_workspace_root(
    path: Path | str,
    *,
    sensitive_roots: tuple[Path, ...] = (),
) -> Path:
    """规范化并拒绝过宽、系统级或产品敏感的 Agent 工作区授权。"""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SecurityError("工作区不存在或无法访问") from exc
    if not resolved.is_dir():
        raise SecurityError("工作区必须是目录")
    if resolved == Path(resolved.anchor):
        raise SecurityError("不能授权整个驱动器")

    home = Path.home().resolve()
    if resolved == home:
        raise SecurityError("不能授权整个用户目录")

    protected: list[Path] = []
    appdata_root = home / "AppData"
    if appdata_root.exists():
        protected.append(appdata_root.resolve())
    for name in (
        "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
        "APPDATA", "LOCALAPPDATA",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            try:
                protected.append(Path(value).resolve())
            except OSError:
                continue
    for item in sensitive_roots:
        try:
            protected.append(Path(item).resolve())
        except OSError:
            continue
    for root in protected:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise SecurityError("不能授权系统目录、AppData 或 AgentMailBridge 敏感目录")
    return resolved


def assert_not_sensitive_delivery_file(path: Path | str) -> None:
    """即使工作区已授权，也不允许 Agent 交付常见配置与 OAuth 文件。"""
    candidate = Path(path)
    if candidate.name.casefold() in SENSITIVE_DELIVERY_FILENAMES:
        raise SecurityError("配置或 OAuth 敏感文件禁止通过 Agent 交付")


def check_size_ok(size_bytes: int, max_bytes: int) -> bool:
    """判断文件大小是否在限制内。"""
    return 0 <= size_bytes <= max_bytes
