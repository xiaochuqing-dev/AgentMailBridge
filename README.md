# AgentMailBridge

AgentMailBridge v1.1.0 是面向个人用户的本地优先 Windows 邮箱桥接工具。它通过 Gmail 收取完整邮件，通过 QQ SMTP 将正文、链接和多个附件作为一封邮件发送到固定 Gmail，并向 Codex、Claude Code 等本地 Agent 提供受控的 stdio MCP 交付能力。

项目不提供多租户、任意收件人、通用 Gmail MCP、遥测或云同步。邮箱凭据、OAuth、数据库、邮件附件和归档由用户保留在本机。

## 统一邮件归档

每封新邮件只创建一个本地归档对象，正文、内嵌图片、普通附件、网页链接和按规则下载的文件都归属于这封邮件，不再成为彼此无关的散落数据。程序同时保留 Gmail API 或 IMAP 实际取得的原始邮件，便于以后重新解析和核验。

正文按纯文本、HTML 和可读版本分层保存。普通链接默认只识别，不访问、不下载；可信网站列表默认为空。用户显式配置可信域后，只有通过 HTTPS、地址与重定向安全检查、大小和 MIME 限制的直接文件才会保存到原邮件目录，下载文件不会被执行或自动解压。

旧邮件会在升级时无损映射到统一归档，但不会伪造历史上没有保留的原始邮件。邮件、资源和会话事实由只读查询层按账号、邮箱目录、时间、发件人、主题、附件和状态检索，并直接驱动收件箱、邮件详情、会话和关联文件界面。

## 正式版界面

- 顶部主工作区只有“收件”和“发件”。
- 左侧已有 Gmail、QQ 账号卡片用于管理当前账号；“添加邮箱账号”仅展示未来扩展说明，不新增第二个同类型账号。
- 左侧底部只有“历史记录”“文件与数据”“设置”“关于”。
- Agent / MCP 位于发件页标题区；高级设置是“设置 > 高级设置”的二级页面。
- 历史记录只回答“发生过什么业务行为”，以产品化摘要、中文状态和结构化详情展示收件、发件和 Agent / MCP；文件与数据管理真实收件文件、发送归档、Agent 结果、存储概览、备份、恢复和一致性扫描。
- 收件页不重复显示 Gmail 管理卡；正常窗口完整展示今日文件与最近日志，不出现页面级滚动，较矮窗口才启用滚动兜底。数据较多时表格内部滚动，不使用分页。
- 收件页标题右侧提供唯一刷新入口；“当前收件偏好”支持“仅本人邮件”“当前扫描范围内全部邮件”和“自定义规则”。自定义规则可按发件人/域名、主题关键词和是否含附件过滤，分类之间为 AND、分类内部为 OR；手动与自动收件、Gmail API 与 IMAP 共用同一业务规则。
- 收件结果明确区分成功、无新邮件、部分完成和失败；无新邮件不计入失败或错误。
- “今日收到邮件”每封邮件只显示一条紧凑记录；正文摘要最多一至两行，附件、邮件图片、链接和下载数量始终可见。双击整行或点击“查看邮件”进入详情查看完整正文和资源。
- 收件搜索使用邮件事实层，可按主题、发件人、收件人、抄送人、完整可读正文、附件名、邮件图片名、链接文字、域名、URL 和状态查找；同一邮件多个资源命中仍只显示一条。
- 发件页支持可选主题、正文、0 至多个附件和 0 至多个链接；一次确认只生成并发送一封 MIME 邮件，最近发送与历史记录也按邮件展示。
- “文件与数据”以 `mail_resources` 作为新收件资源权威来源，并保留未映射旧数据兼容；表格显示所属邮件，可从文件进入邮件详情，也可从邮件详情定位附件。未知大小、真实 0 字节和文件不存在分别显示。
- 右侧连接健康以五个独立状态项展示 Gmail、QQ SMTP、Agent/MCP、凭据/OAuth 和 SQLite/数据目录，并提供定向处理入口。
- v1.1.0 的收件和最近发送表格使用统一整行视觉，浅色/深色 Hover 不闪白、不出现竖向单元格分割，同时保持 Windows 中文 UI、线性图标和 100%/125%/150% DPI 适配。

