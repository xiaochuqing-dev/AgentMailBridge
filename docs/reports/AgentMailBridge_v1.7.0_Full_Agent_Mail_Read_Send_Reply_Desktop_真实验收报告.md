# AgentMailBridge v1.7.0 Agent 全权限邮件收发闭环与 Codex Desktop 真实验收报告

更新时间：2026-07-29

## 1. 报告结论

AgentMailBridge v1.7.0 已完成邮箱目录与任意历史、Client 独立读写权限、确认与自主发送、新建/回复/回复全部/转发、outbound 正式事实、Sent 同步去重、确定性线程关系、完整资料复用、GUI 和 Windows 发布产物。

源码、QQ/163 真实收发、Claude Code、Hermes、Codex CLI、最终打包 Agent、测试、构建、安全扫描和 v1.6→v1.7 隔离生命周期均已通过。Codex Desktop 已在新任务中真实调用打包 MCP，完成搜索、邮件详情、资源读取、完整资料准备、confirm reply、GUI 用户确认、一次真实发送、outbound 归档、Sent 副本、幂等重放和暂停/恢复即时门禁。配置、tools/list 或其他 Agent 结果未被当作 Desktop 替代证据。v1.7.0 功能验收结论为 PASS。

起始 HEAD：`4282675693478119e9651f1d6c60adc5925d3f24`

最终验收对象：本报告随 v1.7.0 实现位于同一 `master` 提交并普通推送；提交后本地与远端 HEAD 以第 18 节和最终推送记录为准。

## 2. 产品理念和秘密边界

产品仍是 local-first、single-user、Windows-first 的邮件桥。用户只需接入一次邮箱，再按 Client 授权 Codex、Claude Code、Hermes 等 Agent。AgentMailBridge 不内置 AI，不判断用户意图，不提供草稿模式、自动回复、监控触发发送、定时或营销邮件，也不修改邮件删除、移动和已读状态。

读取权限和发件权限相互独立，迁移与新 Client 均默认拒绝。通用发件只在 Client 明确获准、发件账号在范围内且 confirm/autonomous 模式被执行时成立。`submit_result` 继续保持固定收件人兼容语义，不扩展通用发件权限。

密码、授权码、OAuth Token、Client Token、Credential Manager 值和第三方密钥不得进入 MCP 响应、错误、审计、GUI 预览、报告、备份、Git、dist 或安装器。Gmail scope 仍严格为 `gmail.readonly`，Gmail send 未实现。

## 3. Research Gate

完整调研记录位于 `docs/research/AgentMailBridge_v1.7.0_工程调研与设计决策.md`。本阶段优先复用成熟实现，没有自行重写协议栈或地址解析。

主要依据：

- IMAPClient：复用 LIST/XLIST、modified UTF-7、只读 SELECT、UIDVALIDITY 和目录状态能力。
- MailKit：参考 SPECIAL-USE 优先、Provider 名称回退的特殊目录识别策略。
- OfflineIMAP3：参考每目录 UIDVALIDITY checkpoint，变化时只失效该目录进度，不修改历史事实。
- python-email-validator：使用成熟 Unicode/IDNA/头注入防护和确定性语法校验。
- RQ/Celery：参考过期任务在读取与执行前原子转终态的处理方式。
- notebooklm-py：参考仅对 Windows WinError 5/32 执行有界原子替换退避；其他错误立即失败。
- RFC 6154、RFC 9051、RFC 5322、Python email/smtplib、Gmail API readonly label 语义和 Windows ACL 官方资料。

关键判断：SMTP 接受后，本地归档发布若因 Windows 短暂句柄竞争失败，允许对原子替换做有限重试；不得重建 MIME、不得再次调用 SMTP。最终失败进入 `sent_archive_failed`，保留恢复事实。

## 4. Provider 状态

QQ 与 163 已完成真实双向收发、回复、回复全部、转发、Sent 同步与去重，可表述为正式支持。Gmail 仍为 readonly receive。Generic IMAP/SMTP 复用统一 Adapter，保持 implementation ready / E2E required。Outlook/Microsoft 与 Gmail send 不在 v1.7.0 范围。

邮箱只接一次，各 Agent 共用本地事实与统一 MCP，但每个 Client 的账号、邮箱目录、发件账号和附件目录权限独立。

## 5. 邮箱目录与任意历史

`mailboxes` 保存稳定 mailbox_id、account_id、原始名、显示名、层级、delimiter、flags、Special-Use role、角色来源、UIDVALIDITY/UIDNEXT、enabled 和 sync 状态。Inbox、Sent、Archive、Drafts、Spam/Junk、Trash、Important/Starred、Provider label 与自定义目录均作为只读服务器事实处理。

