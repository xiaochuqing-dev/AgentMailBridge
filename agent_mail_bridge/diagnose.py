"""Gmail / 网络诊断命令逻辑。

与 CLI 解耦：cli.py 只负责参数解析与调用，本模块负责分步骤输出。
诊断命令不写数据库、不建目录、不发送真实邮件。
Gmail 登录测试只做 login + logout，不读取/搜索/修改邮件。
所有输出中敏感字段脱敏（不打印应用专用密码 / 授权码）。
"""

from __future__ import annotations

import platform
import sys

from agent_mail_bridge.config import (
    AppConfig,
    ConfigError,
    require_gmail_network_config,
)
from agent_mail_bridge.logging_setup import get_logger
from agent_mail_bridge.network import (
    GmailAuthError,
    NetworkConfigError,
    create_direct_imap_client,
    create_socks5_imap_client,
    is_pytsocks_installed,
    login_and_logout,
    probe_direct_tls_connect,
    probe_qq_smtp_direct,
    probe_socks5_connect,
    probe_socks5_port,
)

logger = get_logger("diagnose")


# ============================================================
# diagnose-gmail
# ============================================================

def _print_block(lines: list[str]) -> None:
    print()
    for ln in lines:
        print(ln)


def run_diagnose_gmail(cfg: AppConfig) -> int:
    """分步骤诊断 Gmail 连接，输出可读结论。"""
    print("[AgentMailBridge] Gmail 连接诊断")
    print()

    # ---- 配置块 ----
    mode = cfg.gmail_network_mode
    print("配置：")
    print(f"- GMAIL_NETWORK_MODE={mode}")
    print(f"- GMAIL_IMAP_HOST={cfg.gmail_imap_host}")
    print(f"- GMAIL_IMAP_PORT={cfg.gmail_imap_port}")
    print(f"- GMAIL_CONNECT_TIMEOUT={cfg.gmail_connect_timeout}")
    if mode in ("socks5", "auto"):
        print(f"- GMAIL_SOCKS5_HOST={cfg.gmail_socks5_host}")
        print(f"- GMAIL_SOCKS5_PORT={cfg.gmail_socks5_port}")
        print(f"- GMAIL_SOCKS5_REMOTE_DNS={cfg.gmail_socks5_remote_dns}")
    # 不打印任何密码
    print()

    # [1] 配置读取
    print("[1] 配置读取：", end="")
    try:
        require_gmail_network_config(cfg)
        if not cfg.gmail_address or not cfg.gmail_app_password:
            raise ConfigError(
                "缺少 GMAIL_ADDRESS 或 GMAIL_APP_PASSWORD（应用专用密码）。"
            )
    except (ConfigError, NetworkConfigError) as exc:
        print("失败")
        _print_block([
            f"原因：{exc}",
            "",
            "建议：",
            "- 检查 .env 中 GMAIL_NETWORK_MODE 取值（direct/socks5/auto）；",
            "- socks5 模式必须填写 GMAIL_SOCKS5_HOST 与 GMAIL_SOCKS5_PORT；",
            "- 确认已填写 GMAIL_ADDRESS 与 GMAIL_APP_PASSWORD。",
        ])
        return 1
    print("成功")

    # ---- 各模式分支 ----
    if mode == "direct":
        return _diag_gmail_direct(cfg)
    if mode == "socks5":
        return _diag_gmail_socks5(cfg)
    return _diag_gmail_auto(cfg)


def _diag_gmail_direct(cfg: AppConfig) -> int:
    # [2] direct TLS
    print("[2] Direct TCP/TLS 连接 "
          f"{cfg.gmail_imap_host}:{cfg.gmail_imap_port}：", end="")
    r = probe_direct_tls_connect(
        cfg.gmail_imap_host, cfg.gmail_imap_port, cfg.gmail_connect_timeout
    )
    if not r["ok"]:
        print("失败")
        _print_block([
            f"错误：{r['error']}",
            "",
            "可能原因：",
            "- 当前网络无法直接访问 Gmail IMAP；",
            "- 系统代理/规则模式未放行 imap.gmail.com:993；",
            "- 本地 DNS 解析被污染。",
            "",
            "建议：",
            "- 国内用户改用 GMAIL_NETWORK_MODE=socks5；",
            "- 确认是否需要开启代理；",
            "- 若使用 TUN，确认确实接管了 993 端口流量。",
        ])
        return 1
    print("成功")

    # [3] 登录
    return _diag_gmail_login_direct(cfg)


