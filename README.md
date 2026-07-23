# AgentMailBridge

AgentMailBridge v1.4.4 是面向个人用户的本地优先 Windows 邮箱桥接工具。QQ 与 163 已完成真实双向收发、真实自发自收、富 MIME、目录、增量、重启、调度隔离、统一归档和附件 Hash 验收，现为正式支持。163 的 RFC 2971 IMAP ID 差异集中在 Provider Profile；QQ 兼容配置与统一账号凭据保持同步。Generic IMAP/SMTP Core、本地 stdio MCP 的受控邮件读取、资源准备和固定目标结果回邮边界保持不变。

项目不提供多租户、Agent 任意外发、通用 Gmail MCP、遥测或云同步。GUI 用户可为一次手动发件明确填写一个合法收件人；MCP `submit_result` 仍固定受 `OWNER_GMAIL` 控制。邮箱凭据、OAuth、数据库、邮件附件和归档由用户保留在本机。

## Multi-Account Runtime

`MailAccount` 使用 provider 与规范化邮箱地址生成稳定、不含明文地址的 `account_id`，并单独声明认证类型、收件/发件开关、能力和数据命名空间。账号可创建、编辑、启停和保守移除；移除默认保留历史邮件、附件、发件记录与审计，并让用户单独选择是否清理该账号凭据或 OAuth Token。GUI 可添加同 Provider 的多个账号，手工收件、历史补扫、连接测试和 GUI 发件均可明确选择账号。

Account Runtime Router 由 `account_id` 解析 Provider Adapter、运行配置、Credential Manager key 和 OAuth 文件。新账号秘密不扩张为 `GMAIL_1/QQ_2` 环境变量，也不进入 SQLite；Token 位于账号专属目录。调度器按账号保存状态、锁、重试和退避，一个账号失败不会阻断其他账号。v1.3/v1.4 兼容配置仍可幂等映射，迁移不移动归档、不改写 `raw.eml` 或历史 Hash。

当前 Provider 状态：

| Provider | Receive | Send | 认证 | 状态 |
| --- | --- | --- | --- | --- |
| Gmail | Gmail API/IMAP | 未实现 | Desktop OAuth `gmail.readonly` 或应用专用密码 | 收件正式支持；发件 planned，不扩大 scope |
| QQ | IMAP | SMTP | QQ 邮箱授权码 | 正式支持；真实 E2E 与 QQ/163 互发通过 |
| 163 | IMAP | SMTP | 163 邮箱授权码 | 正式支持；真实 E2E 与 QQ/163 互发通过 |
| Generic | 按配置启用 IMAP | 按配置启用 SMTP | 账号级 IMAP/SMTP secret | implementation ready / E2E required；第三方真实服务器 NOT_TESTED |
| Outlook/Microsoft | 未实现 | 未实现 | 未来 MSAL/PKCE/OAuth | planned，不作为密码型 Generic 宣称支持 |

## 统一邮件归档

每封新邮件只创建一个本地归档对象，正文、内嵌图片、普通附件、网页链接和按规则下载的文件都归属于这封邮件，不再成为彼此无关的散落数据。程序同时保留 Gmail API 或 IMAP 实际取得的原始邮件，便于以后重新解析和核验。

正文按纯文本、HTML 和可读版本分层保存。普通链接默认只识别，不访问、不下载；可信网站列表默认为空。用户显式配置可信域后，只有通过 HTTPS、地址与重定向安全检查、大小和 MIME 限制的直接文件才会保存到原邮件目录，下载文件不会被执行或自动解压。

旧邮件会在升级时无损映射到统一归档，但不会伪造历史上没有保留的原始邮件。manifest v2 和数据库同时保存 `account_id`、`mailbox_id` 及 v1.3 兼容引用。邮件、资源和会话事实由只读查询层按账号、邮箱目录、时间、发件人、主题、附件和状态检索，并直接驱动收件箱、邮件详情、会话和关联文件界面。

## 正式版界面

