"""隔离的大数据量与多周期稳定性基准。"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import tracemalloc
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.database import close_connection, get_connection


def _handle_count() -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    kernel32.GetProcessHandleCount.restype = ctypes.c_bool
    count = ctypes.c_uint32()
    process = kernel32.GetCurrentProcess()
    if kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
        return int(count.value)
    return None


def _working_set_bytes() -> int | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), ctypes.c_uint32
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
    process = kernel32.GetCurrentProcess()
    if psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    ):
        return int(counters.WorkingSetSize)
    return None


def _timed(operation, cycles: int = 1) -> dict[str, float]:
    samples = []
    for _ in range(cycles):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "average_ms": round(sum(samples) / len(samples), 3),
        "maximum_ms": round(max(samples), 3),
    }


def _seed_database(cfg: AppConfig, records: int) -> None:
    connection = get_connection(cfg.db_path)
    now = "2026-07-11 12:00:00"
    with connection:
        connection.executemany(
            """
            INSERT INTO received_messages
                (message_id, subject, from_email, to_email, received_at, saved_date,
                 status, created_at, updated_at, backend)
            VALUES (?, ?, 'test@gmail.com', 'test@gmail.com', ?, '2026-07-11',
                    'saved', ?, ?, 'gmail_api')
            """,
            ((f"<perf-{index}@test>", f"性能邮件 {index}", now, now, now) for index in range(records)),
        )
        connection.executemany(
            """
            INSERT INTO sent_files
                (request_id, source_path, original_filename, size_bytes, source_origin,
                 subject, sent_at, status, created_at, updated_at)
            VALUES (?, '', ?, 100, 'controlled', ?, ?, 'sent', ?, ?)
            """,
            (
                (f"perf-send-{index}", f"result-{index}.txt", f"结果 {index}", now, now, now)
                for index in range(records // 2)
            ),
        )
        connection.executemany(
            """
            INSERT INTO mcp_calls
                (request_id, file_path, title, status, created_at, updated_at)
            VALUES (?, '', ?, 'sent', ?, ?)
            """,
            ((f"perf-mcp-{index}", f"MCP {index}", now, now) for index in range(records)),
        )
        connection.executemany(
            """
            INSERT INTO app_events (level, event_type, message, created_at)
            VALUES ('INFO', 'performance', ?, ?)
            """,
            ((f"性能日志 {index}", now) for index in range(records)),
        )


def _create_representative_files(cfg: AppConfig, count: int = 200) -> None:
    folder = cfg.received_dir / "2026-07-11"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (folder / f"sample-{index:04d}.txt").write_text("sample", encoding="utf-8")


def _exercise_log_rotation(log_dir: Path) -> dict[str, Any]:
    logger = logging.getLogger(f"agent_mail_bridge.performance.{time.time_ns()}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_dir / "rotation-test.log",
        maxBytes=32 * 1024,  # 基准轮转阈值：32 KB
        backupCount=3,
        encoding="utf-8",
    )
    logger.addHandler(handler)
    try:
        for index in range(800):
            logger.info("rotation %d %s", index, "x" * 100)
    finally:
        handler.close()
        logger.removeHandler(handler)
    files = list(log_dir.glob("rotation-test.log*"))
    return {"files": len(files), "rotated": len(files) > 1}


def _close_project_log_handlers() -> None:
    """释放 Windows 文件句柄，确保隔离临时目录可清理。"""
    logger = logging.getLogger("agent_mail_bridge")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def run_stability_benchmark(
    *, records: int = 10_000, cycles: int = 50, output: str | Path
) -> dict[str, Any]:
    """在临时目录生成数据并输出 JSON，不触碰真实配置和用户文件。"""
    if records < 1_000 or records > 100_000:
        raise ValueError("记录数必须在 1000 到 100000 之间")
    if cycles < 5 or cycles > 1_000:
        raise ValueError("周期数必须在 5 到 1000 之间")
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tracemalloc.start()
    memory_before = _working_set_bytes()
    threads_before = threading.active_count()
    handles_before = _handle_count()
    with tempfile.TemporaryDirectory(prefix="agent-mail-bridge-performance-") as temporary:
        cfg = AppConfig(
            gmail_address="test@gmail.com",
            qq_email="test@qq.com",
            owner_gmail="test@gmail.com",
            data_root=Path(temporary) / "data",
        )
        service = ApplicationService(cfg)
        startup = _timed(service.initialize)
        seed = _timed(lambda: _seed_database(cfg, records))
        _create_representative_files(cfg)
        query_metrics = {
            "history": _timed(lambda: service.get_history(100), cycles),
            "logs": _timed(lambda: service.get_recent_logs(100), cycles),
            "mcp": _timed(lambda: service.get_mcp_history(100), cycles),
            "today_files": _timed(service.get_today_files, cycles),
            "refresh_bundle": _timed(
                lambda: (
                    service.get_config_and_connection_status(),
                    service.get_today_files(),
                    service.get_recent_logs(100),
                    service.get_history(100),
                    service.get_mcp_history(100),
                ),
                cycles,
            ),
        }
        connection = get_connection(cfg.db_path)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        query_plan = [
            row[3] for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM received_messages "
                "WHERE saved_date = ? ORDER BY received_at ASC",
                ("2026-07-11",),
            ).fetchall()
        ]
        rotation = _exercise_log_rotation(cfg.logs_dir)
        database_size = cfg.db_path.stat().st_size
        close_connection()
        _close_project_log_handlers()
    current_traced, peak_traced = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report = {
        "records": {
            "received": records,
            "sent": records // 2,
            "mcp": records,
            "events": records,
            "representative_files": 200,
        },
        "cycles": cycles,
        "startup": startup,
        "seed": seed,
        "queries": query_metrics,
        "resources": {
            "working_set_before": memory_before,
            "working_set_after": _working_set_bytes(),
            "traced_current_bytes": current_traced,
            "traced_peak_bytes": peak_traced,
            "threads_before": threads_before,
            "threads_after": threading.active_count(),
            "handles_before": handles_before,
            "handles_after": _handle_count(),
        },
        "database": {
            "size_bytes": database_size,
            "integrity_check": integrity,
            "date_query_plan": query_plan,
        },
        "log_rotation": rotation,
        "isolated": True,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