def _diag_gmail_login_direct(cfg: AppConfig) -> int:
    print("[3] Gmail IMAP 登录：", end="")
    try:
        client = create_direct_imap_client(cfg)
    except Exception as exc:  # noqa: BLE001
        print("失败")
        _print_block([f"连接建立失败：{exc}"])
        return 1
    r = login_and_logout(client, cfg.gmail_address, cfg.gmail_app_password)
    if not r["ok"]:
        print("失败")
        _print_block(_auth_fail_advice(r["error"]))
        return 1
    print("成功")
    _print_block(["结论：Gmail IMAP direct 模式可用。"])
    return 0


def _diag_gmail_socks5(cfg: AppConfig) -> int:
    # [2] socks5 端口
    print(f"[2] 本地 SOCKS5 端口 "
          f"{cfg.gmail_socks5_host}:{cfg.gmail_socks5_port}：", end="")
    r = probe_socks5_port(
        cfg.gmail_socks5_host, cfg.gmail_socks5_port, cfg.gmail_connect_timeout
    )
    if not r["ok"]:
        print("失败")
        _print_block([
            "",
            "可能原因：",
            "- v2rayN / Clash Verge / sing-box 没有启动；",
            "- SOCKS5 端口不是 10808；",
            "- 当前代理客户端没有开启本地 SOCKS5 入站；",
            "- 端口被防火墙拦截。",
            "",
            "建议：",
            "- 打开代理客户端，查看本地 SOCKS5 端口；",
            "- 将 GMAIL_SOCKS5_PORT 改成实际端口；",
            "- v2rayN 常见 SOCKS5 端口是 10808；",
            "- Clash Verge 的 mixed-port / socks-port 需要到设置里确认。",
        ])
        return 1
    print("成功")

    # [3] socks5 + TLS
    print(f"[3] 通过 SOCKS5 连接 "
          f"{cfg.gmail_imap_host}:{cfg.gmail_imap_port} 并完成 TLS：", end="")
    r = probe_socks5_connect(
        cfg.gmail_imap_host, cfg.gmail_imap_port,
        cfg.gmail_socks5_host, cfg.gmail_socks5_port,
        cfg.gmail_socks5_remote_dns, cfg.gmail_connect_timeout,
    )
    if not r["ok"]:
        print("失败")
        _print_block([
            f"错误：{r['error']}",
            "",
            "可能原因：",
            "- 代理节点无法访问 Gmail IMAP；",
            "- 代理链路不稳定；",
            "- 规则模式没有允许 IMAP 993；",
            "- TLS 被中断；",
            "- 本机或代理客户端拦截了 TLS。",
            "",
            "建议：",
            "- 尝试切换代理节点；",
            "- 确认 Gmail 网页和 imap.gmail.com:993 都能访问；",
            "- 优先使用 SOCKS5 remote DNS；",
            "- 如果使用 TUN 仍失败，改用 GMAIL_NETWORK_MODE=socks5。",
        ])
        return 1
    print("成功")

    # [4] 登录
    print("[4] Gmail IMAP 登录：", end="")
    try:
        client = create_socks5_imap_client(cfg)
    except Exception as exc:  # noqa: BLE001
        print("失败")
        _print_block([f"连接建立失败：{exc}"])
        return 1
    r = login_and_logout(client, cfg.gmail_address, cfg.gmail_app_password)
    if not r["ok"]:
        print("失败")
        _print_block(_auth_fail_advice(r["error"]))
        return 1
    print("成功")
    _print_block(["结论：Gmail IMAP SOCKS5 模式可用。"])
    return 0