- 顶部主工作区只有“收件”和“发件”。
- 左侧统一“邮箱账号”列表展示真实账号、Provider、地址、能力和启停状态；“添加邮箱账号”可创建 Gmail、QQ、163 与 Generic 账号，动态账号页可测试连接、发现目录、管理按账号凭据/OAuth，并安全移除账号。
- 左侧独立“Agent / MCP”入口与“历史记录”“文件与数据”“设置”“关于”组成贴合的统一导航卡；高级设置是“设置 > 高级设置”的二级页面。
- 历史记录只回答“发生过什么业务行为”，以产品化摘要、中文状态和结构化详情展示收件、发件和 Agent / MCP；文件与数据管理真实收件文件、发送归档、Agent 结果、存储概览、备份、恢复和一致性扫描。
- 收件页不重复显示 Gmail 管理卡；正常窗口完整展示今日文件与最近日志，不出现页面级滚动，较矮窗口才启用滚动兜底。数据较多时表格内部滚动，不使用分页。
- 收件和发件共用顶部唯一“刷新本地页面数据”入口；新配置默认接收“当前扫描范围内全部邮件”。“仅 Gmail 自发自收邮件（高级）”和自定义规则仍可明确选择；自定义规则可按发件人/域名、主题关键词和是否含附件过滤。手动、自动、Gmail API、IMAP 和 MCP 新鲜度同步共用同一业务规则。
- “立即收取”只检查当前增量范围；“历史补扫”可分页重扫最近 24 小时、7 天、30 天或自定义日期，支持取消、进度、当前规则重评、正式归档去重和有限重试。规则跳过只是一次评估，不会永久阻止以后补收。
- 收件结果明确区分成功、无新邮件、部分完成和失败；无新邮件不计入失败或错误。
- “今日收到邮件”每封邮件只显示一条紧凑记录；RFC 2047 联系人显示名会解码，正文摘要最多一至两行，附件、邮件图片、链接和下载数量始终可见。双击整行或点击“查看邮件”进入详情；正文和资源使用可拖动纵向分隔，图片、附件、链接与下载只在非空时分区显示。
- 收件搜索使用邮件事实层，可按主题、发件人、收件人、抄送人、完整可读正文、附件名、邮件图片名、链接文字、域名、URL 和状态查找；同一邮件多个资源命中仍只显示一条。
- 发件页可从已启用且具备正式 send 能力的账号中选择发件身份，并支持一个用户明确输入的合法 To、可选主题、正文、0 至多个附件和 0 至多个链接；发送前展示实际账号与收件人并拒绝空值、非法地址、多地址和 CRLF 注入。MCP 不获得此自由选择能力。
- “文件与数据”以 `mail_resources` 作为新收件资源权威来源，并保留未映射旧数据兼容；表格显示所属邮件，可从文件进入邮件详情，也可从邮件详情定位附件。未知大小、真实 0 字节和文件不存在分别显示。
- 右侧连接健康以五个独立状态项展示 Gmail、QQ SMTP、Agent/MCP、凭据/OAuth 和 SQLite/数据目录，并提供定向处理入口。
- v1.2.0 的收件、发件资源和最近调用表格使用统一整行视觉，浅色/深色 Hover 不闪白、不出现竖向单元格分割或选择分块，同时保持 Windows 中文 UI、线性图标和 100%/125%/150% DPI 适配。

## Windows 安装

运行 `AgentMailBridge-1.4.4-Setup.exe`。默认安装到 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需 Python、Git 或管理员权限。桌面和开始菜单只指向 `AgentMailBridge.exe`；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式、托盘或开机启动项。

安装版数据位置：

- 配置：`%LOCALAPPDATA%\AgentMailBridge\Config\.env`
- OAuth：`%LOCALAPPDATA%\AgentMailBridge\OAuth`
- 数据、SQLite、日志和归档：`%LOCALAPPDATA%\AgentMailBridge\Data`
- 缓存：`%LOCALAPPDATA%\AgentMailBridge\Cache`

覆盖升级和普通卸载不会静默删除配置、OAuth、Credential Manager 凭据或用户数据。

## 邮箱配置

Gmail API 与 Gmail IMAP 使用互斥的条件配置页。Gmail API 页负责选择并验证 `credentials.json`、导入受控 OAuth 目录、授权和连接测试；Gmail IMAP 页只管理 Google 生成的应用专用密码。QQ 与 163 使用邮箱服务生成的授权码，同时保存到该账号隔离的 IMAP/SMTP 凭据槽；Generic 可分别配置 IMAP 与 SMTP 凭据。界面从不回显旧秘密。

