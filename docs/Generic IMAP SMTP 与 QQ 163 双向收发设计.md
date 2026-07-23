# Generic IMAP/SMTP 与 QQ/163 双向收发设计

## 目标和边界

v1.4.2 在既有 Multi-Account Runtime 上补齐标准 IMAP 收件和标准 SMTP 发件，让 QQ、163 与 Generic 账号复用同一协议 Core。它不替换 Gmail API/IMAP Adapter，不增加 Gmail scope，不拆分 MCP，不建立第二套归档、调度、重试或发件审计，也不把 Outlook 当作密码型 Generic 账号。

实现完成不等于真实服务端验收。v1.4.4 已用本机安全凭据完成 QQ 与 163 的真实双向收发和富 MIME 验收；Generic 仍没有独立第三方测试账号，保持 NOT_TESTED。用户新增账号后仍应先完成连接测试。

## 开源调研结论

| 问题 | Thunderbird | Mailspring | Cypht/Nextcloud Mail | IMAPClient | email-mcp/better-email-mcp | AgentMailBridge 决策 |
| --- | --- | --- | --- | --- | --- | --- |
| 账号与协议 | Incoming、Identity、SMTP 分离并用稳定 key 关联 | 每账号独立同步实例 | 多账号由上层聚合，协议适配复用成熟库 | 提供稳定 IMAP 命令与解析 | 每账号分别保存 IMAP/SMTP 配置 | account_id 仍是权限与 ownership 边界；Incoming/Outgoing Runtime 只存在内存 |
| UID checkpoint | mailbox UIDVALIDITY 改变时清理旧 UID cache | 按 folder 保存 uidvalidity、uidnext 和同步游标 | 由成熟 IMAP 层维护目录身份 | select_folder 返回 UIDVALIDITY/UIDNEXT | 多为简单 UID watcher | mailbox 级 UIDVALIDITY/UIDNEXT/last_uid；变化时重置游标，不改写归档 |
| 增量与大邮箱 | 基于 UID 增量并允许重建 | UID range 分段同步 | 后台任务有界运行 | UID search/fetch 可批量 | 常见 polling/reconnect | polling + 有界 scan cap + 25 封批量；不无界加载邮箱 |
| 漏信与重复 | 重建后依靠本地索引 | 本地 cache 和事务去重 | 数据库事实去重 | 只提供协议能力 | 常用 Message-ID 去重 | 少量 UID overlap 防边界漏信；account_id + Message-ID/provider id 正式去重 |
| 坏邮件隔离 | 单邮件解析失败不应终止账号 | 同步事务和错误隔离 | 后台 job 记录单项失败 | fetch 异常由调用方处理 | watcher 常做重连 | 批量失败逐 UID 降级；单封进入现有有限 retry，后续邮件继续 |
| 原始邮件 | 保存或传递真实 RFC822 | 同步真实 MIME 数据 | 复用协议库获得原文 | BODY.PEEK[] 返回原始字节 | 多数直接解析 RFC822 | 只把真实 BODY.PEEK[] 交给统一 package；不伪造 raw.eml |
| 目录 | ISPDB、LIST、SPECIAL-USE | folder/label 归一化 | Horde 等兼容层处理差异 | list_folders 解析 flags/delimiter | 常提供 folder 参数 | LIST/SPECIAL-USE 继续由 IMAPClient 解析；目录刷新失败不阻断已知 INBOX |
| IDLE/MODSEQ/QRESYNC | 成熟客户端可按能力使用 | 使用 CONDSTORE/QRESYNC 优化 | 由协议层选择 | 提供部分扩展入口 | 常见 IDLE 或 polling | v1.4.2 只做 polling；记录 HIGHESTMODSEQ，不宣称增量扩展支持 |
| SMTP | Identity 显式引用 SMTP 服务 | 独立发送配置 | IMAP/SMTP 配置分离 | 不负责 SMTP | SSL/STARTTLS 与每账号认证 | 复用 smtplib/email；SSL 与 STARTTLS；分阶段稳定错误 |
| Provider 默认值 | ISPDB 提供可审计静态 profile | 账号配置检测 | Provider 模块封装差异 | 不提供 profile | 常内置常见服务端 | QQ/163 使用 ISPDB 993/465 SSL profile；Generic 完全按用户配置 |
| 权限与秘密 | 密码按 server/identity 保存 | 账号隔离 | 每账号 secret | 不保存秘密 | 配置通常含凭据 | Windows Credential Manager 按 account_id + secret kind；SQLite/checkpoint 不含秘密 |