def _diag_gmail_auto(cfg: AppConfig) -> int:
    """auto 模式：先 direct，失败后尝试 socks5。"""
    print("[2] 尝试 Direct 连接 "
          f"{cfg.gmail_imap_host}:{cfg.gmail_imap_port}：", end="")
    direct_ok = probe_direct_tls_connect(
        cfg.gmail_imap_host, cfg.gmail_imap_port, cfg.gmail_connect_timeout
    )
    if direct_ok["ok"]:
        print("成功（优先使用 direct）")
        # 登录
        print("[3] Gmail IMAP 登录：", end="")
        try:
            client = create_direct_imap_client(cfg)
        except Exception as exc:  # noqa: BLE001
            print("失败")
            _print_block([f"连接建立失败：{exc}"])
            return 1
        r = login_and_logout(client, cfg.gmail_address, cfg.gmail_app_password)
        if not r["ok"]:
            print("失败")
            _print_block(_auth_fail_advice(r["error"]))
            return 1
        print("成功")
        _print_block(["结论：Gmail IMAP direct 模式可用（auto 已选定 direct）。"])
        return 0

    # direct 失败
    print(f"失败（{direct_ok['error']}）")
    if not (cfg.gmail_socks5_host and cfg.gmail_socks5_port):
        _print_block([
            "",
            "auto 模式 direct 失败，且未配置 SOCKS5 代理，无法回退。",
            "",
            "建议：在 .env 配置 GMAIL_NETWORK_MODE=socks5 并填写 "
            "GMAIL_SOCKS5_HOST / GMAIL_SOCKS5_PORT。",
        ])
        return 1

    print("[3] 回退尝试 SOCKS5 端口 "
          f"{cfg.gmail_socks5_host}:{cfg.gmail_socks5_port}：", end="")
    rp = probe_socks5_port(
        cfg.gmail_socks5_host, cfg.gmail_socks5_port, cfg.gmail_connect_timeout
    )
    if not rp["ok"]:
        print("失败")
        _print_block([
            f"错误：{rp['error']}",
            "",
            "建议：确认代理客户端已启动且 SOCKS5 端口正确。",
        ])
        return 1
    print("成功")

    print(f"[4] 通过 SOCKS5 连接 "
          f"{cfg.gmail_imap_host}:{cfg.gmail_imap_port} 并完成 TLS：", end="")
    rs = probe_socks5_connect(
        cfg.gmail_imap_host, cfg.gmail_imap_port,
        cfg.gmail_socks5_host, cfg.gmail_socks5_port,
        cfg.gmail_socks5_remote_dns, cfg.gmail_connect_timeout,
    )
    if not rs["ok"]:
        print("失败")
        _print_block([
            f"错误：{rs['error']}",
            "",
            "建议：切换代理节点；确认规则模式放行 imap.gmail.com:993；"
            "优先使用 SOCKS5 remote DNS。",
        ])
        return 1
    print("成功")

    print("[5] Gmail IMAP 登录：", end="")
    try:
        client = create_socks5_imap_client(cfg)
    except Exception as exc:  # noqa: BLE001
        print("失败")
        _print_block([f"连接建立失败：{exc}"])
        return 1
    r = login_and_logout(client, cfg.gmail_address, cfg.gmail_app_password)
    if not r["ok"]:
        print("失败")
        _print_block(_auth_fail_advice(r["error"]))
        return 1
    print("成功")
    _print_block(["结论：Gmail IMAP SOCKS5 模式可用（auto 回退到 socks5）。"])
    return 0


def _auth_fail_advice(err: str) -> list[str]:
    """认证失败时的可读建议（不含密码）。"""
    low = str(err).lower()
    if "auth" in low or "credential" in low or "password" in low:
        return [
            f"错误：{err}",
            "",
            "可能原因：",
            "- Gmail 应用专用密码错误；",
            "- Gmail 账号没有开启 IMAP；",
            "- 使用了 Google 登录密码，而不是 16 位应用专用密码；",
            "- Google 账号安全策略阻止了登录。",
            "",
            "建议：",
            "- 确认 Gmail 已开启 IMAP；",
            "- 确认使用的是应用专用密码；",
            "- 重新生成 Gmail App Password；",
            "- 不要把普通 Google 登录密码填入配置。",
        ]
    return [
        f"错误：{err}",
        "",
        "可能原因：",
        "- Gmail 账号未开启 IMAP 或被 Google 拒绝登录；",
        "- 账号安全策略限制了 IMAP 登录；",
        "- 连接被重置。",
        "",
        "建议：登录 Gmail 网页检查 IMAP 设置与账号安全提醒。",
    ]


# ============================================================
# diagnose-network
# ============================================================