兼容账号继续读取原有 Gmail/QQ Credential Manager key；新账号使用不含邮箱明文的 `account:<account_id>:<secret-kind>` key。界面不回显旧值。Gmail OAuth credentials 与 Token 按 `account_id` 隔离，清除或重新授权一个账号不影响其他账号；scope 仍严格且唯一为 `gmail.readonly`。

Gmail API 首次配置必须使用 Google Cloud 创建的 Desktop app `credentials.json`，Web application JSON 会被明确拒绝。OAuth 在后台 Worker 中运行，本地回调只绑定 `127.0.0.1` 随机端口，默认 5 分钟超时；等待期间 GUI 保持响应，可取消、重新打开浏览器或复制同一授权链接。Google OAuth 应用“已发布”不等于“已通过 Google 验证”，安全警告只能由用户在浏览器中自行决定是否继续。

Token 仅在 state 校验成功后交换，并通过同目录临时文件、落盘和原子替换保存。Token 交换成功但 Gmail API 暂时不可用时，会保留已取得的长期授权并允许“重新验证 Gmail API”，无需重复打开浏览器。请确保 `127.0.0.1`、`localhost` 和 `::1` 不经过代理，并在 Google Cloud 项目中启用 Gmail API。详细错误可按结构化错误码排查，见 `docs/Gmail OAuth配置与故障排查说明.md`。

## Agent / MCP

从左侧进入“Agent / MCP”。页面提供邮件读取总开关、同步状态、单一通用 MCP 配置、两个简短示例、授权工作区和统一最近调用。读取开关默认关闭；用户启用一次后，本机兼容 MCP 的 Agent 可直接搜索本地归档，不需要逐封分享、打开 GUI、复制 AppData 路径或重新上传附件。MCP 按需启动，stdin 关闭后退出，提供七个工具：

- `search_mails`：按最新、今天、昨天、最近若干天或日期范围搜索，支持账号、主题、发件人、收件人、附件和状态过滤；省略账号时查询全部已归档账号。
- `get_mail`：分页读取邮件元数据、完整可读正文和资源清单。
- `read_mail_resource`：严格按邮件归属分页读取文本、CSV/TSV、图片元数据和真实 raw.eml，二进制只返回安全描述。
- `prepare_mail_resources`：把指定资源原子复制到授权工作区，校验大小和 SHA-256，不执行、不解压。
- `list_agent_workspaces`：列出明确授权的工作区。
- `get_mail_sync_status`：读取指定或当前账号的后台同步、新鲜度、重试和跨进程互斥状态。
- `submit_result`：保持向后兼容，把 Agent 最终结果发送到固定 Gmail。

`submit_result` 结构仍为：

```json
{
  "file_path": "C:\\允许目录\\report.md",
  "title": "可选标题",
  "request_id": "stable-request-001"
}
```

邮件读取只能访问 `DATA_ROOT` 内的规范归档，不能读取凭据或任意文件系统路径；资源准备只能写入用户明确授权的工作区。MCP/Agent 结果收件人固定为 `OWNER_GMAIL`，提交文件必须位于 `DATA_ROOT` 或授权工作区。GUI 手动收件人和全局文件选择都不会扩大 Agent 信任范围。

Windows MCP stdin、stdout 和 stderr 明确使用 UTF-8，首条请求可兼容 UTF-8 BOM；stdout 只输出逐行 JSON-RPC 并在每条响应后 flush。中文目录、中文文件名、空格路径和中文标题可直接提交，不需要 Agent 修改 code page 或手工执行 Copy-Item。

`submit_result` 会先验证源路径仍在允许目录，再原子复制到产品受控 staging，并校验 source、staged、SMTP 附件来源与 sent 归档的大小和 SHA-256。读取、准备、同步和发送统一写入 `mcp_audit_events`，旧 `mcp_calls` 保持兼容；审计不保存完整邮件正文、附件内容或秘密。

## 自动收件可靠性

自动收件默认每 60 秒检查一次，最低可设为 30 秒；开启或应用启动后约 3 秒执行首次检查。Gmail API 使用约 30 分钟重叠回看并分页到安全扫描上限。共享 Generic IMAP Core 使用 mailbox 级 UIDVALIDITY/UIDNEXT/last_uid checkpoint、少量 UID 重叠扫描和有界批量 `BODY.PEEK[]`；Message-ID 与数据库唯一约束继续承担正式归档去重。

