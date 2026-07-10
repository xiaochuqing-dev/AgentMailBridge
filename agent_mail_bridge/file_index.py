"""文件索引与状态扫描模块。

职责：
1. 计算文件 sha256（委托 utils）。
2. scan_file_status()：检查 received_files 表中记录的文件是否被
   删除 / 修改 / 改名，并更新 status 字段。
3. 提供“列出今日收到文件”的高层封装，便于 GUI / CLI 使用。

MVP 规则（见需求第十节）：
    文件不存在   -> missing
    文件 hash 改变 -> modified
    同目录下 hash 相同但文件名不同 -> renamed（并更新路径）
不要自动删除数据库记录，不要覆盖用户改过的文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import (
    query_all_received_files,
    query_received_files_by_date,
    update_received_file_status,
)
from agent_mail_bridge.security import SecurityError, assert_within_root
from agent_mail_bridge.utils import sha256_of_file


def list_received_files_for_date(
    cfg: AppConfig, date_str: str
) -> list[dict[str, Any]]:
    """列出某天收到的文件（含正文与附件），附带文件系统实时信息。

    返回的每条记录在数据库字段基础上追加：
        exists_now (bool)       文件当前是否存在
        size_now (int|None)     当前大小
        path_display (str)      便于展示 / 复制的绝对路径
    """
    rows = query_received_files_by_date(cfg.db_path, date_str)
    result: list[dict[str, Any]] = []
    for row in rows:
        p = Path(row["saved_path"]) if row["saved_path"] else None
        if p is not None:
            try:
                assert_within_root(p, cfg.data_root_path)
            except SecurityError:
                item = dict(row)
                item.update({
                    "exists_now": False,
                    "size_now": None,
                    "path_display": "",
                    "status": "unsafe_path",
                })
                result.append(item)
                continue
        exists_now = p is not None and p.exists()
        size_now = p.stat().st_size if exists_now and p else None
        item = dict(row)
        item["exists_now"] = exists_now
        item["size_now"] = size_now
        item["path_display"] = str(p) if p else ""
        result.append(item)
    return result


def scan_file_status(cfg: AppConfig) -> list[dict[str, Any]]:
    """扫描所有 received_files，检测 missing / modified / renamed。

    流程：
    1. 取出所有记录。
    2. 若 saved_path 不存在：
       a. 在同目录下找 hash 相同的文件 -> renamed，更新路径为找到的文件。
       b. 找不到 -> missing。
    3. 若存在但 hash 变化 -> modified。
    4. 其余恢复为 normal（之前曾被标 missing 但文件又出现的情形）。

    返回发生状态变化的记录摘要列表。
    """
    rows = query_all_received_files(cfg.db_path)
    changes: list[dict[str, Any]] = []

    for row in rows:
        file_id = row["id"]
        recorded_path_str = row["saved_path"]
        recorded_sha = row["sha256"]
        old_status = row["status"]

        if not recorded_path_str:
            # 没有路径记录，跳过
            continue

        recorded_path = Path(recorded_path_str)
        try:
            assert_within_root(recorded_path, cfg.data_root_path)
        except SecurityError:
            if old_status != "unsafe_path":
                update_received_file_status(cfg.db_path, file_id, "unsafe_path")
                changes.append({
                    "id": file_id,
                    "original_filename": row["original_filename"],
                    "old_status": old_status,
                    "new_status": "unsafe_path",
                    "new_path": None,
                })
            continue

        # --- 文件不存在 ---
        if not recorded_path.exists():
            # 尝试在同目录按 hash 找到改名后的文件
            renamed_path = _find_by_hash_in_dir(recorded_path.parent, recorded_sha)
            if renamed_path is not None:
                update_received_file_status(
                    cfg.db_path, file_id, "renamed", saved_path=str(renamed_path)
                )
                changes.append({
                    "id": file_id,
                    "original_filename": row["original_filename"],
                    "old_status": old_status,
                    "new_status": "renamed",
                    "new_path": str(renamed_path),
                })
            else:
                if old_status != "missing":
                    update_received_file_status(cfg.db_path, file_id, "missing")
                    changes.append({
                        "id": file_id,
                        "original_filename": row["original_filename"],
                        "old_status": old_status,
                        "new_status": "missing",
                        "new_path": None,
                    })
            continue

        # --- 文件存在，校验 hash ---
        if recorded_sha:
            try:
                current_sha = sha256_of_file(recorded_path)
            except OSError:
                # 读取失败，标 missing
                if old_status != "missing":
                    update_received_file_status(cfg.db_path, file_id, "missing")
                    changes.append({
                        "id": file_id,
                        "original_filename": row["original_filename"],
                        "old_status": old_status,
                        "new_status": "missing",
                        "new_path": None,
                    })
                continue

            if current_sha != recorded_sha:
                if old_status != "modified":
                    update_received_file_status(cfg.db_path, file_id, "modified")
                    changes.append({
                        "id": file_id,
                        "original_filename": row["original_filename"],
                        "old_status": old_status,
                        "new_status": "modified",
                        "new_path": str(recorded_path),
                    })
                continue

        # --- 文件正常且 hash 一致 ---
        if old_status not in (
            "normal", "renamed", "allowed", "dangerous", "unknown_type"
        ):
            update_received_file_status(cfg.db_path, file_id, "normal")
            changes.append({
                "id": file_id,
                "original_filename": row["original_filename"],
                "old_status": old_status,
                "new_status": "normal",
                "new_path": str(recorded_path),
            })

    return changes


def _find_by_hash_in_dir(directory: Path, expected_sha: str | None) -> Path | None:
    """在 directory 下查找 sha256 等于 expected_sha 的文件。"""
    if not expected_sha:
        return None
    directory = Path(directory)
    if not directory.exists() or not directory.is_dir():
        return None
    for p in directory.iterdir():
        if not p.is_file():
            continue
        try:
            if sha256_of_file(p) == expected_sha:
                return p
        except OSError:
            continue
    return None