## Windows 安装

运行 `AgentMailBridge-1.1.0-Setup.exe`。默认安装到 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需 Python、Git 或管理员权限。桌面和开始菜单只指向 `AgentMailBridge.exe`；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式、托盘或开机启动项。

安装版数据位置：

- 配置：`%LOCALAPPDATA%\AgentMailBridge\Config\.env`
- OAuth：`%LOCALAPPDATA%\AgentMailBridge\OAuth`
- 数据、SQLite、日志和归档：`%LOCALAPPDATA%\AgentMailBridge\Data`
- 缓存：`%LOCALAPPDATA%\AgentMailBridge\Cache`

覆盖升级和普通卸载不会静默删除配置、OAuth、Credential Manager 凭据或用户数据。

## 邮箱配置

Gmail API 与 Gmail IMAP 使用互斥的条件配置页。Gmail API 页负责选择并验证 `credentials.json`、导入受控 OAuth 目录、授权和连接测试；Gmail IMAP 页只管理 Google 生成的应用专用密码。QQ 账号页管理 QQ 地址、SMTP 授权码和连接测试。

Gmail IMAP 密码和 QQ SMTP 授权码保存在 Windows Credential Manager。界面只显示固定掩码和配置状态，不回显旧值。Gmail OAuth scope 固定为 `gmail.readonly`。

## Agent / MCP

在发件页标题区进入“Agent 发件 / MCP”。页面提供可直接复制的完整交付指令和工作区授权管理；指令不要求用户填写最终文件路径，Agent 应自行识别本轮最终交付文件并直接提交原始路径。MCP 按需启动，stdin 关闭后退出，只提供 `submit_result`：

```json
{
  "file_path": "C:\\允许目录\\report.md",
  "title": "可选标题",
  "request_id": "stable-request-001"
}
```

收件人固定为 `OWNER_GMAIL`，文件必须位于 `DATA_ROOT` 或用户明确授权的工作区。授权只在下一次 Agent/MCP 进程启动时生效，并拒绝磁盘根目录、用户主目录、Windows、Program Files、ProgramData、AppData、产品数据目录和秘密文件。GUI 手动选择全局文件不会扩大 Agent 信任范围。

Windows MCP stdin、stdout 和 stderr 明确使用 UTF-8，首条请求可兼容 UTF-8 BOM；stdout 只输出逐行 JSON-RPC 并在每条响应后 flush。中文目录、中文文件名、空格路径和中文标题可直接提交，不需要 Agent 修改 code page 或手工执行 Copy-Item。

`submit_result` 会先验证源路径仍在允许目录，再原子复制到产品受控 staging，并校验 source、staged、SMTP 附件来源与 sent 归档的大小和 SHA-256。结果返回文件名、字节数和完整 Hash 链，`mcp_calls` 保存 staging 状态与失败原因；安全白名单和固定收件人不变。

## 自动收件可靠性

自动收件默认每 60 秒检查一次，最低可设为 30 秒；开启或应用启动后约 3 秒执行首次检查。Gmail API 使用约 30 分钟重叠回看并分页到安全扫描上限，IMAP 使用同期 SINCE 回看，两个后端继续依靠 Message-ID 与数据库唯一约束去重。

调度状态持久化保存上次检查、上次成功、最近结果、下次检查、连续全局失败和 checkpoint。认证、网络或后端整体失败按 30 秒、1 分钟、2 分钟、5 分钟、最长 15 分钟退避，成功后恢复正常周期；睡眠或长暂停后由超时看门狗立即补偿。窗口进入托盘时调度继续，只有真正退出才停止。

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

流程会清理旧构建、运行 pytest、构建 GUI 与内部 MCP、执行 packaged smoke 和秘密排除扫描，并生成：

- `release\AgentMailBridge-1.1.0-Setup.exe`
- `release\AgentMailBridge-1.1.0-Windows-x64.zip`
- `release\checksums.sha256`

详细说明见 `docs/GUI使用说明.md`、`docs/MCP使用说明.md`、`docs/安全与诊断说明.md`、`docs/Windows安装与升级说明.md`、`docs/统一邮件归档设计.md`、`docs/邮件事实查询说明.md` 和最终专项报告。