def run_diagnose_network(cfg: AppConfig) -> int:
    """网络环境综合诊断：Python / PySocks / direct / socks5 / QQ SMTP。"""
    print("[AgentMailBridge] 网络诊断")
    print()

    steps: list[tuple[bool, str]] = []

    # [1] Python 环境
    py_ver = sys.version.split()[0]
    ok_py = sys.version_info >= (3, 11)
    steps.append((ok_py, f"[1] Python 环境（{py_ver} / {platform.system()}）"
                         f"：{'成功' if ok_py else '失败（需要 3.11+）'}"))

    # [2] PySocks
    ok_socks = is_pytsocks_installed()
    steps.append((ok_socks, "[2] PySocks 是否安装："
                            + ("成功" if ok_socks else "失败（pip install PySocks）")))

    # [3] direct TLS 连接 Gmail
    r3 = probe_direct_tls_connect(
        cfg.gmail_imap_host, cfg.gmail_imap_port, cfg.gmail_connect_timeout
    )
    steps.append((r3["ok"],
                  f"[3] Direct 连接 {cfg.gmail_imap_host}:{cfg.gmail_imap_port}："
                  + ("成功" if r3["ok"] else f"失败（{r3['error']}）")))

    # [4] SOCKS5 端口
    r4 = probe_socks5_port(
        cfg.gmail_socks5_host, cfg.gmail_socks5_port, cfg.gmail_connect_timeout
    )
    steps.append((r4["ok"],
                  f"[4] SOCKS5 端口 {cfg.gmail_socks5_host}:{cfg.gmail_socks5_port}："
                  + ("成功" if r4["ok"] else f"失败（{r4['error']}）")))

    # [5] SOCKS5 remote DNS 连接 Gmail
    r5 = probe_socks5_connect(
        cfg.gmail_imap_host, cfg.gmail_imap_port,
        cfg.gmail_socks5_host, cfg.gmail_socks5_port,
        cfg.gmail_socks5_remote_dns, cfg.gmail_connect_timeout,
    )
    steps.append((r5["ok"],
                  f"[5] SOCKS5 remote DNS 连接 "
                  f"{cfg.gmail_imap_host}:{cfg.gmail_imap_port}："
                  + ("成功" if r5["ok"] else f"失败（{r5.get('error')}）")))

    # [6] QQ SMTP direct
    r6 = probe_qq_smtp_direct(cfg)
    steps.append((r6["ok"],
                  f"[6] QQ SMTP direct 连接 "
                  f"{cfg.qq_smtp_host}:{cfg.qq_smtp_port}："
                  + ("成功" if r6["ok"] else f"失败（{r6['error']}）")))

    for _ok, line in steps:
        print(line)

    print()
    failed = [s for ok, s in steps if not ok]
    if not failed:
        print("结论：所有网络检查项通过。")
        return 0
    print(f"结论：{len(failed)} 项失败。请根据上面失败项排查：")
    print("- Gmail 网络问题 -> 检查 [3][5]；")
    print("- 代理问题 -> 检查 [4][5]；")
    print("- QQ SMTP 问题 -> 检查 [6]；")
    print("- 认证问题不属于本诊断范围，请运行 diagnose-gmail。")
    return 1


# ============================================================
# diagnose-gmail-api
# ============================================================

