# CHANGELOG

## 1.4.2 Generic IMAP/SMTP 与 QQ/163 双向收发 - 2026-07-23

- 新增 Provider-neutral Incoming/Outgoing Runtime Config；QQ、163 与 Generic 账号通过 Account Runtime Router 复用同一个 IMAPClient 收件 Core、标准库 SMTP 发送 Core、统一邮件归档、规则、重试、调度、历史补扫和 outbound 审计，不复制既有 Gmail/QQ 协议实现。
- IMAP 增量同步使用 mailbox 级 UIDVALIDITY、UIDNEXT、last_uid 与有限 UID overlap；UIDVALIDITY 变化时只重置对应游标，依靠 Message-ID/账号归档去重安全重扫。批量 `BODY.PEEK[]` 失败时降级到逐 UID，单封坏邮件进入有限重试而不阻断后续邮件。
- SMTP 支持 SSL/TLS 与 STARTTLS，分阶段区分连接、TLS、认证、收件人/发件人拒绝、临时错误、超时和超大邮件；发送前后的 staging、Hash、outbound/sent ownership 与 GUI 单收件人/MCP 固定 owner 边界保持不变。
- QQ 与 163 使用静态、可审计的 SSL profile；Generic 只按实际填写的 IMAP/SMTP host 开启能力。账号秘密继续按 account_id 分别保存在 Windows Credential Manager，不写 SQLite、报告或日志。
- Multi-Account schema 升级到 v3，事务内幂等开放存量 QQ/Generic 能力；不移动归档、不改写 raw.eml、不重算历史 Hash，也不覆盖以后用户的启停选择。
- GUI 新增 163 账号入口，QQ/163 授权码同时配置账号隔离的 IMAP/SMTP 凭据；连接测试只认证和发现目录，不收信、不发信。Gmail scope 仍严格且唯一为 `gmail.readonly`，Outlook/Microsoft 仍为 planned。
- Python、GUI、MCP、EXE metadata 与 Inno Setup 版本统一升级为 1.4.2。QQ、163、Generic 的自动化协议与归档验收完成；真实账号网络 E2E 因无独立测试凭据明确标记 NOT_TESTED。

## 1.4.1 Multi-Account Runtime 与 Provider Foundation - 2026-07-23

- 新增统一 Account Runtime Router，业务入口可按稳定 `account_id` 解析 Provider、运行配置、能力、Credential 与 OAuth 文件；Gmail 收件、GUI QQ 发件、历史补扫、MCP 新鲜度同步和连接测试不再只能依赖单个全局账号。
- 新增账号 create/read/update/enable/disable/soft remove 生命周期。移除默认保留邮件、附件、发件事实和审计，凭据与 OAuth Token 由用户明确选择是否按账号清理；旧配置同步不会重新启用已停用或已移除账号。
- 新账号凭据按 `account_id` 写入 Windows Credential Manager，Gmail OAuth credentials/Token 进入账号专属目录；同 Provider 多账号的密码、Token、授权状态和失败互不覆盖，原 Gmail/QQ key 只为地址精确匹配的兼容账号回退。
- 自动收件升级为逐账号状态、锁、重试与退避，一个账号失败不阻断其他到期账号；GUI 可选择手工收件、历史补扫、连接测试和正式发件账号，新增账号入口、动态卡片、启停与安全移除开始真实工作。
- Generic IMAP/SMTP Foundation 引入 New BSD 许可的 IMAPClient，提供 TLS-only 配置校验、Provider Profile、连接测试、LIST/SPECIAL-USE 目录发现及 UIDVALIDITY/UIDNEXT/HIGHESTMODSEQ checkpoint；正式 receive/send 仍未开放。
- 调研 Google 官方发件方案后保留 Gmail `gmail.readonly` 唯一 scope。Gmail send 需要额外 `gmail.send`/`gmail.compose` 或更宽 SMTP OAuth 权限，与当前硬安全边界冲突，因此本版明确不实现、不触发重新授权。
- 统一 MCP 保持七个工具和 `submit_result` 固定 `OWNER_GMAIL` 边界；`get_mail_sync_status` 新增可选 `account_id`。数据库 Multi-Account schema 升级到 v2，升级前备份、事务、raw.eml/历史 Hash 不改写规则保持不变。
- Python、GUI、MCP、EXE metadata、Inno Setup 与构建产物版本统一升级为 1.4.1；Windows 包显式包含 IMAPClient 模块和许可证 metadata。

