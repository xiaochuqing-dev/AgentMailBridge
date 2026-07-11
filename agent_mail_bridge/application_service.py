"""CLI、GUI 与未来 MCP 共用的应用服务入口。"""

from __future__ import annotations

import smtplib
import ssl
import platform
import os
import sys
import threading
import uuid
import shutil
from datetime import datetime
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
    init_db,
    insert_mcp_call,
    log_event,
    query_recent_mcp_calls,
    query_recent_events,
    query_recent_received_messages,
    query_recent_sent_files,
    query_sent_files_by_date,
    update_mcp_call,
)
from agent_mail_bridge.file_index import list_received_files_for_date, scan_file_status
from agent_mail_bridge.gmail_api_auth import get_oauth_state
from agent_mail_bridge.logging_setup import setup_logging
from agent_mail_bridge.mail_receive import receive_mails
from agent_mail_bridge.mail_send import send_file_with_request
from agent_mail_bridge.models import OperationStatus, ReceiveResult, SendResult, ServiceResult
from agent_mail_bridge.security import (
    SecurityError,
    assert_within_allowed_roots,
    check_size_ok,
    is_dangerous,
)
from agent_mail_bridge.storage import ensure_data_dirs
from agent_mail_bridge.utils import fmt_date, sanitize_filename, sha256_of_file


class ApplicationService:
    """本地单用户应用的稳定业务入口。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._receive_lock = threading.Lock()
        self._setup_lock = threading.Lock()
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
        with self._setup_lock:
            if not self._ready:
                ensure_data_dirs(self.cfg)
                init_db(self.cfg.db_path)
                setup_logging(self.cfg.logs_dir, self.cfg.log_level)
                self._ready = True
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
        from agent_mail_bridge.config import PROJECT_ROOT
        try:
            result = CredentialService().migrate_env(env_path or PROJECT_ROOT / ".env")
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
            if failures and saved:
                status = OperationStatus.PARTIAL
            elif failures or not raw.get("ok", True):
                status = OperationStatus.FAILED
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
                error_code=(
                    "partial_receive" if status == OperationStatus.PARTIAL
                    else _classify_receive_error(errors[0]) if errors else None
                ),
                message=(
                    "收件完成" if status == OperationStatus.SUCCESS
                    else errors[0] if errors else "收件存在错误"
                ),
            )
        finally:
            self._receive_lock.release()

    def send_file(
        self,
        file_path: str | Path,
        *,
        subject: str | None = None,
        request_id: str | None = None,
    ) -> SendResult:
        """发送白名单目录内文件，request_id 用于安全重试。"""
        self.initialize()
        stable_request_id = request_id or str(uuid.uuid4())
        raw = send_file_with_request(
            file_path, request_id=stable_request_id,
            subject=subject, cfg=self.cfg,
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
            send_status=raw.get("send_status", "not_sent"),
            source_path=raw.get("source_path", ""),
            send_copy_path=raw.get("send_copy_path", ""),
            sent_copy_path=raw.get("sent_copy_path", ""),
            subject=raw.get("subject", subject or ""),
            to_email=raw.get("to", self.cfg.owner_gmail),
            sent_at=raw.get("sent_at", ""),
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
            result = self.send_file(
                file_path,
                subject=title,
                request_id=stable_request_id,
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
            "SUCCESS" if result.ok else "ERROR",
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
            ("file_path",),
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
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={
                "received": _sanitize_history_paths(
                    query_recent_received_messages(self.cfg.db_path, limit),
                    ("body_file_path",), [self.cfg.data_root_path],
                ),
                "sent": _sanitize_history_paths(
                    query_recent_sent_files(self.cfg.db_path, limit),
                    ("source_path", "send_copy_path", "sent_copy_path"),
                    self.cfg.effective_allowed_send_roots,
                ),
            },
        )

    def get_recent_logs(self, limit: int = 50) -> ServiceResult:
        self.initialize()
        return ServiceResult(
            OperationStatus.SUCCESS,
            details={"events": query_recent_events(self.cfg.db_path, max(1, limit))},
        )

    def scan_file_status(self) -> ServiceResult:
        self.initialize()
        changes = scan_file_status(self.cfg)
        return ServiceResult(
            OperationStatus.SUCCESS,
            message=f"发现 {len(changes)} 项变化",
            details={"changes": changes},
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