检查间隔决定“多久检查一次”，Gmail lookback 或 IMAP UID overlap 决定“每次增量向前重查多少”，两者不是同一概念。改变检查间隔不会扩大历史范围；更早邮件应使用历史补扫。历史补扫直接查询指定日期范围，按账号、Message-ID 与 provider id 去重，不删除邮件、不执行附件；Gmail API 保持只读，IMAP 使用 `BODY.PEEK[]` 不误标已读。UIDVALIDITY 变化会安全重置该 mailbox 的 UID 游标并清理旧 UID 代际的技术重试，再依靠归档去重重扫，避免把旧 UID 的失败错误套到新邮件。

调度状态按账号持久化保存上次检查、上次成功、最近结果、下次检查、连续失败和 checkpoint，并保留 v1.3 全局状态兼容快照。GUI 自动收件轮询所有到期账号；每个账号使用独立线程锁和进程锁，认证、网络或后端整体失败按 30 秒、1 分钟、2 分钟、5 分钟、最长 15 分钟独立退避，`no_changes`、成功或部分完成恢复正常周期。窗口进入托盘时调度继续，只有真正退出才停止。

单邮件处理失败按 1 分钟、5 分钟、30 分钟、2 小时有限重试，继续失败后标记为“需要处理”；到期重试即使已经离开重叠回看窗口，也会按 Gmail 资源 ID 或 IMAP UID 单独发现，不会每分钟污染后续轮询。无新邮件始终是健康的 `no_changes`。收件页展示真实运行状态、上次检查、上次成功、下次检查、最近结果和待重试数量；“立即收取”与自动任务共用同一互斥服务。

正常自动检查和无新邮件结果只更新调度状态，不再永久写入技术事件。SQLite 技术日志默认普通记录保留 30 天、WARNING/ERROR 保留 90 天，最多 10,000 条，超限后批量降到约 8,000 条；启动、每 24 小时和用户手动操作可触发清理。文件日志仍独立按约 2 MB、5 个备份轮转。日志管理支持概览、级别/事件类型/时间/关键词组合筛选、日常检查开关、分页加载、当前筛选脱敏导出、保留设置和安全清理；这些操作不会删除邮件、附件、收发历史或 MCP 审计。

## 开发与构建

需要 Python 3.11 或更高版本：

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python -m agent_mail_bridge --version
python -m agent_mail_bridge.gui
python -m agent_mail_bridge.mcp_server
```

Windows 构建：

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

第一次完整 pytest 前应先运行 `python scripts\full_suite_preflight.py`。它检查版本、Provider 状态、schema、已知硬编码断言、`git diff --check`、compileall，并执行 Provider、Generic、多账号、GUI 与包装定向回归。真实 Provider 验收使用已配置账号的 Credential Manager 凭据；脚本不接受命令行密码，无 `--confirm-network` 不联网，无 `--confirm-real-send` 不发信：

```powershell
python scripts\provider_validation.py --account-id <account_id> --confirm-network --output evidence.json
python scripts\provider_interop_validation.py --from-account-id <account_id> --to-account-id <account_id> --confirm-network --confirm-real-send --output interop.json
python scripts\provider_mime_receive_validation.py --from-account-id <account_id> --to-account-id <account_id> --confirm-network --confirm-real-send --output mime.json
```

构建流程会先执行 Preflight，再运行完整 pytest、构建 GUI 与内部 MCP、执行 packaged smoke 和秘密排除扫描，并生成：

- `release\AgentMailBridge-1.4.4-Setup.exe`
- `release\AgentMailBridge-1.4.4-Windows-x64.zip`
- `release\checksums.sha256`

详细说明见 `docs/GUI使用说明.md`、`docs/Gmail OAuth配置与故障排查说明.md`、`docs/MCP使用说明.md`、`docs/Agent邮件读取与资源交付设计.md`、`docs/MCP邮件读取工具说明.md`、`docs/安全与诊断说明.md`、`docs/Windows安装与升级说明.md`、`docs/统一邮件归档设计.md`、`docs/邮件事实查询说明.md` 和最终专项报告。