## 1.4.0 Multi-Account Core 多邮箱架构地基 - 2026-07-22

- 新增无秘密 `MailAccount` 一等实体、稳定 `account_id/data_namespace`、`Mailbox` 与 Provider Adapter 能力注册表；Gmail 继续复用现有 API/IMAP 收件归档，QQ 继续复用现有 SMTP 发件归档，Generic IMAP/SMTP 与 Microsoft 仅作未接通扩展边界。
- SQLite 新增 `mail_accounts`、`mailboxes`、`account_sync_states`，并为收件、邮件 package、重试、规则评估、outbound 和 sent 事实补充账号归属；同一 Message-ID 可在不同账号独立存在，同类型账号的同步状态和发件事实互不串联。
- v1.3.0 Gmail/QQ 配置与历史数据首次启动前自动备份，并在同一可回滚事务中幂等映射到正式账号；迁移不移动文件、不改写 `raw.eml` 或历史 Hash，失败保留旧库。
- Mail Facts、邮件会话与 MCP `search_mails` 支持可选 `account_id`；省略时保持统一跨账号查询。统一 MCP 仍为七工具，`submit_result` 固定 `OWNER_GMAIL`、只读 Gmail scope、路径白名单和审计边界不变。
- 左侧收敛为统一“邮箱账号”可滚动列表，账号卡显示当前真实能力；添加入口说明未来 Provider，尚不开放第二个同类型账号或 Gmail 发件、QQ 收件、163、Outlook。
- manifest 升级为 v2 并写入稳定账号/邮箱目录身份；Python、GUI、MCP、EXE metadata 与 Inno Setup 版本统一升级为 1.4.0。

## 1.3.0 收发语义、历史补扫、联系人解码与邮件详情统一整改 - 2026-07-21

- 新配置默认使用 `all_scanned`，正常归档当前 Inbox 查询范围内的候选邮件；`self_only` 更名为“仅 Gmail 自发自收邮件（高级）”。显式旧模式和 custom 规则保留，旧隐式默认通过版本、来源标记一次性原子迁移，迁移幂等且失败保留旧配置。
- Gmail API、IMAP、自动收件、立即收取和 MCP 新鲜度同步统一走 `match_receive_rule`，删除旧的发件人私自过滤；`rule_skipped` 只记录本次评估，不成为永久去重事实。
- 新增可取消的“历史补扫”，支持最近 24 小时、7 天、30 天和自定义日期；按页查询当前后端，显示扫描、匹配、新增、重复、规则跳过和失败，使用 Message-ID/provider id 去重并延续有限重试。
- 防回流从收件规则中解耦。产品发送增加无秘密 outbound Header，并结合本地 outbound 记录和实际发件身份精确标记；同一 QQ 地址从其他设备人工发送不会被粗暴过滤。
- GUI 手动发件新增可编辑 To，默认 `OWNER_GMAIL`，发送前展示实际地址并拒绝空值、非法地址、多地址及 CRLF 注入；发送历史保存真实 To。MCP `submit_result` 参数和固定 owner 安全边界不变。
- From、To、Cc、Bcc、Reply-To 统一解析 RFC 2047 显示名，保留 raw Header 和原始 `raw.eml`，增加结构化、可搜索、可供 GUI/MCP 使用的 decoded 联系人事实。
- 邮件详情改为正文/资源纵向 QSplitter，正文最小高度 240px、默认约 380px且会话内记忆；图片、附件、链接与下载按非空内容动态分区，长文件名、真实 0 字节和完整 URL 均可理解。
- 链接继续离线识别 plain HTTP/HTTPS、HTML href 和非 CID img，不依赖正文提示词；显示名综合 anchor、类型、provider、hostname 和路径，不再只显示 view/report。
- SQLite 新增联系人、outbound origin 和规则评估事实，迁移与索引位于同一可回滚事务，旧库升级前自动备份；Python、GUI、MCP、EXE metadata、Inno Setup、installer 和 portable ZIP 统一升级为 1.3.0。

## 1.2.1 Gmail OAuth 首次配置可靠性与 GUI 无阻塞 Hotfix - 2026-07-18

