"""Gmail OAuth 授权、Token 生命周期与 Gmail Profile 验证。"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.mail_common import canonical_gmail_address
from agent_mail_bridge.models import OperationStatus, ServiceResult
from agent_mail_bridge.oauth_flow import (
    LoopbackCallbackServer,
    OAUTH_STATE_MESSAGES,
    OAuthCallback,
    OAuthState,
    OAuthStateMachine,
    OAuthWaitCancelled,
    OAuthWaitTimeout,
    loopback_no_proxy,
)
from agent_mail_bridge.oauth_storage import (
    OAuthImportError,
    ValidatedOAuthCredentials,
    atomic_write_private_text,
    validate_oauth_credentials_file,
)
from agent_mail_bridge.process_lock import ProcessLock


logger = get_logger("gmail_api_auth")
_oauth_lock = threading.RLock()
_active_session_guard = threading.Lock()
_active_session_id: str | None = None
_diagnostics_guard = threading.Lock()
_last_oauth_diagnostics: dict[str, Any] = {
    "active": False,
    "stage": OAuthState.IDLE.value,
    "callback_host": "127.0.0.1",
    "callback_bound": False,
    "callback_received": False,
    "port_released": True,
}


try:
    import httplib2
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from oauthlib.oauth2 import OAuth2Error
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import ProxyError, Timeout as RequestsTimeout
    HttpLib2ProxiesUnavailableError = getattr(
        httplib2, "ProxiesUnavailableError", ()
    )
    HttpLib2ServerNotFoundError = getattr(httplib2, "ServerNotFoundError", ())
except ImportError:  # pragma: no cover - 缺依赖时保留可读错误。
    httplib2 = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    Credentials = None  # type: ignore[assignment]
    AuthorizedHttp = None  # type: ignore[assignment]
    InstalledAppFlow = None  # type: ignore[assignment]
    build = None  # type: ignore[assignment]
    HttpError = ()  # type: ignore[assignment,misc]
    OAuth2Error = ()  # type: ignore[assignment,misc]
    RefreshError = ()  # type: ignore[assignment,misc]
    TransportError = ()  # type: ignore[assignment,misc]
    ProxyError = ()  # type: ignore[assignment,misc]
    RequestsTimeout = ()  # type: ignore[assignment,misc]
    RequestsConnectionError = ()  # type: ignore[assignment,misc]
    HttpLib2ProxiesUnavailableError = ()  # type: ignore[assignment,misc]
    HttpLib2ServerNotFoundError = ()  # type: ignore[assignment,misc]

try:
    from socks import ProxyError as SocksProxyError
except ImportError:  # pragma: no cover - PySocks 是正式依赖。
    SocksProxyError = ()  # type: ignore[assignment,misc]


class GmailApiAuthError(Exception):
    """带稳定 error_code 的 Gmail API 授权错误。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "internal_error",
        retryable: bool = False,
        technical_detail: str = "",
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.technical_detail = technical_detail


class CredentialsNotFoundError(GmailApiAuthError):
    """credentials.json 找不到。"""


class TokenScopeMismatchError(GmailApiAuthError):
    """Token scope 与当前只读 scope 不一致。"""


class TokenClientMismatchError(GmailApiAuthError):
    """Token 所属 Client ID 与当前 credentials 不一致。"""


class OAuthLockBusyError(GmailApiAuthError):
    """另一进程正在修改同一 OAuth 状态。"""


@dataclass(frozen=True)
class OAuthErrorInfo:
    error_code: str
    title: str
    reason: str
    next_step: str
    retryable: bool
    stage: str
    technical_detail: str = ""


