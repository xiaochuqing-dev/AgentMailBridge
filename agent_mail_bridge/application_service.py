"""CLI、GUI 与未来 MCP 共用的应用服务入口。"""

from __future__ import annotations

import csv
import sqlite3
import smtplib
import ssl
import platform
import os
import re
import subprocess
import sys
import threading
import uuid
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agent_mail_bridge.config import (
    AppConfig,
    ConfigError,
    _effective_receive_backend,
    effective_incoming_runtime,
    effective_outgoing_runtime,
    require_receive_config,
)
from agent_mail_bridge.credentials import (
    ACCOUNT_IMAP_SECRET,
    ACCOUNT_SMTP_SECRET,
    CredentialError,
    CredentialService,
    GMAIL_IMAP_SECRET,
    MemoryCredentialBackend,
    QQ_SMTP_SECRET,
)
from agent_mail_bridge.database import (
    account_owned_fact_counts,
    agent_integration_migration_needed,
    app_event_overview,
    close_connection,
    clear_all_app_events,
    clear_daily_check_events,
    configure_app_event_retention,
    count_receive_retries,
    create_mail_account as create_mail_account_record,
    delete_agent_config_backup,
    get_auto_receive_state,
    get_history_import_run,
    get_mail_account as query_mail_account,
    get_mail_package as query_raw_mail_package,
    get_agent_config_backup,
    init_db,
    insert_mcp_call,
    insert_mcp_audit_event,
    insert_agent_config_backup,
    legacy_archive_backfill_needed,
    multi_account_migration_needed,
    outbound_mail_migration_needed,
    v17_mail_flow_migration_needed,
    v171_consistency_migration_needed,
    v13_mail_migration_needed,
    log_event,
    get_outbound_message as query_outbound_message,
    query_recent_mcp_calls,
    query_recent_mcp_audit_events,
    query_agent_config_backups,
    query_agent_client_permissions,
    query_history_import_runs,
    query_recent_outbound_messages,
    query_recent_events,
    query_app_events,
    query_recent_received_messages,
    query_recent_sent_files,
    query_trusted_domains,
    query_sent_files_by_date,
    remove_mail_account as remove_mail_account_record,
    update_mcp_call,
    update_mcp_staging,
    update_agent_client,
    update_agent_config_backup,
    upsert_history_import_run,
    save_auto_receive_state,
    delete_trusted_domain,
    upsert_trusted_domain,
    prune_app_events,
    query_mail_accounts,
    query_mailboxes,
    sync_mail_accounts,
    update_mail_account as update_mail_account_record,
    upsert_mailboxes,
)
from agent_mail_bridge.agent_integration import (
    AgentAccessError,
    AgentIdentity,
    AgentIntegrationService,
    new_client_token,
)
from agent_mail_bridge.file_index import list_received_files_for_date, scan_file_status
from agent_mail_bridge.gmail_api_auth import get_oauth_state
from agent_mail_bridge.logging_setup import get_logger, setup_logging
from agent_mail_bridge.mail_receive import historical_rescan_mails, receive_mails
from agent_mail_bridge.mail_archive import (
    backfill_legacy_mail_packages,
    backfill_mail_contact_facts,
)
from agent_mail_bridge.mail_facts import (
    get_mail_message as query_mail_message,
    get_mail_thread as query_mail_thread,
    list_mail_messages as query_mail_messages,
    list_mail_resources as query_mail_resources,
    list_mail_threads as query_mail_threads,
    search_mail_facts as query_mail_facts,
)
from agent_mail_bridge.mail_send import send_file_with_request, send_outbound_mail
from agent_mail_bridge.outbound_mail import (
    OutboundMailError,
    execute_agent_send,
    prepare_agent_send,
    recover_agent_send_archive,
)
from agent_mail_bridge.send_requests import (
    SendRequestError,
    cancel_send_request as cancel_pending_send_request,
    expire_due_send_requests,
    expire_send_request_if_due,
    get_send_request as query_send_request,
    list_send_requests as query_send_requests,
    public_send_request,
)
from agent_mail_bridge.send_recovery import (
    list_recovery_requests as query_recovery_requests,
    mark_send_request_resolution,
    reconcile_send_request_locally,
    recover_incomplete_send_request,
    recover_incomplete_send_requests,
)
from agent_mail_bridge.mail_accounts import (
    MailAccount,
    current_receive_account_id,
    legacy_accounts_from_config,
    normalize_email_address,
    stable_account_id,
    stable_mailbox_id,
)
from agent_mail_bridge.provider_adapters import (
    get_provider_adapter,
    list_provider_adapters,
)
from agent_mail_bridge.account_runtime import (
    AccountRuntimeError,
    AccountRuntimeRouter,
)
from agent_mail_bridge.provider_foundation import (
    ProviderFoundationError,
    detect_provider_profile,
    discover_imap_mailboxes,
    test_smtp_connection,
    validate_non_secret_provider_settings,
    validate_server_settings,
)
from agent_mail_bridge.mail_resource_access import (
    MAX_TEXT_CHARS,
    MailAccessError,
    enrich_resource_descriptor,
    prepare_mail_resources as prepare_resources_to_workspace,
    read_mail_resource as read_archived_mail_resource,
    workspace_dtos,
)
from agent_mail_bridge.mailbox_sync import selected_sync_mailboxes
from agent_mail_bridge.send_permissions import query_extended_scopes
from agent_mail_bridge.process_lock import ProcessLock, is_lock_available
from agent_mail_bridge.maintenance import (
    create_database_backup,
    data_statistics,
    export_maintenance_report,
    list_database_backups,
    restore_database_backup,
    scan_consistency,
    verify_database_backup,
)
from agent_mail_bridge.models import OperationStatus, ReceiveResult, SendResult, ServiceResult
from agent_mail_bridge.managed_files import get_managed_files as query_managed_files
from agent_mail_bridge.security import (
    SecurityError,
    assert_within_allowed_roots,
    assert_within_root,
    assert_not_sensitive_delivery_file,
    check_size_ok,
    is_dangerous,
    validate_agent_workspace_root,
)
from agent_mail_bridge.storage import (
    atomic_copy_file,
    atomic_write_text,
    ensure_data_dirs,
)
from agent_mail_bridge.trusted_downloads import normalize_trusted_domain
from agent_mail_bridge.utils import fmt_date, sanitize_filename, sha256_of_bytes, sha256_of_file
from agent_mail_bridge.mcp_client_config import (
    CLIENT_ID_ENV,
    CLIENT_TOKEN_ENV,
    ClientConfigError,
    ConfigPlan,
    apply_client_config,
    codex_mcp_toml,
    detect_client,
    generic_mcp_json,
    hermes_mcp_yaml,
    mcp_launch,
    preview_client_config,
    restore_client_config,
)
from agent_mail_bridge.health_service import get_mail_health
from agent_mail_bridge.consistency_repair import (
    ConsistencyRepairError,
    begin_consistency_repair,
    finish_consistency_repair,
    list_consistency_repair_candidates,
    mark_consistency_repair_backup,
)
from agent_mail_bridge.retention_cleanup import (
    cleanup_send_snapshots,
    preview_send_snapshot_cleanup,
    recover_snapshot_cleanup_transactions,
)


logger = get_logger("application_service")
AGENT_CONFIG_BACKUP_MAX_COUNT = 20
AGENT_CONFIG_BACKUP_MAX_AGE_DAYS = 90

LOG_EVENT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "收件": ("receive", "receive_auto", "auto_receive"),
    "发件": ("send", "sent", "outbound"),
    "Agent / MCP": ("mcp", "agent"),
    "配置": ("config", "oauth", "credential"),
    "数据库与文件": ("db", "database", "file", "maintenance", "log_maintenance"),
    "系统与诊断": ("system", "diagnostic", "network", "startup"),
}


class _McpStagingHandled(Exception):
    """携带已产品化的 staging 拒绝结果，避免落入内部错误。"""

    def __init__(self, result: SendResult):
        super().__init__(result.message)
        self.result = result


