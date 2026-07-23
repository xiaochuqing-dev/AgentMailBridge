# Multi-Account Runtime 与 Provider Foundation 设计

## Research Gate

本设计先核对 Thunderbird、Mailspring、Cypht、email-mcp、Nextcloud Mail、Google 官方资料、IMAP 标准和 Python 邮件库，再决定 AgentMailBridge 的运行时边界。外部项目只用于理解成熟架构、协议和测试方法，没有复制其业务源码或资源。

主要资料：

- Thunderbird Account Configuration：https://developer.thunderbird.net/thunderbird-development/codebase-overview/account-configuration
- Thunderbird Autoconfig / ISPDB：https://github.com/thunderbird/autoconfig
- Mailspring Sync：https://github.com/Foundry376/Mailspring-Sync
- Cypht：https://github.com/cypht-org/cypht
- email-mcp：https://github.com/codefuturist/email-mcp
- Nextcloud Mail：https://github.com/nextcloud/mail
- Gmail API 发件：https://developers.google.com/workspace/gmail/api/guides/sending
- Gmail OAuth scope：https://developers.google.com/workspace/gmail/api/auth/scopes
- Google Desktop OAuth：https://developers.google.com/identity/protocols/oauth2/native-app
- Microsoft Graph Mail API：https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview
- Exchange Online IMAP/SMTP OAuth：https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
- Microsoft 桌面授权码 + PKCE：https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- IMAP4rev2 RFC 9051：https://datatracker.ietf.org/doc/html/rfc9051
- IMAP SPECIAL-USE RFC 6154：https://datatracker.ietf.org/doc/html/rfc6154
- IMAPClient：https://github.com/mjs/imapclient
- Python smtplib：https://docs.python.org/3/library/smtplib.html

## Community Architecture Research Matrix

| 问题 | Thunderbird | Mailspring | Cypht | email-mcp | Nextcloud Mail | AgentMailBridge 决策 |
| --- | --- | --- | --- | --- | --- | --- |
| Account ID | Account、Incoming Server、Identity、SMTP 各有稳定 key | 每个同步进程绑定一个 account | 多服务账号独立，上层聚合 | 配置中按账号选择服务 | 数据库账号实体驱动同步 | 保留稳定不透明 account_id，业务调用必须先解析 account_id |
| Provider abstraction | Incoming Server 子类型与独立 SMTP 服务 | MailCore2 后端加账号配置 | 模块集隔离协议能力 | 分层 service 与 provider autodetect | Account 服务复用 Horde 协议库 | Registry 只声明真实 capability，Router 统一分派，禁止业务层散落 provider if/elif |
| Credential storage | Login Manager 按 server/identity 保存 | 账号配置与进程隔离 | 每账号配置隔离 | 每账号配置，禁止日志秘密 | 每账号 secret，与账号实体关联 | Windows Credential Manager key 使用 account_id + secret kind；.env 仅兼容 |
| OAuth per account | OAuth token 与账号/server identity 关联 | 每账号同步实例持有自己的认证状态 | Provider 模块管理各账号认证 | OAuth2 为每账号实验能力 | 每账号 OAuth 配置 | Token 和 OAuth 锁按 account_id 隔离；Desktop client 配置可以复用但不能共享 Token |
| SMTP identity | Identity 显式引用 SMTP server | 每账号发送配置 | SMTP 模块独立 | account/service 分层 | 每账号 IMAP/SMTP | GUI 可选 from_account_id；MCP submit_result 继续使用固定受控发件账号，不新增任意账号参数 |
| Connection lifecycle | 账号/server 独立连接 | 每账号一个可重启同步进程 | 模块按服务连接 | 每账号连接与测试 | 每账号连接服务 | 每账号 runtime context 和锁；单账号故障不能持有其他账号锁 |
| Scheduler | 每账号检查设置 | 每账号进程独立启动、停止、重启 | 聚合视图不合并连接状态 | watcher 按账号运行 | 命令和后台作业显式带 account id | 轻量单进程 coordinator，逐个运行到期账号；状态、retry、backoff 全部按 account_id |
| Retry / backoff | 连接和账号级错误隔离 | 进程退出可由上层重启，SQLite 事务保证中断安全 | 服务错误不阻断其他账号 | 账号级 rate limit 和连接错误 | 账号级同步错误 | 连接级 backoff 写 account_sync_states，单邮件 retry 继续独立；聚合任务捕获每账号异常后继续 |
| Folder discovery | server/folder 模型和 Autoconfig | IMAP folder/label 归一化 | IMAP 模块统一目录 | provider-aware label 管理 | Horde 负责 IMAP 兼容 | Generic Foundation 使用成熟 IMAPClient 解析 LIST、UID 和国际化目录，不自写响应解析器 |
| Special folders | Identity 与目录设置分开 | Gmail label 特殊处理 | Provider 模块处理差异 | provider-aware | Horde 兼容层 | 首选 RFC 6154 SPECIAL-USE；缺失时只做保守 profile fallback，不猜测并合并 |
| UID semantics | IMAP server identity 独立 | UID 空间按 folder 同步，并使用 CONDSTORE/QRESYNC | 交给 IMAP 层 | IMAP watcher 使用 UID | Horde IMAP client | checkpoint 预留 mailbox_id + UIDVALIDITY + UID；Message-ID 只做归档去重，不能代替 IMAP 同步身份 |
| Same-provider multi-account | 原生支持 | 每账号单独进程 | 原生聚合 | 原生支持 | 原生支持 | 同一 Provider 多账号共用 adapter 实现，但配置、secret、token、state、lock、ownership 不共享 |
| Account removal | 删除账号信息与删除本地消息数据分开确认 | 停止对应实例 | 账号配置独立移除 | 账号命令独立 | 账号删除与数据处理分层 | 默认 soft remove：停止连接并保留历史 ownership；secret/token 清理由用户明确选择，邮件数据不静默删除 |
| Failure isolation | server/account 独立 | auth/connection 错误只终止单账号进程 | 模块错误局部化 | account service 独立 | 单账号同步命令 | Router 只返回单账号结果；coordinator 聚合时保留成功项并把部分失败标为 partial |
| Offline/local cache | Profile 内按账号目录 | SQLite 本地缓存 | 聚合视图 | 主要直连 | 数据库缓存 | 继续使用统一 SQLite + package archive，ownership 按 account_id，不拆数据库 |
| Provider autodetect | ISPDB 与 autoconfig XML | 账号设置检测 | Provider 模块 | 内置 provider profiles | IMAP/SMTP account setup | 本阶段只建立可验证的静态 profile 和手动配置，不联网下载 ISPDB，不把 profile 存在误报为 Provider 已上线 |
| Unified Inbox | 上层聚合账号 | UI 查询本地缓存 | 核心目标 | 一套 MCP 多账号 | 正式支持 | Mail Facts 和一个 MCP 继续默认跨账号查询；Runtime 底层绝不合并账号状态 |
| MCP scope | 不适用 | 不适用 | 不适用 | 一个 MCP 管多账号 | 不适用 | 继续一个 MCP；读取允许 account_id 过滤，Agent 发件权限不随 GUI 多账号扩大 |

