"""本机 Agent Client 身份、权限与撤销地基。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.credentials import (
    CredentialError,
    CredentialService,
    agent_client_credential_name,
)
from agent_mail_bridge.database import (
    create_agent_client,
    get_agent_client,
    get_mail_account,
    get_mail_package,
    query_agent_client_permissions,
    query_agent_clients,
    query_mail_accounts,
    query_mailboxes,
    replace_agent_client_permissions,
    update_agent_client,
)
from agent_mail_bridge.send_permissions import (
    SEND_MODES,
    effective_attachment_workspace_ids,
    effective_mailbox_ids,
    effective_send_account_ids,
    query_extended_scopes,
    replace_extended_scopes,
    validate_extended_scope_values,
)
from agent_mail_bridge.mail_resource_access import (
    workspace_dtos,
    workspace_id_for_path,
)
from agent_mail_bridge.security import SecurityError, assert_within_root


CLIENT_TYPES = {"claude_code", "codex", "hermes", "claude_desktop", "custom"}
CLIENT_STATES = {"active", "paused", "revoked"}
CONFIG_MODES = {"managed", "assisted", "manual"}
CONFIG_SCOPES = {"user", "project", "custom"}
PERMISSION_MODES = {"recommended", "full", "custom"}
SCOPE_MODES = {"all", "selected"}

CAPABILITIES = {
    "mail.search",
    "mail.get",
    "resource.read",
    "resource.prepare",
    "sync.status",
    "sync.ensure_fresh",
    "workspace.list",
    "result.submit",
    "mail.accounts.list",
    "mailboxes.list",
    "mail.send",
    "send.status",
}
RECOMMENDED_CAPABILITIES = CAPABILITIES - {"result.submit", "mail.send", "send.status"}

TOOL_CAPABILITIES = {
    "search_mails": "mail.search",
    "get_mail": "mail.get",
    "read_mail_resource": "resource.read",
    "prepare_mail_resources": "resource.prepare",
    "get_mail_sync_status": "sync.status",
    "list_agent_workspaces": "workspace.list",
    "submit_result": "result.submit",
    "list_mail_accounts": "mail.accounts.list",
    "list_mailboxes": "mailboxes.list",
    "send_mail": "mail.send",
    "get_send_request_status": "send.status",
}

ACCOUNT_SCOPE_CAPABILITY = "account.access"
WORKSPACE_SCOPE_CAPABILITY = "workspace.access"

ERROR_MESSAGES = {
    "agent_access_disabled": "Agent 邮件访问总开关已关闭",
    "unknown_client": "未识别的 Agent Client，需先在 GUI 中连接",
    "client_disabled": "该 Agent Client 已暂停或未启用",
    "client_revoked": "该 Agent Client 已撤销，请重新连接",
    "client_auth_failed": "Agent Client 身份验证失败",
    "capability_denied": "该 Agent Client 未获准使用此能力",
    "account_denied": "该 Agent Client 未获准访问此邮箱账号",
    "workspace_denied": "该 Agent Client 未获准使用此资料目录",
    "mailbox_denied": "该 Agent Client 未获准访问此邮箱目录",
    "send_account_denied": "该 Agent Client 未获准使用此发件账号",
    "attachment_scope_denied": "该 Agent Client 未获准使用此附件资料目录",
    "token_rotation_failed": "Client scoped token 轮换失败，旧 token 仍然有效",
    "token_rotation_rollback_failed": "Client scoped token 轮换回滚失败，请暂停该 Client 后处理",
}


class AgentAccessError(RuntimeError):
    """稳定、可审计且不携带 secret 的 Client 访问拒绝。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or ERROR_MESSAGES.get(code, "Agent Client 访问被拒绝"))


@dataclass(frozen=True)
class AgentIdentity:
    client_id: str
    client_type: str
    display_name: str
    config_scope: str
    permission_mode: str
    account_scope_mode: str
    mailbox_scope_mode: str
    send_account_scope_mode: str
    workspace_scope_mode: str
    attachment_scope_mode: str
    send_mode: str
    capabilities: frozenset[str]
    denied_capabilities: frozenset[str]
    account_ids: frozenset[str]
    denied_account_ids: frozenset[str]
    mailbox_ids: frozenset[str]
    denied_mailbox_ids: frozenset[str]
    send_account_ids: frozenset[str]
    denied_send_account_ids: frozenset[str]
    workspace_ids: frozenset[str]
    denied_workspace_ids: frozenset[str]
    attachment_workspace_ids: frozenset[str]
    denied_attachment_workspace_ids: frozenset[str]