借鉴的是架构、协议事实和失败处理思路，没有复制 Thunderbird、Mailspring、Cypht、Nextcloud Mail 或邮件 MCP 项目的业务源码。IMAPClient 作为 New BSD 依赖使用，SMTP 使用 Python 标准库。

## 运行架构

ApplicationService 和 GUI 只传 account_id。AccountRuntimeRouter 读取无秘密账号配置，从 Windows Credential Manager 取得该账号的 IMAP/SMTP secret，构造 IncomingRuntimeConfig 与 OutgoingRuntimeConfig。

Gmail 继续走原 Adapter。QQ、163 与 Generic 的 receive 进入 `imap_sync.py`，send 进入 provider-neutral `mail_send.py`。两条链路都复用现有收件规则、统一 package、Mail Facts、scheduler、retry、outbound、sent archive、Hash 和业务历史。

QQ 与 163 的一个授权码会写入同一 account_id 下的 IMAP 和 SMTP 两个凭据槽，便于以后独立替换且不扩大旧 QQ key 的 fallback。Generic 可为两个协议分别配置秘密。所有配置对象把 secret 标记为不参与 repr。

## IMAP 同步算法

首次同步选择配置的 mailbox，读取 UIDVALIDITY、UIDNEXT 和可用的 HIGHESTMODSEQ。没有游标时从 UID 1 开始搜索，但只处理 scan cap 允许的候选；随后保存 last_uid。

增量同步从 `last_uid - uid_overlap + 1` 搜索，重取少量旧 UID 并处理有限的新 UID。重取项由统一归档判定 duplicate，不创建第二个 package。到期 retry 可脱离普通窗口追加到候选。

UIDVALIDITY 改变表示原 UID 空间失效。程序把该 mailbox 的 last_uid 重置为 0、增加 reset count，清理同账号同 mailbox 的旧 UID 代际技术重试，再有界重扫；正式归档仍按账号与邮件身份去重。不会移动包、重写 raw.eml 或重算历史 Hash。

fetch 使用 25 个 UID 一批的 `BODY.PEEK[]`。批量异常或缺项时逐 UID 再取；单封失败记录 `mailbox:uidvalidity:uid` retry，成功新 UID 才推进 checkpoint。旧 `mailbox:uid` 与纯 UID retry 只在 UIDVALIDITY 未改变时兼容读取。目录 LIST 刷新失败只告警，不阻断已配置 INBOX。

历史补扫使用 SINCE/BEFORE、page size 与 scan cap，支持取消和进度，不推进普通增量 checkpoint。Date Header 只用于范围复核，无法解析时使用本地当前时间安全退化。

## SMTP 发送算法

Outgoing Runtime 只允许 `ssl` 或 `starttls`。SSL 直接建立 TLS；STARTTLS 要求 EHLO、升级 TLS、再次 EHLO。随后使用账号邮箱和该账号 secret 登录，再发送现有 EmailMessage。

错误稳定分类为 connect、tls、auth、recipient_rejected、sender_rejected、timeout、disconnected、temporary、permanent、server_unavailable、message_too_large 和 send。RFC 5321 的 4xx 归为可重试临时失败，5xx 归为永久拒绝；增强状态 `5.3.4` 归为超大邮件。日志只写阶段和产品化原因，不写用户名、授权码或服务器响应中的秘密。

GUI 可选择具备 send 能力的账号并输入一个明确合法收件人。MCP `submit_result` 的 schema、固定 `OWNER_GMAIL`、默认 QQ 兼容配置和允许路径完全不变；Generic 账号不会自动获得 MCP 任意外发权限。

## v1.4.3 验收与错误硬化

`scripts/provider_validation.py` 复用 ApplicationService、AccountRuntimeRouter 和 Credential Manager，不接受命令行 secret。网络测试与真实发件分别要求显式确认；证据只记录 account_id、Provider、状态、错误码和计数，不保存邮箱地址、邮件正文、目录名、授权码或完整服务端响应。