IMAP 优先使用 SPECIAL-USE/XLIST；Gmail 使用 readonly label metadata；Provider 名称仅为可审计回退。每个 account_id + mailbox_id 有独立 checkpoint。UIDVALIDITY 变化只失效受影响目录的 UID 进度，不移动旧包、不改 raw.eml、不重算 Hash。

历史查询支持 all history、任意自然年份、跨年、自定义开始/结束日期和 Inbox/Sent 双向事实。年份是查询值，不是权限边界。`all` 动态包含未来启用账号/目录；`selected` 不自动扩展。Spam/Trash 仍需明确授权。

## 6. Agent 读取与发件权限

授权顺序为：有效且启用的 Client、操作总开关、账号/发件账号范围、读取时的 mailbox 范围、附件目录范围、canonical ownership、路径安全、大小与 SHA-256。暂停与撤销在下一次调用立即拒绝，无需重启其他 Client。

读取范围支持 all accounts / selected accounts 和 all mailboxes / selected mailboxes。发件范围独立支持 all send accounts / selected send accounts。发件模式只有 confirm 和 autonomous，不实现 Agent draft。

收件人允许任意语法合法地址和多个 To/Cc/Bcc；不增加固定收件人、域名、历史联系人、单附件或年份限制。Bcc 只用于 SMTP envelope，不进入公开 MIME 头。

本地附件必须同时位于全局 `ALLOWED_SEND_ROOTS` 与当前 Client 授权根目录。回复/转发源包必须可读，原附件只有显式选择时才加入。

## 7. 新建、回复、回复全部和转发

Provider-neutral `send_mail` 支持 `new`、`reply`、`reply_all`、`forward`。回复优先 Reply-To，再回退 From；reply-all 排除发送者身份并在保持 To/Cc 语义时去重。回复写入 In-Reply-To、References 和 `reply_to_package_id`；转发写入 `forward_from_package_id`。

地址使用成熟库校验与标准化，拒绝无效地址、换行注入和 ownership 伪造。多 To/Cc/Bcc、多本地附件、Unicode 文件名和选择性原附件均已覆盖。

## 8. 确认发送、自主发送与幂等

confirm 只创建 durable `pending_confirmation`，不接触 SMTP，也不创建 sent fact。MCP 不暴露确认操作；只有 GUI 用户可以确认、取消或返回 Agent 重写。GUI 展示完整正文、收件人和附件大小/Hash。

确认时重新校验 Client 状态、权限、发件账号范围、过期时间、附件 ownership、大小和 SHA-256。autonomous 在相同授权门禁后立即发送，但 AgentMailBridge 不解释自然语言意图。

Client + idempotency_key 唯一。重试返回既有结果，已确认请求最多发送一次；`delivery_unknown` 不自动重试。真实 confirm 证据显示确认前 SMTP 尝试为 0，确认后尝试为 1，真实收件一次。Hermes autonomous 证据显示直接发送一次并完成幂等回查。

## 9. Outbound 事实、Sent 去重与线程

MIME 只构建一次，SMTP DATA 与 outbound `raw.eml` 使用完全相同的 bytes。正式 outbound package 原子保存正文、元数据、附件、Hash、Client、发送模式、确认事实、idempotency key、Provider 结果和线程字段。

本地 outbound 与服务器 Sent 通过 account ownership、Message-ID、Provider 标识、RFC 头、内容/附件指纹和有限时间窗对账。同一邮件只有一个正式 package，不因 Sent 同步生成第二份事实。

线程关系只使用 Message-ID、In-Reply-To、References、Provider thread/conversation ID、reply_to_package_id 与 forward_from_package_id。事实层不使用 AI 或主题相似度聚类。

## 10. 完整资料复用与原始事实

收到和发出的邮件均为一等 mail fact。每封正式邮件只有一个 package；正文、inline image、attachment、link、download 和 raw.eml 都归属该 package。

`prepare_mail_resources` 只向授权目录原子准备资料，并核对源/目标大小与 SHA-256。相同 package_id、目标目录与 manifest/Hash 一致时返回 `reused=true`，不会生成无意义的 `(1)(2)(3)` 副本。工作副本被用户修改时不覆盖。

最终真实 Agent 读取证据覆盖 Inbox、Sent、任意历史、正文、附件和完整资料。既有 packaged Agent 回归以 6 次成功工具调用准备 7 个资源；本次 Codex Desktop 闭环实际完成 search/get/resource read/workspace list/complete package，完整资料输出 6 项、失败 0。完整资料复用成立，canonical source archive 及其 Hash 保持不变。

