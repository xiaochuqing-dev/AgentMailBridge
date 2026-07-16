"""CLI、GUI 与未来 MCP 共用的应用服务入口。"""

from __future__ import annotations

import csv
import smtplib
import ssl
import platform
import os
import re
import sys
import threading
import uuid
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import (
    AppConfig,
    ConfigError,
    _effective_receive_backend,
    require_receive_config,
)
from agent_mail_bridge.credentials import (
    CredentialError,
    CredentialService,
    GMAIL_IMAP_SECRET,
    MemoryCredentialBackend,
    QQ_SMTP_SECRET,
)
from agent_mail_bridge.database import (
    app_event_overview,
    clear_all_app_events,
    clear_daily_check_events,
    configure_app_event_retention,
    count_receive_retries,
    get_auto_receive_state,
    init_db,
    insert_mcp_call,
    legacy_archive_backfill_needed,
    outbound_mail_migration_needed,
    log_event,
    get_outbound_message as query_outbound_message,
    query_recent_mcp_calls,
    query_recent_outbound_messages,
    query_recent_events,
    query_app_events,
    query_recent_received_messages,
    query_recent_sent_files,
    query_trusted_domains,
    query_sent_files_by_date,
    update_mcp_call,
    update_mcp_staging,
    save_auto_receive_state,
    delete_trusted_domain,
    upsert_trusted_domain,
    prune_app_events,
)
from agent_mail_bridge.file_index import list_received_files_for_date, scan_file_status
from agent_mail_bridge.gmail_api_auth import get_oauth_state
from agent_mail_bridge.logging_setup import get_logger, setup_logging
from agent_mail_bridge.mail_receive import receive_mails
from agent_mail_bridge.mail_archive import backfill_legacy_mail_packages
from agent_mail_bridge.mail_facts import (
    get_mail_message as query_mail_message,
    get_mail_thread as query_mail_thread,
    list_mail_messages as query_mail_messages,
    list_mail_resources as query_mail_resources,
    list_mail_threads as query_mail_threads,
    search_mail_facts as query_mail_facts,
)
from agent_mail_bridge.mail_send import send_file_with_request, send_outbound_mail
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
    assert_not_sensitive_delivery_file,
    check_size_ok,
    is_dangerous,
    validate_agent_workspace_root,
)
from agent_mail_bridge.storage import atomic_copy_file, ensure_data_dirs
from agent_mail_bridge.trusted_downloads import normalize_trusted_domain
from agent_mail_bridge.utils import fmt_date, sanitize_filename, sha256_of_file