- Gmail OAuth 授权改为 QObject + QThread 后台会话；Gmail API、IMAP 和 QQ 连接测试使用后台任务，Qt GUI 主线程不再执行网络等待。
- 新增可测试状态机、唯一 session_id、非法跳转保护、旧会话隔离、同进程单会话和操作系统级跨进程 OAuth 锁。
- 用受控 `127.0.0.1` 随机端口回环服务器替代阻塞式 `run_local_server`，支持 5 分钟超时、真实取消、应用退出清理、state 校验、静态完成页和端口释放。
- OAuth 页面新增阶段状态、取消、重新打开浏览器、复制同一授权链接、重新验证 Gmail API、清除本地 Token 和代理/防火墙提示。
- credentials.json 只接受严格有效的 Google Desktop app；Web application、混合节点、非官方端点、非法 redirect URI、损坏编码和过大文件均拒绝，替换失败保留旧配置。
- Token 改为 fsync 后原子替换，校验 Client ID、只读 scope 和 refresh token；失败授权、账号不匹配或保存失败不会破坏旧 Token。
- Token 交换与 Gmail Profile 验证分离；Gmail API 未启用、网络、配额或 5xx 可进入 AUTHORIZED_UNVERIFIED 并直接重试验证，账号不匹配不会静默改绑。
- OAuth 日志和诊断只记录阶段、回环状态、错误码与耗时，不记录授权 URL、state、code、Client Secret 或 Token。
- 修复测试隔离只禁止读取 `.env`、却仍可能通过默认配置路径写入源码 `.env` 的漏洞；全局测试夹具现在同时重定向运行目录与配置文件，并增加默认写入隔离回归。
- Python、GUI、MCP、EXE metadata 和 Inno Setup 版本统一升级为 1.2.1。

## 1.2.0 后台邮件运行时通用 Agent 读取与 GUI 统一收口专项 - 2026-07-17

- 新增全局一次性邮件读取开关；GUI 关闭后，本机兼容 stdio MCP 的 Agent 仍可按安全边界读取规范本地归档。
- MCP 扩展为 `search_mails`、`get_mail`、`read_mail_resource`、`prepare_mail_resources`、`list_agent_workspaces`、`get_mail_sync_status` 和兼容的 `submit_result` 七个工具，并补齐标准副作用注解。
- 搜索支持最新、今天、昨天、最近若干天、日期范围、排序、分页及主题、发件人、收件人、附件和状态过滤；正文、资源名和链接多处命中仍只返回一封邮件。
- 正文与 TXT/Markdown/代码/JSON/YAML/XML 等文本支持严格编码识别和字符分页；CSV/TSV 支持流式列名与行预览；图片返回格式和尺寸；PDF、Office、ZIP、EXE 等二进制只返回描述。
- 资源准备只复制指定邮件资源到授权工作区的受控目录，保留文件名、处理冲突并校验源与目标 SHA-256；拒绝越界、目录联接逃逸、执行和解压。
- `ensure_fresh` 接入真实收件层；新增崩溃可恢复的操作系统级跨进程收件锁，GUI、自动调度和多个 Agent 不会并发收取。
- 新增 `mcp_audit_events`，统一记录搜索、读信、读资源、准备、同步、工作区和发送；兼容旧 `mcp_calls`，审计不保存正文全文或秘密。
- 左侧新增独立 Agent/MCP 页面，统一展示状态、读取开关、同步、单一配置、两个示例、工作区和最近调用；删除旧长篇交付指令和发件页重复入口。
- 收件与发件共用全局刷新；Agent/MCP 与底部导航合并为贴合的统一导航卡；附件、链接及最近调用表格移除竖向网格、焦点边界和单元格分块。
- Python、GUI、MCP、EXE metadata、Inno Setup、安装器和 portable ZIP 版本统一升级为 1.2.0。

## 1.1.0 邮件摘要、事实搜索、深色表格交互与日志长期运行专项 - 2026-07-16

