# AgentMailBridge v1.7.0 工程调研与设计决策

更新时间：2026-07-29

## 1. 调研边界

本阶段只扩展 AgentMailBridge 已有的本地单用户、多账号、统一归档和 stdio MCP 能力。不会重写 IMAP/SMTP 协议栈，不引入 Gmail 发件、Outlook、Web API、Webhook、内置 AI、草稿、自动回复、定时或营销发送。

## 2. GitHub 开源社区参考

### IMAPClient

来源：

- https://github.com/mjs/imapclient
- https://github.com/mjs/imapclient/blob/master/imapclient/imapclient.py

核对结果：

- `list_folders()` 已返回 `(flags, delimiter, name)`，并负责 modified UTF-7 目录名解码。
- `xlist_folders()` 可在服务器支持 XLIST 时取得特殊目录标识。
- `select_folder(..., readonly=True)` 与 `folder_status()` 可取得 UIDVALIDITY、UIDNEXT 等目录状态。
- `find_special_folder()` 已体现“特殊标志优先、常见名称回退”的成熟思路。

决策：继续复用项目现有 IMAPClient，不自行实现 LIST、XLIST、modified UTF-7、FETCH 或 UID 解析。AgentMailBridge 只负责把这些事实转成稳定 mailbox_id、权限和 checkpoint。

### MailKit

来源：

- https://github.com/jstedfast/MailKit
- https://github.com/jstedfast/MailKit/blob/master/MailKit/Net/Imap/ImapClient.cs

核对结果：MailKit 只在服务器具备 SPECIAL-USE 或 XLIST 时直接返回 `SpecialFolder.Sent` 等正式特殊目录，并覆盖 All、Archive、Drafts、Flagged、Important、Junk、Sent、Trash。

决策：Sent 等角色以协议标志为第一依据。服务器不提供标志时才进入 Provider 候选匹配，并把识别来源和置信度持久化，避免只硬编码英文目录名。

### OfflineIMAP3

来源：

- https://github.com/OfflineIMAP/offlineimap3
- https://github.com/OfflineIMAP/offlineimap3/blob/master/offlineimap/folder/Base.py

核对结果：OfflineIMAP 按 folder 单独保存 UIDVALIDITY，并在缓存值与服务器值不匹配时拒绝把旧 UID 状态当成当前事实。

决策：checkpoint 必须属于 account_id + mailbox_id。UIDVALIDITY 改变时清空该目录的 UID 进度，但不移动旧 Mail Package、不改写 raw.eml、不重算历史 Hash。

### python-email-validator

来源：

- https://github.com/JoshData/python-email-validator
- https://github.com/JoshData/python-email-validator/blob/main/README.md

核对结果：库提供 Unicode、IDNA、安全字符和标准化处理；`check_deliverability=False` 可只做确定性语法检查而不产生 DNS 网络访问。

决策：通用发件地址使用该成熟库校验和标准化，不扩展项目原有的简化正则。发件阶段关闭 DNS deliverability 检查，真实投递结果由 SMTP Provider 决定。

### Thunderbird Autoconfig

来源：

- https://github.com/thunderbird/autoconfig
- https://github.com/thunderbird/autoconfig/wiki

决策：QQ、163、Generic 的服务器参数继续通过 Provider Profile/用户配置进入统一 Adapter，不在发送或同步流程复制 Provider 分支。新增 Provider 配置仍应优先参考 Thunderbird ISPDB 和 Provider 官方说明。

### RQ 与 Celery 的过期任务处理

来源：

- https://github.com/rq/rq/blob/cb25f2ea50917177e6ed55b63b282b5527b15f61/rq/registry.py
- https://github.com/celery/celery/blob/7c5d9a62d90c685bd0e1ae002d66ae40980b2847/celery/worker/request.py

核对结果：

- RQ 在读取 registry 的计数或任务列表前默认执行 cleanup，并按当前时间处理已经到期的记录，避免列表继续呈现不可执行任务。
- Celery 在执行前重新比较 `expires`，将到期任务持久化为 revoked/expired 结果并跳过执行。

决策：待确认发件不能只在用户点击确认时临时判断过期。GUI 列表、Agent 状态查询和取消入口在返回结果前统一执行原子过期转换；确认路径仍保留发送前的第二次过期校验。这样过期请求从可操作列表中消失，同时保留明确终态、零 SMTP 尝试和完整审计事实。

### notebooklm-py 的 Windows 原子替换退避

来源：

- https://github.com/teng-lin/notebooklm-py/blob/7d0aa42c1b858780250f9c6cfbea422aff3e8ce0/src/notebooklm/_atomic_io.py

真实行为：2026-07-29 的 163→QQ 源邮件发送中，SMTP 已接受且服务器 Sent 副本随后可见，但本地正式 outbound package 从 `.staging` 提升到最终目录时出现 Windows `ERROR_ACCESS_DENIED`。暂存目录、raw.eml、正文和附件均完整，说明问题发生在最后一次目录 rename，而不是 MIME 构建或 SMTP 阶段。

核对结果：notebooklm-py 仅对 Windows `ERROR_ACCESS_DENIED`（5）和 `ERROR_SHARING_VIOLATION`（32）执行 10 次有界指数退避，其他 PermissionError 立即抛出；源临时对象在成功前保持不变，因此重试不破坏原子性。

