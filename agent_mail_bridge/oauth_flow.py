"""可测试、可取消的 OAuth 状态机与 IPv4 本地回环回调服务。"""

from __future__ import annotations

import html
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterator
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


class OAuthState(StrEnum):
    IDLE = "IDLE"
    VALIDATING_CREDENTIALS = "VALIDATING_CREDENTIALS"
    PREPARING_CALLBACK = "PREPARING_CALLBACK"
    CALLBACK_READY = "CALLBACK_READY"
    OPENING_BROWSER = "OPENING_BROWSER"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    CALLBACK_RECEIVED = "CALLBACK_RECEIVED"
    EXCHANGING_TOKEN = "EXCHANGING_TOKEN"
    VERIFYING_GMAIL = "VERIFYING_GMAIL"
    AUTHORIZED = "AUTHORIZED"
    AUTHORIZED_UNVERIFIED = "AUTHORIZED_UNVERIFIED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


OAUTH_STATE_MESSAGES = {
    OAuthState.IDLE: "准备就绪",
    OAuthState.VALIDATING_CREDENTIALS: "正在检查桌面应用凭据",
    OAuthState.PREPARING_CALLBACK: "正在启动本地安全回调",
    OAuthState.CALLBACK_READY: "本地安全回调已就绪",
    OAuthState.OPENING_BROWSER: "正在打开系统浏览器",
    OAuthState.WAITING_FOR_USER: "已打开浏览器，等待 Google 授权",
    OAuthState.CALLBACK_RECEIVED: "已收到 Google 回调",
    OAuthState.EXCHANGING_TOKEN: "正在交换授权 Token",
    OAuthState.VERIFYING_GMAIL: "正在验证 Gmail 账号",
    OAuthState.AUTHORIZED: "Gmail 授权成功",
    OAuthState.AUTHORIZED_UNVERIFIED: "已取得授权，但 Gmail API 验证暂未通过",
    OAuthState.CANCELLING: "正在取消授权",
    OAuthState.CANCELLED: "授权已取消",
    OAuthState.TIMED_OUT: "授权等待超时",
    OAuthState.FAILED: "Gmail OAuth 授权失败",
}

_ALLOWED_TRANSITIONS: dict[OAuthState, set[OAuthState]] = {
    OAuthState.IDLE: {OAuthState.VALIDATING_CREDENTIALS},
    OAuthState.VALIDATING_CREDENTIALS: {
        OAuthState.PREPARING_CALLBACK,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.PREPARING_CALLBACK: {
        OAuthState.CALLBACK_READY,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.CALLBACK_READY: {
        OAuthState.OPENING_BROWSER,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.OPENING_BROWSER: {
        OAuthState.WAITING_FOR_USER,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.WAITING_FOR_USER: {
        OAuthState.CALLBACK_RECEIVED,
        OAuthState.CANCELLING,
        OAuthState.TIMED_OUT,
        OAuthState.FAILED,
    },
    OAuthState.CALLBACK_RECEIVED: {
        OAuthState.EXCHANGING_TOKEN,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.EXCHANGING_TOKEN: {
        OAuthState.VERIFYING_GMAIL,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.VERIFYING_GMAIL: {
        OAuthState.AUTHORIZED,
        OAuthState.AUTHORIZED_UNVERIFIED,
        OAuthState.CANCELLING,
        OAuthState.FAILED,
    },
    OAuthState.CANCELLING: {OAuthState.CANCELLED},
    OAuthState.AUTHORIZED: {OAuthState.IDLE},
    OAuthState.AUTHORIZED_UNVERIFIED: {OAuthState.IDLE},
    OAuthState.CANCELLED: {OAuthState.IDLE},
    OAuthState.TIMED_OUT: {OAuthState.IDLE},
    OAuthState.FAILED: {OAuthState.IDLE},
}


class OAuthStateTransitionError(RuntimeError):
    """状态机发生非法跳转。"""


class OAuthStateMachine:
    """线程安全、拒绝非法跳转的单会话状态机。"""

    def __init__(self):
        self._state = OAuthState.IDLE
        self._lock = threading.RLock()

    @property
    def state(self) -> OAuthState:
        with self._lock:
            return self._state

    def transition(self, target: OAuthState) -> OAuthState:
        with self._lock:
            if target == self._state:
                return self._state
            if target not in _ALLOWED_TRANSITIONS.get(self._state, set()):
                raise OAuthStateTransitionError(
                    f"非法 OAuth 状态跳转：{self._state.value} -> {target.value}"
                )
            self._state = target
            return target


class OAuthWaitCancelled(RuntimeError):
    """用户取消等待回调。"""


class OAuthWaitTimeout(TimeoutError):
    """等待回调超时。"""


@dataclass(frozen=True)
class OAuthCallback:
    """仅驻留内存的回调参数；repr 隐藏敏感值。"""

    code: str | None = field(default=None, repr=False)
    state: str | None = field(default=None, repr=False)
    error: str | None = field(default=None, repr=False)
    error_description: str | None = field(default=None, repr=False)
    invalid_parameters: bool = False


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, _format, *_args) -> None:
        return


class _BoundedWSGIServer(WSGIServer):
    allow_reuse_address = False
    request_timeout_seconds = 0.5

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address


class _CallbackApplication:
    def __init__(self):
        self.callback: OAuthCallback | None = None
        self.received = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):
        path = str(environ.get("PATH_INFO") or "")
        query = str(environ.get("QUERY_STRING") or "")
        if path != "/":
            return self._respond(start_response, "404 Not Found", "未找到回调页面")
        try:
            values = parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=20,
            )
        except ValueError:
            values = {}
        relevant = {"code", "state", "error", "error_description"}
        if not relevant.intersection(values):
            return self._respond(
                start_response,
                "400 Bad Request",
                "尚未收到有效的 Google OAuth 回调，请返回 AgentMailBridge。",
            )
        with self._lock:
            if self.callback is None:
                invalid = any(len(values.get(key, [])) > 1 for key in relevant)
                self.callback = OAuthCallback(
                    code=_single(values, "code"),
                    state=_single(values, "state"),
                    error=_single(values, "error"),
                    error_description=_single(values, "error_description"),
                    invalid_parameters=invalid,
                )
                self.received.set()
        if self.callback and self.callback.error:
            message = "Google 未完成授权。请返回 AgentMailBridge 查看原因并重试。"
        else:
            message = "AgentMailBridge 已收到授权结果。你可以关闭此页面并返回应用。"
        return self._respond(start_response, "200 OK", message)

    @staticmethod
    def _respond(start_response, status: str, message: str):
        body = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>AgentMailBridge OAuth</title></head>"
            "<body><main><h1>AgentMailBridge</h1><p>"
            + html.escape(message)
            + "</p></main></body></html>"
        ).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Pragma", "no-cache"),
                ("X-Content-Type-Options", "nosniff"),
                ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
                ("Referrer-Policy", "no-referrer"),
            ],
        )
        return [body]


