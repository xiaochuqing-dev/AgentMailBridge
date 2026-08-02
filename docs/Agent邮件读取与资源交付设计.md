# Agent 邮件读取与资源交付设计

v1.7.2 允许读取用户授权的任意账号、邮箱目录和历史时间，并保持独立的通用 Agent 发件权限。QQ、163 与 Gmail 继续共用统一 account_id、mailbox membership、package、resource、raw.eml 和 Hash 事实；同一邮件属于多个目录时仍只返回一个正式 package。没有 Provider 专用或智能判断工具。兼容 `submit_result` 的固定收件人语义不扩张到通用 `send_mail`。

## 产品边界

AgentMailBridge 在同一个 MCP 中使用稳定 Client 身份和账号归属。动态 `all` 范围在每次会话解析当前及以后新增账号/资料目录；`selected` 只使用保存的显式 ID。`ensure_fresh` 还需要独立 capability，并通过 Account Runtime Router 同步指定账号，不替代 GUI“导入历史邮件”。

## 数据流

Provider Adapter 复用现有 Gmail API/IMAP 收件实现，先完成带 `account_id/mailbox_id` 的 raw、正文、资源和 manifest 原子 package。Agent 调用 `search_mails` 获取稳定 mail_id，再用 `get_mail` 读取有界正文和资源清单。文本与 CSV 可用 `read_mail_resource` 分页读取；指定资源继续使用兼容准备模式，整封邮件使用同一工具的 `mode=complete` 复制到授权资料目录。任务结果仍通过兼容的 `submit_result` 回邮。

## 授权模型

`MCP_MAIL_READ_ENABLED` 保留为全局 Agent 邮件读取总开关，默认 false。其后依次校验 Client token、active 状态、capability、account 范围、资料目录范围，再执行 DATA_ROOT、ownership、路径、大小和 Hash 校验。旧匿名配置不获得隐式权限。Client token 只代表 AgentMailBridge 内的有限权限，不改变 Gmail `gmail.readonly` scope，也不能读取邮箱凭据。

邮件读取始终以 `DATA_ROOT` 为硬边界。数据库 package_root 和每个资源路径在访问时重新解析，必须位于规范 package 内；资源 ID 必须属于指定邮件，已有 SHA-256 必须匹配。路径事实被篡改、资源缺失或 Hash 不一致都会拒绝。

## 有界读取

正文、文本附件和 raw.eml 按字符 offset/max_chars 分页，单页最多 50,000 字符。编码检测优先 UTF BOM 与严格 UTF-8，再检查 GB18030、GBK、Big5；二进制探测失败时不会用替换字符伪装文本。CSV/TSV 使用流式 csv 解析，支持引号内换行，返回列名、总行数、row_offset、最多 100 行和截断状态。

图片只读取文件头，返回 PNG、JPEG、WebP、GIF 或 BMP 的格式和尺寸。PDF、DOCX、XLSX、PPTX、ZIP、EXE 与未知二进制返回类型、MIME、大小、Hash 和能力描述；桥接器不执行、不解压、不渲染宏。普通链接只返回已归档 URL 事实，不自动访问网页。

## 受控资源与完整邮件资料准备

Agent 可用资料目录来自 `ALLOWED_SEND_ROOTS`，每个路径有稳定 workspace_id。目标固定在 `<允许目录>/.agentmailbridge/mail/<mail-id>/`，可增加安全相对子目录。目录逐级解析，现有符号链接、目录联接、绝对路径和 `..` 不能逃逸。复制采用同目录临时文件与原子替换；源事实 Hash、实时源 Hash、目标大小和目标 Hash 必须闭合。默认同名目录自动安全重命名，也可选择 error 或 overwrite。

兼容资源模式继续生成 UTF-8 `邮件说明.md`。完整模式生成 `邮件正文.md`、`原始邮件.eml`、`邮件信息.json`、`原始归档manifest.json`、`完整资料manifest.json`、`附件`、`邮件内图片` 和 `下载文件`。邮件信息包含账号归属、发件人、收件人、时间、主题、package_id；manifest 逐项保存 resource_id、来源关系、相对路径、大小和 SHA-256。所有文件先写入短名 staging，完整复核后一次重命名发布；失败清理 staging，内部 package 在前后 Hash 快照中必须不变。

## 同步与并发

`ensure_fresh` 先查询目标账号的持久化调度状态，只在数据过期时触发收件。兼容默认调用保留 `receive.lock`；明确账号调用使用 `receive-<account_id>.lock`，进程内也按账号互斥。一个账号的连接错误、认证失败或退避不会阻塞其他账号；单邮件重试继续携带账号 ownership。

## 审计与兼容

`mcp_audit_events` 统一审计 search、get、read、prepare、workspace、sync 和 send，并与旧 `mcp_calls` 合并查询。正文全文、附件内容、凭据和 OAuth 不进入审计。`submit_result` 的输入、幂等 request_id、`OWNER_GMAIL` 固定目标、白名单、原子 staging 和四段 Hash 链保持兼容；GUI 用户手动填写 To 不会扩大 MCP 权限。

远程 MCP、任意收件人、邮箱修改、普通网页抓取、附件执行和 Agent 自动挑选“相关邮件”均不在 v1.6.0 范围内。