决策：邮件归档的文件和目录原子替换采用相同边界，初始等待 1ms、上限 50ms、最多 10 次。绝不重试 POSIX 权限错误、Windows 其他错误或非 PermissionError；最终失败仍保留 `sent_archive_failed`，不得自动重发 SMTP。该修复针对杀毒、索引和并发读取造成的短暂 Windows 句柄竞争，不掩盖真实 ACL 或磁盘问题。

## 3. 协议与标准依据

- RFC 6154：https://www.rfc-editor.org/rfc/rfc6154
  - SPECIAL-USE 是 `\Sent`、`\Drafts`、`\Junk`、`\Trash` 等角色的首选依据。
- RFC 9051：https://www.rfc-editor.org/rfc/rfc9051
  - 邮箱名称、层级 delimiter、UID 与 UIDVALIDITY 共同决定服务器消息身份。
- RFC 5322：https://www.rfc-editor.org/rfc/rfc5322
  - 新建消息生成 Message-ID；回复保留 In-Reply-To 与 References；Bcc 不进入公开头字段。
- Python `email` / `smtplib`：
  - https://docs.python.org/3/library/email.message.html
  - https://docs.python.org/3/library/smtplib.html
  - MIME 使用 `EmailMessage` 构建一次，通过 `as_bytes(policy=SMTP)` 固化，SMTP 发送这些 bytes，归档同一份 bytes。
- Gmail API：
  - https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
  - `labelIds` 与 `includeSpamTrash` 在现有唯一 `gmail.readonly` scope 内提供 Label/mailbox 过滤，不扩大 OAuth scope。
- Windows ACL：
  - https://learn.microsoft.com/windows-server/administration/windows-commands/icacls
  - Client 配置和本地敏感运行文件继续使用当前用户 ACL、原子替换、备份和冲突检测；不得记录秘密值。

## 4. Codex Desktop 当前行为依据

来源：

- https://learn.chatgpt.com/docs/extend/mcp
- 2026-07-29 刷新的 Codex Manual 本地缓存

官方当前行为：

- ChatGPT desktop app、Codex CLI 和 IDE extension 在同一 Codex host 上共享 MCP 配置。
- 本地 stdio MCP 配置保存在 `~/.codex/config.toml` 或可信项目的 `.codex/config.toml`。
- Desktop 在 Settings > MCP servers 保存配置后必须选择 Restart。
- stdio MCP 由 host 按需启动，适合 AgentMailBridgeMCP.exe 的 stdin/stdout 生命周期。

验收结论：配置存在、`tools/list`、CLI 或共享配置不能替代 Codex Desktop 新任务中的真实读取与确认发送。若安装新构建后工具集变化，只请求用户执行一次明确的 Desktop Restart。

## 5. 增量架构决策

### 数据模型

- 扩展 `mailboxes`：raw/display 名称、父目录、delimiter、flags、role source、UIDVALIDITY/UIDNEXT、启用与发现状态。
- 扩展 `agent_clients`：mailbox scope mode、send account scope mode、send mode。
- 权限行继续复用 `agent_client_permissions`，新增 mailbox、send-account 和 attachment-root capability 维度，旧 Client 的通用 send 默认关闭。
- 新增 durable send request、recipient、attachment、Sent mapping 与 thread relation 表。
- `mail_packages` 增加 direction、RFC thread headers 和 outbound 关联；现有 package/resource/account ID 与原始文件保持不变。

### 模块边界

- `mailbox_sync.py`：Provider-neutral 目录发现、角色规范化、目录同步入口。
- `send_permissions.py`：发件账号、目录和附件范围校验。
- `send_requests.py`：pending、confirm/autonomous、取消、过期、幂等状态机。
- `outbound_mail.py`：地址、reply/reply-all/forward 语义、单次 MIME 构建、SMTP 调用与正式 outbound package。
- `mail_threading.py`：确定性线程关系与 Sent 回流匹配。
- `ApplicationService` 只编排，现有 IMAP、SMTP、归档和账号路由保持 Adapter 角色。

### 可靠性

- Client + idempotency_key 唯一；相同请求只执行一次。
- confirm 创建 pending，不连接 SMTP；只有 GUI 可确认或取消。
- 确认时重新校验 Client、权限、账号、过期时间和附件大小/Hash。
- SMTP 在明确接受前失败可记录失败；接受结果不确定时进入 `delivery_unknown`，不自动重试。
- MIME bytes 一次构建；SMTP DATA 和 outbound raw.eml 使用完全相同 bytes。
- Provider 接受后即使正式归档发布失败，也记录 `sent_archive_failed` 并提供恢复，不重新发送。
- 本地 outbound 与服务器 Sent 通过 account_id、Message-ID、Provider ID、RFC 头、内容/附件指纹和有界时间窗匹配，不能生成第二个正式 package。

## 6. 明确不采用的方案

- 不自行实现 IMAP parser、modified UTF-7 或邮箱地址解析。
- 不把四种发送动作拆成四套重复工具。
- 不让 `submit_result` 承担通用发件语义。
- 不用主题相似度或 AI 聚类线程。
- 不按年份、联系人、域名或历史往来限制已授权 Client。
- 不在 MCP 暴露 confirm 工具。
- 不自动修改任何 Agent 全局规则。
