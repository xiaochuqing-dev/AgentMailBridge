"""Gmail API OAuth 授权模块。

职责：
1. 加载 / 刷新 / 保存 OAuth token（token.json）。
2. 首次授权时打开浏览器完成 Desktop App OAuth flow。
3. 创建并返回 Gmail API service 对象。
4. 绝不打印 token / credentials 内容到日志。

安全要求：
- 不输出 token.json 内容；
- 不输出 credentials.json 内容；
- token scope 与当前 scope 不一致时，提示用户删除旧 token 重新授权。
"""

from __future__ import annotations

import json
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.logging_setup import get_logger

logger = get_logger("gmail_api_auth")
_oauth_lock = threading.RLock()


def _serialized_oauth(func):
    """串行化 token 读取、刷新和保存。"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _oauth_lock:
            return func(*args, **kwargs)
    return wrapped

# Gmail API 客户端库（必装依赖）。提到模块级以便单元测试 mock，
# 同时用 try/except 防御：未安装时仍可导入本模块，调用时给出友好错误。
try:
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover - 仅在未安装依赖时触发
    httplib2 = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    Credentials = None  # type: ignore[assignment]
    AuthorizedHttp = None  # type: ignore[assignment]
    InstalledAppFlow = None  # type: ignore[assignment]
    build = None  # type: ignore[assignment]


class GmailApiAuthError(Exception):
    """Gmail API 授权相关错误。"""


class CredentialsNotFoundError(GmailApiAuthError):
    """credentials.json 找不到。"""


class TokenScopeMismatchError(GmailApiAuthError):
    """token scope 与当前配置 scope 不一致，需重新授权。"""


@_serialized_oauth
def get_gmail_api_service(cfg: AppConfig, *, interactive: bool = True) -> Any:
    """获取已授权的 Gmail API service。

    流程：
    1. 读取 token.json，若有效直接用。
    2. token 过期但有 refresh_token，自动刷新并保存。
    3. 无 token 或无法刷新，启动浏览器 OAuth 授权并保存 token。
    4. 返回 build("gmail", "v1", credentials=creds)。

    Args:
        cfg: 应用配置。
        interactive: 是否允许启动浏览器授权。诊断命令应传 False，
            无 token 时直接抛错而非阻塞等待浏览器。

    Raises:
        CredentialsNotFoundError: credentials.json 不存在。
        TokenScopeMismatchError: token scope 与当前配置不一致。
        GmailApiAuthError: 其它授权失败（含无 token 且非交互）。
    """
    from agent_mail_bridge.config import require_readonly_gmail_scope
    require_readonly_gmail_scope(cfg)
    if Credentials is None:
        raise GmailApiAuthError(
            "Gmail API 依赖未安装。请运行：pip install -r requirements.txt"
        )

    scopes = list(cfg.gmail_api_scopes)
    token_path = Path(cfg.gmail_api_token_path)
    credentials_path = Path(cfg.gmail_api_credentials_path)

    creds: Credentials | None = None

    # ---- 1. 尝试加载已有 token ----
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("token.json 解析失败，将重新授权（不输出 token 内容）。")
            creds = None

    # ---- 2. 校验 scope 是否匹配 ----
    if creds is not None and creds.valid:
        _ensure_scopes_match(creds, scopes)
        logger.info("Gmail API token 有效，直接复用。")
        return _build_gmail_service(cfg, creds)

    # ---- 3. token 过期但有 refresh_token，刷新 ----
    if creds is not None and creds.expired and creds.refresh_token:
        _ensure_scopes_match(creds, scopes)
        logger.info("Gmail API token 过期，自动刷新。")
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            raise GmailApiAuthError(
                "Gmail API token 刷新失败。请删除 token.json 后重新运行：\n"
                "python -m agent_mail_bridge gmail-api-auth"
            ) from exc
        _save_token(creds, token_path)
        return _build_gmail_service(cfg, creds)

    # ---- 4. 无可用 token，启动浏览器授权 ----
    if not credentials_path.exists():
        raise CredentialsNotFoundError(
            "找不到 Gmail API credentials.json。\n"
            "请确认已从 Google Cloud Console 下载 OAuth Desktop Client JSON，\n"
            f"并放到：{credentials_path}\n"
            "（由 GMAIL_API_CREDENTIALS_PATH 指定）"
        )

    if not interactive:
        raise GmailApiAuthError(
            "Gmail API token 不存在或无法刷新。\n"
            "请先运行授权命令：\n"
            "python -m agent_mail_bridge gmail-api-auth"
        )

    logger.info("未找到有效 token，启动浏览器 OAuth 授权。")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), scopes
        )
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001
        raise GmailApiAuthError(
            "Gmail API 浏览器授权失败。"
            "若应用还在 Testing 状态，请确认当前 Gmail 已加入 OAuth 测试用户。"
        ) from exc

    _save_token(creds, token_path)
    logger.info("Gmail API 授权成功，token 已保存。")
    return _build_gmail_service(cfg, creds)


def _build_gmail_service(cfg: AppConfig, creds: Any) -> Any:
    """创建带明确网络超时的 Gmail API 客户端。"""
    if httplib2 is None or AuthorizedHttp is None or build is None:
        raise GmailApiAuthError(
            "Gmail API 依赖未安装。请运行：pip install -r requirements.txt"
        )
    # 每个 HTTPS 请求最多等待配置秒数，避免 GUI 后台任务无限挂起。
    http = httplib2.Http(timeout=cfg.gmail_connect_timeout)
    authorized_http = AuthorizedHttp(creds, http=http)
    return build(
        "gmail", "v1", http=authorized_http, cache_discovery=False
    )


def _ensure_scopes_match(creds: Any, scopes: list[str]) -> None:
    """校验 token 的 scope 与当前配置一致，不一致提示重新授权。

    不输出 token 内容。
    """
    token_scopes = set(getattr(creds, "scopes", None) or [])
    want_scopes = set(scopes)
    if token_scopes and token_scopes != want_scopes:
        raise TokenScopeMismatchError(
            "Gmail API token 权限与当前配置不一致。\n"
            f"请删除 token.json 后重新运行：\n"
            "python -m agent_mail_bridge gmail-api-auth"
        )


def _save_token(creds: Any, token_path: Path) -> None:
    """保存 token 到指定路径（不打印内容）。"""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    # 权限收紧：仅所有者可读写（Unix；Windows 下忽略）
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


# ============================================================
# token 状态检查（供诊断命令使用，不输出敏感内容）
# ============================================================

def describe_token_status(cfg: AppConfig) -> dict[str, Any]:
    """返回 token 状态摘要（不含 token 内容），供诊断命令使用。

    Returns:
        {
          "exists": bool,
          "valid": bool,
          "expired": bool,
          "refreshable": bool,   # 过期但有 refresh_token
          "scopes_match": bool,  # token scope 与配置是否一致
          "error": str | None,
        }
    """
    token_path = Path(cfg.gmail_api_token_path)
    scopes = list(cfg.gmail_api_scopes)

    result: dict[str, Any] = {
        "exists": token_path.exists(),
        "valid": False,
        "expired": False,
        "refreshable": False,
        "scopes_match": True,
        "error": None,
    }
    if not token_path.exists():
        result["error"] = "token.json 不存在"
        return result

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    except Exception as exc:  # noqa: BLE001
        result["error"] = "token.json 解析失败（建议删除后重新授权）"
        return result

    token_scopes = set(getattr(creds, "scopes", None) or [])
    if token_scopes and token_scopes != set(scopes):
        result["scopes_match"] = False
        result["error"] = "token scope 与当前配置不一致（建议删除后重新授权）"

    if creds.valid:
        result["valid"] = True
    elif creds.expired and creds.refresh_token:
        result["expired"] = True
        result["refreshable"] = True
    else:
        result["error"] = result["error"] or "token 无效（建议删除后重新授权）"
    return result


def get_oauth_state(cfg: AppConfig) -> dict[str, Any]:
    """返回 GUI 可直接消费的 OAuth 明确状态。"""
    from agent_mail_bridge.config import ConfigError, require_readonly_gmail_scope

    if not Path(cfg.gmail_api_credentials_path).exists():
        return {"state": "CREDENTIALS_MISSING", "message": "缺少 credentials.json"}
    try:
        require_readonly_gmail_scope(cfg)
    except ConfigError as exc:
        return {"state": "SCOPE_MISMATCH", "message": str(exc)}
    status = describe_token_status(cfg)
    if not status["exists"]:
        return {"state": "AUTH_REQUIRED", "message": "尚未完成 Gmail API 授权"}
    if not status["scopes_match"]:
        return {"state": "SCOPE_MISMATCH", "message": status["error"] or "权限不匹配"}
    if status["valid"]:
        return {"state": "READY", "message": "Gmail API 已授权"}
    if status["expired"] and status["refreshable"]:
        return {
            "state": "TOKEN_EXPIRED_REFRESHABLE",
            "message": "token 已过期，可自动刷新",
        }
    return {"state": "TOKEN_INVALID", "message": status["error"] or "token 无效"}


def validate_credentials_file(cfg: AppConfig) -> dict[str, Any]:
    """检查 credentials.json 是否存在且结构合法（不输出敏感内容）。"""
    credentials_path = Path(cfg.gmail_api_credentials_path)
    result: dict[str, Any] = {
        "exists": credentials_path.exists(),
        "valid": False,
        "error": None,
    }
    if not credentials_path.exists():
        result["error"] = f"credentials.json 不存在：{credentials_path}"
        return result
    try:
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        result["error"] = "credentials.json 不是合法 JSON"
        return result
    # Desktop App 类型应为 {"installed": {...}}
    if "installed" not in data and "web" not in data:
        result["error"] = (
            "credentials.json 结构异常，缺少 installed/web 节点。"
            "请确认下载的是 Desktop App OAuth Client。"
        )
        return result
    result["valid"] = True
    return result