## 11. GUI 与 Windows 视觉验收

Agent Integration 独立承载每 Client 的读取账号、邮箱目录、发件账号、发送模式、附件目录和 pending send。Receive/Send 页面未混入账号秘密或 Agent 权限。

GUI 使用真实按钮和共享线性图标，不用 Emoji 作为正式图标。六档截图覆盖 100%、125%、150% DPI 的浅色与深色主题。最终修复深色账号列表 viewport 白底后重拍，账号卡、pending send、表格、文本和操作区域均未发现遮挡或主题泄漏。相关截图位于 gitignored QA 目录，不进入 Git。

## 12. 真实邮件与 Agent E2E

真实邮件证据均使用唯一时间戳主题，未删除服务器邮件、未修改私人邮件状态，正文、附件、完整地址和本地私人路径未进入本报告或 Git。

- QQ→163 new：SMTP、真实送达、接收附件 Hash、outbound raw、package ownership 全部 PASS。
- 163→QQ new：同上，全部 PASS。
- reply、reply_all、forward：真实发送与接收 PASS，RFC 和 package 关系 PASS。
- confirm：pending、确认前零 SMTP、GUI 确认、确认后一次投递、outbound raw、Sent 映射 PASS。
- autonomous：Hermes 真实一次发送、真实送达、outbound raw、Sent 映射与幂等 PASS。
- QQ/163 Sent：同步、映射、本地 outbound 去重 PASS。
- 权限：未授权读取/发件/发件账号/附件目录拒绝，暂停、恢复、撤销均 PASS。
- Claude Code：源码读取、确认回复和最终 packaged read PASS。
- Hermes：读取与 autonomous send PASS。
- Codex CLI：源码读取 PASS；最终 packaged CLI 两次外部 Agent 超时且无 MCP 审计，保留为真实失败证据，不冒充 PASS。最终 packaged MCP 已由 Claude Code 真实任务证明可用。
- Codex Desktop：新任务真实加载打包 MCP，读取与 complete package 成功；confirm reply 在 GUI 中经用户确认后真实发送一次，outbound/Sent/幂等和暂停恢复证据均 PASS。
- 普通 Claude Desktop：SKIPPED_BY_SCOPE。

## 13. Codex Desktop 真实闭环

新 Codex Desktop 任务使用自然语言完成全部阻断项，实际 MCP 审计记录 `client_type=codex`，没有用配置存在、连接测试、tools/list、Codex CLI 或其他 Agent 结果替代：

首次真实 complete package 暴露了一个此前 fixture 未覆盖的权限视图矛盾：内部 DATA_ROOT 为 ownership 校验进入 `identity.workspace_ids`，但并不是 `list_agent_workspaces` 对 Agent 可见的输出目录；默认选择却按 identity 中的总数量判断，导致界面列出唯一默认工作区时仍返回 `workspace_denied`。修复不扩大权限，而是只在“可见且已授权”的工作区交集中做唯一默认选择；同时增加语义明确的 `workspace_id` 参数并保留 `target_workspace` 兼容。回归测试真实构造“2 个内部身份范围、1 个可见输出工作区”，覆盖显式 ID、默认选择和复用。

- 本地归档 search、get、文本附件 resource read、工作区列举和 complete package 均成功。源邮件共 3 项资源、1 个附件；附件读取为 43 字节且 SHA-256 与归档事实一致。完整资料输出 6 项、失败 0，canonical source archive 未变化。
- 验收 CSV 为 2 行、223 字节，仅含 UTC/本地时间、非敏感调用摘要和 PASS，提交前后 SHA-256 一致；CSV 位于 gitignored 验收目录，未进入 Git。
- `send_mail` 创建 confirm reply 后状态为 `pending_confirmation`，SMTP 尝试数为 0。GUI 完整显示正文与 1 个 CSV 附件，用户完成最终确认后状态转为 `sent`。
- 确认后 SMTP 尝试数为 1，Provider 接受并成功追加 Sent 副本。outbound package 状态 `ready`，包含正文、可读正文和附件共 3 项资源；实际 `raw.eml`、正文与附件 SHA-256 均与数据库事实一致。
- 收件端使用既有 163 Adapter 做只读增量收件，不标记已读：扫描 11、保存 1、重复 10、失败 0。Inbox 中恰有 1 个 package 同时匹配本次主题指纹、发送时间、223 字节附件及附件 SHA-256，证明实际送达一次。Provider 改写了 Message-ID，因此验收没有错误地把单一 Message-ID 当成唯一证据，而是使用目录、时间、主题和附件指纹联合核对。
- 复用原 idempotency key 返回 `duplicate` 和原 `sent` 请求，SMTP 尝试数仍为 1，没有第二次发送。
- Codex Client 切换为 paused 后下一次 MCP 调用立即返回 `client_disabled`；恢复 active 后同一会话立即成功。未撤销 Client、未更换 Token。