Microsoft 官方资料进一步确认：Exchange Online 的 IMAP、POP、SMTP 现代认证需要 Microsoft Entra OAuth 与各自权限，Graph Mail.Send 也是独立发件权限；桌面应用应使用系统浏览器的授权码流程并配合 PKCE，官方不建议自行拼装低层协议。由此，Outlook 不能被当成“填服务器和密码即可工作”的 Generic 特例。本阶段只保留 planned Adapter；后续必须单独设计 MSAL/PKCE、个人账号与组织租户、管理员同意、Graph 与协议路线及真实租户 E2E。

## 最终架构决策

ApplicationService、GUI 和 MCP 只把 account_id 交给 AccountRuntimeRouter。Router 从 SQLite 读取账号，验证 enabled、removed 和 capability，构造只属于该账号的 Runtime Context，再复用现有 Gmail API/IMAP 收件、QQ SMTP 发件、归档和 Mail Facts 实现。

MailAccount 的 provider 与 email_address 决定稳定 account_id。为避免历史 ownership 被误绑，账号创建后不允许原地改变 provider 或邮箱地址；需要换地址时创建新账号并移除旧账号。display_name、启用状态、认证类型和非秘密 provider_settings 可以更新。

账号删除采用 soft remove。默认操作只停止同步和发送、保留 mail package、raw.eml、附件、outbound、审计和 ownership。Credential 与 OAuth 清理是独立显式选项。产品暂不提供物理删除历史邮件的账号删除快捷方式。

Windows Credential Manager target 使用 account:<account_id>:<secret_kind>。旧 gmail_imap_app_password 和 qq_smtp_auth_code 只给对应 legacy 账号一次性兼容迁移，不能作为第二个同类型账号的 fallback。

OAuth Desktop client 配置可由同一桌面应用复用，但 token.json、锁和状态目录必须按 account_id 隔离。旧单 Gmail Token 只复制到匹配的 legacy Gmail 账号目录，原文件保留，失败不覆盖有效文件。

Scheduler 使用每账号持久化状态和每账号进程锁。单次 coordinator 可以顺序执行多个到期账号以减少 SQLite 写竞争，但必须逐账号捕获错误并继续；这与“一个失败不能拖死其他账号”一致，不要求为本地单用户产品复制 Mailspring 的一账号一进程架构。

## Gmail 发件决策

Google 官方文档明确：Gmail API messages.send 需要 gmail.send、gmail.compose 或更宽 scope；gmail.readonly 只能查看邮件与设置。SMTP XOAUTH2 通常需要 mail.google.com，权限更宽。

本项目安全不变量要求 Gmail scope 必须保持且只能是 gmail.readonly。因此 v1.4.1 不扩大 scope、不触发存量用户重新授权，也不把 Gmail 标记为支持发件。Gmail 发件保留为 planned。若以后单独立项，必须先完成权限模型、MCP 发件边界、重新授权迁移和真实 E2E。

## Generic IMAP/SMTP Foundation 决策

不自行解析复杂 LIST、UID、国际化目录、SPECIAL-USE、IDLE 或 MODSEQ 响应。Foundation 采用 New BSD 许可的 IMAPClient 作为未来 Generic IMAP 解析层，SMTP 继续复用 Python 标准库 smtplib 和 email。

本阶段只把 profile、TLS 配置校验、连接测试、目录发现、SPECIAL-USE 映射、UIDVALIDITY checkpoint 结构和 adapter 边界建立起来。QQ 收件、163、Outlook、Yahoo 和任意 Generic 账号的正式持续同步仍需各自真实服务器验证，未验证前保持 experimental 或 planned。

## 许可证边界

Thunderbird Autoconfig 为 MPL-2.0，Mailspring Sync 为 GPL-3.0，Cypht 为 LGPL-2.1，email-mcp 为 LGPL-3.0，Nextcloud Mail 为 AGPL-3.0。AgentMailBridge 只借鉴账号、运行时、失败隔离、目录和调度思想，不复制这些项目的业务源码、资源或配置数据库。

IMAPClient 为 New BSD 许可，可以作为独立依赖使用；其版权与许可需写入 THIRD_PARTY_NOTICES。Python 标准库和 Google 官方客户端继续按现有依赖边界使用。
