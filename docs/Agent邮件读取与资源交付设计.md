# Agent 邮件读取与资源交付设计

## 产品边界

AgentMailBridge v1.2.0 让本机兼容 stdio MCP 的 Agent 直接读取已由后台收件形成的规范归档。GUI 用于一次性授权、工作区配置和审计观察，不参与每次读件。功能仍是本地单用户邮箱桥接，不扩展为通用邮件客户端、SaaS、Agent 编排或知识管理系统。

## 数据流

后台 Gmail API 或 IMAP 收件先完成 raw、正文、资源和 manifest 的原子 package。Agent 调用 `search_mails` 获取稳定 mail_id，再用 `get_mail` 读取有界正文和资源清单。文本与 CSV 可用 `read_mail_resource` 分页读取；图片和文档先获得安全描述，需要 Agent 自身能力处理时由 `prepare_mail_resources` 复制到授权项目工作区。任务结果仍通过兼容的 `submit_result` 回邮。

## 授权模型

`MCP_MAIL_READ_ENABLED` 是全局一次性 opt-in，默认 false，范围是能启动本机 MCP 配置的进程。它不是逐封分享，不创建 token，也不改变 Gmail `gmail.readonly` scope。关闭读取时，搜索、正文、资源和准备返回 `read_access_disabled`；同步状态与 `submit_result` 保持可用。

邮件读取始终以 `DATA_ROOT` 为硬边界。数据库 package_root 和每个资源路径在访问时重新解析，必须位于规范 package 内；资源 ID 必须属于指定邮件，已有 SHA-256 必须匹配。路径事实被篡改、资源缺失或 Hash 不一致都会拒绝。

## 有界读取

正文、文本附件和 raw.eml 按字符 offset/max_chars 分页，单页最多 50,000 字符。编码检测优先 UTF BOM 与严格 UTF-8，再检查 GB18030、GBK、Big5；二进制探测失败时不会用替换字符伪装文本。CSV/TSV 使用流式 csv 解析，支持引号内换行，返回列名、总行数、row_offset、最多 100 行和截断状态。

图片只读取文件头，返回 PNG、JPEG、WebP、GIF 或 BMP 的格式和尺寸。PDF、DOCX、XLSX、PPTX、ZIP、EXE 与未知二进制返回类型、MIME、大小、Hash 和能力描述；桥接器不执行、不解压、不渲染宏。普通链接只返回已归档 URL 事实，不自动访问网页。

## 受控资源准备

工作区来自 `ALLOWED_SEND_ROOTS`，每个路径有稳定 workspace_id。目标固定在 `<workspace>/.agentmailbridge/mail/<mail-id>/`，可增加安全相对子目录。目录逐级解析，现有符号链接、目录联接、绝对路径和 `..` 不能逃逸。复制采用同目录临时文件与原子替换；源事实 Hash、实时源 Hash、目标大小和目标 Hash 必须闭合。默认同名文件自动安全重命名，也可选择 error 或 overwrite。

每次准备生成 UTF-8 `邮件说明.md`，包含必要邮件摘要和已准备资源 Hash。准备不会修改正式邮件 package，也不会让工作区反向成为邮件事实源。

## 同步与并发

`ensure_fresh` 先查询持久化调度状态，只在数据超过 30 至 600 秒可配置阈值时触发收件。GUI 手动收件、自动调度和所有 Agent 使用同一个 `DATA_ROOT/.locks/receive.lock` 字节锁；锁由操作系统持有，进程崩溃后自动释放。连接级失败、单邮件重试、no_changes 和 partial 保持原有语义。

## 审计与兼容

`mcp_audit_events` 统一审计 search、get、read、prepare、workspace、sync 和 send，并与旧 `mcp_calls` 合并查询。正文全文、附件内容、凭据和 OAuth 不进入审计。`submit_result` 的输入、幂等 request_id、固定收件人、白名单、原子 staging 和四段 Hash 链保持兼容。

Route B 多邮箱、远程 MCP、任意收件人、邮箱修改、普通网页抓取和附件执行均不在 v1.2.0 范围内。