- 收件和最近发送复用紧凑摘要构建器，正文预览严格受限，固定稳定行高，完整正文继续在详情展示。
- 摘要无论是否有正文都显示非零附件、邮件图片、链接和下载数量；主题与正文使用不同展示边界并提供可控 Tooltip。
- 两张邮件摘要表采用专用整行样式，移除单元格选择分块与竖线，修复深色 Hover 继承浅色背景导致的白闪，同时保留整行双击和真实操作按钮。
- 收件搜索升级为 Mail Facts 数据库查询，覆盖联系人、收件人、抄送人、完整可读正文、附件/图片名、链接文字、域名、URL 和自然状态；多空格查询与多资源命中保持单邮件去重。
- 中文正文解码增加 UTF-8、GBK/GB2312、Big5、无 charset 和错误 charset 的严格候选选择，继续支持 RFC 2047 主题与 RFC 2231 中文附件名。
- 周期自动检查和正常无变化不再写入永久 `app_events`；新邮件、部分失败、连接故障/恢复、退避、发件、MCP、配置和维护事件继续记录。
- SQLite 技术事件新增普通 30 天、警告/错误 90 天和 10,000 条硬上限，超限批量降到约 8,000 条；启动异步、每 24 小时、插入超限和手动清理均受控触发。
- 日志管理新增概览、事件类型和日常检查筛选、数据库分页、当前筛选脱敏导出、保留设置、立即清理、清除日常检查和清空全部技术日志二次确认。
- GUI、About、CLI、MCP、EXE metadata、installer 与 portable ZIP 统一升级为 1.1.0。

## 1.0.0 统一邮件归档地基与邮件事实模型专项 - 2026-07-16

- 收件页升级为邮件级列表，一封邮件一行；新增正文、内嵌图片、附件、链接和同会话详情，历史记录与最近发送统一按邮件展示。
- 发件页升级为完整邮件编辑器，支持可选主题、正文、0 至多个附件、0 至多个链接，并保证一次确认只发送一封 MIME 邮件。
- 新增 `outbound_messages`、`outbound_resources`、`outbound_links` 及旧发件幂等回填，Agent `submit_result` 与人工发件共用邮件级归档和审计链。
- Agent / MCP 入口移至发件标题区，新增完整可复制交付指令和安全工作区授权管理；Agent 自行定位最终文件并直接提交原路径，不再要求用户填写路径或手工搬运。

- 新增统一邮件归档模型：一封邮件对应一个稳定 package，正文、内嵌图片、附件、链接和下载资源全部关联 `package_id`。
- Gmail API 改为读取真实 raw 响应，IMAP 继续保存 `BODY.PEEK[]` 原始字节；每个新包保存可核验 SHA-256 的 `raw.eml`，旧数据明确标记 raw 不可恢复且不伪造。
- 新增按年/月/日组织的独立邮件目录、相对路径 manifest、plain/html/readable 正文分层、CID 图片分类、附件冲突命名和 Windows 路径预算。
- 新增离线链接识别，区分网页、直接文件、云文档和外部图片；默认不访问网络，可信域列表默认为空。
- 新增 SSRF-safe 可信下载引擎：仅 HTTPS、DNS 与 IP 校验、固定解析地址 TLS、每次重定向重验、超时/大小/MIME 限制、原子落盘、Hash 审计且只保存到当前邮件 downloads 目录。
- 新增 `mail_packages`、`mail_resources`、`trusted_domains` 和迁移元数据；启动前在线备份，历史正文和附件幂等回填到 legacy package，旧文件与兼容表保留。
- 新增只读 Mail Facts Service，支持消息、资源、会话、过滤和正文/主题/发件人/附件名/链接检索，不返回 raw 字节。
- 一致性扫描新增 package、manifest、raw、资源路径与 Hash、残留 staging 检查；存储统计新增邮件包和资源分类占用。
- 保持自动收件、旧历史和旧文件数据兼容，邮件级 GUI 与完整发件体验已经落地。

## 1.0.0 核心链路可靠性与自动收件稳定性专项 - 2026-07-14