当前状态：PASS。

## 14. Migration、升级、卸载和重装

使用随机 AppId、隔离安装名和隔离用户目录，从已验证 v1.6 双 EXE 构建旧安装器，从最终 v1.7 候选构建新安装器，真实执行 install→upgrade→uninstall→reinstall。

结果：旧安装、升级和重装均 PASS；migration backup 生成；v1.7 表和 schema PASS；旧 Client scope 不扩大且 send 默认关闭；account_id、OAuth 文件、Credential、数据库、package/raw/attachment Hash 均保留；卸载移除程序但保留用户数据；重装恢复；测试凭据和隔离安装均清理。

升级前、升级后、卸载后和重装后计数一致：5 个账号、4 个 package、1 个 outbound、4 个 scheduler state；4 个 raw.eml、4 个附件、2 个 OAuth JSON 的事实与 Hash 保持一致。

验收状态：PASS

## 15. 测试、构建与安全门禁

- compileall：PASS。
- git diff --check：PASS，仅有工作区 CRLF 提示，无空白错误。
- targeted tests：原候选 54 passed；workspace 默认选择与 `workspace_id` 兼容修复后的定向回归 48 passed。
- Full Suite Preflight：124 passed，11.03 秒；版本、Provider 状态、schema、diff、compileall 均 PASS。
- final full pytest：634 passed、2 skipped，1997.74 秒。
- pip check：No broken requirements found。
- clean Windows build：Python 3.11.15、PyInstaller 6.21.0、Inno Setup 6.7.3，PASS。
- GUI packaged self-test、MCP 11 工具 UTF-8/BOM/EOF/path/default-deny smoke：PASS。
- real packaged Agent read：Claude Code PASS，runtime=packaged，6 次成功工具调用、7 个资源。
- installer：`AgentMailBridge-1.7.0-Setup.exe`，PASS。
- ZIP：`AgentMailBridge-1.7.0-Windows-x64.zip`，PASS。
- checksums：安装器 `0a94a192e51f7b103f5d2a3b96d393aa03102084a44bb06465082b6bf8181b52`；ZIP `3f1923db68e0067977008099c13038e4e777273ca3f831f6d5c8c1711b6c3e28`；实际复核与 `checksums.sha256` 一致。
- secret scan：337 个候选文件，PASS；`.env`、OAuth、数据库、日志、邮件、附件和证据未进入发布物或 Git 候选。
- Defender：Antivirus/Realtime 均启用，engine 1.1.26060.3008、signature 1.455.404.0；release 与 dist 自定义扫描新增检测 0，PASS。
- Authenticode：安装器、GUI EXE、MCP EXE 均为版本 1.7.0，实际状态 NotSigned。

构建与功能验收为 PASS。由于没有合法代码签名证书，公开可信发布仍为 CONDITIONALLY PASS；不得表述为已签名或可信发行。

## 16. P0 / P1 / P2

P0：未发现 secret/token 泄露、Client secret 跨界、未授权发件、未授权账号/附件、重复发送、Bcc 公开头泄漏、发送 bytes 与归档不一致、源事实修改、ownership/path 绕过或私人资料进入 Git。

P1：未发现功能验收阻断项。安装器与双 EXE 无可信代码签名，属于公开分发风险并已如实披露，不得表述为已签名或可信发行。

P2：Generic 尚无独立第三方真实 E2E；普通 Claude Desktop 未测试；Outlook 与 Gmail send 未实现；视觉只在当前 Windows 环境完成六档验收。

当前功能结论：PASS。NotSigned 作为明确发布限制保留；公开可信发布结论仍为 CONDITIONALLY PASS。

## 17. 已知限制

Gmail 只读且 scope 不扩展。Generic 保持 implementation ready / E2E required。Outlook/Microsoft、Gmail send、普通 Claude Desktop 产品化、SaaS、多租户、Web API/Webhook、删除/移动/已读修改、草稿、自动回复和定时发送均不在本版。

