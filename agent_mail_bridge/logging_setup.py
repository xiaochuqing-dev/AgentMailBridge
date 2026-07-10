"""日志配置模块。

职责：
1. 配置文件日志（写入 DATA_ROOT/logs/app.log）。
2. 配置控制台日志输出。
3. 提供统一的 logger 获取入口。
4. 日志中绝不打印完整密码 / 授权码。

后续 GUI 可直接读取 app.log 展示最近日志。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 防止重复初始化
_initialized = False

# 日志格式
_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    logs_dir: Path,
    level: str = "INFO",
    *,
    force: bool = False,
) -> logging.Logger:
    """初始化全局日志。

    Args:
        logs_dir: 日志目录，app.log 将写入其中。
        level: 日志级别字符串，如 INFO / DEBUG。
        force: 是否强制重新初始化（测试场景使用）。

    Returns:
        项目根 logger（名为 agent_mail_bridge）。
    """
    global _initialized
    root_logger = logging.getLogger("agent_mail_bridge")

    if _initialized and not force:
        return root_logger

    # 重置已有 handler，避免重复输出
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    # 文件 handler：滚动，单文件 2MB，保留 5 个
    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(numeric_level)

    root_logger.setLevel(numeric_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    root_logger.propagate = False

    _initialized = True
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """获取子 logger，命名空间统一为 agent_mail_bridge.xxx。"""
    if not name.startswith("agent_mail_bridge"):
        name = f"agent_mail_bridge.{name}"
    return logging.getLogger(name)
