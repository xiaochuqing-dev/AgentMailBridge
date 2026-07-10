"""网络连接适配层。

职责：
1. 创建 direct IMAP SSL 连接。
2. 创建 SOCKS5 IMAP SSL 连接（支持 remote DNS）。
3. 测试本地 SOCKS5 端口是否可达。
4. 测试 Gmail IMAP TCP/TLS 是否可达。
5. 为诊断命令提供分步骤结果。
6. 不处理邮件业务逻辑，只处理网络连接逻辑。

设计原则：
- 不把代理逻辑散落到 mail_receive.py。
- 所有异常包装为项目自己的异常类型，方便 CLI 打印用户可读错误。
- 日志中绝不打印 Gmail 应用专用密码。
"""

from __future__ import annotations

import imaplib
import socket
import ssl
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.logging_setup import get_logger

logger = get_logger("network")


# ============================================================
# 错误分类码
# ============================================================

CONFIG_ERROR = "CONFIG_ERROR"
PROXY_PORT_UNAVAILABLE = "PROXY_PORT_UNAVAILABLE"
DIRECT_CONNECT_FAILED = "DIRECT_CONNECT_FAILED"
SOCKS5_CONNECT_FAILED = "SOCKS5_CONNECT_FAILED"
TLS_HANDSHAKE_FAILED = "TLS_HANDSHAKE_FAILED"
GMAIL_AUTH_FAILED = "GMAIL_AUTH_FAILED"
GMAIL_IMAP_DISABLED_OR_REJECTED = "GMAIL_IMAP_DISABLED_OR_REJECTED"
TIMEOUT = "TIMEOUT"
UNKNOWN_NETWORK_ERROR = "UNKNOWN_NETWORK_ERROR"


# ============================================================
# 异常类型
# ============================================================

class NetworkConfigError(Exception):
    """网络配置缺失或不合法。"""

    code: str = CONFIG_ERROR


class NetworkConnectError(Exception):
    """网络连接失败（direct / socks5 / TLS）。"""

    code: str = UNKNOWN_NETWORK_ERROR


class ProxyConnectError(NetworkConnectError):
    """SOCKS5 代理端口不可达或代理连接失败。"""

    code: str = PROXY_PORT_UNAVAILABLE


class GmailTlsError(NetworkConnectError):
    """Gmail IMAP TLS 握手失败。"""

    code: str = TLS_HANDSHAKE_FAILED


class GmailAuthError(Exception):
    """Gmail IMAP 登录认证失败。"""

    code: str = GMAIL_AUTH_FAILED


# ============================================================
# SOCKS5 IMAP SSL 连接类
# ============================================================

class SocksIMAP4SSL(imaplib.IMAP4_SSL):
    """通过本地 SOCKS5 代理连接 IMAP SSL 服务器。

    rdns=True 时，域名交给 SOCKS5 代理端解析，避免本机 DNS 污染/泄露。
    TLS 包裹时必须设置 server_hostname，保证 SNI 与证书校验正常。
    """

    def __init__(
        self,
        host: str,
        port: int = 993,
        *,
        proxy_host: str,
        proxy_port: int,
        timeout: int = 20,
        rdns: bool = True,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._proxy_timeout = timeout
        self._proxy_rdns = rdns
        self.ssl_context = ssl_context or ssl.create_default_context()
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            ssl_context=self.ssl_context,
        )

    def _create_socket(self, timeout):
        import socks  # 延迟导入，未配置 socks5 时不强制依赖

        # 注意：socks.create_connection 的远端 DNS 参数名为 proxy_rdns，
        # 而非 rdns（不同 PySocks 版本签名差异）。这里用 proxy_rdns 兼容 1.7.1+。
        sock = socks.create_connection(
            (self.host, self.port),
            timeout=timeout,
            proxy_type=socks.SOCKS5,
            proxy_addr=self._proxy_host,
            proxy_port=self._proxy_port,
            proxy_rdns=self._proxy_rdns,
        )
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


# ============================================================
# 连接工厂
# ============================================================

def _classify_connect_exception(exc: Exception) -> NetworkConnectError:
    """把底层连接异常映射为项目异常类型。"""
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, ssl.SSLError) or "ssl" in low or "certificate" in low or "tls" in low:
        return GmailTlsError(f"TLS 握手失败：{exc}")
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in low or "timeout" in low:
        err = NetworkConnectError(f"连接超时：{exc}")
        err.code = TIMEOUT
        return err
    return NetworkConnectError(f"连接失败：{exc}")