_ERROR_COPY: dict[str, tuple[str, str, str, bool]] = {
    "oauth_already_running": (
        "授权正在进行",
        "当前进程已经存在一个 Gmail OAuth 会话。",
        "请完成或取消当前会话后重试。",
        True,
    ),
    "oauth_lock_busy": (
        "OAuth 正由另一进程使用",
        "另一个 AgentMailBridge 进程正在授权或更新 Token。",
        "请关闭另一进程的授权窗口，稍后重试。",
        True,
    ),
    "credentials_unreadable": (
        "无法读取 OAuth 凭据",
        "Desktop app credentials.json 不存在、过大或无法访问。",
        "请重新选择 Google Cloud 下载的 Desktop app JSON。",
        True,
    ),
    "credentials_invalid_json": (
        "OAuth 凭据格式无效",
        "所选文件不是有效的 UTF-8 JSON。",
        "请重新下载 Desktop app credentials.json。",
        True,
    ),
    "credentials_wrong_type": (
        "OAuth 客户端类型错误",
        "该 JSON 不是 Google Desktop app OAuth Client。",
        "请在 Google Cloud 创建 Desktop app 凭据；Web application 凭据不能使用。",
        True,
    ),
    "credentials_missing_field": (
        "OAuth 凭据字段不完整",
        "Desktop app credentials.json 缺少必需字段或 Client ID 无效。",
        "请重新下载完整的 Desktop app JSON。",
        True,
    ),
    "credentials_invalid_endpoint": (
        "OAuth 端点不安全",
        "凭据包含非 Google 官方端点或非本地回环地址。",
        "请使用 Google Cloud 原始下载的 Desktop app JSON。",
        False,
    ),
    "callback_bind_failed": (
        "本地回调未能启动",
        "AgentMailBridge 无法绑定 127.0.0.1 随机端口。",
        "请检查安全软件、防火墙和本机端口策略后重试。",
        True,
    ),
    "browser_open_failed": (
        "浏览器未能自动打开",
        "系统默认浏览器没有接受打开请求。",
        "请使用复制授权链接或重新打开浏览器。",
        True,
    ),
    "oauth_timeout": (
        "授权等待超时",
        "规定时间内没有收到浏览器回调。",
        "请重试，并确认浏览器能访问 127.0.0.1 且本地回环不经过代理。",
        True,
    ),
    "oauth_cancelled": (
        "授权已取消",
        "当前授权会话已安全停止，未保存新的 Token。",
        "需要时可立即重新授权。",
        True,
    ),
    "access_denied": (
        "Google 未同意授权",
        "Google 账号未同意授权，或账号策略阻止了该应用。",
        "确认账号和组织策略后重新授权。",
        True,
    ),
    "oauth_error_response": (
        "Google 返回 OAuth 错误",
        "浏览器回调包含 OAuth 错误结果。",
        "请根据错误详情检查 Google Cloud OAuth 配置。",
        True,
    ),
    "oauth_state_mismatch": (
        "OAuth 安全状态不匹配",
        "收到的回调不属于当前授权会话。",
        "请关闭旧浏览器页面并重新发起授权。",
        True,
    ),
    "callback_invalid": (
        "OAuth 回调无效",
        "本地回调缺少唯一的 code 或 state。",
        "请关闭旧页面并重新授权。",
        True,
    ),
    "redirect_uri_mismatch": (
        "本地回环地址被 Google 拒绝",
        "Google 拒绝了当前 Desktop app 回环地址。",
        "请确认使用的是 Desktop app 凭据，而不是 Web application 凭据。",
        True,
    ),
    "invalid_client": (
        "OAuth Client 无效",
        "Google 无法识别当前 OAuth Client。",
        "请重新下载有效的 Desktop app credentials.json。",
        False,
    ),
    "deleted_client": (
        "OAuth Client 已删除",
        "Google Cloud 中的 OAuth Client 已被删除。",
        "请创建新的 Desktop app Client 并重新导入。",
        False,
    ),
    "token_exchange_failed": (
        "Token 交换失败",
        "Google 没有完成授权码到 Token 的交换。",
        "请检查网络、系统时间和 OAuth Client 后重试。",
        True,
    ),
    "refresh_token_missing": (
        "缺少长期授权凭据",
        "Google 没有返回可长期使用的 refresh token。",
        "请重新授权并完成同意页面。",
        True,
    ),
    "token_save_failed": (
        "Token 保存失败",
        "新的 OAuth Token 无法安全写入本地文件。",
        "请检查 OAuth 目录权限后重试；旧 Token 已保留。",
        True,
    ),
    "token_client_mismatch": (
        "OAuth Client 已更换",
        "现有 Token 不属于当前 credentials.json。",
        "请重新授权；程序不会使用不匹配的旧 Token。",
        True,
    ),
    "refresh_invalid_grant": (
        "Gmail 授权已失效",
        "Google 拒绝了 refresh token，授权可能已撤销或过期。",
        "请重新进行浏览器授权；Desktop app 凭据会保留。",
        True,
    ),
    "refresh_revoked": (
        "Gmail 授权已撤销",
        "Google 账号已撤销 AgentMailBridge 的长期授权。",
        "请重新进行浏览器授权。",
        True,
    ),
    "network_error": (
        "网络连接失败",
        "Token 交换或 Gmail Profile 验证遇到网络超时或连接错误。",
        "请检查网络后重试；已取得的有效 Token 会安全保留。",
        True,
    ),
    "proxy_error": (
        "代理连接失败",
        "OAuth 网络请求受到代理配置影响。",
        "请检查代理，并确保 127.0.0.1 和 localhost 不经过代理。",
        True,
    ),
    "gmail_api_disabled": (
        "Gmail API 尚未启用",
        "OAuth 已完成，但当前 Google Cloud 项目未启用 Gmail API。",
        "请启用 Gmail API，然后点击重新验证 Gmail API。",
        True,
    ),
    "insufficient_scope": (
        "Gmail 权限不足",
        "当前 Token 不包含 gmail.readonly。",
        "请重新授权，只授予 AgentMailBridge 所需的只读权限。",
        True,
    ),
    "profile_check_failed": (
        "Gmail API 验证暂未通过",
        "Token 已取得，但 Gmail Profile 暂时无法验证。",
        "稍后点击重新验证 Gmail API，无需重复浏览器授权。",
        True,
    ),
    "account_mismatch": (
        "授权账号不匹配",
        "Google 返回的 Gmail 账号与当前配置账号不同。",
        "请取消并重新选择正确的 Gmail 账号。",
        True,
    ),
    "internal_error": (
        "OAuth 内部错误",
        "授权流程遇到未预期错误。",
        "请重试并查看脱敏诊断信息。",
        True,
    ),
}