- MCP stdio 明确使用 UTF-8，兼容首条 BOM，保持 stdout 纯 JSON-RPC、逐条 flush、EOF 正常退出，并隔离单请求异常。
- `submit_result` 新增白名单内原子 staging；审计 source/staged 大小、SHA-256、路径、时间、状态和失败原因，Agent 不再负责手工搬运文件。
- 发件结果新增 filename、size、source/staged/attachment/sent archive SHA-256；MIME 附件直接读取已校验 send 副本，归档后再次校验。
- 自动收件默认 60 秒、最低 30 秒，启动约 3 秒首次检查；持久化检查、成功、结果、失败次数、下次时间和 checkpoint。
- Gmail API 新增 30 分钟重叠回看、分页扫描与安全上限；IMAP 新增同期 SINCE 回看并使用 BODY.PEEK，继续依靠 Message-ID 去重。
- 单邮件失败新增 1 分钟、5 分钟、30 分钟、2 小时有限重试及 needs_attention 终态；到期项即使离开 30 分钟回看窗口仍按资源 ID 单独重试，IMAP 使用真实 UID 搜索/读取，旧失败项不会污染每轮。
- 全局连接失败新增 30 秒至 15 分钟退避，成功后复位；睡眠、长暂停或事件循环恢复后看门狗补偿，托盘隐藏不停止调度。
- `no_changes` 保持健康中性状态，`partial` 不触发全局退避；收件页展示真实调度状态、检查时间、结果和待重试数。
- Windows 最大化、还原、最小化和关闭按钮改用统一线性图标，支持按钮和标题栏双击切换；只恢复受当前屏幕工作区约束的 normal geometry。
- 收件页删除与左侧账号卡重复的 Gmail 管理卡，扩大中央工作区；正常窗口完整显示文件和最近日志且不出现外层滚动，较矮窗口才自动滚动，表格保持独立滚动并通过 150% DPI 浅色/深色截图验收。

## 1.0.0 历史、文件数据与收件规则专项 - 2026-07-13

- 调整 Gmail / QQ 账号卡片标题、状态和邮箱布局，完整显示账号职责、状态与地址，并通过 100%、125%、150% DPI 检查。
- “今日收到文件”改为文件名、大小、`HH:mm:ss` 收取时间和操作四列；路径退出主表但继续支持搜索、打开、复制和双击预览，操作按钮统一高度并垂直居中。
- 收件偏好扩展为仅本人、全部扫描范围和自定义三种模式；自定义支持发件人/域名、主题关键词和仅含附件，分类间 AND、分类内 OR，Gmail API/IMAP 与手动/自动收件共用统一业务规则。
- 旧 `AUTO_RECEIVE_ONLY_SELF_MAIL` 无损映射到新模式；新增配置有默认值且不包含秘密，规则校验或保存失败不会覆盖旧有效配置。
- 历史记录改为类型、摘要、时间、中文状态和操作的业务页面；完整 request_id、路径、原始状态和错误信息进入结构化详情。
- 新增统一受管文件 DTO 与 `get_managed_files` 查询，收件文件直接读取 `received_files`，发送归档读取 `sent_files`，Agent / MCP 与发送记录按 request_id 和路径归一化去重。
- 修复从 `received_messages` 错误推导文件大小导致的 0 B；旧发送记录只在安全允许路径内使用 `stat` 补全，并明确区分真实 0 B、未知大小和文件不存在。
- 文件与数据主表移除绝对路径列，补齐结构化文件详情与预览、打开、复制路径操作；数据概览升级为数据库状态及数据库、收件、发送、Agent、备份占用卡片。

## 1.0.0 UI 交互与 Windows 视觉质量专项 - 2026-07-13

- 收件页刷新移至标题右侧，删除最近日志重复刷新；搜索框增加线性图标、清空入口，并支持文件名、完整路径和邮件主题过滤。
- 今日文件表格同屏显示文件名、大小、精简路径、收取时间和操作；路径仅展示约 30%，完整值仍用于悬停提示、复制和打开。增加“打开”“复制路径”真实按钮并保留双击安全预览。
- 收件偏好改为独立编辑对话框，安全默认仍为仅本人邮件，API/IMAP 与手动/自动收件共用配置。
- 新增 `no_changes` 状态并统一 success/no_changes/partial/failed 语义；无新邮件不再计为失败，部分完成使用 WARNING 并保留成功计数。
- 连接健康重构为五项状态面板，逐项展示正常、部分异常、故障、未检查、说明、最近检查时间与定向处理入口。
- 月亮字符图标替换为单色线性主题按钮；统一搜索、刷新、文件、打开、复制和健康图标，以及 Primary/Secondary/Compact 按钮体系。
- Windows 字体固定使用已注册的 Microsoft YaHei UI Regular/Bold，字重收敛到 400/700。主窗口启动时自动扩展到当前屏幕可用高度，底边支持垂直拉伸；收件页采用 900 像素高内容页，今日文件与最近日志各保留至少 220 像素高度，当前屏幕可一次完整显示，较矮窗口才启用页面滚动兜底。左右侧栏适度收窄以加宽中间工作区，文件表格无需横向滚动即可操作，并完成 100%、125%、150% 与深色主题截图 QA。

