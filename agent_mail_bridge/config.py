"""配置加载模块。

职责：
1. 源码模式读取项目 `.env`，安装版读取当前用户配置目录 `.env`。
2. 把环境变量解析为强类型的 AppConfig 对象。
3. 提供安全校验：缺少关键配置时给出明确错误提示。
4. 不允许真实密钥进入代码库；日志中不打印完整密码 / 授权码。

后续 GUI / MCP 只需调用 load_config() 即可拿到配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agent_mail_bridge.receive_rules import (
    ALL_SCANNED,
    SELF_ONLY,
    VALID_MODES,
    normalize_sender_rules,
    normalize_subject_keywords,
)

from agent_mail_bridge.runtime_paths import get_runtime_paths

try:
    # python-dotenv 为可选依赖，缺失时回退到纯环境变量
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - 仅在未安装时触发
    load_dotenv = None  # type: ignore[assignment]


# 兼容旧调用；新代码应通过 RuntimePaths 明确路径语义。
PROJECT_ROOT = get_runtime_paths().source_root


def _as_bool(value: str | None, default: bool = False) -> bool:
    """把字符串解析为布尔值。"""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: str | None, default: int) -> int:
    """把字符串解析为整数，失败时返回默认值。"""
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


# 网络模式允许值
_VALID_NETWORK_MODES = {"direct", "socks5", "auto"}

# Gmail 收件后端允许值
_VALID_RECEIVE_BACKENDS = {"imap", "gmail_api", "auto"}

# Gmail API 默认 scope（只读）
_DEFAULT_GMAIL_API_SCOPES = "https://www.googleapis.com/auth/gmail.readonly"


def _parse_network_mode(value: str | None, field: str, default: str) -> str:
    """解析网络模式，非法值直接报错（不静默回退）。"""
    raw = (value if value is not None else default).strip().lower()
    if raw not in _VALID_NETWORK_MODES:
        raise ConfigError(
            f"{field} 取值非法：{value!r}，允许值：{sorted(_VALID_NETWORK_MODES)}。"
        )
    return raw


def _parse_port_strict(value: str | None, field: str) -> int:
    """严格解析端口，非整数或越界直接报错。"""
    raw = (value or "").strip()
    if not raw:
        return 0
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError(f"{field} 必须是整数，当前值：{value!r}。") from None
    if port <= 0 or port > 65535:
        raise ConfigError(f"{field} 必须在 1-65535 范围内，当前值：{port}。")
    return port


def _parse_receive_backend(value: str | None, default: str) -> str:
    """解析 Gmail 收件后端，非法值直接报错（不静默回退）。"""
    raw = (value if value is not None else default).strip().lower()
    if raw not in _VALID_RECEIVE_BACKENDS:
        raise ConfigError(
            f"GMAIL_RECEIVE_BACKEND 取值非法：{value!r}，"
            f"允许值：{sorted(_VALID_RECEIVE_BACKENDS)}。"
        )
    return raw


def _as_positive_int(value: str | None, field: str, default: int) -> int:
    """解析正整数，非正数或非整数直接报错。"""
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        raise ConfigError(f"{field} 必须是正整数，当前值：{value!r}。") from None
    if n <= 0:
        raise ConfigError(f"{field} 必须是正整数，当前值：{n}。")
    return n


def _resolve_path(raw: str, default: str, *, base_dir: Path) -> Path:
    """把配置中的路径解析为绝对路径；相对路径基于配置文件目录。"""
    text = (raw or default).strip()
    p = Path(text)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p


@dataclass
class AppConfig:
    """应用配置。所有字段均可由 .env 覆盖。"""

    # --- Gmail 收件 ---
    gmail_address: str = ""
    gmail_app_password: str = ""
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    # 网络适配层：direct / socks5 / auto
    gmail_network_mode: str = "auto"
    gmail_connect_timeout: int = 20
    gmail_socks5_host: str = "127.0.0.1"
    gmail_socks5_port: int = 10808
    gmail_socks5_remote_dns: bool = True

    # --- Gmail 收件后端切换 ---
    # imap      = 通过 IMAP 993 收件（需应用专用密码）
    # gmail_api = 通过 Gmail API over HTTPS 443 收件（需 OAuth）
    # auto      = 优先 gmail_api（已配置），否则回退 imap
    gmail_receive_backend: str = "auto"
    # Gmail API OAuth 文件（安装版默认位于当前用户 OAuth 目录）
    gmail_api_credentials_path: Path = field(
        default_factory=lambda: get_runtime_paths().oauth_root / "credentials.json"
    )
    gmail_api_token_path: Path = field(
        default_factory=lambda: get_runtime_paths().oauth_root / "token.json"
    )
    # Gmail API scope（默认只读）
    gmail_api_scopes: list[str] = field(
        default_factory=lambda: [_DEFAULT_GMAIL_API_SCOPES]
    )
    # Gmail API 查询设置
    gmail_api_max_results: int = 20
    gmail_api_query: str = "in:inbox"

    # --- QQ 发件 ---
    qq_email: str = ""
    qq_auth_code: str = ""
    qq_smtp_host: str = "smtp.qq.com"
    qq_smtp_port: int = 465
    # QQ SMTP 默认 direct，本阶段仅预留 socks5 配置键，不实现连接
    qq_smtp_network_mode: str = "direct"
    qq_smtp_connect_timeout: int = 20
    qq_smtp_socks5_host: str = ""
    qq_smtp_socks5_port: int = 0
    qq_smtp_socks5_remote_dns: bool = False

    # --- 固定收件人 ---
    owner_gmail: str = ""

    # --- 本地数据目录 ---
    data_root: Path = field(default_factory=lambda: get_runtime_paths().data_root)
    # 允许应用服务读取并发送文件的额外目录；默认只允许 DATA_ROOT。
    allowed_send_roots: list[Path] = field(default_factory=list)

    # --- 收件规则 ---
    auto_receive_only_self_mail: bool = True
    receive_rule_mode: str = ""
    receive_rule_senders: tuple[str, ...] = field(default_factory=tuple)
    receive_rule_subject_keywords: tuple[str, ...] = field(default_factory=tuple)
    receive_rule_require_attachment: bool = False
    max_fetch_limit: int = 30
    receive_unseen_only: bool = False
    receive_mark_seen: bool = False
    receive_lookback_minutes: int = 30
    receive_scan_cap: int = 500

    # --- 大小限制 (MB) ---
    max_attachment_mb: int = 25
    max_send_file_mb: int = 25
    trusted_download_max_mb: int = 25
    trusted_download_timeout_seconds: int = 20
    trusted_download_max_redirects: int = 3

    # --- 日志 ---
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        """旧布尔配置无损映射到新模式，并归一化非敏感规则。"""
        if not self.receive_rule_mode:
            self.receive_rule_mode = (
                SELF_ONLY if self.auto_receive_only_self_mail else ALL_SCANNED
            )
        if self.receive_rule_mode not in VALID_MODES:
            raise ConfigError(
                f"RECEIVE_RULE_MODE 取值非法：{self.receive_rule_mode!r}"
            )
        self.auto_receive_only_self_mail = self.receive_rule_mode == SELF_ONLY
        self.receive_rule_senders = normalize_sender_rules(self.receive_rule_senders)
        self.receive_rule_subject_keywords = normalize_subject_keywords(
            self.receive_rule_subject_keywords
        )

    @property
    def data_root_path(self) -> Path:
        """数据根目录的绝对路径。"""
        return Path(self.data_root)

    # ---- 各子目录绝对路径 ----
    @property
    def received_dir(self) -> Path:
        return self.data_root_path / "received"

    @property
    def send_dir(self) -> Path:
        return self.data_root_path / "send"

    @property
    def sent_dir(self) -> Path:
        return self.data_root_path / "sent"

    @property
    def logs_dir(self) -> Path:
        return self.data_root_path / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_root_path / "agent_mail_bridge.db"

    @property
    def max_attachment_bytes(self) -> int:
        return self.max_attachment_mb * 1024 * 1024

    @property
    def max_send_file_bytes(self) -> int:
        return self.max_send_file_mb * 1024 * 1024

    @property
    def trusted_download_max_bytes(self) -> int:
        return self.trusted_download_max_mb * 1024 * 1024

    @property
    def effective_allowed_send_roots(self) -> list[Path]:
        """返回包含 DATA_ROOT 的明确发送路径白名单。"""
        roots = [self.data_root_path, *self.allowed_send_roots]
        return list(dict.fromkeys(Path(item).resolve() for item in roots))

    @property
    def gmail_api_scopes_str(self) -> str:
        """Gmail API scopes 的字符串表示（逗号分隔），便于诊断输出。"""
        return ",".join(self.gmail_api_scopes)

    @property
    def gmail_api_configured(self) -> bool:
        """Gmail API 是否已配置（credentials.json 存在即视为已配置）。

        auto 模式据此判断是否优先走 gmail_api。
        """
        return self.gmail_api_credentials_path.exists()

    def mask(self) -> dict:
        """返回脱敏后的配置摘要，供 show-config / 日志使用。"""
        return {
            "gmail_address": self.gmail_address,
            "gmail_app_password": _mask_secret(self.gmail_app_password),
            "gmail_imap_host": self.gmail_imap_host,
            "gmail_imap_port": self.gmail_imap_port,
            "gmail_network_mode": self.gmail_network_mode,
            "gmail_connect_timeout": self.gmail_connect_timeout,
            "gmail_socks5_host": self.gmail_socks5_host,
            "gmail_socks5_port": self.gmail_socks5_port,
            "gmail_socks5_remote_dns": self.gmail_socks5_remote_dns,
            "gmail_receive_backend": self.gmail_receive_backend,
            "gmail_api_credentials_path": str(self.gmail_api_credentials_path),
            "gmail_api_token_path": str(self.gmail_api_token_path),
            "gmail_api_scopes": self.gmail_api_scopes_str,
            "gmail_api_max_results": self.gmail_api_max_results,
            "gmail_api_query": self.gmail_api_query,
            "gmail_api_configured": self.gmail_api_configured,
            "qq_email": self.qq_email,
            "qq_auth_code": _mask_secret(self.qq_auth_code),
            "qq_smtp_host": self.qq_smtp_host,
            "qq_smtp_port": self.qq_smtp_port,
            "qq_smtp_network_mode": self.qq_smtp_network_mode,
            "qq_smtp_connect_timeout": self.qq_smtp_connect_timeout,
            "owner_gmail": self.owner_gmail,
            "data_root": str(self.data_root_path),
            "allowed_send_roots": [str(item) for item in self.effective_allowed_send_roots],
            "auto_receive_only_self_mail": self.auto_receive_only_self_mail,
            "receive_rule_mode": self.receive_rule_mode,
            "receive_rule_senders": list(self.receive_rule_senders),
            "receive_rule_subject_keywords": list(self.receive_rule_subject_keywords),
            "receive_rule_require_attachment": self.receive_rule_require_attachment,
            "max_fetch_limit": self.max_fetch_limit,
            "receive_unseen_only": self.receive_unseen_only,
            "receive_mark_seen": self.receive_mark_seen,
            "receive_lookback_minutes": self.receive_lookback_minutes,
            "receive_scan_cap": self.receive_scan_cap,
            "max_attachment_mb": self.max_attachment_mb,
            "max_send_file_mb": self.max_send_file_mb,
            "trusted_download_max_mb": self.trusted_download_max_mb,
            "trusted_download_timeout_seconds": self.trusted_download_timeout_seconds,
            "trusted_download_max_redirects": self.trusted_download_max_redirects,
            "log_level": self.log_level,
        }


def _mask_secret(secret: str) -> str:
    """对密钥脱敏：只保留首尾各 1 位，中间用星号代替。"""
    if not secret:
        return ""
    if len(secret) <= 2:
        return "*" * len(secret)
    return f"{secret[0]}{'*' * (len(secret) - 2)}{secret[-1]}"


# ---- 配置缺失错误 ----
class ConfigError(Exception):
    """配置缺失或不合法时抛出。"""


def load_config(env_path: Path | str | None = None) -> AppConfig:
    """加载配置。

    Args:
        env_path: 自定义 .env 路径；默认由 RuntimePaths 决定。

    Returns:
        AppConfig 实例。

    注意：本函数只负责读取，不强制要求所有字段都填写。
    收 / 发件时再按需校验。
    """
    runtime = get_runtime_paths()
    env_file = Path(env_path).expanduser().resolve() if env_path else runtime.config_file
    if load_dotenv is not None and not _as_bool(
        os.getenv("AGENT_MAIL_BRIDGE_DISABLE_DOTENV"), False
    ):
        if env_file.exists():
            load_dotenv(env_file, override=False)

    from agent_mail_bridge.credentials import load_secure_secrets
    secure_secrets = load_secure_secrets()

    base_dir = env_file.parent
    default_data_root = (
        str(runtime.data_root) if runtime.frozen else "./AgentMailBridgeData"
    )
    data_root_raw = os.getenv("DATA_ROOT", default_data_root).strip()
    data_root = Path(data_root_raw)
    if not data_root.is_absolute():
        data_root = (base_dir / data_root).resolve()

    allowed_send_roots = []
    for raw_path in os.getenv("ALLOWED_SEND_ROOTS", "").split(os.pathsep):
        if raw_path.strip():
            allowed_send_roots.append(
                _resolve_path(raw_path, raw_path, base_dir=base_dir)
            )

    # 安装版不从普通配置读取秘密；环境变量回退仅保留给源码迁移兼容。
    legacy_gmail_secret = "" if runtime.frozen else os.getenv("GMAIL_APP_PASSWORD", "")
    legacy_qq_secret = "" if runtime.frozen else os.getenv("QQ_AUTH_CODE", "")
    default_credentials = (
        str(runtime.oauth_root / "credentials.json")
        if runtime.frozen else "secrets/credentials.json"
    )
    default_token = (
        str(runtime.oauth_root / "token.json")
        if runtime.frozen else "secrets/token.json"
    )

    legacy_only_self = _as_bool(os.getenv("AUTO_RECEIVE_ONLY_SELF_MAIL"), True)
    receive_rule_mode = os.getenv("RECEIVE_RULE_MODE", "").strip().lower()
    if not receive_rule_mode:
        receive_rule_mode = SELF_ONLY if legacy_only_self else ALL_SCANNED

    cfg = AppConfig(
        gmail_address=os.getenv("GMAIL_ADDRESS", "").strip(),
        gmail_app_password=secure_secrets.get(
            "GMAIL_APP_PASSWORD", legacy_gmail_secret
        ).strip(),
        gmail_imap_host=os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com").strip(),
        gmail_imap_port=_as_int(os.getenv("GMAIL_IMAP_PORT"), 993),
        gmail_network_mode=_parse_network_mode(
            os.getenv("GMAIL_NETWORK_MODE"), "GMAIL_NETWORK_MODE", "auto"
        ),
        gmail_connect_timeout=_as_positive_int(
            os.getenv("GMAIL_CONNECT_TIMEOUT"), "GMAIL_CONNECT_TIMEOUT", 20
        ),
        gmail_socks5_host=os.getenv("GMAIL_SOCKS5_HOST", "127.0.0.1").strip(),
        gmail_socks5_port=_parse_port_strict(
            os.getenv("GMAIL_SOCKS5_PORT"), "GMAIL_SOCKS5_PORT"
        ) if os.getenv("GMAIL_SOCKS5_PORT") else 10808,
        gmail_socks5_remote_dns=_as_bool(
            os.getenv("GMAIL_SOCKS5_REMOTE_DNS"), True
        ),
        gmail_receive_backend=_parse_receive_backend(
            os.getenv("GMAIL_RECEIVE_BACKEND"), "auto"
        ),
        gmail_api_credentials_path=_resolve_path(
            os.getenv("GMAIL_API_CREDENTIALS_PATH", ""), default_credentials,
            base_dir=base_dir,
        ),
        gmail_api_token_path=_resolve_path(
            os.getenv("GMAIL_API_TOKEN_PATH", ""), default_token,
            base_dir=base_dir,
        ),
        gmail_api_scopes=[
            s.strip()
            for s in (os.getenv("GMAIL_API_SCOPES")
                      or _DEFAULT_GMAIL_API_SCOPES).split(",")
            if s.strip()
        ],
        gmail_api_max_results=_as_positive_int(
            os.getenv("GMAIL_API_MAX_RESULTS"), "GMAIL_API_MAX_RESULTS", 20
        ),
        gmail_api_query=os.getenv("GMAIL_API_QUERY", "in:inbox").strip(),
        qq_email=os.getenv("QQ_EMAIL", "").strip(),
        qq_auth_code=secure_secrets.get(
            "QQ_AUTH_CODE", legacy_qq_secret
        ).strip(),
        qq_smtp_host=os.getenv("QQ_SMTP_HOST", "smtp.qq.com").strip(),
        qq_smtp_port=_as_int(os.getenv("QQ_SMTP_PORT"), 465),
        qq_smtp_network_mode=_parse_network_mode(
            os.getenv("QQ_SMTP_NETWORK_MODE"), "QQ_SMTP_NETWORK_MODE", "direct"
        ),
        qq_smtp_connect_timeout=_as_positive_int(
            os.getenv("QQ_SMTP_CONNECT_TIMEOUT"), "QQ_SMTP_CONNECT_TIMEOUT", 20
        ),
        qq_smtp_socks5_host=os.getenv("QQ_SMTP_SOCKS5_HOST", "").strip(),
        qq_smtp_socks5_port=_parse_port_strict(
            os.getenv("QQ_SMTP_SOCKS5_PORT"), "QQ_SMTP_SOCKS5_PORT"
        ) if os.getenv("QQ_SMTP_SOCKS5_PORT") else 0,
        qq_smtp_socks5_remote_dns=_as_bool(
            os.getenv("QQ_SMTP_SOCKS5_REMOTE_DNS"), False
        ),
        owner_gmail=os.getenv("OWNER_GMAIL", "").strip(),
        data_root=data_root,
        allowed_send_roots=allowed_send_roots,
        auto_receive_only_self_mail=legacy_only_self,
        receive_rule_mode=receive_rule_mode,
        receive_rule_senders=normalize_sender_rules(
            os.getenv("RECEIVE_RULE_SENDERS", "")
        ),
        receive_rule_subject_keywords=normalize_subject_keywords(
            os.getenv("RECEIVE_RULE_SUBJECT_KEYWORDS", "")
        ),
        receive_rule_require_attachment=_as_bool(
            os.getenv("RECEIVE_RULE_REQUIRE_ATTACHMENT"), False
        ),
        max_fetch_limit=_as_int(os.getenv("MAX_FETCH_LIMIT"), 30),
        receive_unseen_only=_as_bool(os.getenv("RECEIVE_UNSEEN_ONLY"), False),
        receive_mark_seen=_as_bool(os.getenv("RECEIVE_MARK_SEEN"), False),
        receive_lookback_minutes=_as_positive_int(
            os.getenv("RECEIVE_LOOKBACK_MINUTES"), "RECEIVE_LOOKBACK_MINUTES", 30
        ),
        receive_scan_cap=_as_positive_int(
            os.getenv("RECEIVE_SCAN_CAP"), "RECEIVE_SCAN_CAP", 500
        ),
        max_attachment_mb=_as_int(os.getenv("MAX_ATTACHMENT_MB"), 25),
        max_send_file_mb=_as_int(os.getenv("MAX_SEND_FILE_MB"), 25),
        trusted_download_max_mb=_as_positive_int(
            os.getenv("TRUSTED_DOWNLOAD_MAX_MB"), "TRUSTED_DOWNLOAD_MAX_MB", 25
        ),
        trusted_download_timeout_seconds=_as_positive_int(
            os.getenv("TRUSTED_DOWNLOAD_TIMEOUT_SECONDS"),
            "TRUSTED_DOWNLOAD_TIMEOUT_SECONDS", 20,
        ),
        trusted_download_max_redirects=_as_positive_int(
            os.getenv("TRUSTED_DOWNLOAD_MAX_REDIRECTS"),
            "TRUSTED_DOWNLOAD_MAX_REDIRECTS", 3,
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
    return cfg


def require_readonly_gmail_scope(cfg: AppConfig) -> None:
    """拒绝缺失或扩大 Gmail API 权限。"""
    expected = {_DEFAULT_GMAIL_API_SCOPES}
    actual = set(cfg.gmail_api_scopes)
    if actual != expected:
        raise ConfigError(
            "GMAIL_API_SCOPES 必须且只能是 "
            f"{_DEFAULT_GMAIL_API_SCOPES}，当前配置已拒绝。"
        )


def require_receive_config(cfg: AppConfig) -> None:
    """收件前校验必需配置，缺失时给出明确错误。

    根据 gmail_receive_backend 分支：
    - imap: 需要 GMAIL_ADDRESS + GMAIL_APP_PASSWORD
    - gmail_api: 需要 GMAIL_ADDRESS + credentials.json（不要求应用专用密码）
    - auto: 解析实际后端后再校验
    """
    backend = _effective_receive_backend(cfg)
    if backend == "gmail_api":
        require_readonly_gmail_scope(cfg)
        missing: list[str] = []
        if not cfg.gmail_address:
            missing.append("GMAIL_ADDRESS")
        if not cfg.gmail_api_credentials_path.exists():
            missing.append(
                f"GMAIL_API_CREDENTIALS_PATH({cfg.gmail_api_credentials_path})"
            )
        if missing:
            raise ConfigError(
                "Gmail API 收件缺少必需配置：" + ", ".join(missing)
                + "。请在 .env 中填写（参考 .env.example）。"
            )
        return

    # imap 模式
    missing = []
    if not cfg.gmail_address:
        missing.append("GMAIL_ADDRESS")
    if not cfg.gmail_app_password:
        missing.append("GMAIL_APP_PASSWORD")
    if missing:
        raise ConfigError(
            "IMAP 收件缺少必需配置：" + ", ".join(missing)
            + "。请在 .env 中填写（参考 .env.example）。"
        )


def _effective_receive_backend(cfg: AppConfig) -> str:
    """解析 auto 模式实际使用的后端。

    auto: 优先 gmail_api（credentials.json 存在），否则回退 imap。
    imap / gmail_api: 原样返回。
    """
    backend = cfg.gmail_receive_backend
    if backend == "auto":
        return "gmail_api" if cfg.gmail_api_configured else "imap"
    return backend


def require_send_config(cfg: AppConfig) -> None:
    """发件前校验必需配置，缺失时给出明确错误。"""
    missing: list[str] = []
    if not cfg.qq_email:
        missing.append("QQ_EMAIL")
    if not cfg.qq_auth_code:
        missing.append("QQ_AUTH_CODE")
    if not cfg.owner_gmail:
        missing.append("OWNER_GMAIL")
    if missing:
        raise ConfigError(
            "发件缺少必需配置：" + ", ".join(missing)
            + "。请在 .env 中填写（参考 .env.example）。"
        )


def require_gmail_network_config(cfg: AppConfig) -> None:
    """Gmail 网络模式校验：socks5 模式下必须有可用 host/port。"""
    mode = cfg.gmail_network_mode
    if mode in ("socks5", "auto"):
        # auto 在没配 socks5 时可只用 direct，故仅在 socks5 模式强制
        if mode == "socks5":
            if not cfg.gmail_socks5_host:
                raise ConfigError(
                    "GMAIL_NETWORK_MODE=socks5 但未配置 GMAIL_SOCKS5_HOST。"
                    "请填写本地代理 SOCKS5 地址（如 127.0.0.1）。"
                )
            if not cfg.gmail_socks5_port or cfg.gmail_socks5_port <= 0:
                raise ConfigError(
                    "GMAIL_NETWORK_MODE=socks5 但 GMAIL_SOCKS5_PORT 非法。"
                    "请填写本地代理 SOCKS5 端口（如 10808）。"
                )
