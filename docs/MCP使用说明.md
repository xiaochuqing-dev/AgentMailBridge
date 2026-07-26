# AgentMailBridge MCP 使用说明

AgentMailBridge v1.5.0 MCP 是本机按需启动的统一 stdio 服务。它不按 Provider 或 Client 拆分，不监听端口、不常驻、不创建快捷方式或托盘；stdin 关闭后服务退出。七工具、`submit_result` 参数、只读 Gmail scope 和固定 owner 安全边界保持兼容。Gmail、QQ、163 与 Generic 的本地归档都由同一个服务按受授权的 `account_id` 查询。

从左侧“Agent 接入”创建 Client 并选择 capability、邮箱账号和工作区。Managed Setup 会显示隐藏 scoped token 的预览，确认后备份并幂等合并外部配置；Assisted/Manual Setup 可复制 Client 专属 JSON 或 TOML。下面的匿名结构只说明 command 形态，不具备 v1.5.0 调用身份，不能直接连接：

```json
{
  "mcpServers": {
    "agent-mail-bridge": {
      "command": "python",
      "args": ["-m", "agent_mail_bridge.mcp_server"]
    }
  }
}
```

每个有效配置还必须通过 env 传入 `AGENT_MAIL_BRIDGE_CLIENT_ID` 与 `AGENT_MAIL_BRIDGE_CLIENT_TOKEN`。后者只是可独立撤销的 AgentMailBridge scoped token，不是邮箱凭据；管理副本位于 Windows Credential Manager，SQLite 只保存 Hash。匿名、未知、token 错误、暂停或撤销 Client 默认拒绝。

邮件读取默认关闭。调用依次通过全局总开关、Client 身份、Client 状态、capability、账号、工作区和既有资源安全校验。关闭全局读取不会扩大任何 Client 权限。服务不会获取 Gmail 修改权限，不提供删除、移动、标记、任意收件人或凭据读取。

七个工具：

- `search_mails`：按 latest、today、yesterday、recent_days、date_range 或 all 搜索，支持可选 `account_id`、兼容 `account_ref`、query、主题、解码后的联系人显示名/地址、收件人、有无附件、状态、排序、分页及 `ensure_fresh`。省略账号时查询全部本地归档账号。
- `get_mail`：按稳定 mail_id/package_id 返回兼容旧字段，并增加 `from_display`、`from_address`、`to_addresses`、`cc_addresses`、`bcc_addresses`、`reply_to` 和独立 raw Header；正文仍用 offset/max_chars 有界分页。
- `read_mail_resource`：验证资源属于邮件后读取。text/preview 处理严格编码文本，csv_preview 返回列名和行范围，raw 只用于真实 raw.eml；图片返回格式与尺寸，二进制只返回描述。
- `prepare_mail_resources`：把指定资源复制到授权工作区的 `.agentmailbridge/mail/<mail-id>/`，校验大小与 SHA-256，不执行、不解压。
- `list_agent_workspaces`：列出授权工作区的稳定 ID、完整路径、可用和默认状态。
- `get_mail_sync_status`：可选 `account_id`，返回该账号或当前兼容账号的自动收件、上次检查/成功、下次检查、重试、新鲜度、独立进程锁状态及已启用账号摘要。
- `submit_result`：向固定 `OWNER_GMAIL` 提交 Agent 结果，保持 v1.1 及更早客户端兼容。

身份与权限的稳定拒绝包括 `agent_access_disabled`、`unknown_client`、`client_disabled`、`client_revoked`、`client_auth_failed`、`capability_denied`、`account_denied` 和 `workspace_denied`。资源拒绝还包括 `mail_not_found`、`resource_not_found`、`resource_not_local`、`unsupported_resource_type`、`binary_resource`、`invalid_range`、`sync_in_progress` 和 `hash_mismatch`。错误通过正常 tools/call 结果中的 `structuredContent` 返回，协议连接不应因业务拒绝中断。

`ensure_fresh=true` 只在本地数据过期时触发受控增量同步，并与 GUI 立即收取、自动调度共用同一收件规则和跨进程锁；它不是历史补扫入口。其他进程正在收件时返回 `sync_in_progress`。同步失败且允许缓存时，搜索明确返回 cached 与 sync_error；不得把旧数据伪装为新鲜。

`account_id` 是稳定的不透明标识。Client 只授权一个账号时可安全省略并自动收窄；其 allowlist 等于全部启用账号时可使用统一视图；其他多账号组合必须显式指定账号。`search_mails` 指定账号并请求 `ensure_fresh` 时，只同步该账号，账号锁、凭据、OAuth、重试和数据归属不会跨账号。

资源路径必须来自当前邮件 package 并位于 `DATA_ROOT`。工作区准备只能写入 `ALLOWED_SEND_ROOTS`，逐级拒绝路径逃逸、符号链接和 Windows 目录联接。GUI 曾选择任意文件不会扩大 MCP 信任。文本读取最多 50,000 字符一页，CSV 最多 100 行一页；服务不执行附件、宏、脚本、压缩包或网页链接。

`submit_result` 输入保持不变：

```json
{
  "file_path": "C:\\允许目录\\report.md",
  "title": "可选标题",
  "request_id": "stable-request-001"
}
```

request_id 用于幂等重试。程序验证白名单后原子 staging，并核对 source、staged、SMTP 附件来源和 sent 归档的大小与 SHA-256；SMTP 已接受但归档失败时返回部分完成，不能盲目重发。Agent 不应使用 Copy-Item、cp 或另存副本规避白名单。

GUI 手动发件可由用户输入一个合法 To，但该能力不进入 MCP schema。`submit_result` 不接受 recipient 字段，始终使用配置的 `OWNER_GMAIL`；GUI 的收件人或全局文件选择不会扩大 Agent 权限。

所有工具调用写入统一 `mcp_audit_events`，旧发送 `mcp_calls` 继续兼容。审计记录 Client ID/type/显示名快照、工具、capability、账号、工作区、状态、拒绝码、correlation id、计数、耗时和必要 Hash，不保存完整正文、附件内容、Client token、邮箱秘密或自然语言对话。GUI 可按 Client、工具和结果筛选。

Windows stdin、stdout、stderr 明确使用 UTF-8，兼容首条 BOM；stdout 只写逐行 JSON-RPC，每条响应立即 flush，EOF 后正常退出。发布前必须用真实 packaged MCP 验证 initialize、tools/list、七个 tools/call、错误输入、中文路径、Hash 和 EOF，不能只用 mock。