def new_client_id() -> str:
    return f"client_{uuid.uuid4().hex[:24]}"


def new_client_token() -> str:
    return "ambc_" + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class AgentIntegrationService:
    """GUI 与 MCP 共用的 Client 注册、权限和身份解析入口。"""

    def __init__(
        self,
        cfg: AppConfig,
        credentials: CredentialService,
    ) -> None:
        self.cfg = cfg
        self.credentials = credentials

    def create_client_profile(
        self,
        *,
        client_type: str,
        display_name: str,
        config_mode: str | None = None,
        config_scope: str = "user",
        permission_mode: str = "custom",
        account_scope_mode: str = "selected",
        mailbox_scope_mode: str = "selected",
        send_account_scope_mode: str = "selected",
        workspace_scope_mode: str = "selected",
        attachment_scope_mode: str = "selected",
        send_mode: str = "confirm",
        notes: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        normalized_type = str(client_type or "").strip().casefold()
        if normalized_type not in CLIENT_TYPES:
            raise ValueError("不支持的 Agent Client 类型")
        name = " ".join(str(display_name or "").split())[:120]
        if not name:
            raise ValueError("Client 显示名称不能为空")
        scope = str(config_scope or "user").strip().casefold()
        if scope not in CONFIG_SCOPES:
            raise ValueError("配置范围无效")
        mode = str(
            config_mode
            or ("manual" if normalized_type == "custom" else "managed")
        ).strip().casefold()
        if mode not in CONFIG_MODES:
            raise ValueError("配置方式无效")
        normalized_permission_mode = str(permission_mode or "custom").strip().casefold()
        normalized_account_scope = str(account_scope_mode or "selected").strip().casefold()
        normalized_mailbox_scope = str(mailbox_scope_mode or "selected").strip().casefold()
        normalized_send_account_scope = str(
            send_account_scope_mode or "selected"
        ).strip().casefold()
        normalized_workspace_scope = str(workspace_scope_mode or "selected").strip().casefold()
        normalized_attachment_scope = str(
            attachment_scope_mode or "selected"
        ).strip().casefold()
        normalized_send_mode = str(send_mode or "confirm").strip().casefold()
        if normalized_permission_mode not in PERMISSION_MODES:
            raise ValueError("权限模式无效")
        if normalized_account_scope not in SCOPE_MODES:
            raise ValueError("邮箱范围模式无效")
        if normalized_mailbox_scope not in SCOPE_MODES:
            raise ValueError("邮箱目录范围模式无效")
        if normalized_send_account_scope not in SCOPE_MODES:
            raise ValueError("发件账号范围模式无效")
        if normalized_workspace_scope not in SCOPE_MODES:
            raise ValueError("资料输出目录范围模式无效")
        if normalized_attachment_scope not in SCOPE_MODES:
            raise ValueError("附件资料目录范围模式无效")
        if normalized_send_mode not in SEND_MODES:
            raise ValueError("发件模式无效")
        client_id = new_client_id()
        token = new_client_token()
        credential_ref = agent_client_credential_name(client_id)
        self.credentials.set(credential_ref, token)
        try:
            record = create_agent_client(
                self.cfg.db_path,
                client_id=client_id,
                client_type=normalized_type,
                display_name=name,
                config_mode=mode,
                config_scope=scope,
                credential_ref=credential_ref,
                token_hash=token_hash(token),
                enabled=False,
                permission_mode=normalized_permission_mode,
                account_scope_mode=normalized_account_scope,
                mailbox_scope_mode=normalized_mailbox_scope,
                send_account_scope_mode=normalized_send_account_scope,
                workspace_scope_mode=normalized_workspace_scope,
                attachment_scope_mode=normalized_attachment_scope,
                send_mode=normalized_send_mode,
                notes=(" ".join(str(notes or "").split())[:500] or None),
            )
        except Exception:
            try:
                self.credentials.delete(credential_ref)
            except CredentialError:
                pass
            raise
        return self._client_view(record), token

    def list_clients(self, *, include_revoked: bool = True) -> list[dict[str, Any]]:
        return [
            self._client_view(row)
            for row in query_agent_clients(
                self.cfg.db_path, include_revoked=include_revoked
            )
        ]

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        row = get_agent_client(self.cfg.db_path, client_id)
        return self._client_view(row) if row else None

    def ensure_legacy_submit_scopes(self) -> int:
        """把 v1.5 隐含的 DATA_ROOT 提交边界固化为 v1.6 显式目录权限。"""
        changed = 0
        data_root_workspace = workspace_id_for_path(self.cfg.data_root_path)
        for client in query_agent_clients(
            self.cfg.db_path, include_revoked=True
        ):
            if str(client.get("workspace_scope_mode") or "selected") != "selected":
                continue
            rows = query_agent_client_permissions(
                self.cfg.db_path, str(client["client_id"])
            )
            has_submit = any(
                row.get("enabled")
                and row.get("effect") == "allow"
                and row.get("capability") == "result.submit"
                for row in rows
            )
            has_workspace = any(
                row.get("enabled")
                and row.get("effect") == "allow"
                and row.get("capability") == WORKSPACE_SCOPE_CAPABILITY
                for row in rows
            )
            if not has_submit or has_workspace:
                continue
            rows.append(
                {
                    "capability": WORKSPACE_SCOPE_CAPABILITY,
                    "effect": "allow",
                    "workspace_id": data_root_workspace,
                }
            )
            replace_agent_client_permissions(
                self.cfg.db_path, str(client["client_id"]), rows
            )
            changed += 1
        return changed

    def seed_v17_legacy_mailbox_scopes(self) -> int:
        """升级时把旧读权限固化到当前目录，不授权未来新增目录。"""
        changed = 0
        for client in query_agent_clients(
            self.cfg.db_path, include_revoked=True
        ):
            client_id = str(client["client_id"])
            rows = query_agent_client_permissions(self.cfg.db_path, client_id)
            has_mail_read = any(
                row.get("enabled")
                and row.get("effect") == "allow"
                and row.get("capability")
                in {"mail.search", "mail.get", "resource.read", "resource.prepare"}
                for row in rows
            )
            if (
                not has_mail_read
                or str(client.get("mailbox_scope_mode") or "selected")
                != "selected"
            ):
                continue
            existing = query_extended_scopes(self.cfg.db_path, client_id)
            if existing["mailbox_ids"] or existing["denied_mailbox_ids"]:
                continue
            selected_accounts = {
                str(row["account_id"])
                for row in rows
                if row.get("enabled")
                and row.get("effect") == "allow"
                and row.get("capability") == ACCOUNT_SCOPE_CAPABILITY
                and row.get("account_id")
            }
            if str(client.get("account_scope_mode") or "selected") == "all":
                selected_accounts = {
                    str(row["account_id"])
                    for row in query_mail_accounts(
                        self.cfg.db_path, enabled_only=True
                    )
                }
            mailbox_ids = {
                str(row["mailbox_id"])
                for row in query_mailboxes(
                    self.cfg.db_path, enabled_only=True
                )
                if str(row["account_id"]) in selected_accounts
            }
            if not mailbox_ids:
                continue
            replace_extended_scopes(
                self.cfg.db_path,
                client_id,
                mailbox_ids=mailbox_ids,
            )
            changed += 1
        return changed

    def set_permissions(
        self,
        client_id: str,
        *,
        capabilities: Iterable[str],
        account_ids: Iterable[str],
        workspace_ids: Iterable[str],
        mailbox_ids: Iterable[str] = (),
        send_account_ids: Iterable[str] = (),
        attachment_workspace_ids: Iterable[str] = (),
        denied_capabilities: Iterable[str] = (),
        denied_account_ids: Iterable[str] = (),
        denied_workspace_ids: Iterable[str] = (),
        denied_mailbox_ids: Iterable[str] = (),
        denied_send_account_ids: Iterable[str] = (),
        denied_attachment_workspace_ids: Iterable[str] = (),
        permission_mode: str = "custom",
        account_scope_mode: str = "selected",
        mailbox_scope_mode: str = "selected",
        send_account_scope_mode: str = "selected",
        workspace_scope_mode: str = "selected",
        attachment_scope_mode: str = "selected",
        send_mode: str = "confirm",
    ) -> list[dict[str, Any]]:
        if get_agent_client(self.cfg.db_path, client_id) is None:
            raise AgentAccessError("unknown_client")
        normalized_permission_mode = str(permission_mode or "custom").strip().casefold()
        normalized_account_scope = str(account_scope_mode or "selected").strip().casefold()
        normalized_mailbox_scope = str(mailbox_scope_mode or "selected").strip().casefold()
        normalized_send_account_scope = str(
            send_account_scope_mode or "selected"
        ).strip().casefold()
        normalized_workspace_scope = str(workspace_scope_mode or "selected").strip().casefold()
        normalized_attachment_scope = str(
            attachment_scope_mode or "selected"
        ).strip().casefold()
        normalized_send_mode = str(send_mode or "confirm").strip().casefold()
        if normalized_permission_mode not in PERMISSION_MODES:
            raise ValueError("权限模式无效")
        if normalized_account_scope not in SCOPE_MODES:
            raise ValueError("邮箱范围模式无效")
        if normalized_mailbox_scope not in SCOPE_MODES:
            raise ValueError("邮箱目录范围模式无效")
        if normalized_send_account_scope not in SCOPE_MODES:
            raise ValueError("发件账号范围模式无效")
        if normalized_workspace_scope not in SCOPE_MODES:
            raise ValueError("资料输出目录范围模式无效")
        if normalized_attachment_scope not in SCOPE_MODES:
            raise ValueError("附件资料目录范围模式无效")
        if normalized_send_mode not in SEND_MODES:
            raise ValueError("发件模式无效")
        allow_caps = {str(item).strip() for item in capabilities}
        if normalized_permission_mode == "recommended":
            allow_caps = set(RECOMMENDED_CAPABILITIES)
        elif normalized_permission_mode == "full":
            allow_caps = set(CAPABILITIES)
        deny_caps = {str(item).strip() for item in denied_capabilities}
        if not allow_caps.issubset(CAPABILITIES) or not deny_caps.issubset(
            CAPABILITIES
        ):
            raise ValueError("包含未知 Client capability")
        known_accounts = {
            str(row["account_id"])
            for row in query_mail_accounts(
                self.cfg.db_path, enabled_only=True
            )
        }
        selected_accounts = {str(item).strip() for item in account_ids if str(item).strip()}
        denied_accounts = {
            str(item).strip() for item in denied_account_ids if str(item).strip()
        }
        if not selected_accounts.issubset(known_accounts) or not denied_accounts.issubset(
            known_accounts
        ):
            raise ValueError("包含不存在或未启用的邮箱账号")
        known_workspaces = {
            workspace_id_for_path(path)
            for path in self.cfg.effective_allowed_send_roots
        }
        selected_workspaces = {
            str(item).strip() for item in workspace_ids if str(item).strip()
        }
        denied_workspaces = {
            str(item).strip() for item in denied_workspace_ids if str(item).strip()
        }
        if not selected_workspaces.issubset(
            known_workspaces
        ) or not denied_workspaces.issubset(known_workspaces):
            raise ValueError("包含不存在的 Agent 可用资料目录")
        if (
            normalized_workspace_scope == "selected"
            and "result.submit" in allow_caps
            and not selected_workspaces
        ):
            # v1.5 API 只用 result.submit 表达 DATA_ROOT 发件权限；
            # 为旧 Client 保留原有边界，同时在 v1.6 落成显式目录 scope。
            selected_workspaces.add(
                workspace_id_for_path(self.cfg.data_root_path)
            )
        selected_mailboxes = {
            str(item).strip() for item in mailbox_ids if str(item).strip()
        }
        denied_mailboxes = {
            str(item).strip() for item in denied_mailbox_ids if str(item).strip()
        }
        selected_send_accounts = {
            str(item).strip() for item in send_account_ids if str(item).strip()
        }
        denied_send_accounts = {
            str(item).strip()
            for item in denied_send_account_ids
            if str(item).strip()
        }
        selected_attachment_workspaces = {
            str(item).strip()
            for item in attachment_workspace_ids
            if str(item).strip()
        }
        denied_attachment_workspaces = {
            str(item).strip()
            for item in denied_attachment_workspace_ids
            if str(item).strip()
        }
        validate_extended_scope_values(
            self.cfg.db_path,
            mailbox_ids=selected_mailboxes | denied_mailboxes,
            send_account_ids=selected_send_accounts | denied_send_accounts,
            attachment_workspace_ids=(
                selected_attachment_workspaces | denied_attachment_workspaces
            ),
            configured_roots=self.cfg.effective_allowed_send_roots,
        )
        rows: list[dict[str, Any]] = [
            {"capability": item, "effect": "allow"} for item in sorted(allow_caps)
        ]
        rows.extend(
            {"capability": item, "effect": "deny"} for item in sorted(deny_caps)
        )
        rows.extend(
            {
                "capability": ACCOUNT_SCOPE_CAPABILITY,
                "effect": "allow",
                "account_id": item,
            }
            for item in sorted(
                selected_accounts if normalized_account_scope == "selected" else ()
            )
        )
        rows.extend(
            {
                "capability": ACCOUNT_SCOPE_CAPABILITY,
                "effect": "deny",
                "account_id": item,
            }
            for item in sorted(denied_accounts)
        )
        rows.extend(
            {
                "capability": WORKSPACE_SCOPE_CAPABILITY,
                "effect": "allow",
                "workspace_id": item,
            }
            for item in sorted(
                selected_workspaces if normalized_workspace_scope == "selected" else ()
            )
        )
        rows.extend(
            {
                "capability": WORKSPACE_SCOPE_CAPABILITY,
                "effect": "deny",
                "workspace_id": item,
            }
            for item in sorted(denied_workspaces)
        )
        saved = replace_agent_client_permissions(self.cfg.db_path, client_id, rows)
        replace_extended_scopes(
            self.cfg.db_path,
            client_id,
            mailbox_ids=(
                selected_mailboxes
                if normalized_mailbox_scope == "selected"
                else ()
            ),
            denied_mailbox_ids=denied_mailboxes,
            send_account_ids=(
                selected_send_accounts
                if normalized_send_account_scope == "selected"
                else ()
            ),
            denied_send_account_ids=denied_send_accounts,
            attachment_workspace_ids=(
                selected_attachment_workspaces
                if normalized_attachment_scope == "selected"
                else ()
            ),
            denied_attachment_workspace_ids=denied_attachment_workspaces,
        )
        update_agent_client(
            self.cfg.db_path,
            client_id,
            permission_mode=normalized_permission_mode,
            account_scope_mode=normalized_account_scope,
            mailbox_scope_mode=normalized_mailbox_scope,
            send_account_scope_mode=normalized_send_account_scope,
            workspace_scope_mode=normalized_workspace_scope,
            attachment_scope_mode=normalized_attachment_scope,
            send_mode=normalized_send_mode,
        )
        return saved

    def set_client_state(
        self, client_id: str, state: str, *, enabled: bool | None = None
    ) -> dict[str, Any]:
        normalized = str(state or "").strip().casefold()
        if normalized not in CLIENT_STATES:
            raise ValueError("Client 状态无效")
        row = get_agent_client(self.cfg.db_path, client_id)
        if row is None:
            raise AgentAccessError("unknown_client")
        revoked_at = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if normalized == "revoked"
            else None
        )
        effective_enabled = (
            bool(enabled)
            if enabled is not None
            else normalized == "active"
        )
        if normalized != "active":
            effective_enabled = False
        updated = update_agent_client(
            self.cfg.db_path,
            client_id,
            state=normalized,
            enabled=effective_enabled,
            revoked_at=revoked_at,
        )
        if normalized == "revoked":
            try:
                self.credentials.delete(str(row["credential_ref"]))
            except CredentialError:
                pass
        return self._client_view(updated or {})

    def rotate_client_token(
        self, client_id: str, *, replacement_token: str | None = None
    ) -> str:
        row = get_agent_client(self.cfg.db_path, client_id)
        if row is None:
            raise AgentAccessError("unknown_client")
        if row.get("state") == "revoked":
            raise AgentAccessError("client_revoked")
        credential_ref = str(row["credential_ref"])
        previous = self.credentials.get(credential_ref)
        if not previous or not hmac.compare_digest(
            str(row.get("token_hash") or ""), token_hash(previous)
        ):
            raise AgentAccessError("client_auth_failed")
        token = replacement_token or new_client_token()
        self.credentials.set(credential_ref, token)
        try:
            updated = update_agent_client(
                self.cfg.db_path, client_id, token_hash=token_hash(token)
            )
            if not updated or not hmac.compare_digest(
                str(updated.get("token_hash") or ""), token_hash(token)
            ):
                raise RuntimeError("token hash 未持久化")
        except Exception as exc:
            try:
                self.credentials.set(credential_ref, previous)
            except Exception as rollback_exc:
                raise AgentAccessError(
                    "token_rotation_rollback_failed"
                ) from rollback_exc
            raise AgentAccessError("token_rotation_failed") from exc
        return token

    def restore_client_token(
        self,
        client_id: str,
        *,
        expected_current: str,
        restored_token: str,
    ) -> None:
        """配置应用失败时恢复旧 token；失败时不产生两个同时失效的值。"""
        row = get_agent_client(self.cfg.db_path, client_id)
        if row is None:
            raise AgentAccessError("unknown_client")
        credential_ref = str(row["credential_ref"])
        current = self.credentials.get(credential_ref)
        if (
            not current
            or not hmac.compare_digest(current, expected_current)
            or not hmac.compare_digest(
                str(row.get("token_hash") or ""), token_hash(expected_current)
            )
        ):
            raise AgentAccessError("token_rotation_rollback_failed")
        self.credentials.set(credential_ref, restored_token)
        try:
            updated = update_agent_client(
                self.cfg.db_path,
                client_id,
                token_hash=token_hash(restored_token),
            )
            if not updated or not hmac.compare_digest(
                str(updated.get("token_hash") or ""),
                token_hash(restored_token),
            ):
                raise RuntimeError("旧 token hash 未恢复")
        except Exception as exc:
            try:
                self.credentials.set(credential_ref, expected_current)
            except Exception as rollback_exc:
                raise AgentAccessError(
                    "token_rotation_rollback_failed"
                ) from rollback_exc
            raise AgentAccessError("token_rotation_rollback_failed") from exc

    def get_scoped_token(self, client_id: str) -> str:
        """仅供显式配置/轮换流程取用；列表、审计和日志不得调用。"""
        row = get_agent_client(self.cfg.db_path, client_id)
        if row is None:
            raise AgentAccessError("unknown_client")
        if row.get("state") == "revoked":
            raise AgentAccessError("client_revoked")
        token = self.credentials.get(str(row["credential_ref"]))
        if not token or not hmac.compare_digest(
            str(row.get("token_hash") or ""), token_hash(token)
        ):
            raise AgentAccessError("client_auth_failed")
        return token

    def resolve_identity(self, client_id: str, token: str) -> AgentIdentity:
        normalized_id = str(client_id or "").strip().casefold()
        if not normalized_id or not token:
            raise AgentAccessError("unknown_client")
        row = get_agent_client(self.cfg.db_path, normalized_id)
        if row is None:
            raise AgentAccessError("unknown_client")
        if row.get("state") == "revoked":
            raise AgentAccessError("client_revoked")
        if row.get("state") == "paused" or not row.get("enabled"):
            raise AgentAccessError("client_disabled")
        if not hmac.compare_digest(
            str(row.get("token_hash") or ""), token_hash(token)
        ):
            raise AgentAccessError("client_auth_failed")
        permissions = query_agent_client_permissions(
            self.cfg.db_path, normalized_id
        )
        allow_caps = frozenset(
            str(item["capability"])
            for item in permissions
            if item.get("enabled")
            and item.get("effect") == "allow"
            and item.get("capability") in CAPABILITIES
        )
        deny_caps = frozenset(
            str(item["capability"])
            for item in permissions
            if item.get("enabled")
            and item.get("effect") == "deny"
            and item.get("capability") in CAPABILITIES
        )
        selected_accounts = frozenset(
            str(item["account_id"])
            for item in permissions
            if item.get("enabled")
            and item.get("effect") == "allow"
            and item.get("capability") == ACCOUNT_SCOPE_CAPABILITY
            and item.get("account_id")
        )
        deny_accounts = frozenset(
            str(item["account_id"])
            for item in permissions
            if item.get("enabled")
            and item.get("effect") == "deny"
            and item.get("capability") == ACCOUNT_SCOPE_CAPABILITY
            and item.get("account_id")
        )
        selected_workspaces = frozenset(
            str(item["workspace_id"])
            for item in permissions
            if item.get("enabled")
            and item.get("effect") == "allow"
            and item.get("capability") == WORKSPACE_SCOPE_CAPABILITY
            and item.get("workspace_id")
        )
        deny_workspaces = frozenset(
            str(item["workspace_id"])
            for item in permissions
            if item.get("enabled")
            and item.get("effect") == "deny"
            and item.get("capability") == WORKSPACE_SCOPE_CAPABILITY
            and item.get("workspace_id")
        )
        account_scope_mode = str(
            row.get("account_scope_mode") or "selected"
        ).casefold()
        mailbox_scope_mode = str(
            row.get("mailbox_scope_mode") or "selected"
        ).casefold()
        send_account_scope_mode = str(
            row.get("send_account_scope_mode") or "selected"
        ).casefold()
        workspace_scope_mode = str(
            row.get("workspace_scope_mode") or "selected"
        ).casefold()
        attachment_scope_mode = str(
            row.get("attachment_scope_mode") or "selected"
        ).casefold()
        send_mode = str(row.get("send_mode") or "confirm").casefold()
        current_accounts = frozenset(
            str(item["account_id"])
            for item in query_mail_accounts(
                self.cfg.db_path, enabled_only=True
            )
        )
        current_workspaces = frozenset(
            workspace_id_for_path(path)
            for path in self.cfg.effective_allowed_send_roots
        )
        allow_accounts = (
            current_accounts - deny_accounts
            if account_scope_mode == "all"
            else selected_accounts - deny_accounts
        )
        allow_workspaces = (
            current_workspaces - deny_workspaces
            if workspace_scope_mode == "all"
            else selected_workspaces - deny_workspaces
        )
        extended = query_extended_scopes(self.cfg.db_path, normalized_id)
        allow_mailboxes = effective_mailbox_ids(
            self.cfg.db_path,
            account_ids=allow_accounts,
            mode=mailbox_scope_mode,
            selected=extended["mailbox_ids"],
            denied=extended["denied_mailbox_ids"],
        )
        allow_send_accounts = effective_send_account_ids(
            self.cfg.db_path,
            mode=send_account_scope_mode,
            selected=extended["send_account_ids"],
            denied=extended["denied_send_account_ids"],
        )
        allow_attachment_workspaces = effective_attachment_workspace_ids(
            self.cfg.effective_allowed_send_roots,
            mode=attachment_scope_mode,
            selected=extended["attachment_workspace_ids"],
            denied=extended["denied_attachment_workspace_ids"],
        )
        update_agent_client(
            self.cfg.db_path,
            normalized_id,
            last_seen_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        return AgentIdentity(
            client_id=normalized_id,
            client_type=str(row["client_type"]),
            display_name=str(row["display_name"]),
            config_scope=str(row["config_scope"]),
            permission_mode=str(row.get("permission_mode") or "custom"),
            account_scope_mode=account_scope_mode,
            mailbox_scope_mode=mailbox_scope_mode,
            send_account_scope_mode=send_account_scope_mode,
            workspace_scope_mode=workspace_scope_mode,
            attachment_scope_mode=attachment_scope_mode,
            send_mode=send_mode,
            capabilities=allow_caps,
            denied_capabilities=deny_caps,
            account_ids=allow_accounts,
            denied_account_ids=deny_accounts,
            mailbox_ids=allow_mailboxes,
            denied_mailbox_ids=frozenset(
                extended["denied_mailbox_ids"]
            ),
            send_account_ids=allow_send_accounts,
            denied_send_account_ids=frozenset(
                extended["denied_send_account_ids"]
            ),
            workspace_ids=allow_workspaces,
            denied_workspace_ids=deny_workspaces,
            attachment_workspace_ids=allow_attachment_workspaces,
            denied_attachment_workspace_ids=frozenset(
                extended["denied_attachment_workspace_ids"]
            ),
        )

    def require_capability(
        self, identity: AgentIdentity, capability: str
    ) -> None:
        if (
            capability in identity.denied_capabilities
            or capability not in identity.capabilities
        ):
            raise AgentAccessError("capability_denied")

    def require_account(
        self, identity: AgentIdentity, account_id: str | None
    ) -> str:
        normalized = str(account_id or "").strip()
        if not normalized:
            if len(identity.account_ids) == 1:
                normalized = next(iter(identity.account_ids))
            elif identity.account_ids == frozenset(
                str(row["account_id"])
                for row in query_mail_accounts(
                    self.cfg.db_path, enabled_only=True
                )
            ) and identity.account_ids:
                return "*"
            else:
                raise AgentAccessError("account_denied")
        if (
            normalized in identity.denied_account_ids
            or normalized not in identity.account_ids
            or get_mail_account(self.cfg.db_path, normalized) is None
        ):
            raise AgentAccessError("account_denied")
        return normalized

    def account_for_mail(self, package_id: str) -> str:
        row = get_mail_package(self.cfg.db_path, package_id)
        return str((row or {}).get("account_id") or "")

    def require_mailbox(
        self, identity: AgentIdentity, mailbox_id: str | None
    ) -> str:
        normalized = str(mailbox_id or "").strip()
        if not normalized or normalized not in identity.mailbox_ids:
            raise AgentAccessError("mailbox_denied")
        row = next(
            (
                item
                for item in query_mailboxes(
                    self.cfg.db_path, enabled_only=True
                )
                if str(item["mailbox_id"]) == normalized
            ),
            None,
        )
        if row is None or str(row["account_id"]) not in identity.account_ids:
            raise AgentAccessError("mailbox_denied")
        return normalized

    def require_send_account(
        self, identity: AgentIdentity, account_id: str | None
    ) -> str:
        normalized = str(account_id or "").strip()
        if not normalized and len(identity.send_account_ids) == 1:
            normalized = next(iter(identity.send_account_ids))
        if (
            not normalized
            or normalized in identity.denied_send_account_ids
            or normalized not in identity.send_account_ids
        ):
            raise AgentAccessError("send_account_denied")
        account = get_mail_account(self.cfg.db_path, normalized)
        if (
            account is None
            or not account.get("enabled")
            or not account.get("send_enabled")
        ):
            raise AgentAccessError("send_account_denied")
        return normalized

    def require_workspace(
        self, identity: AgentIdentity, requested: str | None
    ) -> tuple[str, str]:
        rows = workspace_dtos(self.cfg)
        authorized_rows = [
            row
            for row in rows
            if str(row["workspace_id"]) in identity.workspace_ids
            and str(row["workspace_id"]) not in identity.denied_workspace_ids
        ]
        selected = None
        if requested:
            selected = next(
                (
                    row
                    for row in rows
                    if requested in {
                        str(row["workspace_id"]),
                        str(row["display_path"]),
                    }
                ),
                None,
            )
        elif len(authorized_rows) == 1:
            selected = authorized_rows[0]
        if selected is None:
            raise AgentAccessError("workspace_denied")
        workspace_id = str(selected["workspace_id"])
        if (
            workspace_id in identity.denied_workspace_ids
            or workspace_id not in identity.workspace_ids
        ):
            raise AgentAccessError("workspace_denied")
        return workspace_id, str(Path(str(selected["display_path"])).resolve())

    def require_path_workspace(
        self, identity: AgentIdentity, requested_path: str
    ) -> tuple[str, str]:
        candidate = Path(str(requested_path or "")).expanduser().resolve()
        for root_value in self.cfg.effective_allowed_send_roots:
            root = Path(root_value).resolve()
            workspace_id = workspace_id_for_path(root)
            if (
                workspace_id not in identity.workspace_ids
                or workspace_id in identity.denied_workspace_ids
            ):
                continue
            try:
                assert_within_root(candidate, root)
            except SecurityError:
                continue
            return workspace_id, str(root)
        raise AgentAccessError("workspace_denied")

    def require_attachment_path(
        self, identity: AgentIdentity, requested_path: str
    ) -> tuple[str, str]:
        candidate = Path(str(requested_path or "")).expanduser().resolve()
        for root_value in self.cfg.effective_allowed_send_roots:
            root = Path(root_value).resolve()
            workspace_id = workspace_id_for_path(root)
            if (
                workspace_id not in identity.attachment_workspace_ids
                or workspace_id
                in identity.denied_attachment_workspace_ids
            ):
                continue
            try:
                assert_within_root(candidate, root)
            except SecurityError:
                continue
            return workspace_id, str(root)
        raise AgentAccessError("attachment_scope_denied")

    @staticmethod
    def _client_view(row: dict[str, Any]) -> dict[str, Any]:
        if not row:
            return {}
        location = str(row.get("config_location") or "")
        return {
            key: value
            for key, value in row.items()
            if key not in {"token_hash", "credential_ref"}
        } | {
            "config_location_display": (
                str(Path(location).parent.name + "\\" + Path(location).name)
                if location
                else ""
            ),
            "token_stored": True,
        }