def create_direct_imap_client(cfg: AppConfig) -> imaplib.IMAP4_SSL:
    """创建 direct 模式的 IMAP SSL 连接（仅 TCP/TLS，不登录）。"""
    ctx = ssl.create_default_context()
    try:
        client = imaplib.IMAP4_SSL(
            host=cfg.gmail_imap_host,
            port=cfg.gmail_imap_port,
            ssl_context=ctx,
            timeout=cfg.gmail_connect_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        mapped = _classify_connect_exception(exc)
        if mapped.code == UNKNOWN_NETWORK_ERROR:
            mapped.code = DIRECT_CONNECT_FAILED
        logger.warning("direct 连接 %s:%s 失败：%s",
                       cfg.gmail_imap_host, cfg.gmail_imap_port, exc)
        raise mapped from exc
    logger.info("direct IMAP 连接成功：%s:%s",
                cfg.gmail_imap_host, cfg.gmail_imap_port)
    return client


def create_socks5_imap_client(cfg: AppConfig) -> "SocksIMAP4SSL":
    """创建 socks5 模式的 IMAP SSL 连接（仅 TCP/TLS，不登录）。

    先测试本地 SOCKS5 端口是否可达，不可达直接抛 ProxyConnectError，
    避免在 socks.create_connection 内部长时间等待。
    """
    if not cfg.gmail_socks5_host or not cfg.gmail_socks5_port:
        raise NetworkConfigError(
            "socks5 模式缺少代理配置：GMAIL_SOCKS5_HOST / GMAIL_SOCKS5_PORT。"
        )

    port_ok = probe_socks5_port(
        cfg.gmail_socks5_host, cfg.gmail_socks5_port, cfg.gmail_connect_timeout
    )
    if not port_ok["ok"]:
        raise ProxyConnectError(
            f"本地 SOCKS5 端口 {cfg.gmail_socks5_host}:{cfg.gmail_socks5_port} 不可达："
            f"{port_ok['error']}"
        )

    ctx = ssl.create_default_context()
    try:
        client = SocksIMAP4SSL(
            host=cfg.gmail_imap_host,
            port=cfg.gmail_imap_port,
            proxy_host=cfg.gmail_socks5_host,
            proxy_port=cfg.gmail_socks5_port,
            timeout=cfg.gmail_connect_timeout,
            rdns=cfg.gmail_socks5_remote_dns,
            ssl_context=ctx,
        )
    except ProxyConnectError:
        raise
    except Exception as exc:  # noqa: BLE001
        mapped = _classify_connect_exception(exc)
        if mapped.code == UNKNOWN_NETWORK_ERROR:
            mapped.code = SOCKS5_CONNECT_FAILED
        logger.warning("socks5 连接 %s:%s 经 %s:%s 失败：%s",
                       cfg.gmail_imap_host, cfg.gmail_imap_port,
                       cfg.gmail_socks5_host, cfg.gmail_socks5_port, exc)
        raise mapped from exc
    logger.info("socks5 IMAP 连接成功：%s:%s（经 %s:%s，rdns=%s）",
                cfg.gmail_imap_host, cfg.gmail_imap_port,
                cfg.gmail_socks5_host, cfg.gmail_socks5_port,
                cfg.gmail_socks5_remote_dns)
    return client


def create_gmail_imap_client(cfg: AppConfig) -> imaplib.IMAP4_SSL:
    """根据配置的网络模式创建 Gmail IMAP 连接（仅 TCP/TLS，不登录）。

    - direct：直接返回 direct 连接。
    - socks5：直接返回 socks5 连接。
    - auto：先尝试 direct；失败后若配置了 SOCKS5 再尝试 socks5；
            两者都失败抛 NetworkConnectError，message 含两段原因，
            并附 direct_error / socks5_error 属性。
    """
    mode = (cfg.gmail_network_mode or "auto").lower()

    if mode == "direct":
        return create_direct_imap_client(cfg)

    if mode == "socks5":
        return create_socks5_imap_client(cfg)

    if mode == "auto":
        direct_error: Exception | None = None
        try:
            return create_direct_imap_client(cfg)
        except Exception as exc:  # noqa: BLE001
            direct_error = exc
            logger.info("auto 模式 direct 失败，尝试 socks5：%s", exc)

        # 若配置了 socks5 代理则继续尝试
        if cfg.gmail_socks5_host and cfg.gmail_socks5_port:
            try:
                return create_socks5_imap_client(cfg)
            except Exception as exc:  # noqa: BLE001
                socks5_error = exc
                combined = (
                    "auto 模式两种连接均失败：\n"
                    f"  [direct 失败] {direct_error}\n"
                    f"  [socks5 失败] {socks5_error}"
                )
                err = NetworkConnectError(combined)
                err.direct_error = direct_error  # type: ignore[attr-defined]
                err.socks5_error = socks5_error  # type: ignore[attr-defined]
                raise err from socks5_error
        # 没有配置 socks5，仅 direct 失败
        raise NetworkConnectError(
            f"auto 模式 direct 连接失败，且未配置 SOCKS5 代理：{direct_error}"
        ) from direct_error

    # 不应到达此处（load_config 已校验），防御性处理
    raise NetworkConfigError(f"未知的 GMAIL_NETWORK_MODE：{mode}")


def login_imap_client(
    client: imaplib.IMAP4_SSL, address: str, app_password: str
) -> None:
    """登录 IMAP 客户端，认证失败包装为 GmailAuthError。"""
    try:
        client.login(address, app_password)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        low = msg.lower()
        if "auth" in low or "credential" in low or "password" in low:
            raise GmailAuthError(
                "Gmail IMAP 登录失败（认证错误）：请确认使用 16 位应用专用密码，"
                "而非 Google 登录密码，并检查账号已开启 IMAP。"
            ) from exc
        # 非认证类错误（如 IMAP 被禁用、连接被重置）
        raise GmailAuthError(f"Gmail IMAP 登录被拒绝：{exc}") from exc


# ============================================================
# 诊断原语（返回结构化结果，不抛异常给诊断层）
# ============================================================

def is_pytsocks_installed() -> bool:
    """是否已安装 PySocks。"""
    try:
        import socks  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def probe_socks5_port(
    host: str, port: int, timeout: int = 5
) -> dict[str, Any]:
    """测试本地 SOCKS5 端口是否可达（仅 TCP 连通）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "host": host, "port": port}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def probe_direct_connect(
    host: str, port: int, timeout: int = 20
) -> dict[str, Any]:
    """测试 direct TCP 连接到 Gmail IMAP（不含 TLS）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "host": host, "port": port}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def probe_direct_tls_connect(
    host: str, port: int, timeout: int = 20
) -> dict[str, Any]:
    """测试 direct TCP + TLS 握手到 Gmail IMAP。"""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as _ssock:
                return {"ok": True, "host": host, "port": port}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def probe_socks5_connect(
    host: str,
    port: int,
    proxy_host: str,
    proxy_port: int,
    rdns: bool = True,
    timeout: int = 20,
) -> dict[str, Any]:
    """测试通过 SOCKS5 代理连接 Gmail IMAP（含 TLS）。"""
    try:
        import socks
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"未安装 PySocks：{exc}"}
    try:
        sock = socks.create_connection(
            (host, port),
            timeout=timeout,
            proxy_type=socks.SOCKS5,
            proxy_addr=proxy_host,
            proxy_port=proxy_port,
            proxy_rdns=rdns,
        )
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        ssock.close()
        return {"ok": True, "host": host, "port": port,
                "proxy": f"{proxy_host}:{proxy_port}", "rdns": rdns}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": host, "port": port,
                "proxy": f"{proxy_host}:{proxy_port}", "rdns": rdns,
                "error": str(exc)}


def probe_qq_smtp_direct(cfg: AppConfig) -> dict[str, Any]:
    """测试 QQ SMTP direct TCP + TLS 连接（不登录，不发送）。"""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(
            (cfg.qq_smtp_host, cfg.qq_smtp_port),
            timeout=cfg.qq_smtp_connect_timeout,
        ) as sock:
            with ctx.wrap_socket(sock, server_hostname=cfg.qq_smtp_host) as _ssock:
                return {"ok": True, "host": cfg.qq_smtp_host,
                        "port": cfg.qq_smtp_port}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": cfg.qq_smtp_host,
                "port": cfg.qq_smtp_port, "error": str(exc)}


def login_and_logout(client: imaplib.IMAP4_SSL, address: str, password: str) -> dict[str, Any]:
    """登录测试：只 login 立即 logout，不读取/搜索/修改邮件。"""
    try:
        client.login(address, password)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    try:
        client.logout()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}