class LoopbackCallbackServer:
    """只绑定 127.0.0.1，可取消、可超时且始终可关闭。"""

    host = "127.0.0.1"

    def __init__(self):
        self._application = _CallbackApplication()
        self._server: WSGIServer | None = None
        self._close_lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("OAuth 回环服务尚未启动")
        return int(self._server.server_port)

    def start(self) -> int:
        if self._server is not None:
            return self.port
        self._server = make_server(
            self.host,
            0,
            self._application,
            server_class=_BoundedWSGIServer,
            handler_class=_QuietRequestHandler,
        )
        return self.port

    def wait(
        self,
        cancel_event: threading.Event,
        *,
        timeout_seconds: float,
        clock=time.monotonic,
    ) -> OAuthCallback:
        server = self._server
        if server is None:
            raise RuntimeError("OAuth 回环服务尚未启动")
        deadline = clock() + max(0.01, float(timeout_seconds))
        while not self._application.received.is_set():
            if cancel_event.is_set():
                raise OAuthWaitCancelled("OAuth 授权已取消")
            remaining = deadline - clock()
            if remaining <= 0:
                raise OAuthWaitTimeout("等待 OAuth 回调超时")
            server.timeout = min(0.20, max(0.01, remaining))
            server.handle_request()
        callback = self._application.callback
        if callback is None:
            raise RuntimeError("OAuth 回调状态异常")
        return callback

    def wake(self) -> None:
        """唤醒 handle_request，使取消无需等待完整轮询周期。"""
        if self._server is None:
            return
        try:
            with socket.create_connection((self.host, self.port), timeout=0.2) as stream:
                stream.sendall(b"GET /__cancel__ HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        except OSError:
            pass

    def close(self) -> None:
        with self._close_lock:
            server, self._server = self._server, None
            if server is not None:
                server.server_close()

    def __enter__(self) -> "LoopbackCallbackServer":
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.close()


_NO_PROXY_LOCK = threading.RLock()
_LOOPBACK_BYPASS = ("127.0.0.1", "localhost", "::1")


@contextmanager
def loopback_no_proxy() -> Iterator[None]:
    """合并本地回环代理绕过项，并在结束后恢复原环境。"""

    with _NO_PROXY_LOCK:
        keys = ("NO_PROXY",) if os.name == "nt" else ("NO_PROXY", "no_proxy")
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                current = [item.strip() for item in (os.environ.get(key) or "").split(",")]
                merged = [item for item in current if item]
                for item in _LOOPBACK_BYPASS:
                    if item not in merged:
                        merged.append(item)
                os.environ[key] = ",".join(merged)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _single(values: dict[str, list[str]], key: str) -> str | None:
    items = values.get(key) or []
    if len(items) != 1:
        return None
    return items[0]