logger = get_logger("application_service")

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
        self._receive_lock = threading.Lock()
        self._setup_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._event_maintenance_lock = threading.Lock()
        self._last_event_maintenance_at: datetime | None = None
        self._last_event_maintenance_result: dict[str, int] = {}
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

    def initialize(self) -> ServiceResult:
        """初始化安全目录、数据库和日志。"""
        initialized_now = False
        with self._setup_lock:
            if not self._ready:
                ensure_data_dirs(self.cfg)
                archive_migration_needed = legacy_archive_backfill_needed(
                    self.cfg.db_path
                )
                outbound_migration_needed = outbound_mail_migration_needed(
                    self.cfg.db_path
                )
                if archive_migration_needed or outbound_migration_needed:
                    create_database_backup(self.cfg, label="before_mail_models")
                init_db(self.cfg.db_path)
                if archive_migration_needed:
                    migration = backfill_legacy_mail_packages(self.cfg)
                    if migration.get("failed"):
                        log_event(
                            self.cfg.db_path, "WARNING", "db",
                            f"旧邮件归档迁移部分完成：成功 {migration.get('migrated', 0)}，失败 {migration.get('failed', 0)}",
                        )
                setup_logging(self.cfg.logs_dir, self.cfg.log_level)
                configure_app_event_retention(
                    self.cfg.db_path, max_count=self.cfg.app_event_max_count
                )
                self._ready = True
                initialized_now = True
        if initialized_now:
            self.schedule_event_maintenance(force=True)
        return ServiceResult(OperationStatus.SUCCESS, message="初始化完成")

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
        limit: int | None = None,
        unseen_only: bool | None = None,
        mark_seen: bool | None = None,
        automatic: bool = False,
    ) -> ReceiveResult:
        """执行一次互斥收件，普通收件绝不启动浏览器授权。"""
        self.initialize()
        backend = _effective_receive_backend(self.cfg)
        if limit is not None and limit <= 0:
            return ReceiveResult(
                OperationStatus.FAILED, backend=backend,
                error_code="invalid_limit", message="收件数量必须大于 0",
            )
        if not self._receive_lock.acquire(blocking=False):
            return ReceiveResult(
                OperationStatus.CANCELLED, backend=backend,
                error_code="receive_busy", message="已有收件任务正在运行",
            )
        try:
            if backend == "gmail_api":
                oauth = get_oauth_state(self.cfg)
                if oauth["state"] not in {"READY", "TOKEN_EXPIRED_REFRESHABLE"}:
                    return ReceiveResult(
                        OperationStatus.AUTH_REQUIRED,
                        backend=backend,
                        error_code=oauth["state"].lower(),
                        message=oauth["message"],
                        needs_auth=True,
                    )
            try:
                require_receive_config(self.cfg)
                raw = receive_mails(
                    self.cfg, limit=limit,
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
            retry_counts = count_receive_retries(self.cfg.db_path)
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
                    else _classify_receive_error(errors[0]) if errors else None
                ),
                message=(
                    "收件完成" if status == OperationStatus.SUCCESS
                    else "检查完成，暂时没有新邮件" if status == OperationStatus.NO_CHANGES
                    else errors[0] if errors else "收件存在错误"
                ),
            )
        finally:
            self._receive_lock.release()

    def get_auto_receive_state(self) -> ServiceResult:
        """供 GUI 展示真实持久化调度与坏邮件隔离状态。"""
        self.initialize()
        state = get_auto_receive_state(self.cfg.db_path)
        state["enabled"] = bool(state.get("enabled"))
        state.update(count_receive_retries(self.cfg.db_path))
        return ServiceResult(OperationStatus.SUCCESS, details=state)

    def save_auto_receive_state(self, **changes: Any) -> ServiceResult:
        """由 GUI 调度器原子更新可恢复状态。"""
        self.initialize()
        state = save_auto_receive_state(self.cfg.db_path, **changes)
        state["enabled"] = bool(state.get("enabled"))
        state.update(count_receive_retries(self.cfg.db_path))
        return ServiceResult(OperationStatus.SUCCESS, details=state)

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
                shutil.copy2(source, snapshot)
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
        subject: str | None,
        body_text: str,
        attachment_paths: list[str | Path],
        links: list[dict[str, Any] | str],
    ) -> SendResult:
        """发送一封用户编写的邮件；全局文件信任仅限本次 GUI 操作。"""
        self.initialize()
        raw = send_outbound_mail(
            subject=subject,
            body_text=body_text,
            attachment_paths=attachment_paths,
            links=links,
            cfg=self.cfg,
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
            to_email=str(raw.get("to") or self.cfg.owner_gmail),
            sent_at=str(raw.get("sent_at") or ""),
            attachment_count=int(raw.get("attachment_count") or 0),
            link_count=int(raw.get("link_count") or 0),
            error_code=raw.get("error_code"),
            message=str(raw.get("error") or "发送完成"),
            details={"body_text": str(raw.get("body_text") or body_text or "")},
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
            query_recent_mcp_calls(self.cfg.db_path, max(1, limit)),
            ("file_path", "staged_path"),
            self.cfg.effective_allowed_send_roots,
        )
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"calls": rows},
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

    def get_oauth_status(self) -> ServiceResult:
        """获取 Gmail API 授权状态，不刷新也不打开浏览器。"""
        state = get_oauth_state(self.cfg)
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
        self, source: str | Path, *, replace: bool = False
    ) -> ServiceResult:
        """验证并把 OAuth 客户端配置导入当前用户的受控目录。"""
        from agent_mail_bridge.oauth_storage import import_oauth_credentials

        try:
            target = import_oauth_credentials(
                Path(source),
                destination=self.cfg.gmail_api_credentials_path,
                replace=replace,
            )
        except FileExistsError as exc:
            return ServiceResult(
                OperationStatus.CANCELLED,
                error_code="oauth_credentials_exists",
                message=str(exc),
            )
        except (OSError, ValueError) as exc:
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="oauth_import_failed",
                message=str(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="OAuth 客户端配置已安全导入",
            details={"credentials_path": str(target)},
        )

    def authorize_gmail_api(self) -> ServiceResult:
        """显式执行浏览器 OAuth 授权。"""
        self.initialize()
        try:
            from agent_mail_bridge.gmail_api_auth import get_gmail_api_service
            service = get_gmail_api_service(self.cfg, interactive=True)
            profile = service.users().getProfile(userId="me").execute()
            return ServiceResult(
                OperationStatus.SUCCESS,
                message="Gmail API 授权成功",
                details={"email": profile.get("emailAddress", "")},
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED,
                error_code="oauth_failed", message=str(exc), needs_auth=True,
            )

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
        roots = [str(Path(item).resolve()) for item in self.cfg.allowed_send_roots]
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"workspaces": roots, "takes_effect": "next_mcp_session"},
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
                    message="该工作区已授权",
                    details={"workspace": str(resolved)},
                )
            try:
                resolved.relative_to(item)
                return ServiceResult(
                    OperationStatus.NO_CHANGES,
                    error_code="workspace_nested",
                    message="该目录已包含在现有授权工作区中",
                    details={"workspace": str(resolved)},
                )
            except ValueError:
                pass
            try:
                item.relative_to(resolved)
                return ServiceResult(
                    OperationStatus.FAILED,
                    error_code="workspace_broader_than_existing",
                    message="新目录会扩大现有授权范围，请先移除较小工作区后再明确授权",
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
                message=f"保存工作区授权失败：{exc}",
            )
        self.cfg.allowed_send_roots = updated
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="工作区已授权；新授权将在下一次 MCP 会话生效",
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
                message="该工作区未授权",
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
                message=f"保存工作区授权失败：{exc}",
            )
        self.cfg.allowed_send_roots = updated
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="工作区授权已移除；下一次 MCP 会话将使用新范围",
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
        self, thread_ref: str, *, account_ref: str | None = None
    ) -> ServiceResult:
        self.initialize()
        row = query_mail_thread(
            self.cfg.db_path, thread_ref, account_ref=account_ref
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
        if self._receive_lock.locked() or not self._maintenance_lock.acquire(blocking=False):
            return ServiceResult(
                OperationStatus.CANCELLED, error_code="maintenance_busy",
                message="当前有任务运行，暂不能恢复数据库",
            )
        try:
            restored = restore_database_backup(self.cfg, path)
            init_db(self.cfg.db_path)
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
        backend = _effective_receive_backend(self.cfg)
        oauth = get_oauth_state(self.cfg)
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "config": self.cfg.mask(),
                "receive_backend": backend,
                "imap": "configured" if self.cfg.gmail_address and self.cfg.gmail_app_password else "not_configured",
                "gmail_api": oauth,
                "qq_smtp": "configured" if self.cfg.qq_email and self.cfg.qq_auth_code else "not_configured",
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

    def diagnose_gmail_api(self) -> ServiceResult:
        try:
            from agent_mail_bridge.gmail_api_auth import get_gmail_api_service
            service = get_gmail_api_service(self.cfg, interactive=False)
            profile = service.users().getProfile(userId="me").execute()
            return ServiceResult(
                OperationStatus.SUCCESS, message="Gmail API 连接正常",
                details={"email": profile.get("emailAddress", "")},
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceResult(
                OperationStatus.FAILED, error_code="gmail_api_diagnose_failed",
                message=str(exc), needs_auth=True,
            )

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

            oauth = get_oauth_state(self.cfg)
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
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("\n".join(report_lines), encoding="utf-8", errors="strict")
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