## 1.0.0 - 2026-07-12

- 信息架构正式收口：顶部仅保留收件、发件；左侧底部仅保留历史记录、文件与数据、设置、关于。
- 已有 Gmail、QQ 账号由账号卡片管理；“添加邮箱账号”改为独立未来扩展 Demo，不再跳转既有账号编辑。
- Gmail API、Gmail IMAP 和 QQ SMTP 保持专属账号配置页；移除 Gmail API“推荐”标签，凭据继续固定掩码、不回显、失败回滚和确认删除。
- Agent / MCP 统一归入发件页，保留 stdio、按需启动、固定收件人、允许目录、request_id、duplicate、audit 和 rate limit。
- 新增真实历史记录页，汇总收件、发件和 Agent / MCP 业务记录，支持类型、状态、时间、关键词、request_id、详情和关联文件定位。
- 新增真实文件与数据页，管理收件文件、发送归档和 Agent 结果，并接入存储概览、备份、验证、恢复、一致性扫描和维护报告。
- 设置成为一级页面，高级设置降为设置二级入口；数据维护归文件与数据，日志管理归收件，MCP 归发件。
- 收件页增加一键检查，聚合当前 Gmail、QQ SMTP、MCP、凭据/OAuth 和 SQLite/数据目录状态，并提供定向处理入口。
- 日志管理增加搜索、级别、时间范围、详情、脱敏诊断导出和日志目录入口；发件记录管理跳转历史记录并自动应用发件筛选。
- 今日文件、最近日志和最近发送记录统一使用内部滚动与最近 100 条有界加载，不使用分页。
- 接入用户提供的 Gmail SVG 与 QQ 邮箱 WebP 标志；表格增加边框、表头底色、行分隔与统一滚动，右侧状态使用统一图标。
- 完成 100%、125%、150% 三档 10 页面 GUI 截图矩阵；同步 README、AGENTS 和 GUI/MCP/安全/安装/发布文档。
- 285 项自动化测试通过；clean build、双 EXE 自检、MCP smoke、安装器、SHA-256、秘密扫描、Defender、本机覆盖安装、桌面快捷方式、托盘与单实例验收通过。

## 2026-07-12 桌面交互与视觉产品化整改（第一部分）

交互可靠性：新增真实仪表盘；收件、诊断、刷新、备份、恢复和导出统一即时反馈、运行态、重复触发拦截及完成恢复；补齐文件选择或导出取消、打开备份目录失败等反馈；文件表格操作文案与真实行为保持一致。

信息架构：仪表盘集中展示服务健康、今日统计、快捷操作和最近活动；基础操作区与数据维护区改为自适应网格；空表统一显示“暂无数据”；账号卡片区分“已配置”和真实连接状态；用户页不再显示内部状态码。

视觉与品牌：接入桌面最终 Logo，保留原始素材，生成透明主图、16/24/32/48/64/128/256 PNG 与多尺寸 ICO，并用于标题栏、窗口、任务栏、托盘和关于页；导航统一使用 Qt 系统图标；修复深色主题中状态文字、统计卡片和文本区对比度。

验证：新增 7 项产品化 GUI 回归，完整测试为 241 项通过。GUI 测试夹具显式隔离 OAuth credentials/token 路径。Windows 原生图形后端在实际 dpr=1.50 环境完成 7 个主要页面截图验收；Qt 离屏模式补充 100%、125%、150% 和深色主题验收。真实 Gmail API 与 QQ SMTP 只读诊断通过，未收取或发送真实邮件。

## 2026-07-11 全局文件、凭据、维护与稳定性专项

