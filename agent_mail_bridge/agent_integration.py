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
    replace_agent_client_permissions,
    update_agent_client,
)
from agent_mail_bridge.mail_resource_access import workspace_dtos


CLIENT_TYPES = {"claude_code", "codex", "claude_desktop", "custom"}
CLIENT_STATES = {"active", "paused", "revoked"}
CONFIG_MODES = {"managed", "assisted", "manual"}
CONFIG_SCOPES = {"user", "project", "custom"}

CAPABILITIES = {
    "mail.search",
    "mail.get",
    "resource.read",
    "resource.prepare",
    "sync.status",
    "sync.ensure_fresh",
    "workspace.list",
    "result.submit",
}

TOOL_CAPABILITIES = {
    "search_mails": "mail.search",
    "get_mail": "mail.get",
    "read_mail_resource": "resource.read",
    "prepare_mail_resources": "resource.prepare",
    "get_mail_sync_status": "sync.status",
    "list_agent_workspaces": "workspace.list",
    "submit_result": "result.submit",
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
    "workspace_denied": "该 Agent Client 未获准使用此工作区",
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
    capabilities: frozenset[str]
    denied_capabilities: frozenset[str]
    account_ids: frozenset[str]
    denied_account_ids: frozenset[str]
    workspace_ids: frozenset[str]
    denied_workspace_ids: frozenset[str]


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

    def set_permissions(
        self,
        client_id: str,
        *,
        capabilities: Iterable[str],
        account_ids: Iterable[str],
        workspace_ids: Iterable[str],
        denied_capabilities: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        if get_agent_client(self.cfg.db_path, client_id) is None:
            raise AgentAccessError("unknown_client")
        allow_caps = {str(item).strip() for item in capabilities}
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
        if not selected_accounts.issubset(known_accounts):
            raise ValueError("包含不存在或未启用的邮箱账号")
        known_workspaces = {
            str(row["workspace_id"]) for row in workspace_dtos(self.cfg)
        }
        selected_workspaces = {
            str(item).strip() for item in workspace_ids if str(item).strip()
        }
        if not selected_workspaces.issubset(known_workspaces):
            raise ValueError("包含不存在的 Agent 工作区")
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
            for item in sorted(selected_accounts)
        )
        rows.extend(
            {
                "capability": WORKSPACE_SCOPE_CAPABILITY,
                "effect": "allow",
                "workspace_id": item,
            }
            for item in sorted(selected_workspaces)
        )
        return replace_agent_client_permissions(self.cfg.db_path, client_id, rows)

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

    def rotate_client_token(self, client_id: str) -> str:
        row = get_agent_client(self.cfg.db_path, client_id)
        if row is None:
            raise AgentAccessError("unknown_client")
        if row.get("state") == "revoked":
            raise AgentAccessError("client_revoked")
        token = new_client_token()
        self.credentials.set(str(row["credential_ref"]), token)
        update_agent_client(
            self.cfg.db_path, client_id, token_hash=token_hash(token)
        )
        return token

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
        allow_accounts = frozenset(
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
        allow_workspaces = frozenset(
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
            capabilities=allow_caps,
            denied_capabilities=deny_caps,
            account_ids=allow_accounts,
            denied_account_ids=deny_accounts,
            workspace_ids=allow_workspaces,
            denied_workspace_ids=deny_workspaces,
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

    def require_workspace(
        self, identity: AgentIdentity, requested: str | None
    ) -> tuple[str, str]:
        rows = workspace_dtos(self.cfg)
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
        elif len(identity.workspace_ids) == 1:
            workspace_id = next(iter(identity.workspace_ids))
            selected = next(
                (
                    row
                    for row in rows
                    if str(row["workspace_id"]) == workspace_id
                ),
                None,
            )
        if selected is None:
            raise AgentAccessError("workspace_denied")
        workspace_id = str(selected["workspace_id"])
        if (
            workspace_id in identity.denied_workspace_ids
            or workspace_id not in identity.workspace_ids
        ):
            raise AgentAccessError("workspace_denied")
        return workspace_id, str(Path(str(selected["display_path"])).resolve())

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