连接测试统一把 IMAP/SMTP 异常转换为认证、TLS、超时、限流、不可用、断开或通用连接错误。单邮件重试表只保存稳定错误码或异常类型，不再保存可能夹带服务端原文的异常字符串。LIST 返回 bytes 时使用 IMAP modified UTF-7 解码，避免中文目录显示为 Python bytes 字面量；SPECIAL-USE 仍是首选事实，未验证的目录名称不新增猜测规则。

完整 pytest 前运行 `scripts/full_suite_preflight.py`，先核对版本、Provider 状态、schema、硬编码断言、diff、compileall 和定向回归。Windows 构建已接入同一 Preflight；`-SkipTests` 仅跳过测试，仍执行一致性与语法检查。

## Provider 与迁移

QQ 与 163 profile 使用完整邮箱地址、IMAP 993 SSL、SMTP 465 SSL、INBOX 和 10 UID overlap。Generic 至少配置 IMAP 或 SMTP 之一，按实际 host 开启 receive/send。

Multi-Account schema v3 在升级事务中幂等开放存量 QQ 的双向能力，并按存量 Generic host 开启对应能力。迁移只更新账号元数据，不移动归档、不改写 raw.eml、不重算 Hash，也不删除凭据。

v1.4.4 中 QQ 与 163 Provider Adapter 状态为 `supported`；Generic 保持 `implementation_ready_e2e_required`，Outlook/Microsoft 保持 planned。

## v1.4.4 真实 Provider 收口

QQ 与 163 均完成真实登录、目录、首轮收件、第二轮增量、进程重启后继续、SMTP、自发自收与四向互发。每条互发验证发件 staging/sent archive Hash、目标 IMAP 到达、raw.eml、Mail Package、account_id ownership 和收件附件 Hash；两个 Provider 的 HTML、inline image、中文、多附件和零字节附件也通过。

163 的 LOGIN 可以成功，但未声明客户端身份时 SELECT INBOX 返回 Unsafe Login。解决方案由 Provider Profile 的 `imap_id_enabled` 驱动，在认证后发送只含真实产品名和版本的 RFC 2971 ID。连接发现与持续同步共用同一扩展点；QQ 和 Generic 默认关闭，不在业务层散落 provider 条件。

QQ 真实验收发现兼容配置卡更新旧 Credential key 后，统一账号专属槽仍保留旧值。GUI 保存现在同步精确匹配 QQ 账号的 IMAP/SMTP 槽，不影响其他 QQ 账号。旧邮件原始非 ASCII From Header 返回 `Header` 对象的问题也已在共享解析入口修复，失败 UID 到期重试后归零。

## 资料

- IMAP4rev2 RFC 9051：https://www.rfc-editor.org/rfc/rfc9051.html
- SPECIAL-USE RFC 6154：https://www.rfc-editor.org/info/rfc6154/
- IMAPClient：https://imapclient.readthedocs.io/
- Python smtplib：https://docs.python.org/3/library/smtplib.html
- TLS for Email RFC 8314：https://www.rfc-editor.org/rfc/rfc8314.html
- SMTP RFC 5321：https://www.rfc-editor.org/rfc/rfc5321.html
- Enhanced Status Codes RFC 3463：https://www.rfc-editor.org/rfc/rfc3463.html
- Thunderbird Android ImapSync：https://github.com/thunderbird/thunderbird-android/blob/main/backend/imap/src/main/java/com/fsck/k9/backend/imap/ImapSync.kt
- Thunderbird ISPDB：https://github.com/thunderbird/autoconfig
- QQ ISPDB profile：https://github.com/thunderbird/autoconfig/blob/master/ispdb/qq.com.xml
- 163 ISPDB profile：https://github.com/thunderbird/autoconfig/blob/master/ispdb/163.com.xml
- Mailspring Sync：https://github.com/Foundry376/Mailspring-Sync
- Cypht：https://github.com/cypht-org/cypht
- Nextcloud Mail：https://github.com/nextcloud/mail
- email-mcp：https://github.com/codefuturist/email-mcp
- better-email-mcp：https://github.com/n24q02m/better-email-mcp
- QQ 邮箱授权码说明：https://help.mail.qq.com/detail/106/985