def run_diagnose_gmail_api(cfg: AppConfig) -> int:
    """分步骤诊断 Gmail API 收件后端，输出可读结论。

    诊断步骤：
    [1] credentials.json 检查
    [2] token.json 状态
    [3] Gmail API service 创建
    [4] Gmail profile 获取
    [5] messages.list 测试
    [6] messages.get 测试（若有邮件）

    诊断命令不修改邮件、不删除邮件、不标记已读，不输出 token 内容。
    """
    from agent_mail_bridge.gmail_api_auth import (
        CredentialsNotFoundError,
        TokenScopeMismatchError,
        describe_token_status,
        get_gmail_api_service,
        validate_credentials_file,
    )

    print("[AgentMailBridge] Gmail API 诊断")
    print()
    print("配置：")
    print(f"- GMAIL_RECEIVE_BACKEND={cfg.gmail_receive_backend}")
    print(f"- GMAIL_API_CREDENTIALS_PATH={cfg.gmail_api_credentials_path}")
    print(f"- GMAIL_API_TOKEN_PATH={cfg.gmail_api_token_path}")
    print(f"- GMAIL_API_SCOPES={cfg.gmail_api_scopes_str}")
    print(f"- GMAIL_API_MAX_RESULTS={cfg.gmail_api_max_results}")
    print(f"- GMAIL_API_QUERY={cfg.gmail_api_query}")
    print()

    # [1] credentials.json
    print("[1] credentials.json：", end="")
    cr = validate_credentials_file(cfg)
    if not cr["valid"]:
        print("失败")
        _print_block([
            f"原因：{cr['error']}",
            "",
            "建议：",
            "- 从 Google Cloud Console 下载 OAuth Desktop Client JSON；",
            "- 改名为 credentials.json 放到 GMAIL_API_CREDENTIALS_PATH 指定位置；",
            "- 确认已启用 Gmail API。",
        ])
        return 1
    print("成功")

    # [2] token.json
    print("[2] token.json：", end="")
    tr = describe_token_status(cfg)
    if not tr["exists"]:
        print("不存在")
        _print_block([
            "token.json 不存在，首次收件或运行 gmail-api-auth 时会打开浏览器授权。",
            "",
            "建议：运行 `python -m agent_mail_bridge gmail-api-auth` 完成首次授权。",
        ])
        # token 不存在不阻断后续诊断（会触发授权流程）
    elif tr["valid"]:
        print("成功（token 有效）")
    elif tr["expired"] and tr["refreshable"]:
        print("过期但可刷新")
    else:
        print("无效")
        _print_block([
            f"原因：{tr['error']}",
            "",
            "建议：删除 token.json 后重新运行：",
            "python -m agent_mail_bridge gmail-api-auth",
        ])
        if not tr["scopes_match"]:
            _print_block([
                "注意：token scope 与当前配置不一致，需删除旧 token 重新授权。",
            ])
        return 1

    # [3] Gmail API service 创建
    print("[3] Gmail API service 创建：", end="")
    try:
        service = get_gmail_api_service(cfg, interactive=False)
    except CredentialsNotFoundError as exc:
        print("失败")
        _print_block([str(exc)])
        return 1
    except TokenScopeMismatchError:
        print("失败")
        _print_block([
            "Gmail API token 权限与当前配置不一致。",
            "请删除 token.json 后重新运行：",
            "python -m agent_mail_bridge gmail-api-auth",
        ])
        return 1
    except Exception as exc:  # noqa: BLE001
        print("失败")
        _print_block([
            f"原因：{exc}",
            "",
            "建议：",
            "- 若应用还在 Testing 状态，确认当前 Gmail 已加入 OAuth 测试用户；",
            "- 运行 `python -m agent_mail_bridge gmail-api-auth` 重新授权。",
        ])
        return 1
    print("成功")

    # [4] Gmail profile
    print("[4] Gmail profile 获取：", end="")
    try:
        profile = service.users().getProfile(userId="me").execute()
        email_addr = profile.get("emailAddress", "(未知)")
        msgs_total = profile.get("messagesTotal", "?")
    except Exception as exc:  # noqa: BLE001
        print("失败")
        _print_block([
            f"原因：{exc}",
            "",
            "可能原因：",
            "- Google Cloud 项目未启用 Gmail API；",
            "- token 权限不足；",
            "- 当前 Gmail 未加入 OAuth 测试用户。",
        ])
        return 1
    print(f"成功（{email_addr}，总邮件数 {msgs_total}）")

    # [5] messages.list
    print("[5] messages.list 测试：", end="")
    try:
        list_resp = service.users().messages().list(
            userId="me",
            q=cfg.gmail_api_query,
            maxResults=1,
        ).execute()
        messages = list_resp.get("messages", []) or []
    except Exception as exc:  # noqa: BLE001
        print("失败")
        _print_block([f"原因：{exc}"])
        return 1
    print(f"成功（返回 {len(messages)} 封）")

    # [6] messages.get
    print("[6] messages.get 测试：", end="")
    if not messages:
        print("跳过（无邮件可获取，但 list 成功）")
    else:
        try:
            msg = service.users().messages().get(
                userId="me",
                id=messages[0]["id"],
                format="full",
            ).execute()
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in (msg.get("payload") or {}).get("headers") or []}
            subject = headers.get("subject", "(无主题)")
            # 截断长主题
            if len(subject) > 60:
                subject = subject[:60] + "..."
            print(f"成功（主题：{subject}）")
        except Exception as exc:  # noqa: BLE001
            print("失败")
            _print_block([f"原因：{exc}"])
            return 1

    _print_block(["结论：Gmail API 可用。"])
    return 0


# 供 mail_receive 等复用：异常分类文案
def describe_connect_error(exc: Exception, mode: str) -> str:
    """把网络层异常翻译为用户可读文案。"""
    if isinstance(exc, GmailAuthError):
        return (f"Gmail 认证失败：{exc}。"
                "请检查 GMAIL_ADDRESS 与 GMAIL_APP_PASSWORD（应用专用密码，非普通密码）。")
    if isinstance(exc, NetworkConfigError):
        return f"Gmail 网络配置错误：{exc}"
    from agent_mail_bridge.network import NetworkConnectError
    if isinstance(exc, NetworkConnectError):
        return (f"Gmail IMAP 连接失败（模式={mode}）：{exc}。"
                "可运行 `python -m agent_mail_bridge diagnose-gmail` 排查。")
    return (f"IMAP 连接失败：{exc}。"
            "请检查 GMAIL_ADDRESS 与 GMAIL_APP_PASSWORD（应用专用密码，非普通密码）。")