`delivery_unknown` 需要用户审查 Provider 与 Sent 事实，产品不会自动重发。安装器未签名，Windows 可能显示信誉提示。Codex Desktop 对新 MCP 需要 Restart 和新任务加载，旧任务不能热更新工具集。

## 18. Commits 与 push status

全程直接在 `master` 开发，起始本地 HEAD 与 `origin/master` 均为 `4282675693478119e9651f1d6c60adc5925d3f24`。最终实现、测试、migration、GUI、文档和本报告使用普通 fast-forward commit/push 提交；不创建分支、不 force push、不改写历史、不创建 Tag、PR 或 GitHub Release。

最终功能状态：PASS。最终提交和远端 HEAD 以承载本报告的 `origin/master` 提交为准，推送结果在最终交付消息中记录。

## A. Agent 验收矩阵

| Agent | Real Desktop/CLI | Read Any Authorized Mail | Historical Range | Inbox | Sent | Complete Package | New | Reply | Reply All | Forward | Confirm Send | Direct Send | Revoke | Secret Safe | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex Desktop | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | SKIPPED_BY_SCOPE | PASS | PASS | PASS |
| Codex CLI | PASS | PASS | PASS | PASS | PASS | PASS | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | PASS | PASS | PASS |
| Claude Code | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | SKIPPED_BY_SCOPE | PASS | PASS | PASS |
| Hermes | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | SKIPPED_BY_SCOPE | PASS | PASS | PASS | PASS |
| Claude Desktop | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE |

## B. Provider 验收矩阵

| Provider | Folder Discovery | Inbox | Sent | Custom Folder | Historical | Agent Read | Agent Send | Reply | Forward | Outbound Fact | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| QQ | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| 163 | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Gmail | PASS | PASS | PASS | PASS | PASS | PASS | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | PASS |
| Generic IMAP/SMTP | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | CONDITIONALLY PASS |
| Outlook/Microsoft | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE |

Generic 行表示实现与自动化覆盖通过，但在独立第三方 Provider 真实 E2E 前不升级为正式支持。

## C. 权限验收矩阵

| Client | All Accounts | Selected Accounts | All Mailboxes | Selected Mailboxes | All Send Accounts | Selected Send Accounts | Confirm | Autonomous | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex Desktop | PASS | PASS | PASS | PASS | PASS | PASS | PASS | SKIPPED_BY_SCOPE | PASS |
| Codex CLI | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Claude Code | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Hermes | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| 迁移旧 Client | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

## D. 发件可靠性矩阵

| Scenario | Request ID | SMTP Attempts | Actual Deliveries | Outbound Package | Sent Mapping | Duplicate Prevented | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| QQ→163 new | redacted fingerprint | 1 | 1 | PASS | PASS | PASS | PASS |
| 163→QQ new | redacted fingerprint | 1 | 1 | PASS | PASS | PASS | PASS |
| reply | redacted fingerprint | 1 | 1 | PASS | PASS | PASS | PASS |
| reply_all | redacted fingerprint | 1 | 1 | PASS | PASS | PASS | PASS |
| forward | redacted fingerprint | 1 | 1 | PASS | PASS | PASS | PASS |
| Codex Desktop confirm reply | redacted fingerprint | 1 after confirm / 0 before | 1 | PASS | PASS | PASS | PASS |
| autonomous new | redacted fingerprint | 1 | 1 | PASS | PASS | PASS | PASS |
| delivery_unknown | synthetic reliability coverage | 1 | unknown | PASS | CONDITIONALLY PASS | PASS | PASS |

## E. 用户场景矩阵

| 场景 | Agent | 正常语言 | 收件事实 | 发件事实 | 用户最终结果 | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 任意历史完整往来 | Claude Code / Hermes | PASS | PASS | PASS | Inbox 与 Sent 按时间形成完整事实 | PASS |
| 最新测试需求完整资料 | Claude Code packaged | PASS | PASS | SKIPPED_BY_SCOPE | 资料复用、资源 Hash 与原归档不变 | PASS |
| 发送前确认回复 | Claude Code | PASS | PASS | PASS | 用户确认后只发送一次并真实收到 | PASS |
| Agent 自主新建邮件 | Hermes | PASS | PASS | PASS | 授权后直接发送一次并真实收到 | PASS |
| Codex Desktop 资料任务 | Codex Desktop | PASS | PASS | SKIPPED_BY_SCOPE | 完整资料 6 项、Hash 一致、源归档不变 | PASS |
| Codex Desktop 确认发送 | Codex Desktop | PASS | PASS | PASS | pending 后经 GUI 确认，只发送一次并形成 outbound/Sent 事实 | PASS |