阶段 A：GUI 支持选择电脑任意位置的普通文件；确认时计算 SHA-256，发送前创建并验证受控快照；历史增加原文件名、大小、来源和 request_id。CLI 与 MCP 继续执行 DATA_ROOT / ALLOWED_SEND_ROOTS 白名单，不获得 GUI 信任入口。

阶段 B：新增 Windows Credential Manager 统一凭据服务，GUI、CLI、ApplicationService 和诊断共用安全读取；旧 `.env` 成功迁移后清空秘密项，失败时保留；OAuth 文件边界和只读 scope 不变。

阶段 C：新增数据维护页、SQLite 在线备份与 Hash 清单、损坏拒绝、恢复前安全备份、失败回滚、一致性扫描和脱敏维护报告。默认只报告，不自动删除或覆盖附件文件。

阶段 D：新增隔离的大数据量与资源稳定性基准，覆盖查询、组合刷新、内存、线程、句柄、SQLite 完整性、日志轮转和 SMTP 失败恢复。10,000 条收件规模下组合刷新最大 10.259 ms。

兼容性：数据库启动时自动补充 sent_files 的文件名、大小和来源字段；旧 `.env` 仍可作为迁移失败时的兼容回退。未引入新第三方运行依赖，当前仍为 Python 启动方式。

## 2026-07-11 阶段 5.5

交互改进：发件按钮在未选择文件时禁用；显示文件名、大小、类型、修改时间和完整路径提示；增加复制路径、打开文件夹、安全预览和发送前二次确认；确认前后检查文件变化；发送完成后清空本次选择。

异步反馈：耗时操作仅禁用当前按钮，提供执行中、成功和失败视觉状态；统一阻止任务并发和重复点击；手动刷新改为后台执行，显示最后刷新时间；任务完成后自动刷新文件、历史、日志、统计和 MCP 记录。

诊断与配置：四个授权和诊断按钮绑定真实 ApplicationService；增加友好错误映射、脱敏错误详情和脱敏诊断报告导出；增加未保存配置提示、密码临时显示和开机启动回滚。

安全与恢复：不持久化待发送文件；危险文件只定位不执行；当前配置隐藏完整路径并脱敏邮箱；保存窗口几何、上次页面和异常退出标记；诊断报告不覆盖已有文件。

视觉与兼容：优先 Microsoft YaHei UI，启用 Qt 6 PassThrough 高 DPI 比例；提高表单、按钮和表格可读字号；增加紫色主按钮和紫蓝进度条；高级设置按钮改为两行网格，解决 125% 和 150% 缩放文字截断。

测试：新增阶段 5.5 GUI 回归测试，完整测试结果为 213 项通过。Windows 图形后端完成 100% 截图验收，并以 Qt 1.25、1.50 比例检查布局。未改变 Gmail、QQ SMTP、MCP 和 SQLite 核心实现，未新增第三方依赖。

兼容性：仍支持 Python 3.11+ 和 PySide6；保持现有 `.env` 配置键，不要求数据迁移。当前不包含正式 Windows 安装包。
# 0.9.0 - 2026-07-12

- 新增 source/frozen 双模式 Runtime Paths，安装目录与配置、OAuth、数据、缓存彻底分离。
- 新增用户明确选择的旧 `.env` 事务式导入与 OAuth JSON 受控复制；秘密值写入并回读验证后才清空旧文件。
- 修复首次配置向导把 QQ 授权码交给普通配置保存的 P0 风险；普通配置层拒绝写入非空秘密。
- 统一 Python、GUI、MCP、EXE 和安装器版本为 0.9.0。
- 新增 PyInstaller onedir 双入口：用户可见 `AgentMailBridge.exe` 与内部按需 stdio `AgentMailBridgeMCP.exe`。
- 新增 frozen 开机启动命令、安装版 MCP 配置生成、资源发现、EXE 图标和版本元数据。
- 新增 Inno Setup 当前用户安装器，支持覆盖安装、默认保留用户数据的卸载及失效开机启动项清理。
- 新增可重复构建、packaged smoke、真实发送 E2E、秘密排除、portable ZIP、SHA-256 和 Defender 验收流程。
- 251 项自动化测试通过；10,000 条/50 周期稳定性基准通过。
- 最终 Gate 为 CONDITIONALLY PASS：缺真正独立无 Python Sandbox/VM 和跨版本旧候选升级，不建议立即公开发布。