class ApplicationService:
    """本地单用户应用的稳定业务入口。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._account_receive_locks: dict[str, threading.Lock] = {}
        self._account_receive_locks_guard = threading.Lock()
        self._setup_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._event_maintenance_lock = threading.Lock()
        self._last_event_maintenance_at: datetime | None = None
        self._last_event_maintenance_result: dict[str, int] = {}
        self._startup_recovery_done = False
        self._startup_recovery_result: dict[str, Any] = {}
        self._ready = False
        if os.getenv("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE") == "1":
            self._credentials = CredentialService(
                MemoryCredentialBackend(
                    {
                        GMAIL_IMAP_SECRET: cfg.gmail_app_password,
                        QQ_SMTP_SECRET: cfg.qq_auth_code,
                    }
                )
            )
        else:
            self._credentials = CredentialService()
        self._account_router = AccountRuntimeRouter(cfg, self._credentials)
        self._agent_integration = AgentIntegrationService(cfg, self._credentials)
        self._agent_config_plans: dict[str, ConfigPlan] = {}

    def initialize(self) -> ServiceResult:
        """初始化安全目录、数据库和日志。"""
        initialized_now = False
        receive_rule_migration_error = ""
        with self._setup_lock:
            if not self._ready:
                try:
                    from agent_mail_bridge.ui.settings_store import (
                        persist_receive_rule_migration,
                    )

                    persist_receive_rule_migration(self.cfg)
                except OSError as exc:
                    # 原子保存失败会保留旧文件；本次仍使用已验证的新语义。
                    receive_rule_migration_error = str(exc)
                ensure_data_dirs(self.cfg)
                archive_migration_needed = legacy_archive_backfill_needed(
                    self.cfg.db_path
                )
                account_migration_needed = multi_account_migration_needed(
                    self.cfg.db_path
                )
                agent_migration_needed = agent_integration_migration_needed(
                    self.cfg.db_path
                )
                outbound_migration_needed = outbound_mail_migration_needed(
                    self.cfg.db_path
                )
                v17_migration_needed = v17_mail_flow_migration_needed(
                    self.cfg.db_path
                )
                v171_migration_needed = v171_consistency_migration_needed(
                    self.cfg.db_path
                )
                v13_migration_needed = v13_mail_migration_needed(self.cfg.db_path)
                if (
                    archive_migration_needed
                    or outbound_migration_needed
                    or v13_migration_needed
                    or account_migration_needed
                    or agent_migration_needed
                    or v17_migration_needed
                    or v171_migration_needed
                ):
                    create_database_backup(
                        self.cfg,
                        label=(
                            "before_v1_7_1_consistency"
                            if v171_migration_needed
                            else "before_v1_6_agent_ecosystem"
                            if agent_migration_needed
                            else "before_v1_7_mail_flow"
                            if v17_migration_needed
                            else "before_v1_4_multi_account"
                            if account_migration_needed
                            else "before_v1_3_models"
                            if v13_migration_needed
                            else "before_mail_models"
                        ),
                    )
                init_db(
                    self.cfg.db_path,
                    legacy_accounts=legacy_accounts_from_config(self.cfg),
                )
                self._agent_integration.ensure_legacy_submit_scopes()
                if v17_migration_needed:
                    self._agent_integration.seed_v17_legacy_mailbox_scopes()
                if archive_migration_needed:
                    migration = backfill_legacy_mail_packages(self.cfg)
                    if migration.get("failed"):
                        log_event(
                            self.cfg.db_path, "WARNING", "db",
                            f"旧邮件归档迁移部分完成：成功 {migration.get('migrated', 0)}，失败 {migration.get('failed', 0)}",
                        )
                if v13_migration_needed:
                    contacts = backfill_mail_contact_facts(self.cfg)
                    if contacts.get("failed"):
                        log_event(
                            self.cfg.db_path,
                            "WARNING",
                            "db",
                            "旧邮件联系人事实回填部分完成："
                            f"成功 {contacts.get('migrated', 0)}，失败 {contacts.get('failed', 0)}",
                        )
                setup_logging(self.cfg.logs_dir, self.cfg.log_level)
                configure_app_event_retention(
                    self.cfg.db_path, max_count=self.cfg.app_event_max_count
                )
                self._ready = True
                initialized_now = True
                if receive_rule_migration_error:
                    log_event(
                        self.cfg.db_path,
                        "WARNING",
                        "config",
                        "收件规则迁移标记保存失败，旧配置文件已保留",
                    )
        if initialized_now:
            cleanup_recovery = recover_snapshot_cleanup_transactions(self.cfg)
            if cleanup_recovery.get("unresolved"):
                log_event(
                    self.cfg.db_path,
                    "WARNING",
                    "snapshot_cleanup_recovery",
                    "发件快照清理恢复仍有待处理项；未删除永久邮件事实",
                )
            self._run_startup_send_recovery()
            self.schedule_event_maintenance(force=True)
        return ServiceResult(
            OperationStatus.PARTIAL if receive_rule_migration_error else OperationStatus.SUCCESS,
            error_code=(
                "receive_rule_migration_save_failed"
                if receive_rule_migration_error
                else None
            ),
            message=(
                "初始化完成；收件规则迁移标记暂未保存"
                if receive_rule_migration_error
                else "初始化完成"
            ),
        )

    def _run_startup_send_recovery(self) -> None:
        """一次性执行轻量恢复；只恢复归档和对账，绝不调用 SMTP。"""
        if self._startup_recovery_done:
            return
        self._startup_recovery_done = True
        result = recover_incomplete_send_requests(self.cfg.db_path)
        recovered = 0
        failed = 0
        for request_id in result.get("archive_requests", []):
            request = query_send_request(self.cfg.db_path, str(request_id))
            if request is None:
                continue
            try:
                context = self._account_router.context(
                    str(request["sender_account_id"]), capability="send"
                )
                recover_agent_send_archive(
                    self.cfg,
                    runtime_cfg=context.config,
                    send_request_id=str(request_id),
                )
                recovered += 1
            except Exception as exc:  # noqa: BLE001 - 保留材料并等待用户处理
                failed += 1
                log_event(
                    self.cfg.db_path,
                    "WARNING",
                    "send_recovery",
                    "发件归档启动恢复未完成；恢复材料已保留，"
                    f"错误类型={type(exc).__name__}",
                )
        self._startup_recovery_result = {
            **result,
            "archive_recovered": recovered,
            "archive_failed": failed,
        }

    def get_credential_status(self) -> ServiceResult:
        """只返回已配置状态，不返回任何秘密值。"""
        try:
            states = self._credentials.status()
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_store_unavailable",
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "gmail_imap": states[GMAIL_IMAP_SECRET],
                "qq_smtp": states[QQ_SMTP_SECRET],
            },
        )

    def set_credential(self, name: str, value: str) -> ServiceResult:
        """保存或更新凭据，并同步当前进程配置。"""
        if name not in {GMAIL_IMAP_SECRET, QQ_SMTP_SECRET}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_credential_name",
                message="不支持的凭据类型",
            )
        if not value.strip():
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="empty_credential",
                message="凭据不能为空",
            )
        try:
            self._credentials.set(name, value.strip())
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_write_failed",
                message=str(exc),
            )
        if name == GMAIL_IMAP_SECRET:
            self.cfg.gmail_app_password = value.strip()
        else:
            self.cfg.qq_auth_code = value.strip()
        return ServiceResult(OperationStatus.SUCCESS, message="凭据已保存到 Windows 安全存储")

    def delete_credential(self, name: str) -> ServiceResult:
        """删除指定凭据，不影响其他配置。"""
        if name not in {GMAIL_IMAP_SECRET, QQ_SMTP_SECRET}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_credential_name",
                message="不支持的凭据类型",
            )
        try:
            self._credentials.delete(name)
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_delete_failed",
                message=str(exc),
            )
        if name == GMAIL_IMAP_SECRET:
            self.cfg.gmail_app_password = ""
        else:
            self.cfg.qq_auth_code = ""
        return ServiceResult(OperationStatus.SUCCESS, message="凭据已删除")

    def migrate_legacy_credentials(self, env_path: Path | None = None) -> ServiceResult:
        """迁移旧 .env 明文；失败项保留原值。"""
        if os.getenv("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE") == "1":
            return ServiceResult(
                OperationStatus.CANCELLED,
                error_code="credential_store_disabled",
                message="测试环境已禁用真实凭据存储",
            )
        from agent_mail_bridge.runtime_paths import get_runtime_paths
        try:
            result = CredentialService().migrate_env(
                env_path or get_runtime_paths().source_root / ".env"
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_migration_failed",
                message=f"旧凭据迁移失败：{exc}",
            )
        status = OperationStatus.PARTIAL if result.failed else OperationStatus.SUCCESS
        return ServiceResult(
            status,
            message=f"已迁移 {len(result.migrated)} 项，失败 {len(result.failed)} 项",
            details={
                "migrated": result.migrated,
                "skipped": result.skipped,
                "failed_keys": list(result.failed),
            },
        )

    def receive(
        self,
        *,
        account_id: str | None = None,
        limit: int | None = None,
        unseen_only: bool | None = None,
        mark_seen: bool | None = None,
        automatic: bool = False,
        wait_for_process_lock: float = 0.0,
    ) -> ReceiveResult:
        """执行一次线程/进程双重互斥收件，普通收件绝不启动浏览器授权。"""
        self.initialize()
        target_account_id = account_id or current_receive_account_id(self.cfg)
        try:
            runtime_cfg = self._account_router.context(
                target_account_id, capability="receive"
            ).config
        except AccountRuntimeError as exc:
            if account_id is None and exc.error_code == "account_not_found":
                runtime_cfg = self.cfg
            else:
                return ReceiveResult(
                    OperationStatus.FAILED,
                    backend="",
                    error_code=exc.error_code,
                    message=str(exc),
                )
        except CredentialError as exc:
            return ReceiveResult(
                OperationStatus.FAILED,
                backend="",
                error_code="credential_read_failed",
                message=str(exc),
            )
        backend = _effective_receive_backend(runtime_cfg)
        if limit is not None and limit <= 0:
            return ReceiveResult(
                OperationStatus.FAILED, backend=backend,
                error_code="invalid_limit", message="收件数量必须大于 0",
            )
        with self._account_receive_locks_guard:
            receive_lock = self._account_receive_locks.setdefault(
                target_account_id, threading.Lock()
            )
        if not receive_lock.acquire(blocking=False):
            return ReceiveResult(
                OperationStatus.CANCELLED, backend=backend,
                error_code="receive_busy", message="已有收件任务正在运行",
            )
        legacy_process_lock = (
            ProcessLock(
                runtime_cfg.data_root_path / ".locks" / "receive.lock"
            )
            if account_id is None
            else None
        )
        lock_timeout = max(0.0, float(wait_for_process_lock))
        if legacy_process_lock is not None and not legacy_process_lock.acquire(
            timeout=lock_timeout
        ):
            receive_lock.release()
            return ReceiveResult(
                OperationStatus.CANCELLED,
                backend=backend,
                error_code="sync_in_progress",
                message="其他进程正在同步邮件",
            )
        process_lock = ProcessLock(
            runtime_cfg.data_root_path
            / ".locks"
            / f"receive-{target_account_id}.lock"
        )
        if not process_lock.acquire(timeout=lock_timeout):
            if legacy_process_lock is not None:
                legacy_process_lock.release()
            receive_lock.release()
            return ReceiveResult(
                OperationStatus.CANCELLED, backend=backend,
                error_code="sync_in_progress", message="其他进程正在同步邮件",
            )
        try:
            if backend == "gmail_api":
                oauth = get_oauth_state(runtime_cfg)
                if oauth["state"] not in {"READY", "TOKEN_EXPIRED_REFRESHABLE"}:
                    return ReceiveResult(
                        OperationStatus.AUTH_REQUIRED,
                        backend=backend,
                        error_code=oauth["state"].lower(),
                        message=oauth["message"],
                        needs_auth=True,
                    )
            try:
                require_receive_config(runtime_cfg)
                raw = receive_mails(
                    runtime_cfg, limit=limit,
                    unseen_only=unseen_only, mark_seen=mark_seen,
                    automatic=automatic,
                )
            except ConfigError as exc:
                return ReceiveResult(
                    OperationStatus.FAILED, backend=backend,
                    error_code="config_error", message=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                return ReceiveResult(
                    OperationStatus.FAILED, backend=backend,
                    error_code="receive_failed", message=str(exc), failed=1,
                    errors=[str(exc)],
                )
            failures = int(raw.get("failed", len(raw.get("errors", []))))
            saved = int(raw.get("saved", 0))
            errors = list(raw.get("errors", []))
            retry_counts = count_receive_retries(
                runtime_cfg.db_path,
                account_id=target_account_id,
            )
            if raw.get("global_error") or not raw.get("ok", True):
                status = OperationStatus.FAILED
            elif failures:
                status = OperationStatus.PARTIAL
            elif saved == 0:
                status = OperationStatus.NO_CHANGES
            else:
                status = OperationStatus.SUCCESS
            return ReceiveResult(
                status,
                backend=backend,
                scanned=int(raw.get("fetched", 0)),
                accepted=int(raw.get("accepted", saved)),
                saved=saved,
                skipped=int(raw.get("skipped", 0)),
                duplicates=int(raw.get("duplicates", 0)),
                failed=failures,
                attachments=int(raw.get("attachments", 0)),
                saved_files=list(raw.get("saved_files", [])),
                errors=errors,
                pending_retries=int(raw.get("pending_retries", retry_counts["pending"])),
                needs_attention=int(raw.get("needs_attention", retry_counts["needs_attention"])),
                error_code=(
                    "partial_receive" if status == OperationStatus.PARTIAL
                    else str(raw.get("error_code") or "")
                    or (_classify_receive_error(errors[0]) if errors else None)
                ),
                message=(
                    "收件完成" if status == OperationStatus.SUCCESS
                    else "检查完成，暂时没有新邮件" if status == OperationStatus.NO_CHANGES
                    else errors[0] if errors else "收件存在错误"
                ),
            )
        finally:
            process_lock.release()
            if legacy_process_lock is not None:
                legacy_process_lock.release()
            receive_lock.release()

    def historical_rescan(
        self,
        *,
        account_id: str | None = None,
        mailbox_ids: list[str] | None = None,
        date_from: datetime | str,
        date_to: datetime | str,
        apply_receive_rule: bool = True,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        page_size: int = 100,
        scan_cap: int = 5000,
    ) -> ReceiveResult:
        """显式历史补扫；范围、锁、取消和统计均独立于普通增量收件。"""
        self.initialize()
        target_account_id = account_id or current_receive_account_id(self.cfg)
        try:
            runtime_cfg = self._account_router.context(
                target_account_id, capability="receive"
            ).config
        except AccountRuntimeError as exc:
            if account_id is None and exc.error_code == "account_not_found":
                runtime_cfg = self.cfg
            else:
                return ReceiveResult(
                    OperationStatus.FAILED,
                    backend="",
                    error_code=exc.error_code,
                    message=str(exc),
                )
        except CredentialError as exc:
            return ReceiveResult(
                OperationStatus.FAILED,
                backend="",
                error_code="credential_read_failed",
                message=str(exc),
            )
        backend = _effective_receive_backend(runtime_cfg)
        try:
            start = _coerce_rescan_datetime(date_from, end_of_day=False)
            end = _coerce_rescan_datetime(date_to, end_of_day=True)
            if start > end:
                raise ValueError("历史补扫开始时间不能晚于结束时间")
            if end - start > timedelta(days=366):
                raise ValueError("单次历史补扫范围不能超过 366 天")
            if int(page_size) <= 0 or int(page_size) > 500:
                raise ValueError("历史补扫 page_size 必须在 1 到 500 之间")
            if int(scan_cap) <= 0 or int(scan_cap) > 10_000:
                raise ValueError("历史补扫 scan_cap 必须在 1 到 10000 之间")
        except (TypeError, ValueError) as exc:
            return ReceiveResult(
                OperationStatus.FAILED,
                backend=backend,
                error_code="invalid_history_range",
                message=str(exc),
            )
        with self._account_receive_locks_guard:
            receive_lock = self._account_receive_locks.setdefault(
                target_account_id, threading.Lock()
            )
        if not receive_lock.acquire(blocking=False):
            return ReceiveResult(
                OperationStatus.CANCELLED,
                backend=backend,
                error_code="receive_busy",
                message="已有收件或历史补扫任务正在运行",
                cancelled=True,
            )
        legacy_process_lock = (
            ProcessLock(
                runtime_cfg.data_root_path / ".locks" / "receive.lock"
            )
            if account_id is None
            else None
        )
        if legacy_process_lock is not None and not legacy_process_lock.acquire(
            timeout=0.0
        ):
            receive_lock.release()
            return ReceiveResult(
                OperationStatus.CANCELLED,
                backend=backend,
                error_code="sync_in_progress",
                message="其他进程正在同步邮件",
                cancelled=True,
            )
        process_lock = ProcessLock(
            runtime_cfg.data_root_path
            / ".locks"
            / f"receive-{target_account_id}.lock"
        )
        if not process_lock.acquire(timeout=0.0):
            if legacy_process_lock is not None:
                legacy_process_lock.release()
            receive_lock.release()
            return ReceiveResult(
                OperationStatus.CANCELLED,
                backend=backend,
                error_code="sync_in_progress",
                message="其他进程正在同步邮件",
                cancelled=True,
            )
        try:
            if backend == "gmail_api":
                oauth = get_oauth_state(runtime_cfg)
                if oauth["state"] not in {"READY", "TOKEN_EXPIRED_REFRESHABLE"}:
                    return ReceiveResult(
                        OperationStatus.AUTH_REQUIRED,
                        backend=backend,
                        error_code=oauth["state"].lower(),
                        message=oauth["message"],
                        needs_auth=True,
                    )
            try:
                require_receive_config(runtime_cfg)
                raw = historical_rescan_mails(
                    runtime_cfg,
                    date_from=start,
                    date_to=end,
                    mailbox_ids=mailbox_ids,
                    apply_receive_rule=bool(apply_receive_rule),
                    cancel_check=(cancel_event.is_set if cancel_event else None),
                    progress_callback=progress_callback,
                    page_size=int(page_size),
                    scan_cap=int(scan_cap),
                )
            except ConfigError as exc:
                return ReceiveResult(
                    OperationStatus.FAILED,
                    backend=backend,
                    error_code="config_error",
                    message=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                return ReceiveResult(
                    OperationStatus.FAILED,
                    backend=backend,
                    error_code="history_rescan_failed",
                    message=str(exc),
                    failed=1,
                    errors=[str(exc)],
                )
            failed = int(raw.get("failed") or 0)
            saved = int(raw.get("saved") or 0)
            cancelled = bool(raw.get("cancelled"))
            truncated = bool(raw.get("truncated"))
            if raw.get("global_error") or not raw.get("ok", True):
                status = OperationStatus.FAILED
            elif cancelled:
                status = OperationStatus.CANCELLED
            elif failed or truncated:
                status = OperationStatus.PARTIAL
            elif saved == 0:
                status = OperationStatus.NO_CHANGES
            else:
                status = OperationStatus.SUCCESS
            message = (
                "历史补扫已取消，已完成的结果已保留"
                if cancelled
                else "历史补扫达到安全扫描上限，已保留当前结果"
                if truncated
                else f"历史补扫完成：新增 {saved} 封，重复 {int(raw.get('duplicates') or 0)} 封"
            )
            return ReceiveResult(
                status,
                backend=backend,
                scanned=int(raw.get("fetched") or 0),
                matched=int(raw.get("matched") or 0),
                accepted=int(raw.get("accepted") or saved),
                saved=saved,
                skipped=int(raw.get("skipped") or 0),
                rule_skipped=int(raw.get("rule_skipped") or 0),
                duplicates=int(raw.get("duplicates") or 0),
                failed=failed,
                attachments=int(raw.get("attachments") or 0),
                saved_files=list(raw.get("saved_files") or []),
                errors=list(raw.get("errors") or []),
                pending_retries=int(raw.get("pending_retries") or 0),
                needs_attention=int(raw.get("needs_attention") or 0),
                cancelled=cancelled,
                truncated=truncated,
                scan_id=str(raw.get("scan_id") or ""),
                error_code=(
                    "history_rescan_cancelled" if cancelled
                    else "history_rescan_truncated" if truncated
                    else "partial_history_rescan" if failed
                    else None
                ),
                message=message,
            )
        finally:
            process_lock.release()
            if legacy_process_lock is not None:
                legacy_process_lock.release()
            receive_lock.release()

    def import_historical_mails(
        self,
        *,
        account_id: str | None = None,
        mailbox_ids: list[str] | None = None,
        preset: str = "30d",
        date_from: datetime | str | None = None,
        date_to: datetime | str | None = None,
        apply_receive_rule: bool = True,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        page_size: int = 100,
        scan_cap: int = 5000,
        run_id: str | None = None,
        resume_from_segment: int = 1,
        initial_facts: dict[str, int] | None = None,
        persisted_preset: str | None = None,
    ) -> ReceiveResult:
        """产品化历史导入：持久化状态，并把长范围拆为独立有限段。"""
        self.initialize()
        normalized_preset = str(preset or "custom").strip().casefold()
        allowed_presets = {"30d", "1y", "2024", "all", "custom"}
        if normalized_preset not in allowed_presets:
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="invalid_history_range",
                message="历史导入范围预设无效",
            )
        storage_preset = str(persisted_preset or normalized_preset).strip().casefold()
        if storage_preset not in allowed_presets:
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="invalid_history_range",
                message="历史导入范围预设无效",
            )
        now = datetime.now().replace(microsecond=0)
        try:
            if normalized_preset == "30d":
                start, end = now - timedelta(days=30), now
            elif normalized_preset == "1y":
                start, end = now - timedelta(days=365), now
            elif normalized_preset == "2024":
                start, end = datetime(2024, 1, 1), datetime(
                    2024, 12, 31, 23, 59, 59
                )
            elif normalized_preset == "all":
                start, end = datetime(1970, 1, 1), now
            else:
                if date_from is None or date_to is None:
                    raise ValueError("自定义历史导入需要开始和结束日期")
                start = _coerce_rescan_datetime(date_from, end_of_day=False)
                end = _coerce_rescan_datetime(date_to, end_of_day=True)
            if start > end:
                raise ValueError("历史导入开始时间不能晚于结束时间")
        except (TypeError, ValueError) as exc:
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="invalid_history_range",
                message=str(exc),
            )

        target_account_id = account_id or current_receive_account_id(self.cfg)
        if not target_account_id:
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="没有可用于历史导入的收件账号",
            )
        account = query_mail_account(self.cfg.db_path, target_account_id)
        if (
            account is None
            or not account.get("enabled")
            or not account.get("receive_enabled")
        ):
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="历史导入账号不存在、已停用或未启用收件",
            )
        normalized_mailbox_ids = sorted(
            {
                str(mailbox_id).strip()
                for mailbox_id in (mailbox_ids or ())
                if str(mailbox_id).strip()
            }
        )
        try:
            selected_sync_mailboxes(
                self.cfg.db_path,
                target_account_id,
                mailbox_ids=normalized_mailbox_ids or None,
            )
        except ValueError as exc:
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="invalid_mailbox_scope",
                message=str(exc),
            )
        segments: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor <= end:
            segment_end = min(
                end,
                cursor + timedelta(days=91, hours=23, minutes=59, seconds=59),
            )
            segments.append((cursor, segment_end))
            cursor = segment_end + timedelta(seconds=1)
        stable_run_id = run_id or ("history_" + uuid.uuid4().hex)
        start_segment_index = max(
            1, min(int(resume_from_segment or 1), len(segments) + 1)
        )
        prior = dict(initial_facts or {})
        aggregate = {
            key: int(prior.get(key) or 0)
            for key in (
                "scanned",
                "matched",
                "saved",
                "duplicates",
                "skipped",
                "rule_skipped",
                "failed",
                "attachments",
            )
        }
        saved_files: list[str] = []
        errors: list[str] = []
        backend = ""
        cancelled = False
        truncated = False
        hard_failure: ReceiveResult | None = None
        retry_from_segment: int | None = None
        upsert_history_import_run(
            self.cfg.db_path,
            run_id=stable_run_id,
            account_id=target_account_id,
            preset=storage_preset,
            date_from=start.isoformat(timespec="seconds"),
            date_to=end.isoformat(timespec="seconds"),
            mailbox_ids=normalized_mailbox_ids,
            apply_receive_rule=apply_receive_rule,
            status="running",
            **aggregate,
            segment_index=max(0, start_segment_index - 1),
            total_segments=len(segments),
            next_segment_index=start_segment_index,
            message="历史邮件导入中",
        )

        current_segment_index = max(0, start_segment_index - 1)
        next_segment_index = start_segment_index
        for index, (segment_start, segment_end) in enumerate(
            segments[start_segment_index - 1 :],
            start_segment_index,
        ):
            current_segment_index = index
            if cancel_event and cancel_event.is_set():
                cancelled = True
                next_segment_index = index
                break

            def publish(payload: dict[str, Any], *, segment_index: int = index) -> None:
                current = {
                    "fetched": aggregate["scanned"] + int(payload.get("fetched") or 0),
                    "matched": aggregate["matched"] + int(payload.get("matched") or 0),
                    "saved": aggregate["saved"] + int(payload.get("saved") or 0),
                    "duplicates": aggregate["duplicates"] + int(payload.get("duplicates") or 0),
                    "skipped": aggregate["skipped"] + int(payload.get("skipped") or 0),
                    "rule_skipped": aggregate["rule_skipped"] + int(payload.get("rule_skipped") or 0),
                    "failed": aggregate["failed"] + int(payload.get("failed") or 0),
                    "segment_index": segment_index,
                    "total_segments": len(segments),
                }
                upsert_history_import_run(
                    self.cfg.db_path,
                    run_id=stable_run_id,
                    account_id=target_account_id,
                    preset=storage_preset,
                    date_from=start.isoformat(timespec="seconds"),
                    date_to=end.isoformat(timespec="seconds"),
                    mailbox_ids=normalized_mailbox_ids,
                    apply_receive_rule=apply_receive_rule,
                    status="running",
                    scanned=current["fetched"],
                    matched=current["matched"],
                    saved=current["saved"],
                    duplicates=current["duplicates"],
                    rule_skipped=current["rule_skipped"],
                    failed=current["failed"],
                    segment_index=segment_index,
                    total_segments=len(segments),
                    next_segment_index=segment_index,
                    message=f"正在导入第 {segment_index}/{len(segments)} 段",
                )
                if progress_callback:
                    progress_callback(current)

            result = self.historical_rescan(
                account_id=target_account_id,
                mailbox_ids=normalized_mailbox_ids or None,
                date_from=segment_start,
                date_to=segment_end,
                apply_receive_rule=apply_receive_rule,
                cancel_event=cancel_event,
                progress_callback=publish,
                page_size=page_size,
                scan_cap=scan_cap,
            )
            backend = backend or result.backend
            for key, attribute in (
                ("scanned", "scanned"),
                ("matched", "matched"),
                ("saved", "saved"),
                ("duplicates", "duplicates"),
                ("skipped", "skipped"),
                ("rule_skipped", "rule_skipped"),
                ("failed", "failed"),
                ("attachments", "attachments"),
            ):
                aggregate[key] += int(getattr(result, attribute) or 0)
            saved_files.extend(result.saved_files)
            errors.extend(result.errors)
            cancelled = cancelled or result.cancelled
            truncated = truncated or result.truncated
            if (
                result.cancelled
                or result.truncated
                or result.failed
                or result.status
                in {
                    OperationStatus.FAILED,
                    OperationStatus.AUTH_REQUIRED,
                    OperationStatus.PARTIAL,
                }
            ):
                retry_from_segment = (
                    index
                    if retry_from_segment is None
                    else min(retry_from_segment, index)
                )
            next_segment_index = retry_from_segment or (index + 1)
            if result.status in {
                OperationStatus.FAILED,
                OperationStatus.AUTH_REQUIRED,
            }:
                hard_failure = result
                break
            if cancelled or truncated:
                break

        if cancelled:
            status = OperationStatus.CANCELLED
            error_code = "history_import_cancelled"
            message = "历史邮件导入已取消，已完成的结果已保留，可继续导入"
            db_status = "cancelled"
        elif hard_failure is not None and not (
            aggregate["saved"] or aggregate["duplicates"]
        ):
            status = hard_failure.status
            error_code = hard_failure.error_code or "history_import_failed"
            message = hard_failure.message or "历史邮件导入失败"
            db_status = "failed"
        elif (
            hard_failure is not None
            or truncated
            or aggregate["failed"]
            or retry_from_segment is not None
        ):
            status = OperationStatus.PARTIAL
            error_code = (
                "history_import_truncated"
                if truncated
                else "history_import_partial"
            )
            message = "历史邮件导入部分完成，成功结果已保留，可继续导入"
            db_status = "partial"
        elif aggregate["saved"] == 0:
            status = OperationStatus.NO_CHANGES
            error_code = None
            message = f"历史邮件导入完成：没有新增邮件，重复 {aggregate['duplicates']} 封"
            db_status = "no_changes"
        else:
            status = OperationStatus.SUCCESS
            error_code = None
            message = (
                f"历史邮件导入完成：新增 {aggregate['saved']} 封，"
                f"重复 {aggregate['duplicates']} 封"
            )
            db_status = "completed"
        completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        upsert_history_import_run(
            self.cfg.db_path,
            run_id=stable_run_id,
            account_id=target_account_id,
            preset=storage_preset,
            date_from=start.isoformat(timespec="seconds"),
            date_to=end.isoformat(timespec="seconds"),
            mailbox_ids=normalized_mailbox_ids,
            apply_receive_rule=apply_receive_rule,
            status=db_status,
            **aggregate,
            segment_index=min(len(segments), current_segment_index),
            total_segments=len(segments),
            next_segment_index=min(len(segments) + 1, next_segment_index),
            cancel_requested=cancelled,
            truncated=truncated,
            error_code=error_code,
            message=message,
            completed_at=completed_at,
        )
        return ReceiveResult(
            status,
            backend=backend,
            scanned=aggregate["scanned"],
            matched=aggregate["matched"],
            accepted=aggregate["saved"],
            saved=aggregate["saved"],
            skipped=aggregate["skipped"],
            rule_skipped=aggregate["rule_skipped"],
            duplicates=aggregate["duplicates"],
            failed=aggregate["failed"],
            attachments=aggregate["attachments"],
            saved_files=saved_files,
            errors=errors,
            cancelled=cancelled,
            truncated=truncated,
            scan_id=stable_run_id,
            error_code=error_code,
            message=message,
            details={
                "segment_index": min(
                    len(segments), current_segment_index
                ),
                "total_segments": len(segments),
                "next_segment_index": min(
                    len(segments) + 1, next_segment_index
                ),
                "history_run_id": stable_run_id,
            },
        )

    def resume_history_import(
        self,
        run_id: str,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> ReceiveResult:
        self.initialize()
        row = get_history_import_run(self.cfg.db_path, str(run_id))
        if row is None:
            return ReceiveResult(
                OperationStatus.FAILED,
                error_code="history_import_not_found",
                message="历史导入记录不存在",
            )
        next_segment_index = max(1, int(row.get("next_segment_index") or 1))
        total_segments = max(1, int(row.get("total_segments") or 1))
        if (
            next_segment_index > total_segments
            and str(row.get("status") or "") in {"completed", "no_changes"}
        ):
            return ReceiveResult(
                OperationStatus.NO_CHANGES,
                scan_id=str(row["run_id"]),
                message="历史邮件导入已经完成，无需继续",
                details={
                    "segment_index": total_segments,
                    "total_segments": total_segments,
                    "next_segment_index": next_segment_index,
                    "history_run_id": str(row["run_id"]),
                },
            )
        return self.import_historical_mails(
            account_id=str(row["account_id"]),
            mailbox_ids=list(row.get("mailbox_ids") or []),
            preset="custom",
            date_from=str(row["date_from"]),
            date_to=str(row["date_to"]),
            apply_receive_rule=bool(row["apply_receive_rule"]),
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            run_id=str(row["run_id"]),
            resume_from_segment=next_segment_index,
            persisted_preset=str(row.get("preset") or "custom"),
            initial_facts={
                key: (
                    0
                    if key == "failed"
                    else int(row.get(key) or 0)
                )
                for key in (
                    "scanned",
                    "matched",
                    "saved",
                    "duplicates",
                    "skipped",
                    "rule_skipped",
                    "failed",
                    "attachments",
                )
            },
        )

    def list_history_imports(
        self, *, account_id: str | None = None, limit: int = 20
    ) -> ServiceResult:
        self.initialize()
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "runs": query_history_import_runs(
                    self.cfg.db_path,
                    account_id=account_id,
                    limit=limit,
                )
            },
        )

    def get_auto_receive_state(
        self, account_id: str | None = None
    ) -> ServiceResult:
        """供 GUI 展示真实持久化调度与坏邮件隔离状态。"""
        self.initialize()
        state = get_auto_receive_state(
            self.cfg.db_path, account_id=account_id
        )
        state["enabled"] = bool(state.get("enabled"))
        state.update(
            count_receive_retries(
                self.cfg.db_path,
                account_id=account_id or current_receive_account_id(self.cfg),
            )
        )
        return ServiceResult(OperationStatus.SUCCESS, details=state)

    def save_auto_receive_state(
        self, account_id: str | None = None, **changes: Any
    ) -> ServiceResult:
        """由 GUI 调度器原子更新可恢复状态。"""
        self.initialize()
        state = save_auto_receive_state(
            self.cfg.db_path, account_id=account_id, **changes
        )
        state["enabled"] = bool(state.get("enabled"))
        state.update(
            count_receive_retries(
                self.cfg.db_path,
                account_id=account_id or current_receive_account_id(self.cfg),
            )
        )
        return ServiceResult(OperationStatus.SUCCESS, details=state)

    def save_all_auto_receive_states(self, **changes: Any) -> ServiceResult:
        """更新全局兼容状态，并广播到所有已启用收件账号。"""
        self.initialize()
        state = save_auto_receive_state(
            self.cfg.db_path, account_id=None, **changes
        )
        state["enabled"] = bool(state.get("enabled"))
        return ServiceResult(OperationStatus.SUCCESS, details=state)

    def save_global_auto_receive_state(self, **changes: Any) -> ServiceResult:
        """只更新协调器状态，不覆盖任何账号自己的 retry/backoff。"""
        self.initialize()
        state = save_auto_receive_state(
            self.cfg.db_path,
            account_id=None,
            broadcast_accounts=False,
            **changes,
        )
        state["enabled"] = bool(state.get("enabled"))
        return ServiceResult(OperationStatus.SUCCESS, details=state)

    def synchronize_mail_accounts(self) -> ServiceResult:
        """把当前兼容配置同步到正式账号表；不读取、复制或返回秘密。"""
        self.initialize()
        try:
            accounts = sync_mail_accounts(
                self.cfg.db_path, legacy_accounts_from_config(self.cfg)
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_model_sync_failed",
                message=f"邮箱账号模型同步失败：{exc}",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="邮箱账号模型已同步",
            details={
                "accounts": accounts,
                "mailboxes": query_mailboxes(self.cfg.db_path),
            },
        )

    def create_mail_account(
        self,
        *,
        provider: str,
        email_address: str,
        display_name: str = "",
        auth_type: str = "",
        receive_backend: str = "",
        provider_settings: dict[str, Any] | None = None,
        secret: str = "",
        imap_secret: str = "",
        smtp_secret: str = "",
    ) -> ServiceResult:
        """创建真实账号；秘密只写 Credential Manager，不进入 SQLite。"""
        self.initialize()
        normalized_provider = str(provider or "").strip().casefold()
        address = normalize_email_address(email_address)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_email_address",
                message="请输入有效的邮箱地址",
            )
        try:
            adapter = get_provider_adapter(normalized_provider)
        except ValueError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="provider_not_supported",
                message=str(exc),
            )
        try:
            settings = validate_non_secret_provider_settings(
                provider_settings
            )
        except ProviderFoundationError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=exc.error_code,
                message=str(exc),
            )
        capabilities: tuple[str, ...]
        receive_enabled = False
        send_enabled = False
        secret_kind = ""
        if normalized_provider == "gmail":
            backend = str(receive_backend or settings.get("receive_backend") or "gmail_api")
            if backend not in {"gmail_api", "imap"}:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="invalid_receive_backend",
                    message="Gmail 收件后端必须是 Gmail API 或 IMAP",
                )
            settings = {
                "receive_backend": backend,
                **{
                    key: settings[key]
                    for key in ("imap_host", "imap_port")
                    if key in settings
                },
            }
            auth_type = auth_type or ("oauth2" if backend == "gmail_api" else "app_password")
            capabilities = (
                "receive", "archive", "mail_facts",
                "gmail_api" if backend == "gmail_api" else "imap",
            )
            receive_enabled = True
            secret_kind = ACCOUNT_IMAP_SECRET if backend == "imap" else ""
        elif normalized_provider in {"qq", "163"}:
            expected_domain = "@qq.com" if normalized_provider == "qq" else "@163.com"
            if not address.endswith(expected_domain):
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=f"invalid_{normalized_provider}_address",
                    message=f"{adapter.display_name} 只接受 {expected_domain} 地址",
                )
            profile = detect_provider_profile(address)
            if profile is None:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="provider_profile_missing",
                    message="缺少该邮箱 Provider Profile",
                )
            try:
                settings = validate_server_settings(
                    {**profile.to_settings(), **settings}
                )
            except ProviderFoundationError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
            auth_type = auth_type or "app_password"
            capabilities = adapter.implemented_capabilities
            receive_enabled = True
            send_enabled = True
            secret_kind = "shared_imap_smtp"
        elif normalized_provider == "generic_imap_smtp":
            profile = detect_provider_profile(address)
            if profile is not None:
                settings = {**profile.to_settings(), **settings}
            try:
                settings = validate_server_settings(settings)
            except ProviderFoundationError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
            if not (
                settings.get("imap_host") or settings.get("smtp_host")
            ):
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="server_not_configured",
                    message="Generic 邮箱至少需要配置 IMAP 或 SMTP 服务器",
                )
            auth_type = auth_type or "app_password"
            receive_enabled = bool(settings.get("imap_host"))
            send_enabled = bool(settings.get("smtp_host"))
            capabilities = tuple(
                capability
                for capability in adapter.implemented_capabilities
                if (
                    capability
                    not in {
                        "receive",
                        "archive",
                        "mail_facts",
                        "folder_discovery",
                    }
                    or receive_enabled
                )
                and (
                    capability not in {"send", "outbound_archive"}
                    or send_enabled
                )
            )
        else:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="provider_not_implemented",
                message=f"{adapter.display_name} 尚未进入可创建账号阶段",
            )
        account_id = stable_account_id(normalized_provider, address)
        account = MailAccount(
            account_id=account_id,
            provider=normalized_provider,
            email_address=address,
            display_name=display_name.strip() or adapter.display_name,
            auth_type=auth_type,
            receive_enabled=receive_enabled,
            send_enabled=send_enabled,
            enabled=True,
            data_namespace=account_id,
            capabilities=capabilities,
            provider_settings=settings,
            source="user",
        )
        if query_mail_account(self.cfg.db_path, account_id) is not None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_already_exists",
                message="同一 Provider 的该邮箱账号已经存在",
            )
        try:
            record = create_mail_account_record(self.cfg.db_path, account)
        except sqlite3.IntegrityError:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_already_exists",
                message="同一 Provider 的该邮箱账号已经存在",
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_create_failed",
                message=f"创建邮箱账号失败：{exc}",
            )
        if receive_enabled:
            global_state = get_auto_receive_state(self.cfg.db_path)
            save_auto_receive_state(
                self.cfg.db_path,
                account_id=account_id,
                enabled=bool(global_state.get("enabled")),
                interval_seconds=int(
                    global_state.get("interval_seconds") or 60
                ),
                next_check_at=global_state.get("next_check_at"),
            )
        secrets_to_save: list[tuple[str, str]] = []
        if normalized_provider == "generic_imap_smtp":
            generic_secret = secret.strip()
            resolved_imap_secret = imap_secret.strip() or (
                generic_secret if settings.get("imap_host") else ""
            )
            resolved_smtp_secret = smtp_secret.strip() or (
                generic_secret if settings.get("smtp_host") else ""
            )
            if resolved_imap_secret:
                secrets_to_save.append((ACCOUNT_IMAP_SECRET, resolved_imap_secret))
            if resolved_smtp_secret:
                secrets_to_save.append((ACCOUNT_SMTP_SECRET, resolved_smtp_secret))
        elif (
            normalized_provider in {"qq", "163"}
            and secret.strip()
            and secret_kind == "shared_imap_smtp"
        ):
            secrets_to_save.extend(
                (
                    (ACCOUNT_IMAP_SECRET, secret.strip()),
                    (ACCOUNT_SMTP_SECRET, secret.strip()),
                )
            )
        elif secret.strip() and secret_kind:
            secrets_to_save.append((secret_kind, secret.strip()))
        try:
            for kind, value in secrets_to_save:
                self._credentials.set_for_account(account_id, kind, value)
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.PARTIAL,
                error_code="credential_write_failed",
                message=f"账号已创建，但凭据保存失败：{exc}",
                details={"account": record},
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="邮箱账号已创建",
            details={"account": record},
        )

    def update_mail_account(
        self,
        account_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        receive_enabled: bool | None = None,
        send_enabled: bool | None = None,
        auth_type: str | None = None,
        provider_settings: dict[str, Any] | None = None,
    ) -> ServiceResult:
        """更新可变属性；邮箱地址和 Provider 不允许原地变更。"""
        self.initialize()
        account = query_mail_account(self.cfg.db_path, account_id)
        if account is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="邮箱账号不存在或已移除",
            )
        adapter = get_provider_adapter(str(account["provider"]))
        capabilities = set(account.get("capabilities") or ())
        if receive_enabled and (
            "receive" not in capabilities or not adapter.supports("receive")
        ):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="capability_not_available",
                message="该 Provider 尚未正式接通收件能力",
            )
        if send_enabled and (
            "send" not in capabilities or not adapter.supports("send")
        ):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="capability_not_available",
                message="该 Provider 尚未正式接通发件能力",
            )
        settings = provider_settings
        updated_capabilities: tuple[str, ...] | None = None
        resolved_auth_type = auth_type
        if settings is not None:
            try:
                settings = validate_non_secret_provider_settings(settings)
                if account["provider"] == "generic_imap_smtp":
                    settings = validate_server_settings(settings)
                elif account["provider"] == "gmail":
                    backend = str(
                        settings.get("receive_backend")
                        or account.get("provider_settings", {}).get(
                            "receive_backend"
                        )
                        or "gmail_api"
                    )
                    if backend not in {"gmail_api", "imap"}:
                        raise ProviderFoundationError(
                            "invalid_receive_backend",
                            "Gmail 收件后端必须是 Gmail API 或 IMAP",
                        )
                    settings = {
                        "receive_backend": backend,
                        **{
                            key: settings[key]
                            for key in ("imap_host", "imap_port")
                            if key in settings
                        },
                    }
                    updated_capabilities = (
                        "receive",
                        "archive",
                        "mail_facts",
                        "gmail_api" if backend == "gmail_api" else "imap",
                    )
                    if resolved_auth_type is None:
                        resolved_auth_type = (
                            "oauth2"
                            if backend == "gmail_api"
                            else "app_password"
                        )
                elif account["provider"] in {"qq", "163"}:
                    profile = detect_provider_profile(
                        str(account.get("email_address") or "")
                    )
                    settings = validate_server_settings(
                        {
                            **(profile.to_settings() if profile else {}),
                            **settings,
                        }
                    )
            except ProviderFoundationError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
        try:
            record = update_mail_account_record(
                self.cfg.db_path,
                account_id,
                display_name=display_name,
                enabled=enabled,
                receive_enabled=receive_enabled,
                send_enabled=send_enabled,
                auth_type=resolved_auth_type,
                capabilities=updated_capabilities,
                provider_settings=settings,
            )
        except (ValueError, sqlite3.Error) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_update_failed",
                message=str(exc),
            )
        if enabled is True and record.get("receive_enabled"):
            global_state = get_auto_receive_state(self.cfg.db_path)
            save_auto_receive_state(
                self.cfg.db_path,
                account_id=account_id,
                enabled=bool(global_state.get("enabled")),
                interval_seconds=int(
                    global_state.get("interval_seconds") or 60
                ),
                next_check_at=global_state.get("next_check_at"),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="邮箱账号已更新",
            details={"account": record},
        )

    def set_account_credential(
        self, account_id: str, secret_kind: str, value: str
    ) -> ServiceResult:
        """保存按账号秘密；同 Provider 账号之间不会共享。"""
        self.initialize()
        if query_mail_account(self.cfg.db_path, account_id) is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="邮箱账号不存在或已移除",
            )
        if secret_kind not in {ACCOUNT_IMAP_SECRET, ACCOUNT_SMTP_SECRET}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_credential_name",
                message="不支持的账号凭据类型",
            )
        if not value.strip():
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_empty",
                message="账号凭据不能为空",
            )
        try:
            self._credentials.set_for_account(
                account_id, secret_kind, value.strip()
            )
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_write_failed",
                message=str(exc),
            )
        return ServiceResult(OperationStatus.SUCCESS, message="账号凭据已安全保存")

    def delete_account_credential(
        self, account_id: str, secret_kind: str
    ) -> ServiceResult:
        self.initialize()
        account = query_mail_account(self.cfg.db_path, account_id)
        if account is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="邮箱账号不存在或已移除",
            )
        legacy_name = self._legacy_credential_name(account, secret_kind)
        try:
            self._credentials.delete_for_account(account_id, secret_kind)
            if legacy_name:
                self._credentials.delete(legacy_name)
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_delete_failed",
                message=str(exc),
            )
        return ServiceResult(OperationStatus.SUCCESS, message="账号凭据已删除")

    def remove_mail_account(
        self,
        account_id: str,
        *,
        cleanup_credentials: bool = False,
        cleanup_oauth_token: bool = False,
    ) -> ServiceResult:
        """软移除账号；本地邮件、附件、发件与审计默认全部保留。"""
        self.initialize()
        account = query_mail_account(self.cfg.db_path, account_id)
        if account is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="邮箱账号不存在或已移除",
            )
        facts = account_owned_fact_counts(self.cfg.db_path, account_id)
        cleanup_errors: list[str] = []
        token_path: Path | None = None
        if cleanup_oauth_token and account["provider"] == "gmail":
            try:
                _credentials_path, token_path = self._account_router.oauth_paths(
                    account_id
                )
            except (AccountRuntimeError, CredentialError, OSError, ValueError) as exc:
                cleanup_errors.append(str(exc))
        try:
            record = remove_mail_account_record(self.cfg.db_path, account_id)
        except (ValueError, sqlite3.Error) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_remove_failed",
                message=str(exc),
            )
        if cleanup_credentials:
            for kind in (ACCOUNT_IMAP_SECRET, ACCOUNT_SMTP_SECRET):
                try:
                    self._credentials.delete_for_account(account_id, kind)
                    legacy_name = self._legacy_credential_name(account, kind)
                    if legacy_name:
                        self._credentials.delete(legacy_name)
                except CredentialError as exc:
                    cleanup_errors.append(str(exc))
        if token_path is not None:
            try:
                token_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(str(exc))
        status = OperationStatus.PARTIAL if cleanup_errors else OperationStatus.SUCCESS
        return ServiceResult(
            status,
            error_code="credential_cleanup_partial" if cleanup_errors else None,
            message=(
                "账号已移除，本地历史数据已保留；部分凭据清理失败"
                if cleanup_errors
                else "账号已移除，本地历史邮件与发件记录已保留"
            ),
            details={
                "account": record,
                "preserved_facts": facts,
                "credential_cleanup_requested": cleanup_credentials,
                "oauth_cleanup_requested": cleanup_oauth_token,
                "cleanup_errors": cleanup_errors,
            },
        )

    def _legacy_credential_name(
        self, account: dict[str, Any], secret_kind: str
    ) -> str | None:
        """仅为精确匹配的兼容账号返回旧 Credential Manager key。"""
        provider = str(account.get("provider") or "")
        address = str(account.get("email_address") or "").casefold()
        if (
            provider == "gmail"
            and secret_kind == ACCOUNT_IMAP_SECRET
            and address == self.cfg.gmail_address.casefold()
        ):
            return GMAIL_IMAP_SECRET
        if (
            provider == "qq"
            and secret_kind == ACCOUNT_SMTP_SECRET
            and address == self.cfg.qq_email.casefold()
        ):
            return QQ_SMTP_SECRET
        return None

    def list_mail_accounts(self) -> ServiceResult:
        """返回统一账号及 mailbox 基础事实，供 GUI 和统一 MCP 展示。"""
        synced = self.synchronize_mail_accounts()
        if not synced.ok:
            return synced
        accounts: list[dict[str, Any]] = []
        for raw in list(synced.details.get("accounts") or []):
            account = dict(raw)
            account_id = str(account["account_id"])
            settings = dict(account.get("provider_settings") or {})
            legacy_kind = (
                GMAIL_IMAP_SECRET
                if account["provider"] == "gmail"
                and account["email_address"].casefold()
                == self.cfg.gmail_address.casefold()
                else QQ_SMTP_SECRET
                if account["provider"] == "qq"
                and account["email_address"].casefold()
                == self.cfg.qq_email.casefold()
                else None
            )
            try:
                if (
                    account["provider"] == "gmail"
                    and settings.get("receive_backend") == "gmail_api"
                ):
                    runtime = self._account_router.context(
                        account_id, require_enabled=False
                    ).config
                    oauth = get_oauth_state(runtime)
                    account["auth_state"] = oauth.get("state")
                    account["credential_configured"] = oauth.get("state") in {
                        "READY",
                        "TOKEN_EXPIRED_REFRESHABLE",
                    }
                else:
                    credential_states = self._credentials.account_status(
                        account_id
                    )
                    if legacy_kind:
                        legacy_value = self._credentials.get(legacy_kind)
                        if legacy_value:
                            if account["provider"] == "qq":
                                credential_states[ACCOUNT_IMAP_SECRET] = True
                                credential_states[ACCOUNT_SMTP_SECRET] = True
                            else:
                                credential_states[ACCOUNT_IMAP_SECRET] = True
                    account["credential_states"] = credential_states
                    required_kinds = []
                    if account["provider"] == "gmail":
                        required_kinds.append(ACCOUNT_IMAP_SECRET)
                    elif account["provider"] in {"qq", "163"}:
                        required_kinds.extend(
                            (ACCOUNT_IMAP_SECRET, ACCOUNT_SMTP_SECRET)
                        )
                    else:
                        if settings.get("imap_host"):
                            required_kinds.append(ACCOUNT_IMAP_SECRET)
                        if settings.get("smtp_host"):
                            required_kinds.append(ACCOUNT_SMTP_SECRET)
                    account["credential_configured"] = bool(
                        required_kinds
                    ) and all(
                        credential_states.get(kind, False)
                        for kind in required_kinds
                    )
                    account["auth_state"] = (
                        "CONFIGURED"
                        if account["credential_configured"]
                        else "AUTH_REQUIRED"
                    )
            except (CredentialError, AccountRuntimeError, OSError, ValueError):
                account["credential_configured"] = False
                account["auth_state"] = "CREDENTIAL_UNAVAILABLE"
            state = get_auto_receive_state(
                self.cfg.db_path, account_id=account_id
            )
            account["sync_state"] = {
                key: state.get(key)
                for key in (
                    "enabled", "last_check_at", "last_success_at", "last_result",
                    "last_error", "next_check_at", "consecutive_global_failures",
                )
            }
            account["connection_status"] = (
                state.get("last_result") or "not_tested"
            )
            accounts.append(account)
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "accounts": accounts,
                "mailboxes": synced.details.get("mailboxes", []),
            },
        )

    def list_mail_provider_adapters(self) -> ServiceResult:
        self.initialize()
        adapters = [
            {
                "provider": item.provider,
                "display_name": item.display_name,
                "authentication_types": list(item.authentication_types),
                "available_capabilities": list(item.available_capabilities),
                "implemented_capabilities": list(item.implemented_capabilities),
                "receive_backends": list(item.receive_backends),
                "send_backends": list(item.send_backends),
                "status": item.status,
            }
            for item in list_provider_adapters()
        ]
        return ServiceResult(
            OperationStatus.SUCCESS, details={"providers": adapters}
        )

    def test_mail_account_connection(self, account_id: str) -> ServiceResult:
        """按账号测试已实现连接面；只认证，不收件也不发件。"""
        self.initialize()
        try:
            context = self._account_router.context(account_id)
            account = context.account
            runtime_cfg = context.config
            provider = str(account["provider"])
            if provider == "gmail":
                if _effective_receive_backend(runtime_cfg) == "gmail_api":
                    from agent_mail_bridge.gmail_api_auth import (
                        reverify_gmail_authorization,
                    )

                    return reverify_gmail_authorization(runtime_cfg)
                from agent_mail_bridge.mail_receive import _connect_imap

                connection = _connect_imap(runtime_cfg)
                try:
                    connection.logout()
                finally:
                    try:
                        connection.shutdown()
                    except Exception:
                        pass
                return ServiceResult(
                    OperationStatus.SUCCESS,
                    message="Gmail IMAP 连接正常",
                    details={"account_id": account_id, "protocol": "imap"},
                )
            if provider in {"qq", "163", "generic_imap_smtp"}:
                settings = dict(account.get("provider_settings") or {})
                incoming = effective_incoming_runtime(runtime_cfg)
                outgoing = effective_outgoing_runtime(runtime_cfg)
                checks: dict[str, Any] = {}
                if settings.get("imap_host"):
                    discovered = discover_imap_mailboxes(
                        settings=settings,
                        username=incoming.username,
                        secret=incoming.secret,
                    )
                    checks["imap"] = {
                        "authenticated": True,
                        "mailbox_count": len(discovered["mailboxes"]),
                        "capabilities": discovered["capabilities"],
                    }
                if settings.get("smtp_host"):
                    checks["smtp"] = test_smtp_connection(
                        settings=settings,
                        username=outgoing.username,
                        secret=outgoing.secret,
                    )
                if not checks:
                    raise ProviderFoundationError(
                        "server_not_configured",
                        "尚未配置 IMAP 或 SMTP 服务器",
                    )
                return ServiceResult(
                    OperationStatus.SUCCESS,
                    message=f"{context.adapter.display_name} 连接测试通过",
                    details={"account_id": account_id, "checks": checks},
                )
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="provider_not_implemented",
                message="该 Provider 尚未接通连接测试",
            )
        except AccountRuntimeError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=exc.error_code,
                message=str(exc),
            )
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_read_failed",
                message=str(exc),
            )
        except ProviderFoundationError as exc:
            return ServiceResult(
                OperationStatus.AUTH_REQUIRED
                if exc.error_code.endswith("auth_required")
                else OperationStatus.FAILED,
                error_code=exc.error_code,
                message=str(exc),
                needs_auth=exc.error_code.endswith("auth_required"),
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_connection_failed",
                message=str(exc),
            )

    def discover_mail_account_mailboxes(
        self, account_id: str
    ) -> ServiceResult:
        """发现并保存 IMAP 目录事实，不据临时缺失删除历史目录。"""
        self.initialize()
        try:
            context = self._account_router.context(account_id)
            account = context.account
            runtime_cfg = context.config
            provider = str(account["provider"])
            if provider == "gmail":
                if _effective_receive_backend(runtime_cfg) != "imap":
                    raise ProviderFoundationError(
                        "folder_discovery_not_available",
                        "Gmail API 账号不使用 IMAP 目录发现",
                    )
                settings = {
                    "imap_host": runtime_cfg.gmail_imap_host,
                    "imap_port": runtime_cfg.gmail_imap_port,
                    "imap_security": "ssl",
                }
                username = runtime_cfg.gmail_address
                secret = runtime_cfg.gmail_app_password
            elif provider in {"qq", "163", "generic_imap_smtp"}:
                settings = dict(account.get("provider_settings") or {})
                incoming = effective_incoming_runtime(runtime_cfg)
                username = incoming.username
                secret = incoming.secret
            else:
                raise ProviderFoundationError(
                    "folder_discovery_not_available",
                    "该 Provider 当前没有 IMAP 目录发现能力",
                )
            discovered = discover_imap_mailboxes(
                settings=settings,
                username=username,
                secret=secret,
            )
            mailboxes = upsert_mailboxes(
                self.cfg.db_path, account_id, discovered["mailboxes"]
            )
            checkpoints = {
                str(item["external_ref"]): item["checkpoint"]
                for item in discovered["mailboxes"]
                if item.get("checkpoint")
            }
            if checkpoints:
                state = get_auto_receive_state(
                    self.cfg.db_path, account_id=account_id
                )
                try:
                    checkpoint_data = json.loads(
                        str(state.get("checkpoint") or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    checkpoint_data = {}
                existing_mailboxes = checkpoint_data.setdefault(
                    "mailboxes", {}
                )
                if not isinstance(existing_mailboxes, dict):
                    existing_mailboxes = {}
                    checkpoint_data["mailboxes"] = existing_mailboxes
                for mailbox_name, discovered_checkpoint in checkpoints.items():
                    previous = existing_mailboxes.get(mailbox_name)
                    existing_mailboxes[mailbox_name] = {
                        **(
                            previous
                            if isinstance(previous, dict)
                            else {}
                        ),
                        **discovered_checkpoint,
                    }
                save_auto_receive_state(
                    self.cfg.db_path,
                    account_id=account_id,
                    checkpoint=json.dumps(
                        checkpoint_data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            return ServiceResult(
                OperationStatus.SUCCESS,
                message=f"已发现 {len(mailboxes)} 个邮箱目录",
                details={
                    "account_id": account_id,
                    "capabilities": discovered["capabilities"],
                    "mailboxes": mailboxes,
                },
            )
        except AccountRuntimeError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=exc.error_code,
                message=str(exc),
            )
        except CredentialError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credential_read_failed",
                message=str(exc),
            )
        except ProviderFoundationError as exc:
            return ServiceResult(
                OperationStatus.AUTH_REQUIRED
                if exc.error_code.endswith("auth_required")
                else OperationStatus.FAILED,
                error_code=exc.error_code,
                message=str(exc),
                needs_auth=exc.error_code.endswith("auth_required"),
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="folder_discovery_failed",
                message=str(exc),
            )

    def sync_due_mail_accounts(
        self,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> ServiceResult:
        """逐账号调度收件；单账号失败不会阻断其余账号。"""
        self.initialize()
        current = now or datetime.now()
        global_state = get_auto_receive_state(self.cfg.db_path)
        accounts = [
            account
            for account in query_mail_accounts(
                self.cfg.db_path, enabled_only=True
            )
            if account.get("receive_enabled")
        ]
        results: list[dict[str, Any]] = []
        saved_total = 0
        failed_total = 0
        for account in accounts:
            account_id = str(account["account_id"])
            state = get_auto_receive_state(
                self.cfg.db_path, account_id=account_id
            )
            if not state.get("enabled") and not force:
                continue
            next_check = _parse_datetime(state.get("next_check_at"))
            if next_check and next_check > current and not force:
                continue
            try:
                result = self.receive(account_id=account_id, automatic=True)
            except Exception as exc:  # noqa: BLE001
                result = ReceiveResult(
                    OperationStatus.FAILED,
                    backend="",
                    error_code="account_sync_failed",
                    message=f"账号同步异常：{type(exc).__name__}",
                    failed=1,
                )
            interval = max(30, int(state.get("interval_seconds") or 60))
            failure_count = int(
                state.get("consecutive_global_failures") or 0
            )
            if result.status in {
                OperationStatus.SUCCESS,
                OperationStatus.NO_CHANGES,
                OperationStatus.PARTIAL,
            }:
                failure_count = 0
                delay = interval
                last_success_at = current.strftime("%Y-%m-%d %H:%M:%S")
            elif result.status == OperationStatus.CANCELLED:
                delay = interval
                last_success_at = state.get("last_success_at")
            else:
                failure_count += 1
                delay = min(
                    900,
                    (30, 60, 120, 300, 600, 900)[
                        min(failure_count - 1, 5)
                    ],
                )
                last_success_at = state.get("last_success_at")
                failed_total += 1
            next_check_at = (current + timedelta(seconds=delay)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            save_auto_receive_state(
                self.cfg.db_path,
                account_id=account_id,
                enabled=bool(state.get("enabled")),
                interval_seconds=interval,
                last_check_at=current.strftime("%Y-%m-%d %H:%M:%S"),
                last_success_at=last_success_at,
                last_result=result.status.value,
                last_error=None if result.ok else result.message,
                consecutive_global_failures=failure_count,
                next_check_at=next_check_at,
            )
            saved_total += int(result.saved)
            results.append(
                {
                    "account_id": account_id,
                    "status": result.status.value,
                    "saved": int(result.saved),
                    "failed": int(result.failed),
                    "error_code": result.error_code,
                    "next_check_at": next_check_at,
                }
            )
        if failed_total:
            status = OperationStatus.PARTIAL
        elif saved_total:
            status = OperationStatus.SUCCESS
        else:
            status = OperationStatus.NO_CHANGES
        next_checks = [
            parsed
            for account in accounts
            if (
                parsed := _parse_datetime(
                    get_auto_receive_state(
                        self.cfg.db_path,
                        account_id=str(account["account_id"]),
                    ).get("next_check_at")
                )
            )
        ]
        coordinator_delay = min(
            30, max(1, int(global_state.get("interval_seconds") or 60))
        )
        coordinator_next = min(next_checks) if next_checks else (
            current + timedelta(seconds=coordinator_delay)
        )
        checked_at = current.strftime("%Y-%m-%d %H:%M:%S")
        save_auto_receive_state(
            self.cfg.db_path,
            account_id=None,
            broadcast_accounts=False,
            enabled=bool(global_state.get("enabled")),
            interval_seconds=int(global_state.get("interval_seconds") or 60),
            last_check_at=checked_at,
            last_success_at=(
                checked_at
                if results and failed_total < len(results)
                else global_state.get("last_success_at")
            ),
            last_result=status.value,
            last_error=(
                f"{failed_total} 个账号同步失败"
                if failed_total
                else None
            ),
            consecutive_global_failures=0,
            next_check_at=coordinator_next.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return ServiceResult(
            status,
            error_code="account_sync_partial" if failed_total else None,
            message=(
                f"已检查 {len(results)} 个账号，新增 {saved_total} 封，"
                f"失败 {failed_total} 个账号"
            ),
            details={
                "accounts_checked": len(results),
                "saved": saved_total,
                "failed_accounts": failed_total,
                "results": results,
            },
        )

    def send_file(
        self,
        file_path: str | Path,
        *,
        subject: str | None = None,
        request_id: str | None = None,
        attachment_name: str | None = None,
        source_origin: str = "controlled",
        source_sha256: str | None = None,
        staged_sha256: str | None = None,
        original_source_path: str | Path | None = None,
    ) -> SendResult:
        """发送白名单目录内文件，request_id 用于安全重试。"""
        self.initialize()
        stable_request_id = request_id or str(uuid.uuid4())
        raw = send_file_with_request(
            file_path, request_id=stable_request_id,
            subject=subject, cfg=self.cfg,
            attachment_name=attachment_name,
            source_origin=source_origin,
            source_sha256=source_sha256,
            staged_sha256=staged_sha256,
            original_source_path=original_source_path,
        )
        status_map = {
            "success": OperationStatus.SUCCESS,
            "partial": OperationStatus.PARTIAL,
            "duplicate": OperationStatus.DUPLICATE,
            "failed": OperationStatus.FAILED,
        }
        status = status_map.get(raw.get("status", "failed"), OperationStatus.FAILED)
        return SendResult(
            status,
            request_id=stable_request_id,
            outbound_id=raw.get("outbound_id", ""),
            send_status=raw.get("send_status", "not_sent"),
            source_path=raw.get("source_path", ""),
            send_copy_path=raw.get("send_copy_path", ""),
            sent_copy_path=raw.get("sent_copy_path", ""),
            subject=raw.get("subject", subject or ""),
            to_email=raw.get("to", self.cfg.owner_gmail),
            sent_at=raw.get("sent_at", ""),
            filename=raw.get("filename", attachment_name or Path(file_path).name),
            size_bytes=int(raw.get("size_bytes") or 0),
            source_sha256=raw.get("source_sha256", ""),
            staged_sha256=raw.get("staged_sha256", ""),
            attachment_pre_smtp_sha256=raw.get("attachment_pre_smtp_sha256", ""),
            sent_archive_sha256=raw.get("sent_archive_sha256", ""),
            attachment_count=1 if raw.get("filename") else 0,
            error_code=raw.get("error_code"),
            message=raw.get("error", "发送完成"),
            details={"previous_status": raw.get("previous_status")},
        )

    def send_user_selected_file(
        self,
        file_path: str | Path,
        *,
        subject: str | None = None,
        request_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> SendResult:
        """发送用户在 GUI 中明确选择的全局文件，不扩大 MCP/CLI 权限。"""
        self.initialize()
        stable_request_id = request_id or str(uuid.uuid4())
        try:
            source = Path(file_path).resolve(strict=True)
            if not source.is_file():
                raise OSError("文件不存在或不是普通文件")
            if is_dangerous(source.name):
                return SendResult(
                    OperationStatus.FAILED, request_id=stable_request_id,
                    error_code="file_type_not_allowed", message="危险扩展名文件禁止发送",
                )
            size_bytes = source.stat().st_size
            if not check_size_ok(size_bytes, self.cfg.max_send_file_bytes):
                return SendResult(
                    OperationStatus.FAILED, request_id=stable_request_id,
                    error_code="file_too_large", message="文件超过发送大小限制",
                )
            source_sha = sha256_of_file(source)
            if expected_sha256 and source_sha != expected_sha256:
                return SendResult(
                    OperationStatus.FAILED, request_id=stable_request_id,
                    error_code="file_changed", message="文件在确认后发生变化，已阻止发送",
                )
            safe_name = sanitize_filename(source.stem) + source.suffix.lower()
            snapshot = self.cfg.send_dir / "staging" / stable_request_id / safe_name
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if snapshot.exists() and sha256_of_file(snapshot) != source_sha:
                raise OSError("同一请求的受控快照内容不一致")
            if not snapshot.exists():
                atomic_copy_file(source, snapshot)
            if snapshot.stat().st_size != size_bytes or sha256_of_file(snapshot) != source_sha:
                raise OSError("受控快照与源文件校验不一致")
        except (OSError, ValueError) as exc:
            return SendResult(
                OperationStatus.FAILED, request_id=stable_request_id,
                error_code="snapshot_failed", message=f"创建受控快照失败：{exc}",
            )

        raw = send_file_with_request(
            snapshot,
            request_id=stable_request_id,
            subject=subject,
            cfg=self.cfg,
            attachment_name=source.name,
            source_origin="manual_gui",
            original_source_path=source,
        )
        status_map = {
            "success": OperationStatus.SUCCESS,
            "partial": OperationStatus.PARTIAL,
            "duplicate": OperationStatus.DUPLICATE,
            "failed": OperationStatus.FAILED,
        }
        return SendResult(
            status_map.get(raw.get("status", "failed"), OperationStatus.FAILED),
            request_id=stable_request_id,
            outbound_id=raw.get("outbound_id", ""),
            send_status=raw.get("send_status", "not_sent"),
            source_path="",
            send_copy_path=raw.get("send_copy_path", ""),
            sent_copy_path=raw.get("sent_copy_path", ""),
            subject=raw.get("subject", subject or ""),
            to_email=raw.get("to", self.cfg.owner_gmail),
            sent_at=raw.get("sent_at", ""),
            error_code=raw.get("error_code"),
            message=raw.get("error", "发送完成"),
            details={
                "source_origin": "manual_gui",
                "original_filename": source.name,
                "size_bytes": size_bytes,
                "sha256": source_sha,
            },
        )

    def send_user_selected_mail(
        self,
        *,
        from_account_id: str | None = None,
        recipient: str | None = None,
        subject: str | None,
        body_text: str,
        attachment_paths: list[str | Path],
        links: list[dict[str, Any] | str],
    ) -> SendResult:
        """发送一封用户编写的邮件；全局文件信任仅限本次 GUI 操作。"""
        self.initialize()
        runtime_cfg = self.cfg
        if from_account_id:
            try:
                runtime_cfg = self._account_router.context(
                    from_account_id, capability="send"
                ).config
            except AccountRuntimeError as exc:
                return SendResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
            except CredentialError as exc:
                return SendResult(
                    OperationStatus.FAILED,
                    error_code="credential_read_failed",
                    message=str(exc),
                )
        raw = send_outbound_mail(
            subject=subject,
            body_text=body_text,
            attachment_paths=attachment_paths,
            links=links,
            cfg=runtime_cfg,
            recipient=recipient,
            source_origin="manual_gui",
        )
        status_map = {
            "success": OperationStatus.SUCCESS,
            "partial": OperationStatus.PARTIAL,
            "failed": OperationStatus.FAILED,
        }
        return SendResult(
            status_map.get(raw.get("status", "failed"), OperationStatus.FAILED),
            outbound_id=str(raw.get("outbound_id") or ""),
            send_status=str(raw.get("send_status") or "not_sent"),
            subject=str(raw.get("subject") or subject or ""),
            to_email=str(raw.get("to") or recipient or runtime_cfg.owner_gmail),
            sent_at=str(raw.get("sent_at") or ""),
            attachment_count=int(raw.get("attachment_count") or 0),
            link_count=int(raw.get("link_count") or 0),
            error_code=raw.get("error_code"),
            message=str(raw.get("error") or "发送完成"),
            details={
                "body_text": str(raw.get("body_text") or body_text or ""),
                "from_account_id": from_account_id or "",
            },
        )

    def submit_result(
        self,
        file_path: str | Path,
        *,
        title: str | None = None,
        request_id: str | None = None,
    ) -> SendResult:
        """供 MCP 调用的受控结果提交入口。"""
        self.initialize()
        stable_request_id = request_id or str(uuid.uuid4())
        path_text = str(file_path)
        call_id = insert_mcp_call(
            self.cfg.db_path,
            request_id=stable_request_id,
            file_path=path_text,
            title=title,
        )
        try:
            source = assert_within_allowed_roots(
                Path(file_path), self.cfg.effective_allowed_send_roots
            )
            assert_not_sensitive_delivery_file(source)
            if not source.exists() or not source.is_file():
                raise FileNotFoundError("待提交文件不存在")
            if is_dangerous(source.name):
                result = SendResult(
                    OperationStatus.FAILED,
                    request_id=stable_request_id,
                    send_status="not_sent",
                    error_code="file_type_not_allowed",
                    message="危险扩展名文件禁止发送",
                )
                raise _McpStagingHandled(result)
            source_size = source.stat().st_size
            if not check_size_ok(source_size, self.cfg.max_send_file_bytes):
                result = SendResult(
                    OperationStatus.FAILED,
                    request_id=stable_request_id,
                    send_status="not_sent",
                    error_code="file_too_large",
                    message="文件超过发送大小限制",
                )
                raise _McpStagingHandled(result)
            source_sha = sha256_of_file(source)
            request_folder = uuid.uuid5(uuid.NAMESPACE_URL, stable_request_id).hex
            staged_name = sanitize_filename(source.stem) + source.suffix.lower()
            staged = self.cfg.send_dir / "staging" / "mcp" / request_folder / staged_name
            if staged.exists():
                staged_size = staged.stat().st_size
                staged_sha = sha256_of_file(staged)
                if staged_size != source_size or staged_sha != source_sha:
                    raise OSError("同一 request_id 的受控 staging 内容不一致")
            else:
                atomic_copy_file(source, staged)
                staged_size = staged.stat().st_size
                staged_sha = sha256_of_file(staged)
            if staged_size != source_size or staged_sha != source_sha:
                raise OSError("受控 staging 与源文件字节校验不一致")
            update_mcp_staging(
                self.cfg.db_path,
                call_id,
                staging_status="staged",
                staged_path=str(staged),
                source_size_bytes=source_size,
                staged_size_bytes=staged_size,
                source_sha256=source_sha,
                staged_sha256=staged_sha,
            )
            result = self.send_file(
                staged,
                subject=title,
                request_id=stable_request_id,
                attachment_name=source.name,
                source_origin="mcp_staged",
                source_sha256=source_sha,
                staged_sha256=staged_sha,
                original_source_path=source,
            )
            result.source_path = str(source)
            update_mcp_staging(
                self.cfg.db_path,
                call_id,
                staging_status=("verified" if result.send_status in {"sent", "duplicate"} else "send_failed"),
                attachment_sha256=result.attachment_pre_smtp_sha256 or None,
                sent_archive_sha256=result.sent_archive_sha256 or None,
                failure_reason=None if result.send_status in {"sent", "duplicate"} else result.message,
            )
        except _McpStagingHandled as exc:
            result = exc.result
            update_mcp_staging(
                self.cfg.db_path,
                call_id,
                staging_status="rejected",
                failure_reason=result.message,
            )
        except SecurityError as exc:
            result = SendResult(
                OperationStatus.FAILED,
                request_id=stable_request_id,
                send_status="not_sent",
                source_path=path_text,
                error_code="path_not_allowed",
                message=str(exc),
            )
            update_mcp_staging(
                self.cfg.db_path, call_id,
                staging_status="rejected", failure_reason=str(exc),
            )
        except FileNotFoundError as exc:
            result = SendResult(
                OperationStatus.FAILED,
                request_id=stable_request_id,
                send_status="not_sent",
                source_path=path_text,
                error_code="file_not_found",
                message=str(exc),
            )
            update_mcp_staging(
                self.cfg.db_path, call_id,
                staging_status="failed", failure_reason=str(exc),
            )
        except OSError as exc:
            result = SendResult(
                OperationStatus.FAILED,
                request_id=stable_request_id,
                send_status="not_sent",
                source_path=path_text,
                error_code="staging_failed",
                message=f"创建受控 staging 失败：{exc}",
            )
            update_mcp_staging(
                self.cfg.db_path, call_id,
                staging_status="failed", failure_reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            result = SendResult(
                OperationStatus.FAILED,
                request_id=stable_request_id,
                send_status="failed",
                source_path=path_text,
                error_code="internal_error",
                message=str(exc),
            )

        audit_status = _mcp_audit_status(result)
        update_mcp_call(
            self.cfg.db_path,
            call_id,
            status=audit_status,
            error_code=result.error_code,
            message=result.message,
        )
        log_event(
            self.cfg.db_path,
            "SUCCESS" if result.ok or result.status == OperationStatus.DUPLICATE else "ERROR",
            "mcp",
            f"MCP 提交完成：request_id={stable_request_id}，状态={audit_status}",
        )
        result.details = {**result.details, "mcp_call_id": call_id}
        return result

    def get_mcp_history(self, limit: int = 100) -> ServiceResult:
        """返回安全边界内的 MCP 调用审计记录。"""
        self.initialize()
        rows = _sanitize_history_paths(
            query_recent_mcp_audit_events(self.cfg.db_path, max(1, limit)),
            ("file_path", "staged_path", "source_path", "prepared_path"),
            self.cfg.effective_allowed_send_roots,
        )
        for row in rows:
            try:
                row["details"] = json.loads(str(row.get("details_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                row["details"] = {}
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"calls": rows},
        )

    def record_mcp_audit(self, **event: Any) -> int:
        """写入统一 MCP 审计，不记录正文或附件内容。"""
        self.initialize()
        for key in ("query_summary", "target_summary"):
            if event.get(key):
                event[key] = _redact_text(str(event[key]), self.cfg)
        return insert_mcp_audit_event(self.cfg.db_path, **event)

    def create_agent_client(
        self,
        *,
        client_type: str,
        display_name: str,
        config_mode: str | None = None,
        config_scope: str = "user",
        capabilities: list[str] | None = None,
        account_ids: list[str] | None = None,
        workspace_ids: list[str] | None = None,
        mailbox_ids: list[str] | None = None,
        send_account_ids: list[str] | None = None,
        attachment_workspace_ids: list[str] | None = None,
        permission_mode: str = "custom",
        account_scope_mode: str = "selected",
        mailbox_scope_mode: str = "selected",
        send_account_scope_mode: str = "selected",
        workspace_scope_mode: str = "selected",
        attachment_scope_mode: str = "selected",
        send_mode: str = "confirm",
    ) -> ServiceResult:
        """创建默认拒绝的 Client；scoped token 只在本次结果中返回。"""
        self.initialize()
        try:
            client, token = self._agent_integration.create_client_profile(
                client_type=client_type,
                display_name=display_name,
                config_mode=config_mode,
                config_scope=config_scope,
                permission_mode=permission_mode,
                account_scope_mode=account_scope_mode,
                mailbox_scope_mode=mailbox_scope_mode,
                send_account_scope_mode=send_account_scope_mode,
                workspace_scope_mode=workspace_scope_mode,
                attachment_scope_mode=attachment_scope_mode,
                send_mode=send_mode,
            )
            selected_accounts = set(account_ids or [])
            if account_scope_mode == "all":
                selected_accounts = {
                    str(row["account_id"])
                    for row in query_mail_accounts(
                        self.cfg.db_path, enabled_only=True
                    )
                }
            selected_mailboxes = (
                list(mailbox_ids)
                if mailbox_ids is not None
                else [
                    str(row["mailbox_id"])
                    for row in query_mailboxes(
                        self.cfg.db_path, enabled_only=True
                    )
                    if str(row["account_id"]) in selected_accounts
                ]
            )
            permissions = self._agent_integration.set_permissions(
                str(client["client_id"]),
                capabilities=capabilities or [],
                account_ids=account_ids or [],
                workspace_ids=workspace_ids or [],
                mailbox_ids=selected_mailboxes,
                send_account_ids=send_account_ids or [],
                attachment_workspace_ids=attachment_workspace_ids or [],
                permission_mode=permission_mode,
                account_scope_mode=account_scope_mode,
                mailbox_scope_mode=mailbox_scope_mode,
                send_account_scope_mode=send_account_scope_mode,
                workspace_scope_mode=workspace_scope_mode,
                attachment_scope_mode=attachment_scope_mode,
                send_mode=send_mode,
            )
        except (AgentAccessError, CredentialError, sqlite3.Error, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "agent_client_create_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="Agent Client 已创建，默认保持未启用",
            details={
                "client": client,
                "permissions": permissions,
                "scoped_token": token,
                "token_notice": "仅代表 AgentMailBridge 内的有限权限，可独立撤销；不会获得邮箱凭据。",
            },
        )

    def list_agent_clients(self, *, include_revoked: bool = True) -> ServiceResult:
        self.initialize()
        clients = self._agent_integration.list_clients(
            include_revoked=include_revoked
        )
        for client in clients:
            client_id = str(client["client_id"])
            rows = query_agent_client_permissions(self.cfg.db_path, client_id)
            client["capabilities"] = sorted(
                {
                    str(row["capability"])
                    for row in rows
                    if row.get("enabled")
                    and row.get("effect") == "allow"
                    and "." in str(row.get("capability") or "")
                    and row.get("capability")
                    not in {"account.access", "workspace.access"}
                }
            )
            client["account_ids"] = sorted(
                str(row["account_id"])
                for row in rows
                if row.get("enabled")
                and row.get("effect") == "allow"
                and row.get("capability") == "account.access"
                and row.get("account_id")
            )
            if str(client.get("account_scope_mode") or "selected") == "all":
                client["account_ids"] = sorted(
                    str(row["account_id"])
                    for row in query_mail_accounts(
                        self.cfg.db_path, enabled_only=True
                    )
                )
            client["workspace_ids"] = sorted(
                str(row["workspace_id"])
                for row in rows
                if row.get("enabled")
                and row.get("effect") == "allow"
                and row.get("capability") == "workspace.access"
                and row.get("workspace_id")
            )
            if str(client.get("workspace_scope_mode") or "selected") == "all":
                client["workspace_ids"] = sorted(
                    str(row["workspace_id"]) for row in workspace_dtos(self.cfg)
                )
            extended = query_extended_scopes(self.cfg.db_path, client_id)
            client["mailbox_ids"] = sorted(extended["mailbox_ids"])
            if str(client.get("mailbox_scope_mode") or "selected") == "all":
                allowed_accounts = set(client["account_ids"])
                client["mailbox_ids"] = sorted(
                    str(row["mailbox_id"])
                    for row in query_mailboxes(
                        self.cfg.db_path, enabled_only=True
                    )
                    if str(row["account_id"]) in allowed_accounts
                    and str(row["mailbox_id"])
                    not in extended["denied_mailbox_ids"]
                )
            client["send_account_ids"] = sorted(
                extended["send_account_ids"]
            )
            if (
                str(client.get("send_account_scope_mode") or "selected")
                == "all"
            ):
                client["send_account_ids"] = sorted(
                    str(row["account_id"])
                    for row in query_mail_accounts(
                        self.cfg.db_path, enabled_only=True
                    )
                    if row.get("send_enabled")
                    and "send" in set(row.get("capabilities") or ())
                    and str(row["account_id"])
                    not in extended["denied_send_account_ids"]
                )
            client["attachment_workspace_ids"] = sorted(
                extended["attachment_workspace_ids"]
            )
            if (
                str(client.get("attachment_scope_mode") or "selected")
                == "all"
            ):
                denied = extended["denied_attachment_workspace_ids"]
                client["attachment_workspace_ids"] = sorted(
                    str(row["workspace_id"])
                    for row in workspace_dtos(self.cfg)
                    if str(row["workspace_id"]) not in denied
                )
            try:
                detection = detect_client(
                    str(client["client_type"]),
                    config_scope=str(client.get("config_scope") or "user"),
                )
                client["install_status"] = detection.status
                client["installed"] = detection.installed
                client["client_version"] = detection.version
            except ClientConfigError as exc:
                client["install_status"] = exc.code
                client["installed"] = False
                client["client_version"] = None
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"clients": clients},
        )

    def set_agent_client_permissions(
        self,
        client_id: str,
        *,
        capabilities: list[str],
        account_ids: list[str],
        workspace_ids: list[str],
        mailbox_ids: list[str] | None = None,
        send_account_ids: list[str] | None = None,
        attachment_workspace_ids: list[str] | None = None,
        denied_capabilities: list[str] | None = None,
        denied_account_ids: list[str] | None = None,
        denied_workspace_ids: list[str] | None = None,
        denied_mailbox_ids: list[str] | None = None,
        denied_send_account_ids: list[str] | None = None,
        denied_attachment_workspace_ids: list[str] | None = None,
        permission_mode: str | None = None,
        account_scope_mode: str | None = None,
        mailbox_scope_mode: str | None = None,
        send_account_scope_mode: str | None = None,
        workspace_scope_mode: str | None = None,
        attachment_scope_mode: str | None = None,
        send_mode: str | None = None,
    ) -> ServiceResult:
        self.initialize()
        try:
            client = self._agent_integration.get_client(client_id)
            if client is None:
                raise AgentAccessError("unknown_client")
            extended = query_extended_scopes(self.cfg.db_path, client_id)
            rows = self._agent_integration.set_permissions(
                client_id,
                capabilities=capabilities,
                account_ids=account_ids,
                workspace_ids=workspace_ids,
                mailbox_ids=(
                    mailbox_ids
                    if mailbox_ids is not None
                    else sorted(extended["mailbox_ids"])
                ),
                send_account_ids=(
                    send_account_ids
                    if send_account_ids is not None
                    else sorted(extended["send_account_ids"])
                ),
                attachment_workspace_ids=(
                    attachment_workspace_ids
                    if attachment_workspace_ids is not None
                    else sorted(extended["attachment_workspace_ids"])
                ),
                denied_capabilities=denied_capabilities or [],
                denied_account_ids=denied_account_ids or [],
                denied_workspace_ids=denied_workspace_ids or [],
                denied_mailbox_ids=(
                    denied_mailbox_ids
                    if denied_mailbox_ids is not None
                    else sorted(extended["denied_mailbox_ids"])
                ),
                denied_send_account_ids=(
                    denied_send_account_ids
                    if denied_send_account_ids is not None
                    else sorted(extended["denied_send_account_ids"])
                ),
                denied_attachment_workspace_ids=(
                    denied_attachment_workspace_ids
                    if denied_attachment_workspace_ids is not None
                    else sorted(
                        extended["denied_attachment_workspace_ids"]
                    )
                ),
                permission_mode=(
                    permission_mode
                    or str(client.get("permission_mode") or "custom")
                ),
                account_scope_mode=(
                    account_scope_mode
                    or str(client.get("account_scope_mode") or "selected")
                ),
                mailbox_scope_mode=(
                    mailbox_scope_mode
                    or str(client.get("mailbox_scope_mode") or "selected")
                ),
                send_account_scope_mode=(
                    send_account_scope_mode
                    or str(
                        client.get("send_account_scope_mode") or "selected"
                    )
                ),
                workspace_scope_mode=(
                    workspace_scope_mode
                    or str(client.get("workspace_scope_mode") or "selected")
                ),
                attachment_scope_mode=(
                    attachment_scope_mode
                    or str(
                        client.get("attachment_scope_mode") or "selected"
                    )
                ),
                send_mode=(
                    send_mode or str(client.get("send_mode") or "confirm")
                ),
            )
        except (AgentAccessError, sqlite3.Error, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "permission_save_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="Client 权限已保存并立即生效",
            details={"permissions": rows},
        )

    def set_agent_client_state(
        self, client_id: str, state: str, *, enabled: bool | None = None
    ) -> ServiceResult:
        self.initialize()
        try:
            client = self._agent_integration.set_client_state(
                client_id, state, enabled=enabled
            )
        except (AgentAccessError, sqlite3.Error, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "client_state_update_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=(
                "Client 已撤销，后续调用立即拒绝"
                if state == "revoked"
                else "Client 状态已更新并立即生效"
            ),
            details={"client": client},
        )

    def rotate_agent_client_token(self, client_id: str) -> ServiceResult:
        self.initialize()
        client = self._agent_integration.get_client(client_id)
        if client is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="unknown_client",
                message="Agent Client 不存在",
            )
        previous_token = ""
        candidate_token = new_client_token()
        plan: ConfigPlan | None = None
        try:
            previous_token = self._agent_integration.get_scoped_token(client_id)
            configured_path = str(client.get("config_location") or "")
            should_update_config = bool(
                configured_path
                and str(client.get("config_status") or "")
                not in {"not_configured", "removed"}
                and str(client.get("config_mode") or "") == "managed"
            )
            if should_update_config:
                detection = detect_client(
                    str(client["client_type"]),
                    config_scope=str(client.get("config_scope") or "user"),
                )
                if detection.status != "managed_supported":
                    raise ClientConfigError(
                        "unsupported_client_version",
                        "当前 Client 版本未验证，已保留旧 token 和现有配置",
                    )
                plan = preview_client_config(
                    client_id=client_id,
                    client_type=str(client["client_type"]),
                    client_token=candidate_token,
                    config_scope=str(client.get("config_scope") or "user"),
                    target_path=Path(configured_path),
                )
            token = self._agent_integration.rotate_client_token(
                client_id, replacement_token=candidate_token
            )
            if plan is not None:
                backup_root = (
                    self.cfg.data_root_path
                    / "backups"
                    / "agent_client_configs"
                )
                applied = apply_client_config(plan, backup_root=backup_root)
                try:
                    insert_agent_config_backup(
                        self.cfg.db_path,
                        backup_id=applied.backup_id,
                        client_id=plan.client_id,
                        target_type=plan.client_type,
                        original_path=str(applied.target_path),
                        backup_path=str(applied.backup_path),
                        original_hash=applied.original_hash,
                        applied_hash=applied.applied_hash,
                        status="applied",
                    )
                    update_agent_client(
                        self.cfg.db_path,
                        client_id,
                        config_status="reload_required",
                    )
                except sqlite3.Error:
                    restore_client_config(
                        target_path=applied.target_path,
                        backup_path=applied.backup_path,
                        applied_hash=applied.applied_hash,
                    )
                    raise
                self._prune_agent_config_backups(client_id)
        except (
            AgentAccessError,
            ClientConfigError,
            CredentialError,
            sqlite3.Error,
            OSError,
        ) as exc:
            rollback_error: AgentAccessError | None = None
            if previous_token:
                try:
                    current = self._agent_integration.get_scoped_token(client_id)
                except (AgentAccessError, CredentialError):
                    current = ""
                if current == candidate_token:
                    try:
                        self._agent_integration.restore_client_token(
                            client_id,
                            expected_current=candidate_token,
                            restored_token=previous_token,
                        )
                    except AgentAccessError as restore_exc:
                        rollback_error = restore_exc
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=(
                    rollback_error.code
                    if rollback_error
                    else getattr(exc, "code", "token_rotation_failed")
                ),
                message=(
                    str(rollback_error)
                    if rollback_error
                    else f"{exc}；旧 token 保持有效"
                ),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=(
                "Client scoped token 与受管配置已一起轮换；请 reload 或重启 Client"
                if plan is not None
                else "Client scoped token 已轮换，旧 token 立即失效"
            ),
            details={
                "scoped_token": token,
                "config_updated": plan is not None,
                "reload_required": plan is not None,
            },
        )

    def resolve_agent_identity(
        self, client_id: str, client_token: str
    ) -> AgentIdentity:
        self.initialize()
        return self._agent_integration.resolve_identity(client_id, client_token)

    def require_agent_capability(
        self, identity: AgentIdentity, capability: str
    ) -> None:
        self._agent_integration.require_capability(identity, capability)

    def require_agent_account(
        self, identity: AgentIdentity, account_id: str | None
    ) -> str:
        return self._agent_integration.require_account(identity, account_id)

    def agent_mail_account_id(self, package_id: str) -> str:
        return self._agent_integration.account_for_mail(package_id)

    def require_agent_workspace(
        self, identity: AgentIdentity, requested: str | None
    ) -> tuple[str, str]:
        return self._agent_integration.require_workspace(identity, requested)

    def require_agent_submit_path(
        self, identity: AgentIdentity, file_path: str
    ) -> tuple[str, str]:
        return self._agent_integration.require_path_workspace(
            identity, file_path
        )

    def require_agent_mailbox(
        self, identity: AgentIdentity, mailbox_id: str | None
    ) -> str:
        return self._agent_integration.require_mailbox(identity, mailbox_id)

    def require_agent_send_account(
        self, identity: AgentIdentity, account_id: str | None
    ) -> str:
        return self._agent_integration.require_send_account(
            identity, account_id
        )

    def require_agent_attachment_path(
        self, identity: AgentIdentity, file_path: str
    ) -> tuple[str, str]:
        return self._agent_integration.require_attachment_path(
            identity, file_path
        )

    def require_agent_mail_access(
        self, identity: AgentIdentity, package_id: str
    ) -> dict[str, Any]:
        return self._authorized_source_message(identity, package_id)

    def list_authorized_mail_accounts(
        self, identity: AgentIdentity
    ) -> ServiceResult:
        """返回当前 Client 可读或可发件账号，不返回 Provider secret。"""
        self.initialize()
        accounts = []
        for row in query_mail_accounts(self.cfg.db_path, enabled_only=True):
            account_id = str(row["account_id"])
            can_read = account_id in identity.account_ids
            can_send = account_id in identity.send_account_ids
            if not can_read and not can_send:
                continue
            accounts.append(
                {
                    "account_id": account_id,
                    "display_name": str(row.get("display_name") or ""),
                    "email_address": str(row.get("email_address") or ""),
                    "provider": str(row.get("provider") or ""),
                    "can_read": can_read,
                    "can_send": can_send,
                    "send_mode": identity.send_mode if can_send else None,
                }
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "accounts": accounts,
                "send_mode": identity.send_mode,
            },
        )

    def list_authorized_mailboxes(
        self,
        identity: AgentIdentity,
        *,
        account_id: str | None = None,
    ) -> ServiceResult:
        """返回当前 Client 实际生效的目录范围。"""
        self.initialize()
        if account_id:
            self._agent_integration.require_account(identity, account_id)
        rows = [
            {
                "mailbox_id": str(row["mailbox_id"]),
                "account_id": str(row["account_id"]),
                "display_name": str(row.get("display_name") or ""),
                "raw_name": str(row.get("raw_name") or ""),
                "external_ref": str(row.get("external_ref") or ""),
                "parent_mailbox_id": row.get("parent_mailbox_id"),
                "delimiter": str(row.get("delimiter") or ""),
                "mailbox_role": str(row.get("mailbox_role") or "other"),
                "role_source": str(row.get("role_source") or "unknown"),
                "sync_enabled": bool(row.get("sync_enabled")),
            }
            for row in query_mailboxes(
                self.cfg.db_path,
                account_id=account_id,
                enabled_only=True,
            )
            if str(row["mailbox_id"]) in identity.mailbox_ids
        ]
        return ServiceResult(
            OperationStatus.SUCCESS, details={"mailboxes": rows}
        )

    def send_agent_mail(
        self,
        identity: AgentIdentity,
        *,
        request_id: str,
        operation: str,
        sender_account_id: str | None = None,
        to: list[Any] | None = None,
        cc: list[Any] | None = None,
        bcc: list[Any] | None = None,
        subject: str = "",
        body_text: str = "",
        body_html: str = "",
        links: list[dict[str, Any] | str] | None = None,
        source_package_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> ServiceResult:
        """创建通用发件请求；confirm 只持久化，autonomous 才立即发送。"""
        self.initialize()
        try:
            self._agent_integration.require_capability(
                identity, "mail.send"
            )
            account_id = self._agent_integration.require_send_account(
                identity, sender_account_id
            )
            context = self._account_router.context(
                account_id, capability="send"
            )
            source_message = None
            if str(operation or "").casefold() != "new":
                if not source_package_id:
                    raise OutboundMailError(
                        "source_mail_required",
                        "回复、回复全部或转发必须指定原邮件",
                    )
                source_message = self._authorized_source_message(
                    identity, source_package_id
                )
            prepared_attachments = self._authorize_agent_attachments(
                identity,
                attachments or [],
                default_source_package_id=source_package_id,
            )
            request, created = prepare_agent_send(
                self.cfg,
                client_id=identity.client_id,
                send_mode=identity.send_mode,
                sender_account_id=account_id,
                sender_address=str(context.account["email_address"]),
                sender_display_name=str(
                    context.account.get("display_name") or ""
                ),
                own_addresses=[
                    str(row.get("email_address") or "")
                    for row in query_mail_accounts(
                        self.cfg.db_path, enabled_only=True
                    )
                ],
                idempotency_key=request_id,
                operation=operation,
                to=to or [],
                cc=cc or [],
                bcc=bcc or [],
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                links=links or [],
                source_message=source_message,
                attachments=prepared_attachments,
            )
            if (
                created
                and identity.send_mode == "autonomous"
                and request.get("status") == "ready_to_send"
            ):
                request = self._execute_send_request(
                    str(request["send_request_id"])
                )
        except (
            AgentAccessError,
            AccountRuntimeError,
            OutboundMailError,
            SendRequestError,
            OSError,
            ValueError,
        ) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(
                    exc,
                    "code",
                    getattr(exc, "error_code", "send_request_failed"),
                ),
                message=str(exc),
            )
        status = str(request.get("status") or "")
        operation_status = (
            OperationStatus.PARTIAL
            if status in {"delivery_unknown", "sent_archive_failed"}
            else OperationStatus.FAILED
            if status in {"failed", "cancelled", "expired"}
            else OperationStatus.DUPLICATE
            if not created
            else OperationStatus.SUCCESS
        )
        return ServiceResult(
            operation_status,
            error_code=str(request.get("error_code") or "") or None,
            message=(
                "发件请求待用户在 AgentMailBridge 中确认"
                if status == "pending_confirmation"
                else "已返回原幂等发件请求"
                if not created
                else "邮件已发送"
                if status == "sent"
                else "发件请求已处理"
            ),
            details={
                "send_request": request,
                "send_request_id": request.get("send_request_id"),
                "send_status": status,
                "created": created,
            },
        )

    def get_agent_send_request_status(
        self, identity: AgentIdentity, send_request_id: str
    ) -> ServiceResult:
        self.initialize()
        expire_due_send_requests(self.cfg.db_path)
        request = query_send_request(self.cfg.db_path, send_request_id)
        if request is None or str(request.get("client_id") or "") != identity.client_id:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="send_request_not_found",
                message="发件请求不存在",
            )
        public = public_send_request(request)
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "send_request": public,
                "send_request_id": send_request_id,
                "send_status": public.get("status"),
            },
        )

    def list_pending_send_requests(self, limit: int = 100) -> ServiceResult:
        """GUI 使用的持久化待确认列表。"""
        self.initialize()
        expire_due_send_requests(self.cfg.db_path)
        rows = query_send_requests(
            self.cfg.db_path,
            statuses={"pending_confirmation"},
            limit=limit,
        )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "requests": [
                    self._pending_send_preview(row) for row in rows
                ]
            },
        )

    def confirm_agent_send_request(
        self, send_request_id: str
    ) -> ServiceResult:
        """仅供 GUI 用户动作调用，不暴露为 MCP 工具。"""
        self.initialize()
        try:
            request = self._execute_send_request(
                send_request_id, confirmed_by="gui"
            )
        except (
            AgentAccessError,
            AccountRuntimeError,
            OutboundMailError,
            SendRequestError,
            CredentialError,
            OSError,
            ValueError,
        ) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(
                    exc,
                    "code",
                    getattr(exc, "error_code", "send_confirmation_failed"),
                ),
                message=str(exc),
            )
        status = str(request.get("status") or "")
        return ServiceResult(
            OperationStatus.SUCCESS
            if status == "sent"
            else OperationStatus.PARTIAL
            if status in {"delivery_unknown", "sent_archive_failed"}
            else OperationStatus.FAILED,
            error_code=str(request.get("error_code") or "") or None,
            message=(
                "邮件已确认并发送"
                if status == "sent"
                else str(request.get("error_message") or "确认发送未完成")
            ),
            details={
                "send_request": request,
                "send_request_id": send_request_id,
                "send_status": status,
            },
        )

    def cancel_agent_send_request(
        self, send_request_id: str
    ) -> ServiceResult:
        self.initialize()
        try:
            expire_due_send_requests(self.cfg.db_path)
            request = cancel_pending_send_request(
                self.cfg.db_path, send_request_id
            )
        except (SendRequestError, sqlite3.Error) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "send_cancel_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="待确认发件请求已取消，不会发送",
            details={
                "send_request": public_send_request(request),
                "send_request_id": send_request_id,
                "send_status": "cancelled",
            },
        )

    def list_send_recovery_requests(self, limit: int = 100) -> ServiceResult:
        """GUI 读取待恢复请求；DTO 不包含本机原始路径。"""
        self.initialize()
        rows = query_recovery_requests(self.cfg.db_path, limit=limit)
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "requests": [public_send_request(row) for row in rows],
                "startup_recovery": dict(self._startup_recovery_result),
            },
        )

    def reconcile_agent_send_request(
        self, send_request_id: str
    ) -> ServiceResult:
        """手动重跑本地确定性对账；不会调用 SMTP。"""
        self.initialize()
        try:
            request = query_send_request(self.cfg.db_path, send_request_id)
            if request is None:
                raise SendRequestError(
                    "send_request_not_found", "发件请求不存在"
                )
            if str(request.get("status") or "") in {
                "smtp_accepted",
                "sent_archive_pending",
                "sent_archive_failed",
                "recovery_required",
            } and (
                request.get("smtp_accepted_at")
                or str(request.get("delivery_status") or "")
                in {"sent", "smtp_accepted"}
            ):
                context = self._account_router.context(
                    str(request["sender_account_id"]), capability="send"
                )
                recovered = recover_agent_send_archive(
                    self.cfg,
                    runtime_cfg=context.config,
                    send_request_id=send_request_id,
                )
                changed = True
                status = str(recovered.get("status") or "")
            else:
                result = reconcile_send_request_locally(
                    self.cfg.db_path, send_request_id
                )
                changed = bool(result.get("changed"))
                status = str(result.get("status") or "")
        except (SendRequestError, OutboundMailError, AccountRuntimeError, OSError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(
                    exc,
                    "code",
                    getattr(exc, "error_code", "send_reconciliation_failed"),
                ),
                message=str(exc),
            )
        current = query_send_request(self.cfg.db_path, send_request_id)
        return ServiceResult(
            OperationStatus.SUCCESS if changed else OperationStatus.PARTIAL,
            message="发件结果已完成对账" if changed else "暂未发现新的确定性证据",
            details={
                "changed": changed,
                "send_status": status,
                "send_request": public_send_request(current or {}),
            },
        )

    def mark_agent_send_resolution(
        self, send_request_id: str, *, resolution: str
    ) -> ServiceResult:
        """仅供 GUI 用户把未知结果标记为已发送或确定未发送。"""
        self.initialize()
        recovery_error = ""
        try:
            request = mark_send_request_resolution(
                self.cfg.db_path,
                send_request_id,
                resolution=resolution,
            )
            if (
                resolution == "sent"
                and str(request.get("status") or "") == "sent_archive_failed"
            ):
                context = self._account_router.context(
                    str(request["sender_account_id"]), capability="send"
                )
                recover_agent_send_archive(
                    self.cfg,
                    runtime_cfg=context.config,
                    send_request_id=send_request_id,
                )
                request = query_send_request(
                    self.cfg.db_path, send_request_id
                ) or request
        except (
            ValueError,
            sqlite3.Error,
            OSError,
            OutboundMailError,
            AccountRuntimeError,
            CredentialError,
        ) as exc:
            current = query_send_request(self.cfg.db_path, send_request_id)
            if (
                resolution == "sent"
                and current is not None
                and str(current.get("status") or "") == "sent_archive_failed"
            ):
                recovery_error = type(exc).__name__
                request = current
            else:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="invalid_send_resolution",
                    message=str(exc),
                )
        if recovery_error:
            return ServiceResult(
                OperationStatus.PARTIAL,
                error_code="manual_sent_archive_pending",
                message="已记录邮件确认发送；本地归档仍需恢复",
                details={"send_request": public_send_request(request)},
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=(
                "已记录用户确认：邮件已发送"
                if resolution == "sent"
                else "已记录用户确认：邮件确定未发送"
            ),
            details={"send_request": public_send_request(request)},
        )

    def clone_agent_send_request(
        self, send_request_id: str
    ) -> ServiceResult:
        """基于原固定内容创建新的 confirm 请求和全新幂等键。"""
        self.initialize()
        try:
            original = query_send_request(self.cfg.db_path, send_request_id)
            if original is None:
                raise SendRequestError(
                    "send_request_not_found", "发件请求不存在"
                )
            if str(original.get("status") or "") not in {
                "definitely_not_sent",
                "failed",
                "delivery_unknown",
            }:
                raise OutboundMailError(
                    "invalid_send_state", "当前状态不能创建新的发送请求"
                )
            client_id = str(original["client_id"])
            identity = self._current_send_identity(client_id)
            self._authorize_persisted_send(identity, original)
            context = self._account_router.context(
                str(original["sender_account_id"]), capability="send"
            )
            source_message = None
            source_package_id = str(original.get("source_package_id") or "")
            if str(original.get("operation") or "new") != "new":
                source_message = self._authorized_source_message(
                    identity, source_package_id
                )
            grouped: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
            for item in original.get("recipients") or []:
                kind = str(item.get("recipient_type") or "")
                if kind in grouped:
                    grouped[kind].append(str(item.get("email_address") or ""))
            attachments = [
                {
                    "path": str(item.get("snapshot_path") or ""),
                    "display_name": str(item.get("display_name") or ""),
                    "mime_type": str(item.get("mime_type") or ""),
                    "source_kind": "recovery_clone",
                }
                for item in original.get("attachments") or []
            ]
            created, _ = prepare_agent_send(
                self.cfg,
                client_id=client_id,
                send_mode="confirm",
                sender_account_id=str(original["sender_account_id"]),
                sender_address=str(context.account["email_address"]),
                sender_display_name=str(context.account.get("display_name") or ""),
                own_addresses=[
                    str(row.get("email_address") or "")
                    for row in query_mail_accounts(
                        self.cfg.db_path, enabled_only=True
                    )
                ],
                idempotency_key=f"recovery-{uuid.uuid4().hex}",
                operation=str(original.get("operation") or "new"),
                to=grouped["to"],
                cc=grouped["cc"],
                bcc=grouped["bcc"],
                subject=str(original.get("subject") or ""),
                body_text=str(original.get("body_text") or ""),
                body_html=str(original.get("body_html") or ""),
                source_message=source_message,
                attachments=attachments,
            )
        except (
            AgentAccessError,
            AccountRuntimeError,
            OutboundMailError,
            SendRequestError,
            OSError,
            ValueError,
        ) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(
                    exc,
                    "code",
                    getattr(exc, "error_code", "send_clone_failed"),
                ),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="已创建新的待确认发件请求，旧请求保持不变",
            details={"send_request": created},
        )

    def _execute_send_request(
        self,
        send_request_id: str,
        *,
        confirmed_by: str | None = None,
    ) -> dict[str, Any]:
        request = query_send_request(self.cfg.db_path, send_request_id)
        if request is None:
            raise SendRequestError(
                "send_request_not_found", "发件请求不存在"
            )
        client_id = str(request["client_id"])
        token = self._agent_integration.get_scoped_token(client_id)
        identity = self._agent_integration.resolve_identity(client_id, token)
        self._authorize_persisted_send(identity, request)
        context = self._account_router.context(
            str(request["sender_account_id"]), capability="send"
        )
        return execute_agent_send(
            self.cfg,
            runtime_cfg=context.config,
            send_request_id=send_request_id,
            sender_address=str(context.account["email_address"]),
            sender_display_name=str(
                context.account.get("display_name") or ""
            ),
            authorize=lambda current: self._authorize_persisted_send(
                self._current_send_identity(client_id), current
            ),
            confirmed_by=confirmed_by,
        )

    def _current_send_identity(self, client_id: str) -> AgentIdentity:
        token = self._agent_integration.get_scoped_token(client_id)
        return self._agent_integration.resolve_identity(client_id, token)

    def _authorize_persisted_send(
        self, identity: AgentIdentity, request: dict[str, Any]
    ) -> None:
        self._agent_integration.require_capability(identity, "mail.send")
        self._agent_integration.require_send_account(
            identity, str(request["sender_account_id"])
        )
        source_package_id = str(
            request.get("source_package_id") or ""
        )
        if source_package_id:
            self._authorized_source_message(identity, source_package_id)
        for item in request.get("attachments") or []:
            if str(item.get("source_kind") or "") == "mail_resource":
                package_id = str(item.get("source_package_id") or "")
                self._authorized_source_message(identity, package_id)
            else:
                self._agent_integration.require_attachment_path(
                    identity, str(item.get("source_path") or "")
                )

    def _authorized_source_message(
        self, identity: AgentIdentity, package_id: str
    ) -> dict[str, Any]:
        message = query_mail_message(self.cfg.db_path, package_id)
        if message is None:
            raise AgentAccessError("account_denied")
        self._agent_integration.require_account(
            identity, str(message.get("account_id") or "")
        )
        mailbox_ids = {
            str(item.get("mailbox_id") or "")
            for item in message.get("mailboxes") or []
            if item.get("mailbox_id")
        }
        if message.get("mailbox_id"):
            mailbox_ids.add(str(message["mailbox_id"]))
        if (
            not mailbox_ids
            and message.get("mailbox_ref")
            and message.get("account_id")
        ):
            mailbox_ids.add(
                stable_mailbox_id(
                    str(message["account_id"]),
                    str(message["mailbox_ref"]),
                )
            )
        if not mailbox_ids or mailbox_ids.isdisjoint(identity.mailbox_ids):
            raise AgentAccessError("mailbox_denied")
        return message

    def _authorize_agent_attachments(
        self,
        identity: AgentIdentity,
        attachments: list[dict[str, Any]],
        *,
        default_source_package_id: str | None,
    ) -> list[dict[str, Any]]:
        result = []
        for raw in attachments:
            item = dict(raw)
            resource_id = str(item.get("resource_id") or "")
            if resource_id:
                package_id = str(
                    item.get("source_package_id")
                    or default_source_package_id
                    or ""
                )
                if not package_id:
                    raise OutboundMailError(
                        "attachment_source_required",
                        "邮件资源附件必须指定所属邮件",
                    )
                self._authorized_source_message(identity, package_id)
                resource = next(
                    (
                        row
                        for row in query_mail_resources(
                            self.cfg.db_path, package_id
                        )
                        if str(row.get("resource_id") or "") == resource_id
                    ),
                    None,
                )
                if resource is None or not resource.get("absolute_path"):
                    raise OutboundMailError(
                        "attachment_not_found", "邮件资源附件不可用"
                    )
                result.append(
                    {
                        **item,
                        "path": str(resource["absolute_path"]),
                        "display_name": str(
                            item.get("display_name")
                            or resource.get("display_name")
                            or resource.get("original_name")
                            or resource_id
                        ),
                        "source_kind": "mail_resource",
                        "source_package_id": package_id,
                        "source_resource_id": resource_id,
                        "mime_type": str(
                            item.get("mime_type")
                            or resource.get("mime_type")
                            or "application/octet-stream"
                        ),
                    }
                )
                continue
            path = str(item.get("path") or "")
            self._agent_integration.require_attachment_path(identity, path)
            result.append({**item, "source_kind": "local"})
        return result

    def _pending_send_preview(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        public = public_send_request(request)
        client = self._agent_integration.get_client(
            str(request.get("client_id") or "")
        ) or {}
        account = query_mail_account(
            self.cfg.db_path, str(request.get("sender_account_id") or "")
        ) or {}
        return {
            **public,
            "client_display_name": str(client.get("display_name") or ""),
            "client_type": str(client.get("client_type") or ""),
            "sender_display_name": str(account.get("display_name") or ""),
            "sender_address": str(account.get("email_address") or ""),
        }

    def preview_agent_client_config(
        self,
        client_id: str,
        *,
        project_root: str | Path | None = None,
        remove: bool = False,
    ) -> ServiceResult:
        self.initialize()
        client = self._agent_integration.get_client(client_id)
        if client is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="unknown_client",
                message="Agent Client 不存在",
            )
        try:
            detection = detect_client(
                str(client["client_type"]),
                config_scope=str(client.get("config_scope") or "user"),
            )
            if detection.status != "managed_supported":
                raise ClientConfigError(
                    (
                        "client_not_installed"
                        if detection.status == "not_installed"
                        else "unsupported_client_version"
                    ),
                    (
                        "未检测到该 Client，请使用辅助配置"
                        if detection.status == "not_installed"
                        else "当前 Client 版本未验证，请使用辅助配置，未修改外部文件"
                    ),
                )
            token = self._agent_integration.get_scoped_token(client_id)
            plan = preview_client_config(
                client_id=client_id,
                client_type=str(client["client_type"]),
                client_token=token,
                config_scope=str(client.get("config_scope") or "user"),
                project_root=Path(project_root) if project_root else None,
                action="remove" if remove else "apply",
            )
        except (AgentAccessError, ClientConfigError, CredentialError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "config_parse_failed"),
                message=str(exc),
            )
        plan_id = "plan_" + uuid.uuid4().hex
        self._agent_config_plans[plan_id] = plan
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="配置预览已生成，尚未修改外部文件",
            details={
                "plan_id": plan_id,
                "client_id": client_id,
                "client_type": client["client_type"],
                "target_path": str(
                    Path(plan.target_path.parent.name) / plan.target_path.name
                ),
                "original_hash": plan.original_hash,
                "applied_hash": plan.applied_hash,
                "changed": plan.original_hash != plan.applied_hash,
                "preview": plan.preview,
                "action": plan.action,
            },
        )

    def apply_agent_client_config(self, plan_id: str) -> ServiceResult:
        self.initialize()
        plan = self._agent_config_plans.pop(str(plan_id), None)
        if plan is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="config_changed_concurrently",
                message="配置预览已失效，请重新预览",
            )
        backup_root = (
            self.cfg.data_root_path / "backups" / "agent_client_configs"
        )
        try:
            applied = apply_client_config(plan, backup_root=backup_root)
            try:
                backup = insert_agent_config_backup(
                    self.cfg.db_path,
                    backup_id=applied.backup_id,
                    client_id=plan.client_id,
                    target_type=plan.client_type,
                    original_path=str(applied.target_path),
                    backup_path=str(applied.backup_path),
                    original_hash=applied.original_hash,
                    applied_hash=applied.applied_hash,
                    status="applied",
                )
                update_agent_client(
                    self.cfg.db_path,
                    plan.client_id,
                    config_location=str(applied.target_path),
                    config_status=(
                        "removed" if plan.action == "remove" else "reload_required"
                    ),
                )
            except sqlite3.Error:
                restore_client_config(
                    target_path=applied.target_path,
                    backup_path=applied.backup_path,
                    applied_hash=applied.applied_hash,
                )
                raise
            pruned_count = self._prune_agent_config_backups(plan.client_id)
        except (ClientConfigError, sqlite3.Error, OSError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "config_write_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=(
                "已移除 AgentMailBridge 配置项"
                if plan.action == "remove"
                else "配置已安全写入；请 reload 或重启 Client"
            ),
            details={
                "backup": backup,
                "changed": applied.changed,
                "target_path": str(applied.target_path),
                "reload_required": True,
                "pruned_backup_count": pruned_count,
                "backup_security_notice": (
                    "配置备份可能含第三方 Client 的原有敏感值，仅保留在当前用户 DATA_ROOT；"
                    "默认最多 20 份、90 天，并至少保留最近一份可恢复备份。"
                ),
            },
        )

    def _prune_agent_config_backups(self, client_id: str) -> int:
        rows = query_agent_config_backups(self.cfg.db_path, client_id)
        if not rows:
            return 0
        backup_root = (
            self.cfg.data_root_path / "backups" / "agent_client_configs"
        ).resolve()
        valid_rows = [
            row
            for row in rows
            if Path(str(row.get("backup_path") or "")).is_file()
        ]
        preserve_id = str(valid_rows[0]["backup_id"]) if valid_rows else ""
        cutoff = datetime.now() - timedelta(
            days=AGENT_CONFIG_BACKUP_MAX_AGE_DAYS
        )
        removed = 0
        for index, row in enumerate(rows):
            backup_id = str(row.get("backup_id") or "")
            if backup_id == preserve_id:
                continue
            created = _parse_datetime(row.get("created_at"))
            expired = bool(created and created < cutoff)
            over_count = index >= AGENT_CONFIG_BACKUP_MAX_COUNT
            if not expired and not over_count:
                continue
            path = Path(str(row.get("backup_path") or ""))
            try:
                assert_within_root(path, backup_root)
                if path.is_file():
                    path.unlink()
                delete_agent_config_backup(self.cfg.db_path, backup_id)
                removed += 1
            except (OSError, SecurityError, sqlite3.Error):
                continue
        return removed

    def list_agent_client_config_backups(
        self, client_id: str
    ) -> ServiceResult:
        self.initialize()
        rows = query_agent_config_backups(self.cfg.db_path, client_id)
        for row in rows:
            row["original_path_display"] = Path(
                str(row.get("original_path") or "")
            ).name
            row["backup_path_display"] = Path(
                str(row.get("backup_path") or "")
            ).name
        return ServiceResult(OperationStatus.SUCCESS, details={"backups": rows})

    def restore_agent_client_config(self, backup_id: str) -> ServiceResult:
        self.initialize()
        backup = get_agent_config_backup(self.cfg.db_path, backup_id)
        if backup is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="config_restore_failed",
                message="配置备份不存在",
            )
        try:
            restored_hash = restore_client_config(
                target_path=Path(str(backup["original_path"])),
                backup_path=Path(str(backup["backup_path"])),
                applied_hash=str(backup.get("applied_hash") or "") or None,
            )
            update_agent_config_backup(
                self.cfg.db_path,
                backup_id,
                status="restored",
                restored_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            update_agent_client(
                self.cfg.db_path,
                str(backup["client_id"]),
                config_status="restored_reload_required",
            )
        except (ClientConfigError, sqlite3.Error, OSError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "config_restore_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="配置备份已恢复；请 reload 或重启 Client",
            details={"restored_hash": restored_hash, "reload_required": True},
        )

    def export_agent_client_config(self, client_id: str) -> ServiceResult:
        """显式导出辅助配置；结果含 scoped token，调用方不得记录。"""
        self.initialize()
        client = self._agent_integration.get_client(client_id)
        if client is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="unknown_client",
                message="Agent Client 不存在",
            )
        try:
            token = self._agent_integration.get_scoped_token(client_id)
        except (AgentAccessError, CredentialError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "client_auth_failed"),
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="已生成最小 stdio 配置；其中 scoped token 可随时撤销",
            details={
                "json": generic_mcp_json(
                    client_id=client_id, client_token=token
                ),
                "toml": codex_mcp_toml(
                    client_id=client_id, client_token=token
                ),
                "yaml": hermes_mcp_yaml(
                    client_id=client_id, client_token=token
                ),
                "contains_scoped_token": True,
            },
        )

    def test_agent_client_connection(self, client_id: str) -> ServiceResult:
        """启动真实 stdio 进程完成 initialize 与 tools/list。"""
        self.initialize()
        client = self._agent_integration.get_client(client_id)
        if client is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="unknown_client",
                message="Agent Client 不存在",
            )
        try:
            token = self._agent_integration.get_scoped_token(client_id)
        except (AgentAccessError, CredentialError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=getattr(exc, "code", "client_auth_failed"),
                message=str(exc),
            )
        command, args = mcp_launch()
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": str(client["client_type"]),
                        "version": "AgentMailBridge-GUI",
                    },
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        env = os.environ.copy()
        env.update(
            {
                "AGENT_MAIL_BRIDGE_DISABLE_DOTENV": "1",
                "DATA_ROOT": str(self.cfg.data_root_path),
                "MCP_MAIL_READ_ENABLED": (
                    "true" if self.cfg.mcp_mail_read_enabled else "false"
                ),
                "ALLOWED_SEND_ROOTS": os.pathsep.join(
                    str(path) for path in self.cfg.effective_allowed_send_roots
                ),
                CLIENT_ID_ENV: client_id,
                CLIENT_TOKEN_ENV: token,
            }
        )
        try:
            completed = subprocess.run(
                [command, *args],
                input="\n".join(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    for item in messages
                )
                + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env=env,
                check=False,
            )
            responses = [
                json.loads(line)
                for line in completed.stdout.splitlines()
                if line.strip()
            ]
            listed = next(
                row
                for row in responses
                if row.get("id") == 2 and isinstance(row.get("result"), dict)
            )
            tools = listed["result"].get("tools") or []
            names = {str(item.get("name")) for item in tools}
            expected = {
                "submit_result",
                "search_mails",
                "get_mail",
                "read_mail_resource",
                "prepare_mail_resources",
                "list_agent_workspaces",
                "get_mail_sync_status",
                "list_mail_accounts",
                "list_mailboxes",
                "send_mail",
                "get_send_request_status",
            }
            if completed.returncode != 0 or names != expected:
                raise ValueError("tools/list 未返回预期十一工具")
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            json.JSONDecodeError,
            StopIteration,
        ) as exc:
            update_agent_client(
                self.cfg.db_path, client_id, config_status="test_failed"
            )
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="connection_test_failed",
                message=f"连接测试失败：{exc}",
            )
        update_agent_client(
            self.cfg.db_path, client_id, config_status="connected"
        )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="连接测试通过：身份有效且七工具可见",
            details={"tool_count": len(names), "tools": sorted(names)},
        )

    def record_mcp_rejection(
        self,
        *,
        file_path: str = "",
        title: str | None = None,
        request_id: str | None = None,
        error_code: str,
        message: str,
    ) -> tuple[str, int]:
        """记录协议校验或频率限制导致的 MCP 拒绝。"""
        self.initialize()
        stable_request_id = request_id or str(uuid.uuid4())
        call_id = insert_mcp_call(
            self.cfg.db_path,
            request_id=stable_request_id,
            file_path=file_path,
            title=title,
            status=error_code,
        )
        update_mcp_call(
            self.cfg.db_path,
            call_id,
            status=error_code,
            error_code=error_code,
            message=message,
        )
        log_event(
            self.cfg.db_path,
            "WARNING",
            "mcp",
            f"MCP 提交被拒绝：request_id={stable_request_id}，状态={error_code}",
        )
        return stable_request_id, call_id

    def get_oauth_status(
        self, account_id: str | None = None
    ) -> ServiceResult:
        """获取 Gmail API 授权状态，不刷新也不打开浏览器。"""
        if account_id:
            self.initialize()
        oauth_cfg = self.cfg
        if account_id:
            try:
                context = self._account_router.context(
                    account_id, require_enabled=False
                )
                if context.account.get("provider") != "gmail":
                    raise AccountRuntimeError(
                        "oauth_not_supported", "该账号不使用 Gmail OAuth"
                    )
                oauth_cfg = context.config
            except AccountRuntimeError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
            except CredentialError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="credential_read_failed",
                    message=str(exc),
                )
        state = get_oauth_state(oauth_cfg)
        status = (
            OperationStatus.SUCCESS
            if state["state"] in {"READY", "TOKEN_EXPIRED_REFRESHABLE"}
            else OperationStatus.AUTH_REQUIRED
        )
        return ServiceResult(
            status, message=state["message"],
            needs_auth=status == OperationStatus.AUTH_REQUIRED,
            details=state,
        )

    def import_oauth_credentials(
        self,
        source: str | Path,
        *,
        replace: bool = False,
        account_id: str | None = None,
    ) -> ServiceResult:
        """验证并把 OAuth 客户端配置导入当前用户的受控目录。"""
        from agent_mail_bridge.oauth_storage import (
            OAuthImportError,
            import_oauth_credentials,
            validate_oauth_credentials_file,
        )

        if account_id:
            self.initialize()
        destination = self.cfg.gmail_api_credentials_path
        if account_id:
            try:
                destination, _token_path = self._account_router.oauth_paths(
                    account_id
                )
            except AccountRuntimeError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
            except CredentialError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="credential_read_failed",
                    message=str(exc),
                )
        try:
            target = import_oauth_credentials(
                Path(source),
                destination=destination,
                replace=replace,
            )
        except FileExistsError as exc:
            return ServiceResult(
                OperationStatus.CANCELLED,
                error_code="oauth_credentials_exists",
                message=str(exc),
            )
        except OAuthImportError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=exc.error_code,
                message=str(exc),
            )
        except OSError:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="credentials_unreadable",
                message="OAuth 客户端配置无法安全导入",
            )
        try:
            validated = validate_oauth_credentials_file(target)
        except OAuthImportError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=exc.error_code,
                message="OAuth 客户端配置已写入但复核失败，请重新导入",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="Desktop app OAuth 客户端配置已安全导入",
            details={
                "client_type": validated.summary.client_type,
                "project_id": validated.summary.project_id,
                "client_id_suffix": validated.summary.client_id_suffix,
            },
        )

    def create_gmail_oauth_session(
        self,
        *,
        account_id: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        timeout_seconds: float = 300.0,
    ):
        """创建一次可取消 OAuth 会话；创建本身不执行网络或浏览器操作。"""
        from agent_mail_bridge.gmail_api_auth import GmailOAuthSession

        oauth_cfg = self.cfg
        if account_id:
            self.initialize()
            context = self._account_router.context(
                account_id, require_enabled=False
            )
            if context.account.get("provider") != "gmail":
                raise AccountRuntimeError(
                    "oauth_not_supported", "该账号不使用 Gmail OAuth"
                )
            oauth_cfg = context.config
        return GmailOAuthSession(
            oauth_cfg,
            progress_callback=progress_callback,
            timeout_seconds=timeout_seconds,
        )

    def authorize_gmail_api(
        self,
        *,
        account_id: str | None = None,
        session=None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        timeout_seconds: float = 300.0,
    ) -> ServiceResult:
        """执行显式 OAuth 会话；GUI 必须从后台 Worker 调用。"""
        self.initialize()
        active_session = session or self.create_gmail_oauth_session(
            account_id=account_id,
            progress_callback=progress_callback,
            timeout_seconds=timeout_seconds,
        )
        return active_session.run()

    def clear_gmail_oauth_token(
        self, account_id: str | None = None
    ) -> ServiceResult:
        """清除本地 Token，但绝不删除 Desktop credentials。"""
        from agent_mail_bridge.gmail_api_auth import clear_local_gmail_token

        oauth_cfg = self.cfg
        if account_id:
            self.initialize()
            try:
                context = self._account_router.context(
                    account_id, require_enabled=False
                )
                if context.account.get("provider") != "gmail":
                    raise AccountRuntimeError(
                        "oauth_not_supported", "该账号不使用 Gmail OAuth"
                    )
                oauth_cfg = context.config
            except AccountRuntimeError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=exc.error_code,
                    message=str(exc),
                )
            except CredentialError as exc:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="credential_read_failed",
                    message=str(exc),
                )
        return clear_local_gmail_token(oauth_cfg)

    def get_today_files(self) -> ServiceResult:
        self.initialize()
        rows = list_received_files_for_date(self.cfg, fmt_date(datetime.now()))
        return ServiceResult(
            OperationStatus.SUCCESS, message=f"共 {len(rows)} 个文件",
            details={"files": rows},
        )

    def get_received_files(self, date_str: str) -> ServiceResult:
        """按日期获取安全边界内的收件文件。"""
        self.initialize()
        rows = list_received_files_for_date(self.cfg, date_str)
        return ServiceResult(OperationStatus.SUCCESS, details={"files": rows})

    def get_sent_files(self, date_str: str) -> ServiceResult:
        """按日期获取发送记录。"""
        self.initialize()
        rows = _sanitize_history_paths(
            query_sent_files_by_date(self.cfg.db_path, date_str),
            ("source_path", "send_copy_path", "sent_copy_path"),
            self.cfg.effective_allowed_send_roots,
        )
        return ServiceResult(OperationStatus.SUCCESS, details={"files": rows})

    def get_history(self, limit: int = 100) -> ServiceResult:
        self.initialize()
        if limit <= 0:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_limit",
                message="历史记录数量必须大于 0",
            )
        received = query_mail_messages(self.cfg.db_path, limit=limit)
        compatibility = _sanitize_history_paths(
            query_recent_received_messages(self.cfg.db_path, max(limit, 500)),
            ("body_file_path",), [self.cfg.data_root_path],
        )
        by_package = {
            str(row.get("package_id") or ""): row
            for row in compatibility if row.get("package_id")
        }
        by_message = {
            str(row.get("message_id") or "").casefold(): row
            for row in compatibility if row.get("message_id")
        }
        for row in received:
            legacy = by_package.get(str(row.get("package_id") or "")) or by_message.get(
                str(row.get("message_id") or "").casefold()
            ) or {}
            row["status"] = row.get("archive_status")
            row["created_at"] = row.get("received_at") or row.get("saved_at")
            row["body_file_path"] = str(legacy.get("body_file_path") or "")
            row["body_file_path_status"] = str(
                legacy.get("body_file_path_status") or "missing"
            )
        sent = query_recent_outbound_messages(self.cfg.db_path, limit)
        for row in sent:
            row["created_at"] = row.get("sent_at") or row.get("created_at")
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"received": received, "sent": sent},
        )

    def list_outbound_messages(self, limit: int = 100) -> ServiceResult:
        self.initialize()
        if limit <= 0:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_limit",
                message="发送记录数量必须大于 0",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"messages": query_recent_outbound_messages(self.cfg.db_path, limit)},
        )

    def get_outbound_message(self, outbound_id: str) -> ServiceResult:
        self.initialize()
        row = query_outbound_message(self.cfg.db_path, outbound_id)
        if row is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="outbound_not_found",
                message="发送邮件不存在",
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"message": row})

    def list_agent_workspaces(self) -> ServiceResult:
        """只列出用户显式授权工作区，DATA_ROOT 保持内部边界。"""
        details = workspace_dtos(self.cfg)
        roots = [str(item["display_path"]) for item in details]
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "workspaces": roots,
                "workspace_details": details,
                "takes_effect": "next_mcp_session",
            },
        )

    def add_agent_workspace(
        self,
        path: str | Path,
        *,
        env_path: Path | None = None,
    ) -> ServiceResult:
        from agent_mail_bridge.runtime_paths import get_runtime_paths
        from agent_mail_bridge.ui.settings_store import save_env_values

        runtime = get_runtime_paths()
        try:
            resolved = validate_agent_workspace_root(
                path,
                sensitive_roots=(
                    runtime.user_config_root,
                    runtime.oauth_root,
                    runtime.data_root,
                    self.cfg.data_root_path,
                ),
            )
        except SecurityError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="workspace_not_allowed",
                message=str(exc),
            )
        current = [Path(item).resolve() for item in self.cfg.allowed_send_roots]
        for item in current:
            if resolved == item:
                return ServiceResult(
                    OperationStatus.NO_CHANGES,
                    error_code="workspace_duplicate",
                    message="该资料目录已授权",
                    details={"workspace": str(resolved)},
                )
            try:
                resolved.relative_to(item)
                return ServiceResult(
                    OperationStatus.NO_CHANGES,
                    error_code="workspace_nested",
                    message="该目录已包含在现有授权资料目录中",
                    details={"workspace": str(resolved)},
                )
            except ValueError:
                pass
            try:
                item.relative_to(resolved)
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="workspace_broader_than_existing",
                    message="新目录会扩大现有授权范围，请先移除较小资料目录后再明确授权",
                )
            except ValueError:
                pass
        updated = [*current, resolved]
        try:
            save_env_values(
                {"ALLOWED_SEND_ROOTS": os.pathsep.join(str(item) for item in updated)},
                env_path,
            )
        except OSError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="workspace_save_failed",
                message=f"保存资料目录授权失败：{exc}",
            )
        self.cfg.allowed_send_roots = updated
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="资料目录已授权；新授权将在下一次 MCP 会话生效",
            details={"workspace": str(resolved), "takes_effect": "next_mcp_session"},
        )

    def remove_agent_workspace(
        self,
        path: str | Path,
        *,
        env_path: Path | None = None,
    ) -> ServiceResult:
        from agent_mail_bridge.ui.settings_store import save_env_values

        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except OSError:
            resolved = Path(path).expanduser().resolve()
        current = [Path(item).resolve() for item in self.cfg.allowed_send_roots]
        updated = [item for item in current if item != resolved]
        if len(updated) == len(current):
            return ServiceResult(
                OperationStatus.NO_CHANGES,
                error_code="workspace_not_found",
                message="该资料目录未授权",
            )
        try:
            save_env_values(
                {"ALLOWED_SEND_ROOTS": os.pathsep.join(str(item) for item in updated)},
                env_path,
            )
        except OSError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="workspace_save_failed",
                message=f"保存资料目录授权失败：{exc}",
            )
        self.cfg.allowed_send_roots = updated
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="资料目录授权已移除；下一次 MCP 会话将使用新范围",
            details={"workspace": str(resolved), "takes_effect": "next_mcp_session"},
        )

    def list_mail_messages(self, **filters: Any) -> ServiceResult:
        """返回邮件级事实 DTO，不返回 raw.eml bytes。"""
        self.initialize()
        try:
            rows = query_mail_messages(self.cfg.db_path, **filters)
        except (TypeError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_mail_query", message=str(exc)
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"messages": rows})

    def get_mail_message(self, package_id: str) -> ServiceResult:
        self.initialize()
        row = query_mail_message(self.cfg.db_path, package_id)
        if row is None:
            return ServiceResult(
                OperationStatus.FAILED, error_code="mail_not_found", message="邮件不存在"
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"message": row})

    def list_mail_resources(self, package_id: str) -> ServiceResult:
        self.initialize()
        if query_mail_message(self.cfg.db_path, package_id) is None:
            return ServiceResult(
                OperationStatus.FAILED, error_code="mail_not_found", message="邮件不存在"
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"resources": query_mail_resources(self.cfg.db_path, package_id)},
        )

    def list_mail_threads(self, **filters: Any) -> ServiceResult:
        self.initialize()
        try:
            rows = query_mail_threads(self.cfg.db_path, **filters)
        except (TypeError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_mail_query", message=str(exc)
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"threads": rows})

    def get_mail_thread(
        self,
        thread_ref: str,
        *,
        account_id: str | None = None,
        account_ref: str | None = None,
    ) -> ServiceResult:
        self.initialize()
        row = query_mail_thread(
            self.cfg.db_path,
            thread_ref,
            account_id=account_id,
            account_ref=account_ref,
        )
        if row is None:
            return ServiceResult(
                OperationStatus.FAILED, error_code="thread_not_found", message="邮件会话不存在"
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"thread": row})

    def search_mail_facts(self, query: str, **filters: Any) -> ServiceResult:
        self.initialize()
        try:
            rows = query_mail_facts(self.cfg.db_path, query, **filters)
        except (TypeError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_mail_query", message=str(exc)
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"messages": rows})

    def get_mcp_mail_read_access(self) -> ServiceResult:
        """返回本机 Agent 邮件读取总开关，不涉及任何凭据。"""
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "enabled": bool(self.cfg.mcp_mail_read_enabled),
                "scope": "local_mail_archive",
                "submit_result_available": True,
            },
        )

    def set_mcp_mail_read_access(
        self, enabled: bool, *, env_path: Path | None = None
    ) -> ServiceResult:
        """持久化一次性本机邮件读取授权；关闭不影响 submit_result。"""
        from agent_mail_bridge.ui.settings_store import save_env_values

        try:
            save_env_values(
                {"MCP_MAIL_READ_ENABLED": "true" if enabled else "false"},
                env_path,
            )
        except OSError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="read_access_save_failed",
                message=f"保存邮件读取授权失败：{exc}",
            )
        self.cfg.mcp_mail_read_enabled = bool(enabled)
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="已允许本机 Agent 读取本地邮件归档" if enabled else "已关闭本机 Agent 邮件读取",
            details={"enabled": bool(enabled), "submit_result_available": True},
        )

    def search_mails(
        self,
        *,
        query: str = "",
        time_scope: str = "all",
        recent_days: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        subject: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        has_attachments: bool | None = None,
        status: str | None = None,
        direction: str | None = None,
        sort: str = "newest",
        limit: int | None = None,
        offset: int = 0,
        ensure_fresh: bool = False,
        allow_cached: bool = True,
        account_id: str | None = None,
        account_ref: str | None = None,
        mailbox_id: str | None = None,
        mailbox_ref: str | None = None,
        mailbox_ids: list[str] | None = None,
    ) -> ServiceResult:
        """按明确结构搜索本地邮件；可先执行受控同步新鲜度检查。"""
        disabled = self._mail_read_disabled()
        if disabled:
            return disabled
        self.initialize()
        try:
            start, end = _mail_time_range(
                time_scope, recent_days=recent_days, date_from=date_from, date_to=date_to
            )
            safe_limit = 1 if limit is None and time_scope == "latest" else int(limit or 20)
            if safe_limit <= 0 or safe_limit > 100 or int(offset) < 0:
                raise ValueError("limit 必须在 1 到 100 之间，offset 必须为非负整数")
            if direction is not None:
                direction = str(direction).strip().casefold()
                if direction not in {"inbound", "outbound"}:
                    raise ValueError("direction 仅支持 inbound 或 outbound")
            if account_id and account_id not in {
                str(item["account_id"])
                for item in query_mail_accounts(self.cfg.db_path, enabled_only=True)
            }:
                raise ValueError("account_id 不存在或未启用")
        except (TypeError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_range", message=str(exc)
            )

        sync_account_id = account_id or current_receive_account_id(self.cfg)
        sync_before = self.get_mail_sync_status(
            account_id=sync_account_id
        ).details
        sync_triggered = False
        sync_error: dict[str, str] | None = None
        cache_is_stale = sync_before.get("freshness") != "fresh"
        if ensure_fresh and cache_is_stale:
            sync_triggered = True
            received = self.receive(
                account_id=account_id,
                automatic=True,
                wait_for_process_lock=5.0,
            )
            if received.error_code in {"sync_in_progress", "receive_busy"}:
                return ServiceResult(
                    OperationStatus.CANCELLED,
                    error_code="sync_in_progress",
                    message="其他进程正在同步邮件，请稍后重试",
                    details={"sync": sync_before, "sync_triggered": True},
                )
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            success_at = now_text if received.ok else sync_before.get("last_success_at")
            save_auto_receive_state(
                self.cfg.db_path,
                account_id=sync_account_id,
                last_check_at=now_text,
                last_success_at=success_at,
                last_result=received.message,
                last_error=None if received.ok else received.message,
                consecutive_global_failures=(
                    0 if received.ok else int(sync_before.get("consecutive_global_failures") or 0) + 1
                ),
            )
            if not received.ok:
                sync_error = {
                    "error_code": received.error_code or "sync_failed",
                    "message": _redact_text(received.message, self.cfg),
                }
                if not allow_cached:
                    return ServiceResult(
                        OperationStatus.FAILED,
                        error_code="sync_failed",
                        message="邮件同步失败，未使用本地缓存",
                        details={"sync_error": sync_error, "sync_triggered": True},
                    )
            else:
                cache_is_stale = False

        try:
            rows = query_mail_facts(
                self.cfg.db_path,
                query,
                account_id=account_id,
                account_ref=account_ref,
                mailbox_id=mailbox_id,
                mailbox_ids=mailbox_ids,
                mailbox_ref=mailbox_ref,
                date_from=start,
                date_to=end,
                subject=subject,
                sender=sender,
                recipient=recipient,
                has_attachments=has_attachments,
                status=status,
                mail_direction=direction,
                sort=sort,
                limit=safe_limit,
                offset=int(offset),
            )
        except (TypeError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_mail_query", message=str(exc)
            )
        messages = [_mail_search_summary(row) for row in rows]
        sync_after = self.get_mail_sync_status(
            account_id=sync_account_id
        ).details
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="未找到匹配邮件" if not messages else f"找到 {len(messages)} 封邮件",
            details={
                "messages": messages,
                "result_count": len(messages),
                "limit": safe_limit,
                "offset": int(offset),
                "has_more": len(messages) == safe_limit,
                "cached": bool(cache_is_stale or sync_error),
                "sync_triggered": sync_triggered,
                "sync_error": sync_error,
                "sync": sync_after,
            },
        )

    def get_mail(
        self, package_id: str, *, offset: int = 0, max_chars: int = 20_000
    ) -> ServiceResult:
        """返回有界完整正文、资源清单和 raw 描述。"""
        disabled = self._mail_read_disabled()
        if disabled:
            return disabled
        self.initialize()
        try:
            safe_offset = int(offset)
            safe_chars = int(max_chars)
            if safe_offset < 0 or safe_chars <= 0 or safe_chars > MAX_TEXT_CHARS:
                raise ValueError(f"offset 必须非负，max_chars 必须在 1 到 {MAX_TEXT_CHARS} 之间")
        except (TypeError, ValueError) as exc:
            return ServiceResult(OperationStatus.FAILED, error_code="invalid_range", message=str(exc))
        message = query_mail_message(self.cfg.db_path, package_id)
        if message is None:
            return ServiceResult(OperationStatus.FAILED, error_code="mail_not_found", message="邮件不存在")
        try:
            _validate_agent_mail_paths(message, self.cfg)
        except MailAccessError as exc:
            return ServiceResult(OperationStatus.FAILED, error_code=exc.code, message=exc.message)
        message = _safe_agent_mail_message(message, self.cfg)
        resources = [enrich_resource_descriptor(row) for row in message.get("resources") or []]
        message["resources"] = resources
        raw_package = query_raw_mail_package(self.cfg.db_path, package_id) or {}
        readable_text = str(raw_package.get("search_text") or "")
        body_resource = next(
            (row for row in resources if row.get("internal_type") == "body_plain"),
            next((row for row in resources if row.get("internal_type") == "body_readable"), None),
        )
        if readable_text:
            if safe_offset > len(readable_text):
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="invalid_range",
                    message="offset 超出邮件正文字符范围",
                    details={"character_count": len(readable_text)},
                )
            expected_body_sha = str(raw_package.get("body_text_sha256") or "")
            actual_body_sha = sha256_of_bytes(readable_text.encode("utf-8"))
            if expected_body_sha and expected_body_sha.casefold() != actual_body_sha.casefold():
                body = {"content": "", "format": "unavailable", "status": "hash_mismatch", "has_more": False}
            else:
                body = _text_value_page(readable_text, safe_offset, safe_chars)
                body.update({"format": "plain" if raw_package.get("body_plain_path") else "readable", "encoding": "utf-8"})
        elif body_resource:
            try:
                page = read_archived_mail_resource(
                    message,
                    str(body_resource["resource_id"]),
                    mode="text",
                    offset=safe_offset,
                    max_chars=safe_chars,
                )
                body = {
                    "content": page["content"],
                    "format": str(body_resource.get("internal_type") or "body_readable"),
                    "encoding": page.get("encoding"),
                    "character_count": page.get("character_count", 0),
                    "offset": page.get("offset", 0),
                    "next_offset": page.get("next_offset"),
                    "has_more": page.get("has_more", False),
                }
            except MailAccessError as exc:
                body = {"content": "", "format": "unavailable", "status": exc.code, "has_more": False}
        else:
            body = {"content": "", "format": "no_body", "character_count": 0, "offset": 0, "next_offset": None, "has_more": False}
        raw = dict(message.get("raw_eml") or {})
        raw["resource_id"] = "raw.eml"
        raw_path = Path(str(message.get("package_root") or "")) / str(raw.get("path") or "")
        raw["available"] = bool(raw.get("path") and raw_path.is_file())
        raw["size_bytes"] = raw_path.stat().st_size if raw["available"] else None
        message["raw_eml"] = raw
        message["body"] = body
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"mail": message, "bytes_returned": len(body["content"].encode("utf-8"))},
        )

    def read_mail_resource(self, package_id: str, resource_id: str, **options: Any) -> ServiceResult:
        disabled = self._mail_read_disabled()
        if disabled:
            return disabled
        self.initialize()
        message = query_mail_message(self.cfg.db_path, package_id)
        if message is None:
            return ServiceResult(OperationStatus.FAILED, error_code="mail_not_found", message="邮件不存在")
        try:
            _validate_agent_mail_paths(message, self.cfg)
            details = read_archived_mail_resource(message, resource_id, **options)
        except MailAccessError as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code=exc.code, message=exc.message, details=exc.details
            )
        return ServiceResult(OperationStatus.SUCCESS, details={"resource": details, **details})

    def prepare_mail_resources(
        self,
        package_id: str,
        resource_ids: list[str] | None = None,
        **options: Any,
    ) -> ServiceResult:
        disabled = self._mail_read_disabled()
        if disabled:
            return disabled
        self.initialize()
        message = query_mail_message(self.cfg.db_path, package_id)
        if message is None:
            return ServiceResult(OperationStatus.FAILED, error_code="mail_not_found", message="邮件不存在")
        try:
            _validate_agent_mail_paths(message, self.cfg)
            details = prepare_resources_to_workspace(
                self.cfg, message, resource_ids, **options
            )
        except MailAccessError as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code=exc.code, message=exc.message, details=exc.details
            )
        status = (
            OperationStatus.PARTIAL
            if details["status"] == "partial"
            else OperationStatus.FAILED
            if details["status"] == "failed"
            else OperationStatus.SUCCESS
        )
        return ServiceResult(
            status,
            error_code=(
                "preparation_failed" if details["failed_count"] else None
            ),
            message=(
                "完整邮件资料已原子准备到 Agent 可用资料目录"
                if details.get("mode") == "complete"
                else f"已准备 {details['prepared_count']} 个资源"
                if not details["failed_count"]
                else f"已准备 {details['prepared_count']} 个，失败 {details['failed_count']} 个"
            ),
            details=details,
        )

    def get_mail_sync_status(
        self, account_id: str | None = None
    ) -> ServiceResult:
        """返回持久化调度、新鲜度、重试和跨进程同步状态。"""
        self.initialize()
        target_account_id = account_id or current_receive_account_id(self.cfg)
        if account_id and query_mail_account(
            self.cfg.db_path, target_account_id
        ) is None:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="account_not_found",
                message="邮箱账号不存在或已移除",
            )
        state = get_auto_receive_state(
            self.cfg.db_path, account_id=target_account_id
        )
        retries = count_receive_retries(
            self.cfg.db_path, account_id=target_account_id
        )
        newest = query_mail_messages(
            self.cfg.db_path, account_id=target_account_id, limit=1
        )
        latest_local_at = None
        if newest:
            latest_local_at = newest[0].get("saved_at") or newest[0].get("received_at")
        now = datetime.now()
        last_success = _parse_datetime(state.get("last_success_at"))
        latest_local = _parse_datetime(latest_local_at)
        age = max(0, int((now - last_success).total_seconds())) if last_success else None
        local_age = max(0, int((now - latest_local).total_seconds())) if latest_local else None
        threshold = int(self.cfg.mcp_mail_freshness_seconds)
        freshness = "unknown" if age is None else "fresh" if age <= threshold else "stale"
        lock_paths = [
            self.cfg.data_root_path
            / ".locks"
            / f"receive-{target_account_id}.lock"
        ]
        if account_id is None:
            lock_paths.append(
                self.cfg.data_root_path / ".locks" / "receive.lock"
            )
        details = {
            **state,
            **retries,
            "enabled": bool(state.get("enabled")),
            "background_status": "running" if state.get("enabled") else "stopped",
            "is_syncing": any(
                not is_lock_available(lock_path) for lock_path in lock_paths
            ),
            "freshness": freshness,
            "freshness_threshold_seconds": threshold,
            "data_age_seconds": age,
            "latest_local_mail_at": latest_local_at,
            "latest_local_mail_age_seconds": local_age,
            "last_error": _redact_text(str(state.get("last_error") or ""), self.cfg),
            "account_id": target_account_id,
            "accounts": query_mail_accounts(self.cfg.db_path, enabled_only=True),
        }
        return ServiceResult(OperationStatus.SUCCESS, details=details)

    def _mail_read_disabled(self) -> ServiceResult | None:
        if self.cfg.mcp_mail_read_enabled:
            return None
        return ServiceResult(
            OperationStatus.FAILED,
            error_code="read_access_disabled",
            message="本机 Agent 邮件读取尚未启用，请在 Agent/MCP 页面开启总开关",
        )

    def list_trusted_domains(self) -> ServiceResult:
        """默认空列表；只返回域名规则，不包含邮件 URL。"""
        self.initialize()
        rows = query_trusted_domains(self.cfg.db_path)
        for row in rows:
            row["include_subdomains"] = bool(row.get("include_subdomains"))
            row["enabled"] = bool(row.get("enabled"))
        return ServiceResult(OperationStatus.SUCCESS, details={"domains": rows})

    def set_trusted_domain(
        self,
        domain: str,
        *,
        include_subdomains: bool = False,
        enabled: bool = True,
    ) -> ServiceResult:
        self.initialize()
        try:
            normalized = normalize_trusted_domain(domain)
        except ValueError as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_trusted_domain", message=str(exc)
            )
        upsert_trusted_domain(
            self.cfg.db_path, normalized,
            include_subdomains=include_subdomains, enabled=enabled,
        )
        return ServiceResult(
            OperationStatus.SUCCESS, message="可信域名规则已保存",
            details={"domain": normalized},
        )

    def remove_trusted_domain(self, domain: str) -> ServiceResult:
        self.initialize()
        try:
            normalized = normalize_trusted_domain(domain)
        except ValueError as exc:
            return ServiceResult(
                OperationStatus.FAILED, error_code="invalid_trusted_domain", message=str(exc)
            )
        delete_trusted_domain(self.cfg.db_path, normalized)
        return ServiceResult(OperationStatus.SUCCESS, message="可信域名规则已移除")

    def get_managed_files(self, limit: int = 500) -> ServiceResult:
        """返回统一受管文件 DTO，不从 received_messages 推导文件大小。"""
        self.initialize()
        if limit <= 0:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_limit",
                message="文件记录数量必须大于 0",
            )
        try:
            rows = query_managed_files(self.cfg, limit)
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="managed_files_failed",
                message=f"读取受管文件失败：{exc}",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"共 {len(rows)} 个受管文件",
            details={"files": rows},
        )

    def get_recent_logs(
        self, limit: int = 50, *, include_daily_checks: bool = False
    ) -> ServiceResult:
        self.initialize()
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "events": query_recent_events(
                    self.cfg.db_path,
                    max(1, limit),
                    include_daily_checks=include_daily_checks,
                )
            },
        )

    def query_logs(
        self,
        *,
        level: str = "",
        category: str = "",
        date_from: str | None = None,
        search: str = "",
        include_daily_checks: bool = False,
        limit: int = 150,
        offset: int = 0,
    ) -> ServiceResult:
        self.initialize()
        levels = (level,) if level and level != "全部级别" else ()
        event_types = LOG_EVENT_CATEGORIES.get(category, ())
        result = query_app_events(
            self.cfg.db_path,
            levels=levels,
            event_types=event_types,
            date_from=date_from,
            search=search,
            include_daily_checks=include_daily_checks,
            limit=limit,
            offset=offset,
        )
        for event in result["events"]:
            event["category"] = _event_category(str(event.get("event_type") or ""))
        return ServiceResult(OperationStatus.SUCCESS, details=result)

    def get_log_overview(self) -> ServiceResult:
        self.initialize()
        details = app_event_overview(
            self.cfg.db_path,
            normal_days=self.cfg.normal_log_retention_days,
            error_days=self.cfg.warning_error_log_retention_days,
        )
        details.update({
            "normal_days": self.cfg.normal_log_retention_days,
            "error_days": self.cfg.warning_error_log_retention_days,
            "max_count": self.cfg.app_event_max_count,
            "last_run_result": dict(self._last_event_maintenance_result),
        })
        return ServiceResult(OperationStatus.SUCCESS, details=details)

    def set_log_retention(
        self, *, normal_days: int, error_days: int, max_count: int
    ) -> ServiceResult:
        self.initialize()
        if normal_days not in {7, 30, 90}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_normal_retention",
                message="普通日志保留天数不受支持",
            )
        if error_days not in {30, 90, 180}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_error_retention",
                message="错误日志保留天数不受支持",
            )
        if max_count not in {5_000, 10_000, 20_000}:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="invalid_event_max_count",
                message="技术事件数量上限不受支持",
            )
        self.cfg.normal_log_retention_days = normal_days
        self.cfg.warning_error_log_retention_days = error_days
        self.cfg.app_event_max_count = max_count
        configure_app_event_retention(self.cfg.db_path, max_count=max_count)
        return ServiceResult(OperationStatus.SUCCESS, message="日志保留设置已更新")

    def prune_logs(self, *, record_event: bool = True) -> ServiceResult:
        self.initialize()
        try:
            details = prune_app_events(
                self.cfg.db_path,
                normal_days=self.cfg.normal_log_retention_days,
                error_days=self.cfg.warning_error_log_retention_days,
                max_count=self.cfg.app_event_max_count,
            )
            self._last_event_maintenance_result = dict(details)
            self._last_event_maintenance_at = datetime.now()
            if record_event:
                log_event(
                    self.cfg.db_path,
                    "SUCCESS",
                    "log_maintenance",
                    f"技术日志清理完成：删除 {details['deleted']} 条，剩余 {details['after']} 条",
                )
            return ServiceResult(
                OperationStatus.SUCCESS,
                message=f"已清理 {details['deleted']} 条过期或超限技术日志",
                details=details,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("技术日志清理失败")
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="log_prune_failed",
                message=f"技术日志清理失败：{exc}",
            )

    def schedule_event_maintenance(self, *, force: bool = False) -> bool:
        """启动后与每 24 小时异步清理一次，不占用 GUI 线程。"""
        now = datetime.now()
        if (
            not force
            and self._last_event_maintenance_at is not None
            and now - self._last_event_maintenance_at < timedelta(hours=24)
        ):
            return False
        if not self._event_maintenance_lock.acquire(blocking=False):
            return False
        self._last_event_maintenance_at = now

        def run() -> None:
            try:
                result = self.prune_logs(record_event=False)
                deleted = int(result.details.get("deleted") or 0) if result.ok else 0
                if not result.ok:
                    self._last_event_maintenance_at = None
                if deleted:
                    log_event(
                        self.cfg.db_path,
                        "SUCCESS",
                        "log_maintenance",
                        f"自动技术日志清理完成：删除 {deleted} 条",
                    )
            finally:
                # 后台线程的线程本地 SQLite 连接必须在该线程内显式释放；
                # 否则 Windows 上隔离数据目录可能在进程存活期间保持占用。
                close_connection()
                self._event_maintenance_lock.release()

        threading.Thread(
            target=run,
            name="AgentMailBridgeLogMaintenance",
            daemon=True,
        ).start()
        return True

    def clear_daily_check_logs(self) -> ServiceResult:
        self.initialize()
        deleted = clear_daily_check_events(self.cfg.db_path)
        log_event(
            self.cfg.db_path, "SUCCESS", "log_maintenance",
            f"已清除日常自动检查技术日志 {deleted} 条",
        )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"已清除 {deleted} 条日常自动检查技术日志",
            details={"deleted": deleted},
        )

    def clear_all_technical_logs(self) -> ServiceResult:
        self.initialize()
        deleted = clear_all_app_events(self.cfg.db_path)
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"已清空 {deleted} 条技术日志；邮件、附件、收发历史和 MCP 审计未改动",
            details={"deleted": deleted},
        )

    def export_filtered_logs(
        self,
        destination: str | Path,
        **filters: Any,
    ) -> ServiceResult:
        self.initialize()
        path = Path(destination)
        if path.suffix.lower() != ".csv":
            path = path.with_suffix(".csv")
        if path.exists():
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="export_exists",
                message="目标文件已存在，请选择新的文件名",
            )
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < self.cfg.app_event_max_count:
            page = self.query_logs(limit=500, offset=offset, **filters)
            if not page.ok:
                return page
            items = page.details.get("events", [])
            rows.extend(items)
            if len(items) < 500:
                break
            offset += len(items)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(("时间", "级别", "事件类型", "脱敏消息"))
                for row in rows:
                    writer.writerow((
                        row.get("created_at", ""),
                        row.get("level", ""),
                        row.get("category", "系统与诊断"),
                        _redact_event_message(str(row.get("message") or "")),
                    ))
        except OSError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="filtered_log_export_failed",
                message=f"筛选日志导出失败：{exc}",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"已导出 {len(rows)} 条脱敏筛选日志",
            details={"path": str(path), "count": len(rows)},
        )

    def scan_file_status(self) -> ServiceResult:
        self.initialize()
        changes = scan_file_status(self.cfg)
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"发现 {len(changes)} 项变化",
            details={"changes": changes},
        )

    def get_maintenance_status(self) -> ServiceResult:
        self.initialize()
        try:
            return ServiceResult(OperationStatus.SUCCESS, details=data_statistics(self.cfg))
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="maintenance_status_failed",
                message=f"读取维护状态失败：{exc}",
            )

    def get_mail_health_status(self) -> ServiceResult:
        self.initialize()
        try:
            health = get_mail_health(self.cfg)
        except (OSError, sqlite3.Error) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="mail_health_failed",
                message=f"邮件运行状态读取失败：{type(exc).__name__}",
            )
        return ServiceResult(OperationStatus.SUCCESS, details=health)

    def preview_safe_cleanup(self) -> ServiceResult:
        self.initialize()
        try:
            details = preview_send_snapshot_cleanup(self.cfg)
        except (OSError, sqlite3.Error, SecurityError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="cleanup_preview_failed",
                message=f"安全清理预览失败：{type(exc).__name__}",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="已生成安全清理预览，尚未删除任何文件",
            details=details,
        )

    def execute_safe_cleanup(
        self,
        *,
        confirmed: bool = False,
        request_ids: list[str] | None = None,
    ) -> ServiceResult:
        self.initialize()
        if not confirmed:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="cleanup_confirmation_required",
                message="执行安全清理前必须由用户明确确认",
            )
        try:
            details = cleanup_send_snapshots(
                self.cfg,
                dry_run=False,
                request_ids=request_ids,
            )
        except (OSError, sqlite3.Error, SecurityError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="safe_cleanup_failed",
                message=f"安全清理失败：{type(exc).__name__}",
            )
        return ServiceResult(
            OperationStatus.PARTIAL
            if details.get("failed_count")
            else OperationStatus.SUCCESS,
            message=(
                "安全清理部分完成"
                if details.get("failed_count")
                else "安全清理完成"
            ),
            details=details,
        )

    def create_backup(self) -> ServiceResult:
        self.initialize()
        if not self._maintenance_lock.acquire(blocking=False):
            return ServiceResult(
                OperationStatus.CANCELLED, error_code="maintenance_busy",
                message="已有维护任务正在运行",
            )
        try:
            backup = create_database_backup(self.cfg)
            return ServiceResult(
                OperationStatus.SUCCESS, message="数据库备份创建并校验成功",
                details=backup,
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="backup_failed",
                message=f"数据库备份失败：{exc}",
            )
        finally:
            self._maintenance_lock.release()

    def verify_backup(self, path: str | Path) -> ServiceResult:
        try:
            verified = verify_database_backup(self.cfg, path)
            return ServiceResult(
                OperationStatus.SUCCESS, message="备份完整性校验通过", details=verified
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="backup_invalid",
                message=f"备份验证失败：{exc}",
            )

    def list_backups(self) -> ServiceResult:
        self.initialize()
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"backups": list_database_backups(self.cfg)},
        )

    def restore_backup(self, path: str | Path, *, confirmed: bool = False) -> ServiceResult:
        """恢复必须由界面明确确认，且收件或维护任务运行时拒绝。"""
        self.initialize()
        if not confirmed:
            return ServiceResult(
                OperationStatus.CANCELLED, error_code="restore_confirmation_required",
                message="恢复前必须明确确认",
            )
        with self._account_receive_locks_guard:
            receive_busy = any(
                lock.locked()
                for lock in self._account_receive_locks.values()
            )
        if receive_busy or not self._maintenance_lock.acquire(blocking=False):
            return ServiceResult(
                OperationStatus.CANCELLED, error_code="maintenance_busy",
                message="当前有任务运行，暂不能恢复数据库",
            )
        try:
            restored = restore_database_backup(self.cfg, path)
            init_db(
                self.cfg.db_path,
                legacy_accounts=legacy_accounts_from_config(self.cfg),
            )
            return ServiceResult(
                OperationStatus.SUCCESS, message="数据库恢复并重新打开成功",
                details=restored,
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="restore_failed",
                message=f"数据库恢复失败，当前数据库已保留或回滚：{exc}",
            )
        finally:
            self._maintenance_lock.release()

    def scan_consistency(self) -> ServiceResult:
        self.initialize()
        try:
            details = scan_consistency(self.cfg)
            details["repair_preview"] = list_consistency_repair_candidates(
                self.cfg, scan_id=str(details["scan_id"])
            )
            return ServiceResult(
                OperationStatus.SUCCESS,
                message=f"一致性扫描完成，发现 {len(details['issues'])} 项",
                details=details,
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="consistency_scan_failed",
                message=f"一致性扫描失败：{exc}",
            )

    def apply_consistency_repair(
        self,
        *,
        scan_id: str,
        issue_id: str,
        confirmed: bool = False,
    ) -> ServiceResult:
        """备份后只执行一个白名单修复动作，且任何动作都不会发送邮件。"""
        self.initialize()
        if not confirmed:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="consistency_repair_confirmation_required",
                message="一致性修复前必须由用户明确确认",
            )
        if not self._maintenance_lock.acquire(blocking=False):
            return ServiceResult(
                OperationStatus.CANCELLED,
                error_code="maintenance_busy",
                message="已有维护任务正在运行",
            )
        repair: dict[str, str] | None = None
        try:
            repair = begin_consistency_repair(
                self.cfg, scan_id=scan_id, issue_id=issue_id
            )
            try:
                backup = create_database_backup(
                    self.cfg, label="before_consistency_repair"
                )
                mark_consistency_repair_backup(
                    self.cfg,
                    repair_id=repair["repair_id"],
                    backup_name=str(backup["database_file"]),
                )
            except Exception as exc:  # noqa: BLE001
                finish_consistency_repair(
                    self.cfg,
                    repair_id=repair["repair_id"],
                    status="failed",
                    changed=False,
                    result_code=f"backup_{type(exc).__name__}",
                )
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="consistency_repair_backup_failed",
                    message="修复前数据库备份失败，未执行任何修复",
                )
            try:
                changed, result_code = self._execute_consistency_repair_action(
                    repair
                )
            except Exception as exc:  # noqa: BLE001
                error_code = getattr(
                    exc, "code", f"repair_{type(exc).__name__}"
                )
                finish_consistency_repair(
                    self.cfg,
                    repair_id=repair["repair_id"],
                    status="failed",
                    changed=False,
                    result_code=str(error_code),
                )
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code=str(error_code),
                    message="所选一致性问题未能安全修复，原数据已保留",
                )
            finish_consistency_repair(
                self.cfg,
                repair_id=repair["repair_id"],
                status="completed" if changed else "no_change",
                changed=changed,
                result_code=result_code,
            )
            current = scan_consistency(self.cfg)
            current["repair_preview"] = list_consistency_repair_candidates(
                self.cfg, scan_id=str(current["scan_id"])
            )
            return ServiceResult(
                OperationStatus.SUCCESS if changed else OperationStatus.PARTIAL,
                message=(
                    "所选一致性问题已修复并完成审计"
                    if changed
                    else "未发现足够的确定性证据，问题保持不变"
                ),
                details={
                    "repair_id": repair["repair_id"],
                    "action": repair["action"],
                    "changed": changed,
                    "result_code": result_code,
                    "backup_name": str(backup["database_file"]),
                    "scan": current,
                },
            )
        except ConsistencyRepairError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code=exc.code,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            if repair is not None:
                try:
                    finish_consistency_repair(
                        self.cfg,
                        repair_id=repair["repair_id"],
                        status="failed",
                        changed=False,
                        result_code=f"unexpected_{type(exc).__name__}",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="consistency_repair_failed",
                message=f"一致性修复失败：{type(exc).__name__}",
            )
        finally:
            self._maintenance_lock.release()

    def _execute_consistency_repair_action(
        self, repair: dict[str, str]
    ) -> tuple[bool, str]:
        action = repair["action"]
        request_id = repair["entity_id"]
        if action == "expire_pending_request":
            result = expire_send_request_if_due(
                self.cfg.db_path, request_id
            )
            return bool(result["changed"]), (
                "expired" if result["changed"] else "state_changed"
            )
        if action in {"recover_stale_send_lease", "recover_send_state"}:
            recovery = recover_incomplete_send_request(
                self.cfg.db_path, request_id
            )
            changed = bool(recovery.get("changed"))
            result_code = str(recovery.get("outcome") or "unchanged")
            if recovery.get("archive_required"):
                archived = self.reconcile_agent_send_request(request_id)
                if archived.status == OperationStatus.FAILED:
                    raise ConsistencyRepairError(
                        archived.error_code or "send_archive_recovery_failed",
                        "发件归档恢复失败",
                    )
                changed = changed or bool(archived.details.get("changed"))
                result_code = str(
                    archived.details.get("send_status") or result_code
                )
            return changed, result_code
        if action in {"recover_sent_archive", "reconcile_send_locally"}:
            result = self.reconcile_agent_send_request(request_id)
            if result.status == OperationStatus.FAILED:
                raise ConsistencyRepairError(
                    result.error_code or "send_reconciliation_failed",
                    "发件恢复失败",
                )
            return bool(result.details.get("changed")), str(
                result.details.get("send_status") or "unchanged"
            )
        raise ConsistencyRepairError(
            "consistency_repair_not_allowed", "所选修复动作不在安全白名单中"
        )

    def export_maintenance_report(self, destination: str | Path) -> ServiceResult:
        self.initialize()
        try:
            report = export_maintenance_report(self.cfg, destination)
            return ServiceResult(
                OperationStatus.SUCCESS, message="脱敏维护报告已导出",
                details={"report_path": str(report)},
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="maintenance_report_failed",
                message=f"维护报告导出失败：{exc}",
            )

    def get_config_and_connection_status(self) -> ServiceResult:
        """返回脱敏配置及三个连接面的可见状态。"""
        self.initialize()
        backend = _effective_receive_backend(self.cfg)
        oauth = get_oauth_state(self.cfg)
        accounts_result = self.list_mail_accounts()
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "config": self.cfg.mask(),
                "receive_backend": backend,
                "imap": "configured" if self.cfg.gmail_address and self.cfg.gmail_app_password else "not_configured",
                "gmail_api": oauth,
                "qq_smtp": "configured" if self.cfg.qq_email and self.cfg.qq_auth_code else "not_configured",
                "mail_accounts": accounts_result.details.get(
                    "accounts", []
                ),
            },
        )

    def diagnose_imap(self) -> ServiceResult:
        try:
            from agent_mail_bridge.mail_receive import _connect_imap
            connection = _connect_imap(self.cfg)
            connection.logout()
            return ServiceResult(OperationStatus.SUCCESS, message="Gmail IMAP 连接正常")
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="imap_diagnose_failed", message=str(exc)
            )

    def diagnose_gmail_api(self, account_id: str | None = None) -> ServiceResult:
        """按统一账号路由诊断 Gmail API，兼容尚未迁移的旧配置。"""
        self.initialize()
        from agent_mail_bridge.gmail_api_auth import reverify_gmail_authorization

        target_account_id = account_id or current_receive_account_id(self.cfg)
        try:
            context = self._account_router.context(
                target_account_id, capability="receive"
            )
        except AccountRuntimeError:
            if account_id is not None:
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="account_not_found",
                    message="Gmail 账号不存在或未启用",
                )
            return reverify_gmail_authorization(self.cfg)
        if (
            str(context.account.get("provider") or "") != "gmail"
            or _effective_receive_backend(context.config) != "gmail_api"
        ):
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="gmail_api_not_configured",
                message="所选账号不是 Gmail API 收件账号",
            )
        return reverify_gmail_authorization(context.config)

    def diagnose_qq_smtp(self) -> ServiceResult:
        """连接并认证 QQ SMTP，不发送邮件。"""
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.cfg.qq_smtp_host, self.cfg.qq_smtp_port,
                timeout=self.cfg.qq_smtp_connect_timeout, context=context,
            ) as server:
                server.login(self.cfg.qq_email, self.cfg.qq_auth_code)
            return ServiceResult(OperationStatus.SUCCESS, message="QQ SMTP 连接正常")
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="qq_smtp_diagnose_failed",
                message=str(exc),
            )

    def export_diagnostic_report(self, destination: str | Path) -> ServiceResult:
        """导出不含凭据、邮件正文和私人绝对路径的诊断摘要。"""
        self.initialize()
        report_path = Path(destination)
        if report_path.suffix.lower() != ".md":
            report_path = report_path.with_suffix(".md")
        if report_path.exists():
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="report_exists",
                message="目标文件已存在，请选择新的文件名",
            )
        try:
            from PySide6 import __version__ as pyside_version
            from agent_mail_bridge import __version__ as app_version
            from agent_mail_bridge.gmail_api_auth import get_oauth_diagnostics

            oauth = get_oauth_state(self.cfg)
            oauth_runtime = get_oauth_diagnostics(self.cfg)
            recent_events = query_recent_events(self.cfg.db_path, 100)
            recent_errors = [
                row for row in recent_events
                if str(row.get("level", "")).upper() in {"ERROR", "FAILED"}
            ][:10]
            db_state = "正常" if self.cfg.db_path.exists() else "不存在"
            log_files = list(self.cfg.logs_dir.glob("app.log*"))
            report_lines = [
                "# AgentMailBridge 脱敏诊断报告",
                "",
                f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"应用版本：{app_version}",
                f"操作系统：{platform.system()} {platform.release()}",
                f"Python：{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                f"PySide6：{pyside_version}",
                "",
                "## 配置完整性",
                "",
                f"收件后端：{_effective_receive_backend(self.cfg)}",
                f"Gmail 地址：{_mask_email(self.cfg.gmail_address)}",
                f"QQ 地址：{_mask_email(self.cfg.qq_email)}",
                f"Gmail API credentials：{'已配置' if self.cfg.gmail_api_credentials_path.exists() else '未配置'}",
                f"Gmail API token：{'存在' if self.cfg.gmail_api_token_path.exists() else '不存在'}",
                f"OAuth 状态：{oauth.get('state', 'UNKNOWN')}",
                f"OAuth Desktop 凭据有效：{'是' if oauth_runtime['desktop_credentials_valid'] else '否'}",
                f"OAuth 当前阶段：{oauth_runtime['stage']}",
                f"OAuth 回环地址：{oauth_runtime['callback_host']}",
                f"OAuth 回环已绑定：{'是' if oauth_runtime['callback_bound'] else '否'}",
                f"OAuth 已收到回调：{'是' if oauth_runtime['callback_received'] else '否'}",
                f"OAuth 端口已释放：{'是' if oauth_runtime['port_released'] else '否'}",
                f"IMAP：{'已配置' if self.cfg.gmail_address and self.cfg.gmail_app_password else '未配置'}",
                f"QQ SMTP：{'已配置' if self.cfg.qq_email and self.cfg.qq_auth_code else '未配置'}",
                f"允许发送目录数量：{len(self.cfg.effective_allowed_send_roots)}",
                "",
                "## 本地运行状态",
                "",
                f"数据目录：{'可用' if self.cfg.data_root_path.exists() else '不存在'}（路径已隐藏）",
                f"SQLite：{db_state}",
                f"日志轮转文件数量：{len(log_files)}",
                f"最近 100 条事件中的错误数：{len(recent_errors)}",
                "",
                "## 最近错误摘要",
                "",
            ]
            if recent_errors:
                for row in recent_errors:
                    report_lines.append(
                        f"- {str(row.get('created_at', ''))[:19]} "
                        f"[{row.get('event_type', 'unknown')}]（详细内容已隐藏）"
                    )
            else:
                report_lines.append("- 未发现错误事件")
            report_lines.extend(
                [
                    "",
                    "## 隐私说明",
                    "",
                    "本报告不包含密码、授权码、token、credentials 内容、邮件正文、附件内容或私人绝对路径。",
                    "",
                ]
            )
            atomic_write_text(
                report_path,
                "\n".join(report_lines),
                errors="strict",
            )
        except OSError as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="report_write_failed",
                message=f"诊断报告写入失败：{exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="report_failed",
                message=f"诊断报告生成失败：{exc}",
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"脱敏诊断报告已保存：{report_path.name}",
            details={"report_path": str(report_path)},
        )


def _classify_receive_error(message: str) -> str:
    """把后端异常文案归入稳定错误代码。"""
    lowered = message.lower()
    rules = (
        (("配置", "config"), "config_error"),
        (("socks5", "代理"), "socks5_error"),
        (("tls", "ssl"), "tls_error"),
        (("scope", "权限不匹配"), "scope_mismatch"),
        (("oauth", "授权"), "oauth_error"),
        (("认证", "auth"), "gmail_auth_error"),
        (("database", "sqlite", "数据库"), "database_error"),
        (("文件", "附件"), "file_save_error"),
        (("gmail api",), "gmail_api_error"),
        (("网络", "连接"), "network_error"),
    )
    for keywords, code in rules:
        if any(keyword in lowered for keyword in keywords):
            return code
    return "receive_failed"


def _mail_time_range(
    time_scope: str,
    *,
    recent_days: int | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str | None, str | None]:
    scope = str(time_scope or "all").strip().lower()
    today = datetime.now().date()
    if scope in {"all", "latest"}:
        return None, None
    if scope == "today":
        value = today.isoformat()
        return value, f"{value}\uffff"
    if scope == "yesterday":
        value = (today - timedelta(days=1)).isoformat()
        return value, f"{value}\uffff"
    if scope == "recent_days":
        days = int(recent_days or 3)
        if days <= 0 or days > 3650:
            raise ValueError("recent_days 必须在 1 到 3650 之间")
        return (today - timedelta(days=days - 1)).isoformat(), f"{today.isoformat()}\uffff"
    if scope == "date_range":
        if not date_from or not date_to:
            raise ValueError("date_range 必须同时提供 date_from 和 date_to")
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
        if start > end:
            raise ValueError("date_from 不能晚于 date_to")
        return start.isoformat(), f"{end.isoformat()}\uffff"
    raise ValueError("time_scope 仅支持 latest、today、yesterday、recent_days、date_range 或 all")


def _mail_search_summary(row: dict[str, Any]) -> dict[str, Any]:
    package_id = str(row.get("package_id") or "")
    return {
        "mail_id": package_id,
        "package_id": package_id,
        "account_id": str(row.get("account_id") or ""),
        "mailbox_id": str(row.get("mailbox_id") or ""),
        "subject": str(row.get("subject") or ""),
        "from": str(row.get("from") or ""),
        "from_display": str(row.get("from_display") or ""),
        "from_address": str(row.get("from_address") or ""),
        "from_addresses": list(row.get("from_addresses") or []),
        "to": list(row.get("to") or []),
        "to_addresses": list(row.get("to_addresses") or []),
        "cc_addresses": list(row.get("cc_addresses") or []),
        "bcc_addresses": list(row.get("bcc_addresses") or []),
        "reply_to": list(row.get("reply_to") or []),
        "sent_at": row.get("sent_at"),
        "received_at": row.get("received_at"),
        "saved_at": row.get("saved_at"),
        "summary": " ".join(str(row.get("body_summary") or "").split())[:300],
        "counts": dict(row.get("counts") or {}),
        "archive_status": str(row.get("archive_status") or ""),
        "parse_status": str(row.get("parse_status") or ""),
        "thread_ref": str(row.get("thread_ref") or ""),
    }


def _text_value_page(value: str, offset: int, max_chars: int) -> dict[str, Any]:
    if offset > len(value):
        return {
            "content": "", "character_count": len(value), "offset": offset,
            "next_offset": None, "has_more": False, "status": "invalid_range",
        }
    content = value[offset:offset + max_chars]
    next_offset = offset + len(content)
    return {
        "content": content,
        "character_count": len(value),
        "offset": offset,
        "next_offset": next_offset if next_offset < len(value) else None,
        "has_more": next_offset < len(value),
    }


def _safe_agent_mail_message(message: dict[str, Any], cfg: AppConfig) -> dict[str, Any]:
    result = dict(message)
    package_id = str(result.get("package_id") or "")
    result["mail_id"] = package_id
    result["last_error"] = _redact_text(str(result.get("last_error") or ""), cfg)
    result["archive"] = {
        "status": str(result.get("archive_status") or ""),
        "parse_status": str(result.get("parse_status") or ""),
        "last_error": result["last_error"],
        "legacy": bool(result.get("legacy")),
    }
    result["thread"] = {
        "thread_ref": str(result.get("thread_ref") or ""),
        "account_ref": str(result.get("account_ref") or ""),
        "mailbox_ref": str(result.get("mailbox_ref") or ""),
    }
    return result


def _validate_agent_mail_paths(message: dict[str, Any], cfg: AppConfig) -> None:
    """数据库事实也必须在每次 Agent 访问前重新满足 DATA_ROOT 边界。"""
    package_root_text = str(message.get("package_root") or "").strip()
    if not package_root_text:
        raise MailAccessError("resource_not_local", "邮件归档目录不可用")
    try:
        data_root = cfg.data_root_path.resolve()
        package_root = Path(package_root_text).resolve()
        assert_within_root(package_root, data_root)
        for resource in message.get("resources") or []:
            absolute = str(resource.get("absolute_path") or "").strip()
            relative = str(resource.get("path") or "").strip()
            if not absolute and not relative:
                continue
            candidate = Path(absolute) if absolute else package_root / relative
            assert_within_root(candidate, package_root)
        raw = message.get("raw_eml") or {}
        raw_path = str(raw.get("path") or "").strip()
        if raw_path:
            assert_within_root(package_root / raw_path, package_root)
    except (OSError, SecurityError) as exc:
        raise MailAccessError("path_not_allowed", "邮件归档路径超出 DATA_ROOT") from exc


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _coerce_rescan_datetime(value: datetime | str, *, end_of_day: bool) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("历史补扫日期不能为空")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.strptime(text, "%Y-%m-%d")
            if end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _redact_text(value: str, cfg: AppConfig) -> str:
    text = str(value or "")
    for secret in (cfg.gmail_app_password, cfg.qq_auth_code):
        if secret:
            text = text.replace(secret, "[已脱敏]")
    text = re.sub(
        r"(?i)(password|token|auth[_ -]?code|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[已脱敏]",
        text,
    )
    return text[:1000]


def _mask_email(value: str) -> str:
    """诊断报告只保留邮箱首字符和域名。"""
    local, separator, domain = value.partition("@")
    if not local or not separator or not domain:
        return "未配置"
    return f"{local[:1]}***@{domain}"


def _event_category(event_type: str) -> str:
    normalized = str(event_type or "").strip().lower()
    for category, values in LOG_EVENT_CATEGORIES.items():
        if normalized in values:
            return category
    return "系统与诊断"


def _redact_event_message(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)([A-Z0-9._%+-])([A-Z0-9._%+-]*)(@[A-Z0-9.-]+\.[A-Z]{2,})",
        lambda match: f"{match.group(1)}***{match.group(3)}",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:[A-Z]:\\|\\\\)[^\r\n,，;；]+",
        "[本地路径已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|password|auth(?:orization)?|secret)\s*[:=]\s*\S+",
        r"\1=[已隐藏]",
        text,
    )
    return text


def _mcp_audit_status(result: SendResult) -> str:
    """把发送结果映射为稳定的 MCP 审计状态。"""
    validation_codes = {
        "configuration_error",
        "file_not_found",
        "path_not_allowed",
        "file_type_not_allowed",
        "file_too_large",
    }
    if result.error_code in validation_codes:
        return result.error_code
    if result.send_status in {"sent", "sent_archive_failed", "duplicate", "failed"}:
        return result.send_status
    return result.status.value


def _sanitize_history_paths(
    rows: list[dict[str, Any]], path_fields: tuple[str, ...], roots: list[Path]
) -> list[dict[str, Any]]:
    """旧数据库中的越界路径不得由应用服务返回。"""
    safe_rows: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in path_fields:
            value = item.get(field)
            if not value:
                continue
            try:
                assert_within_allowed_roots(Path(value), roots)
            except SecurityError:
                item[field] = ""
                item[f"{field}_status"] = "unsafe_path"
        safe_rows.append(item)
    return safe_rows