class GmailOAuthSession:
    """单次、可取消、有限时且不触碰 QWidget 的 Gmail OAuth 会话。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        timeout_seconds: float = 300.0,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        browser_opener: Callable[..., bool] | None = None,
    ):
        self.cfg = cfg
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.progress_callback = progress_callback
        self.browser_opener = browser_opener
        self.session_id = uuid.uuid4().hex
        # 账号核对必须使用会话开始时的稳定快照，不能受后续配置变更影响。
        self._expected_gmail_address = cfg.gmail_address
        self.machine = OAuthStateMachine()
        self.cancel_event = threading.Event()
        self._started_at = time.monotonic()
        self._authorization_url: str | None = None
        self._expected_state: str | None = None
        self._callback_server: LoopbackCallbackServer | None = None
        self._resource_lock = threading.RLock()

    @property
    def state(self) -> OAuthState:
        return self.machine.state

    @property
    def authorization_url_available(self) -> bool:
        with self._resource_lock:
            return bool(self._authorization_url)

    def cancel(self) -> bool:
        if self.state in {
            OAuthState.AUTHORIZED,
            OAuthState.AUTHORIZED_UNVERIFIED,
            OAuthState.CANCELLED,
            OAuthState.TIMED_OUT,
            OAuthState.FAILED,
        }:
            return False
        self.cancel_event.set()
        with self._resource_lock:
            server = self._callback_server
        if server is not None:
            server.wake()
        return True

    def reopen_browser(self) -> ServiceResult:
        with self._resource_lock:
            url = self._authorization_url
        if not url or self.state != OAuthState.WAITING_FOR_USER:
            return _error_result(
                "browser_open_failed",
                self.state,
                terminal_state=self.state,
                status=OperationStatus.FAILED,
            )
        try:
            with loopback_no_proxy():
                opened = bool(self._open_browser(url))
        except Exception as exc:  # noqa: BLE001
            opened = False
            detail = _safe_exception_detail(exc)
        else:
            detail = ""
        if not opened:
            return _error_result(
                "browser_open_failed",
                self.state,
                terminal_state=self.state,
                status=OperationStatus.FAILED,
                technical_detail=detail,
            )
        return ServiceResult(OperationStatus.SUCCESS, message="已重新打开同一授权链接")

    def run(self) -> ServiceResult:
        process_lock: ProcessLock | None = None
        claimed = False
        self._transition(OAuthState.VALIDATING_CREDENTIALS)
        try:
            claimed = _claim_active_session(self.session_id)
            if not claimed:
                return self._failure("oauth_already_running")
            process_lock = ProcessLock(_oauth_process_lock_path(self.cfg))
            if not process_lock.acquire(timeout=0.25):
                return self._failure("oauth_lock_busy")
            self._raise_if_cancelled()
            try:
                validated = validate_oauth_credentials_file(
                    self.cfg.gmail_api_credentials_path
                )
            except OAuthImportError as exc:
                return self._failure(
                    exc.error_code, technical_detail=_safe_exception_detail(exc)
                )
            self._raise_if_cancelled()
            self._transition(OAuthState.PREPARING_CALLBACK)
            server = LoopbackCallbackServer()
            with self._resource_lock:
                self._callback_server = server
            try:
                port = server.start()
            except OSError as exc:
                return self._failure(
                    "callback_bind_failed", technical_detail=_safe_exception_detail(exc)
                )
            self._transition(
                OAuthState.CALLBACK_READY,
                callback_host="127.0.0.1",
                callback_port=port,
            )
            self._raise_if_cancelled()

            if InstalledAppFlow is None:
                return self._failure("internal_error", technical_detail="OAuth 依赖未安装")
            flow = InstalledAppFlow.from_client_config(
                validated.payload,
                list(self.cfg.gmail_api_scopes),
                autogenerate_code_verifier=True,
            )
            flow.redirect_uri = f"http://127.0.0.1:{port}/"
            authorization_kwargs = {
                "include_granted_scopes": "true",
                # 只有用户显式点击“开始/重新授权”才进入这里；普通启动、收件和
                # “重新验证 Gmail API”都复用现有 Token，不会重复要求 consent。
                # 显式授权必须取得与本次所选账号绑定的新 refresh token。
                "prompt": "consent",
            }
            authorization_url, expected_state = flow.authorization_url(
                **authorization_kwargs
            )
            with self._resource_lock:
                self._authorization_url = authorization_url
                self._expected_state = expected_state

            self._transition(
                OAuthState.OPENING_BROWSER,
                authorization_url=authorization_url,
            )
            try:
                with loopback_no_proxy():
                    browser_opened = bool(self._open_browser(authorization_url))
                browser_detail = ""
            except Exception as exc:  # noqa: BLE001
                browser_opened = False
                browser_detail = _safe_exception_detail(exc)
            self._transition(
                OAuthState.WAITING_FOR_USER,
                authorization_url=authorization_url,
                browser_opened=browser_opened,
                warning_error_code=None if browser_opened else "browser_open_failed",
                technical_detail=browser_detail,
            )

            try:
                callback = server.wait(
                    self.cancel_event,
                    timeout_seconds=self.timeout_seconds,
                )
            except OAuthWaitCancelled:
                return self._cancelled()
            except OAuthWaitTimeout:
                self._transition(OAuthState.TIMED_OUT, error_code="oauth_timeout")
                return _error_result(
                    "oauth_timeout",
                    OAuthState.WAITING_FOR_USER,
                    terminal_state=OAuthState.TIMED_OUT,
                )

            self._transition(OAuthState.CALLBACK_RECEIVED)
            callback_error = self._validate_callback(callback)
            if callback_error:
                status = (
                    OperationStatus.CANCELLED
                    if callback_error == "access_denied"
                    else OperationStatus.FAILED
                )
                return self._failure(callback_error, status=status)
            self._raise_if_cancelled()
            self._transition(OAuthState.EXCHANGING_TOKEN)
            authorization_response = _authorization_response(
                flow.redirect_uri,
                callback.code or "",
                callback.state or "",
            )
            try:
                with loopback_no_proxy():
                    flow.fetch_token(
                        authorization_response=authorization_response,
                        timeout=self.cfg.gmail_connect_timeout,
                    )
            except Exception as exc:  # noqa: BLE001
                self._raise_if_cancelled()
                code = _classify_token_exception(exc)
                return self._failure(code, technical_detail=_safe_exception_detail(exc))
            self._raise_if_cancelled()
            creds = flow.credentials
            try:
                _ensure_scopes_match(creds, list(self.cfg.gmail_api_scopes))
            except TokenScopeMismatchError as exc:
                return self._failure(
                    "insufficient_scope", technical_detail=_safe_exception_detail(exc)
                )
            if not getattr(creds, "refresh_token", None):
                return self._failure("refresh_token_missing")

            self._transition(OAuthState.VERIFYING_GMAIL)
            try:
                profile = (
                    _build_gmail_service(self.cfg, creds)
                    .users()
                    .getProfile(userId="me")
                    .execute()
                )
                actual_email = str(profile.get("emailAddress") or "").strip()
            except Exception as exc:  # noqa: BLE001
                self._raise_if_cancelled()
                error_code = _classify_profile_exception(exc)
                if error_code in {
                    "gmail_api_disabled",
                    "network_error",
                    "proxy_error",
                    "profile_check_failed",
                }:
                    save_error = self._save_candidate_token(creds)
                    if save_error is not None:
                        return save_error
                    self._transition(
                        OAuthState.AUTHORIZED_UNVERIFIED,
                        error_code=error_code,
                    )
                    return _error_result(
                        error_code,
                        OAuthState.VERIFYING_GMAIL,
                        terminal_state=OAuthState.AUTHORIZED_UNVERIFIED,
                        status=OperationStatus.PARTIAL,
                        technical_detail=_safe_exception_detail(exc),
                    )
                return self._failure(
                    error_code, technical_detail=_safe_exception_detail(exc)
                )

            self._raise_if_cancelled()
            if not actual_email:
                save_error = self._save_candidate_token(creds)
                if save_error is not None:
                    return save_error
                self._transition(
                    OAuthState.AUTHORIZED_UNVERIFIED,
                    error_code="profile_check_failed",
                )
                return _error_result(
                    "profile_check_failed",
                    OAuthState.VERIFYING_GMAIL,
                    terminal_state=OAuthState.AUTHORIZED_UNVERIFIED,
                    status=OperationStatus.PARTIAL,
                )
            if canonical_gmail_address(actual_email) != canonical_gmail_address(
                self._expected_gmail_address
            ):
                return self._failure(
                    "account_mismatch",
                    extra_details={
                        "expected_email_masked": _mask_email(
                            self._expected_gmail_address
                        ),
                        "actual_email_masked": _mask_email(actual_email),
                    },
                )
            save_error = self._save_candidate_token(creds)
            if save_error is not None:
                return save_error
            self._transition(OAuthState.AUTHORIZED)
            return ServiceResult(
                OperationStatus.SUCCESS,
                message="Gmail API 授权成功",
                details={
                    "oauth_state": OAuthState.AUTHORIZED.value,
                    "email": actual_email,
                    "client_type": validated.summary.client_type,
                    "client_id_suffix": validated.summary.client_id_suffix,
                },
            )
        except OAuthWaitCancelled:
            return self._cancelled()
        except Exception as exc:  # noqa: BLE001
            return self._failure(
                "internal_error", technical_detail=_safe_exception_detail(exc)
            )
        finally:
            with self._resource_lock:
                server, self._callback_server = self._callback_server, None
                self._authorization_url = None
                self._expected_state = None
            if server is not None:
                server.close()
            _update_oauth_diagnostics(active=False, port_released=True)
            if process_lock is not None:
                process_lock.release()
            if claimed:
                _release_active_session(self.session_id)

    def _save_candidate_token(self, creds: Any) -> ServiceResult | None:
        self._raise_if_cancelled()
        try:
            _save_token(creds, self.cfg.gmail_api_token_path)
        except GmailApiAuthError as exc:
            return self._failure(
                exc.error_code, technical_detail=exc.technical_detail
            )
        return None

    def _validate_callback(self, callback: OAuthCallback) -> str | None:
        if callback.invalid_parameters:
            return "callback_invalid"
        if not callback.state:
            return "callback_invalid"
        with self._resource_lock:
            expected = self._expected_state
        if not expected or not secrets.compare_digest(callback.state, expected):
            return "oauth_state_mismatch"
        if callback.error:
            return "access_denied" if callback.error == "access_denied" else "oauth_error_response"
        if not callback.code:
            return "callback_invalid"
        return None

    def _open_browser(self, url: str) -> bool:
        opener = self.browser_opener or webbrowser.open
        return bool(opener(url, new=1, autoraise=True))

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise OAuthWaitCancelled("OAuth 授权已取消")

    def _cancelled(self) -> ServiceResult:
        if self.state != OAuthState.CANCELLING:
            self._transition(OAuthState.CANCELLING)
        self._transition(OAuthState.CANCELLED, error_code="oauth_cancelled")
        return _error_result(
            "oauth_cancelled",
            OAuthState.CANCELLING,
            terminal_state=OAuthState.CANCELLED,
            status=OperationStatus.CANCELLED,
        )

    def _failure(
        self,
        error_code: str,
        *,
        status: OperationStatus = OperationStatus.FAILED,
        technical_detail: str = "",
        extra_details: dict[str, Any] | None = None,
    ) -> ServiceResult:
        failed_stage = self.state
        if self.state != OAuthState.FAILED:
            self._transition(OAuthState.FAILED, error_code=error_code)
        logger.warning("Gmail OAuth 失败：error_code=%s", error_code)
        return _error_result(
            error_code,
            failed_stage,
            terminal_state=OAuthState.FAILED,
            status=status,
            technical_detail=technical_detail,
            extra_details=extra_details,
        )

    def _transition(self, target: OAuthState, **details: Any) -> None:
        self.machine.transition(target)
        elapsed_ms = int((time.monotonic() - self._started_at) * 1000)
        logger.info("Gmail OAuth 阶段：%s，耗时=%dms", target.value, elapsed_ms)
        diagnostics = {
            "active": target
            not in {
                OAuthState.AUTHORIZED,
                OAuthState.AUTHORIZED_UNVERIFIED,
                OAuthState.CANCELLED,
                OAuthState.TIMED_OUT,
                OAuthState.FAILED,
            },
            "stage": target.value,
            "elapsed_ms": elapsed_ms,
            "callback_host": str(details.get("callback_host") or "127.0.0.1"),
            "port_released": False,
        }
        if target in {
            OAuthState.IDLE,
            OAuthState.VALIDATING_CREDENTIALS,
            OAuthState.PREPARING_CALLBACK,
        }:
            diagnostics["callback_bound"] = False
            diagnostics["callback_received"] = False
        elif target == OAuthState.CALLBACK_READY:
            diagnostics["callback_bound"] = True
            diagnostics["callback_received"] = False
        elif target == OAuthState.CALLBACK_RECEIVED:
            diagnostics["callback_received"] = True
        if isinstance(details.get("callback_port"), int):
            diagnostics["callback_port"] = details["callback_port"]
        if isinstance(details.get("browser_opened"), bool):
            diagnostics["browser_opened"] = details["browser_opened"]
        error_code = details.get("error_code") or details.get("warning_error_code")
        diagnostics["error_code"] = (
            error_code if isinstance(error_code, str) and error_code else None
        )
        _update_oauth_diagnostics(**diagnostics)
        if self.progress_callback is None:
            return
        event = {
            "session_id": self.session_id,
            "state": target.value,
            "message": OAUTH_STATE_MESSAGES[target],
            "elapsed_ms": elapsed_ms,
            **details,
        }
        try:
            self.progress_callback(event)
        except Exception:  # pragma: no cover - UI 销毁不能破坏 OAuth 清理。
            logger.warning("OAuth 状态通知接收方已不可用")


def get_gmail_api_service(cfg: AppConfig, *, interactive: bool = True) -> Any:
    """加载或刷新现有 Token；无 Token 时仅显式 interactive 才启动授权。"""

    from agent_mail_bridge.config import require_readonly_gmail_scope

    require_readonly_gmail_scope(cfg)
    _require_google_dependencies()
    try:
        validated = validate_oauth_credentials_file(cfg.gmail_api_credentials_path)
    except OAuthImportError as exc:
        if not Path(cfg.gmail_api_credentials_path).exists():
            raise CredentialsNotFoundError(
                "找不到 Gmail API credentials.json",
                error_code="credentials_unreadable",
            ) from exc
        raise GmailApiAuthError(str(exc), error_code=exc.error_code) from exc

    process_lock = ProcessLock(_oauth_process_lock_path(cfg))
    if not process_lock.acquire(timeout=2.0):
        raise OAuthLockBusyError(
            "另一个 AgentMailBridge 进程正在使用 OAuth Token",
            error_code="oauth_lock_busy",
            retryable=True,
        )
    missing_token = False
    try:
        with _oauth_lock:
            creds = _load_token_credentials(cfg, validated)
            if creds is None:
                missing_token = True
            else:
                _ensure_scopes_match(creds, list(cfg.gmail_api_scopes))
                if not getattr(creds, "refresh_token", None):
                    raise GmailApiAuthError(
                        "Gmail OAuth Token 缺少 refresh token",
                        error_code="refresh_token_missing",
                    )
                if creds.valid:
                    return _build_gmail_service(cfg, creds)
                if creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(_BoundedGoogleRequest(cfg.gmail_connect_timeout))
                    except Exception as exc:  # noqa: BLE001
                        code = _classify_refresh_exception(exc)
                        raise GmailApiAuthError(
                            "Gmail API Token 刷新失败",
                            error_code=code,
                            retryable=code in {"network_error", "proxy_error"},
                            technical_detail=_safe_exception_detail(exc),
                        ) from exc
                    _save_token(creds, cfg.gmail_api_token_path)
                    return _build_gmail_service(cfg, creds)
                missing_token = True
    finally:
        process_lock.release()

    if not missing_token:
        raise GmailApiAuthError("Gmail OAuth Token 不可用", error_code="token_exchange_failed")
    if not interactive:
        raise GmailApiAuthError(
            "Gmail API Token 不存在或无法刷新，请先完成浏览器授权",
            error_code="refresh_token_missing",
        )
    result = GmailOAuthSession(cfg).run()
    if not result.ok:
        raise GmailApiAuthError(
            result.message,
            error_code=result.error_code or "internal_error",
            retryable=bool(result.details.get("retryable")),
            technical_detail=str(result.details.get("technical_detail") or ""),
        )
    return get_gmail_api_service(cfg, interactive=False)


def reverify_gmail_authorization(cfg: AppConfig) -> ServiceResult:
    """使用现有 Token 后台重试 Gmail Profile，不重复打开浏览器。"""

    try:
        service = get_gmail_api_service(cfg, interactive=False)
        profile = service.users().getProfile(userId="me").execute()
        actual_email = str(profile.get("emailAddress") or "").strip()
        if not actual_email:
            return _error_result(
                "profile_check_failed",
                OAuthState.VERIFYING_GMAIL,
                terminal_state=OAuthState.AUTHORIZED_UNVERIFIED,
                status=OperationStatus.PARTIAL,
            )
        if canonical_gmail_address(actual_email) != canonical_gmail_address(
            cfg.gmail_address
        ):
            return _error_result(
                "account_mismatch",
                OAuthState.VERIFYING_GMAIL,
                terminal_state=OAuthState.FAILED,
                extra_details={
                    "expected_email_masked": _mask_email(cfg.gmail_address),
                    "actual_email_masked": _mask_email(actual_email),
                },
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="Gmail API 连接正常",
            details={"oauth_state": OAuthState.AUTHORIZED.value, "email": actual_email},
        )
    except GmailApiAuthError as exc:
        return _error_result(
            exc.error_code,
            OAuthState.VERIFYING_GMAIL,
            terminal_state=OAuthState.FAILED,
            technical_detail=exc.technical_detail,
        )
    except Exception as exc:  # noqa: BLE001
        code = _classify_profile_exception(exc)
        terminal = (
            OAuthState.AUTHORIZED_UNVERIFIED
            if code in {
                "gmail_api_disabled",
                "network_error",
                "proxy_error",
                "profile_check_failed",
            }
            else OAuthState.FAILED
        )
        status = (
            OperationStatus.PARTIAL
            if terminal == OAuthState.AUTHORIZED_UNVERIFIED
            else OperationStatus.FAILED
        )
        return _error_result(
            code,
            OAuthState.VERIFYING_GMAIL,
            terminal_state=terminal,
            status=status,
            technical_detail=_safe_exception_detail(exc),
        )


def clear_local_gmail_token(cfg: AppConfig) -> ServiceResult:
    """仅删除本地 Token；credentials.json 始终保留。"""

    token_path = Path(cfg.gmail_api_token_path)
    process_lock = ProcessLock(_oauth_process_lock_path(cfg))
    if not process_lock.acquire(timeout=1.0):
        return _error_result(
            "oauth_lock_busy",
            OAuthState.IDLE,
            terminal_state=OAuthState.FAILED,
        )
    try:
        if not token_path.exists():
            return ServiceResult(
                OperationStatus.NO_CHANGES,
                message="本地 Gmail OAuth Token 已不存在",
            )
        try:
            token_path.unlink()
        except OSError as exc:
            return _error_result(
                "token_save_failed",
                OAuthState.IDLE,
                terminal_state=OAuthState.FAILED,
                technical_detail=_safe_exception_detail(exc),
            )
        return ServiceResult(
            OperationStatus.SUCCESS,
            message="本地 Gmail OAuth Token 已清除；Desktop app 凭据已保留",
        )
    finally:
        process_lock.release()


def get_oauth_diagnostics(cfg: AppConfig) -> dict[str, Any]:
    """返回可导出的非敏感 OAuth 诊断，不含 URL、state、code 或 Token。"""

    credentials = validate_credentials_file(cfg)
    with _diagnostics_guard:
        runtime = dict(_last_oauth_diagnostics)
    return {
        "desktop_credentials_valid": bool(credentials.get("valid")),
        "credentials_error_code": credentials.get("error_code"),
        "client_type": credentials.get("client_type", ""),
        "callback_host": runtime.get("callback_host", "127.0.0.1"),
        "callback_bound": bool(runtime.get("callback_bound")),
        "callback_received": bool(runtime.get("callback_received")),
        "port_released": bool(runtime.get("port_released", True)),
        "active": bool(runtime.get("active")),
        "stage": str(runtime.get("stage") or OAuthState.IDLE.value),
        "error_code": runtime.get("error_code"),
        "elapsed_ms": int(runtime.get("elapsed_ms") or 0),
    }


def describe_token_status(cfg: AppConfig) -> dict[str, Any]:
    """返回不含 Token 内容的安全状态摘要。"""

    token_path = Path(cfg.gmail_api_token_path)
    result: dict[str, Any] = {
        "exists": token_path.exists(),
        "valid": False,
        "expired": False,
        "refreshable": False,
        "scopes_match": True,
        "client_match": True,
        "error": None,
        "error_code": None,
    }
    if not token_path.exists():
        result.update(error="token.json 不存在", error_code="refresh_token_missing")
        return result
    if Credentials is None:
        result.update(error="Gmail API 依赖未安装", error_code="internal_error")
        return result
    try:
        validated = validate_oauth_credentials_file(cfg.gmail_api_credentials_path)
        token_client_id = _read_token_client_id(token_path)
        if token_client_id != validated.client_id:
            result.update(
                client_match=False,
                error="现有 Token 不属于当前 OAuth Client",
                error_code="token_client_mismatch",
            )
            return result
        creds = Credentials.from_authorized_user_file(
            str(token_path), list(cfg.gmail_api_scopes)
        )
    except OAuthImportError as exc:
        result.update(error=str(exc), error_code=exc.error_code)
        return result
    except Exception:
        result.update(error="token.json 解析失败", error_code="token_exchange_failed")
        return result
    token_scopes = set(getattr(creds, "scopes", None) or [])
    if token_scopes and token_scopes != set(cfg.gmail_api_scopes):
        result.update(
            scopes_match=False,
            error="Token scope 与 gmail.readonly 不一致",
            error_code="insufficient_scope",
        )
        return result
    refresh = bool(getattr(creds, "refresh_token", None))
    result["refreshable"] = refresh
    if creds.valid and refresh:
        result["valid"] = True
    elif creds.valid:
        result.update(
            error="Token 缺少 refresh token",
            error_code="refresh_token_missing",
        )
    elif creds.expired and refresh:
        result["expired"] = True
    else:
        result.update(error="Token 无效", error_code="token_exchange_failed")
    return result


def get_oauth_state(cfg: AppConfig) -> dict[str, Any]:
    """供 GUI 使用的非敏感 OAuth 状态。"""

    from agent_mail_bridge.config import ConfigError, require_readonly_gmail_scope

    credentials_path = Path(cfg.gmail_api_credentials_path)
    if not credentials_path.exists():
        return {
            "state": "CREDENTIALS_MISSING",
            "message": "缺少 Desktop app credentials.json",
            "error_code": "credentials_unreadable",
        }
    try:
        validated = validate_oauth_credentials_file(credentials_path)
    except OAuthImportError as exc:
        return {
            "state": "CREDENTIALS_INVALID",
            "message": str(exc),
            "error_code": exc.error_code,
        }
    try:
        require_readonly_gmail_scope(cfg)
    except ConfigError as exc:
        return {
            "state": "SCOPE_MISMATCH",
            "message": str(exc),
            "error_code": "insufficient_scope",
        }
    status = describe_token_status(cfg)
    base = {
        "client_type": validated.summary.client_type,
        "client_id_suffix": validated.summary.client_id_suffix,
        "project_id": validated.summary.project_id,
    }
    if not status["exists"]:
        return {
            **base,
            "state": "AUTH_REQUIRED",
            "message": "尚未完成 Gmail API 授权",
            "error_code": "refresh_token_missing",
        }
    if not status["client_match"]:
        return {
            **base,
            "state": "CLIENT_MISMATCH",
            "message": status["error"],
            "error_code": "token_client_mismatch",
        }
    if not status["scopes_match"]:
        return {
            **base,
            "state": "SCOPE_MISMATCH",
            "message": status["error"],
            "error_code": "insufficient_scope",
        }
    if status["valid"]:
        return {**base, "state": "READY", "message": "Gmail API 已授权"}
    if status["expired"] and status["refreshable"]:
        return {
            **base,
            "state": "TOKEN_EXPIRED_REFRESHABLE",
            "message": "Token 已过期，可在后台自动刷新",
        }
    return {
        **base,
        "state": "TOKEN_INVALID",
        "message": status["error"] or "Token 无效",
        "error_code": status["error_code"],
    }


def validate_credentials_file(cfg: AppConfig) -> dict[str, Any]:
    """检查 Desktop credentials，返回安全摘要而非秘密。"""

    path = Path(cfg.gmail_api_credentials_path)
    result: dict[str, Any] = {
        "exists": path.exists(),
        "valid": False,
        "error": None,
        "error_code": None,
    }
    try:
        validated = validate_oauth_credentials_file(path)
    except OAuthImportError as exc:
        result.update(error=str(exc), error_code=exc.error_code)
        return result
    result.update(
        valid=True,
        client_type=validated.summary.client_type,
        project_id=validated.summary.project_id,
        client_id_suffix=validated.summary.client_id_suffix,
    )
    return result


def _build_gmail_service(cfg: AppConfig, creds: Any) -> Any:
    if httplib2 is None or AuthorizedHttp is None or build is None:
        raise GmailApiAuthError(
            "Gmail API 依赖未安装",
            error_code="internal_error",
        )
    http = httplib2.Http(timeout=cfg.gmail_connect_timeout)
    authorized_http = AuthorizedHttp(creds, http=http)
    return build("gmail", "v1", http=authorized_http, cache_discovery=False)


def _load_token_credentials(
    cfg: AppConfig, validated: ValidatedOAuthCredentials
) -> Any | None:
    token_path = Path(cfg.gmail_api_token_path)
    if not token_path.exists():
        return None
    try:
        token_client_id = _read_token_client_id(token_path)
        if token_client_id != validated.client_id:
            raise TokenClientMismatchError(
                "现有 Token 不属于当前 OAuth Client",
                error_code="token_client_mismatch",
            )
        return Credentials.from_authorized_user_file(
            str(token_path), list(cfg.gmail_api_scopes)
        )
    except TokenClientMismatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GmailApiAuthError(
            "token.json 解析失败",
            error_code="token_exchange_failed",
            technical_detail=_safe_exception_detail(exc),
        ) from exc


def _save_token(creds: Any, token_path: Path) -> None:
    try:
        serialized = creds.to_json()
        if not isinstance(serialized, str) or not serialized:
            raise ValueError("empty token serialization")
        atomic_write_private_text(
            token_path,
            serialized,
            error_code="token_save_failed",
            error_message="OAuth Token 保存失败，原有 Token 已保留",
        )
    except (OAuthImportError, OSError, TypeError, ValueError) as exc:
        raise GmailApiAuthError(
            str(exc),
            error_code="token_save_failed",
            retryable=True,
            technical_detail=_safe_exception_detail(exc),
        ) from exc


def _ensure_scopes_match(creds: Any, scopes: list[str]) -> None:
    token_scopes = set(getattr(creds, "scopes", None) or [])
    if token_scopes and token_scopes != set(scopes):
        raise TokenScopeMismatchError(
            "Gmail API Token 权限与当前 gmail.readonly 配置不一致",
            error_code="insufficient_scope",
        )


def _read_token_client_id(path: Path) -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return ""
    value = payload.get("client_id")
    return value.strip() if isinstance(value, str) else ""


def _authorization_response(redirect_uri: str, code: str, state: str) -> str:
    secure_uri = redirect_uri.replace("http://", "https://", 1)
    return f"{secure_uri}?{urlencode({'code': code, 'state': state})}"


def _oauth_process_lock_path(cfg: AppConfig) -> Path:
    return Path(cfg.gmail_api_token_path).parent / ".oauth.lock"


def _claim_active_session(session_id: str) -> bool:
    global _active_session_id
    with _active_session_guard:
        if _active_session_id is not None and _active_session_id != session_id:
            return False
        _active_session_id = session_id
        return True


def _release_active_session(session_id: str) -> None:
    global _active_session_id
    with _active_session_guard:
        if _active_session_id == session_id:
            _active_session_id = None


def _update_oauth_diagnostics(**changes: Any) -> None:
    allowed = {
        "active",
        "stage",
        "elapsed_ms",
        "callback_host",
        "callback_port",
        "callback_bound",
        "callback_received",
        "port_released",
        "browser_opened",
        "error_code",
    }
    safe_changes = {key: value for key, value in changes.items() if key in allowed}
    with _diagnostics_guard:
        _last_oauth_diagnostics.update(safe_changes)


def _error_result(
    error_code: str,
    stage: OAuthState,
    *,
    terminal_state: OAuthState,
    status: OperationStatus = OperationStatus.FAILED,
    technical_detail: str = "",
    extra_details: dict[str, Any] | None = None,
) -> ServiceResult:
    title, reason, next_step, retryable = _ERROR_COPY.get(
        error_code, _ERROR_COPY["internal_error"]
    )
    info = OAuthErrorInfo(
        error_code=error_code,
        title=title,
        reason=reason,
        next_step=next_step,
        retryable=retryable,
        stage=stage.value,
        technical_detail=technical_detail,
    )
    details = {**asdict(info), "oauth_state": terminal_state.value}
    if extra_details:
        details.update(extra_details)
    return ServiceResult(
        status,
        error_code=error_code,
        message=f"{title}：{reason} {next_step}".strip(),
        needs_auth=error_code
        in {
            "access_denied",
            "redirect_uri_mismatch",
            "invalid_client",
            "deleted_client",
            "refresh_token_missing",
            "token_client_mismatch",
            "refresh_invalid_grant",
            "refresh_revoked",
            "insufficient_scope",
            "account_mismatch",
        },
        details=details,
    )


def _classify_token_exception(exc: Exception) -> str:
    if ProxyError and isinstance(exc, ProxyError):
        return "proxy_error"
    if RequestsTimeout and isinstance(exc, RequestsTimeout):
        return "network_error"
    if RequestsConnectionError and isinstance(exc, RequestsConnectionError):
        return "network_error"
    if TransportError and isinstance(exc, TransportError):
        return "network_error"
    error_name, description = _structured_oauth_error(exc)
    combined = f"{error_name} {description}"
    if "redirect_uri_mismatch" in combined:
        return "redirect_uri_mismatch"
    if "deleted_client" in combined:
        return "deleted_client"
    if "invalid_client" in combined or "unauthorized_client" in combined:
        return "invalid_client"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network_error"
    return "token_exchange_failed"


def _classify_refresh_exception(exc: Exception) -> str:
    code = _classify_token_exception(exc)
    if code in {"network_error", "proxy_error", "invalid_client", "deleted_client"}:
        return code
    error_name, description = _structured_oauth_error(exc)
    if error_name in {"revoked_token", "token_revoked"}:
        return "refresh_revoked"
    if error_name == "invalid_grant":
        if "revok" in description:
            return "refresh_revoked"
        return "refresh_invalid_grant"
    return "token_exchange_failed"


def _classify_profile_exception(exc: Exception) -> str:
    if ProxyError and isinstance(exc, ProxyError):
        return "proxy_error"
    if SocksProxyError and isinstance(exc, SocksProxyError):
        return "proxy_error"
    if HttpLib2ProxiesUnavailableError and isinstance(
        exc, HttpLib2ProxiesUnavailableError
    ):
        return "proxy_error"
    if RequestsTimeout and isinstance(exc, RequestsTimeout):
        return "network_error"
    if RequestsConnectionError and isinstance(exc, RequestsConnectionError):
        return "network_error"
    if TransportError and isinstance(exc, TransportError):
        return "network_error"
    if HttpLib2ServerNotFoundError and isinstance(exc, HttpLib2ServerNotFoundError):
        return "network_error"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network_error"
    if isinstance(exc, OSError):
        return "network_error"
    status = _http_status(exc)
    reasons = _http_reasons(exc)
    if status == 403 and reasons.intersection(
        {"accessnotconfigured", "servicedisabled", "api_disabled"}
    ):
        return "gmail_api_disabled"
    if status == 403 and reasons.intersection(
        {"insufficientpermissions", "insufficient_scope"}
    ):
        return "insufficient_scope"
    if status in {401}:
        return "token_exchange_failed"
    if status == 429 or (status is not None and status >= 500):
        return "profile_check_failed"
    if status == 403:
        return "profile_check_failed"
    return "profile_check_failed"


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_reasons(exc: Exception) -> set[str]:
    reasons: set[str] = set()
    for item in getattr(exc, "error_details", None) or []:
        if isinstance(item, dict):
            for key in ("reason", "error", "status"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    reasons.add(value.replace("_", "").lower())
    return reasons


_SENSITIVE_DETAIL = re.compile(
    r"(?i)(access_token|refresh_token|client_secret|authorization|state|code)=?[^\s&]*"
)
_URL_DETAIL = re.compile(r"https?://\S+", re.IGNORECASE)


def _safe_exception_detail(exc: Exception) -> str:
    parts = [type(exc).__name__]
    status = _http_status(exc)
    if status is not None:
        parts.append(f"http_status={status}")
    reasons = sorted(_http_reasons(exc))
    if reasons:
        parts.append("reason=" + ",".join(reasons[:3]))
    error_name, _description = _structured_oauth_error(exc)
    if error_name:
        parts.append("oauth_error=" + error_name[:80])
    detail = "; ".join(parts)
    detail = _URL_DETAIL.sub("[URL已隐藏]", detail)
    detail = _SENSITIVE_DETAIL.sub("[敏感值已隐藏]", detail)
    return detail[:300]


def _structured_oauth_error(exc: Exception) -> tuple[str, str]:
    error = getattr(exc, "error", None)
    description = getattr(exc, "description", None)
    if not isinstance(error, str):
        error = ""
    if not isinstance(description, str):
        description = ""
    for item in getattr(exc, "args", ()):
        if not isinstance(item, dict):
            continue
        if not error and isinstance(item.get("error"), str):
            error = item["error"]
        if not description and isinstance(item.get("error_description"), str):
            description = item["error_description"]
    return error.lower(), description.lower()


def _mask_email(address: str) -> str:
    local, separator, domain = address.partition("@")
    if not separator:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _require_google_dependencies() -> None:
    if Credentials is None or InstalledAppFlow is None:
        raise GmailApiAuthError(
            "Gmail API 依赖未安装。请运行 pip install -r requirements.txt",
            error_code="internal_error",
        )


class _BoundedGoogleRequest:
    """把 google-auth refresh 的默认 120 秒收敛到产品网络超时。"""

    def __init__(self, timeout_seconds: int | float):
        if Request is None:
            raise GmailApiAuthError(
                "Gmail API 依赖未安装",
                error_code="internal_error",
            )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._request = Request()

    def __call__(
        self,
        url,
        method="GET",
        body=None,
        headers=None,
        timeout=None,
        **kwargs,
    ):
        del timeout
        return self._request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=self.timeout_seconds,
            **kwargs,
        )
