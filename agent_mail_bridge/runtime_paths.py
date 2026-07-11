"""源码与 Windows frozen 安装版共用的运行时路径。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APP_DIR_NAME = "AgentMailBridge"


@dataclass(frozen=True)
class RuntimePaths:
    """明确区分只读程序文件与当前用户的持久化可写文件。"""

    frozen: bool
    source_root: Path
    install_root: Path
    resource_root: Path
    user_root: Path
    user_config_root: Path
    oauth_root: Path
    data_root: Path
    cache_root: Path
    temp_root: Path
    config_file: Path

    def ensure_user_directories(self) -> None:
        for path in (
            self.user_config_root,
            self.oauth_root,
            self.data_root,
            self.cache_root,
            self.temp_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def discover_runtime_paths(
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    bundle_root: str | Path | None = None,
    module_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimePaths:
    """发现路径；显式参数便于隔离测试 source/frozen 行为。"""

    env = os.environ if environ is None else environ
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    module = Path(module_file or __file__).resolve()
    source_root = module.parent.parent
    exe = Path(executable or sys.executable).resolve()
    install_root = exe.parent if is_frozen else source_root

    if is_frozen:
        bundle = Path(
            bundle_root or getattr(sys, "_MEIPASS", install_root / "_internal")
        ).resolve()
        resource_candidates = (
            bundle / "agent_mail_bridge" / "resources",
            bundle / "resources",
            install_root / "_internal" / "agent_mail_bridge" / "resources",
        )
        resource_root = next(
            (path for path in resource_candidates if path.exists()),
            resource_candidates[0],
        )
    else:
        resource_root = source_root / "agent_mail_bridge" / "resources"

    home_override = env.get("AGENT_MAIL_BRIDGE_HOME", "").strip()
    if home_override:
        user_root = Path(home_override).expanduser().resolve()
    else:
        local_app_data = env.get("LOCALAPPDATA", "").strip()
        local_root = (
            Path(local_app_data).expanduser()
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
        user_root = (local_root / APP_DIR_NAME).resolve()

    user_config_root = user_root / "Config"
    oauth_root = user_root / "OAuth"
    data_root = user_root / "Data"
    cache_root = user_root / "Cache"
    temp_root = user_root / "Temp"
    config_file = user_config_root / ".env" if is_frozen else source_root / ".env"
    config_override = env.get("AGENT_MAIL_BRIDGE_CONFIG", "").strip()
    if config_override:
        config_file = Path(config_override).expanduser().resolve()

    return RuntimePaths(
        frozen=is_frozen,
        source_root=source_root,
        install_root=install_root,
        resource_root=resource_root,
        user_root=user_root,
        user_config_root=user_config_root,
        oauth_root=oauth_root,
        data_root=data_root,
        cache_root=cache_root,
        temp_root=temp_root,
        config_file=config_file,
    )


def get_runtime_paths() -> RuntimePaths:
    """返回当前进程的实时路径视图。"""

    return discover_runtime_paths()
