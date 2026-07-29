# Agent 发件与确认发送说明

AgentMailBridge v1.7.0 通过统一 `send_mail` 支持新建、回复、回复全部和转发。发件账号必须已启用、具备 Provider send 能力并在当前 Client 范围内；收件人可以是任意合法邮箱地址，支持多 To、Cc、Bcc。地址、显示名、主题和附件名拒绝 CRLF/Header 注入。

发件模式只有两种。

- 发送前确认：MCP 只创建持久化 `pending_confirmation`，不连接 SMTP、不建立发送成功事实。GUI 展示 Agent、发件账号、操作类型、原邮件、To/Cc/Bcc、完整正文、附件来源、创建时间和有效期。只有 GUI 用户可确认或取消，MCP 没有 confirm 工具。
- Agent 自主发送：用户为 Client 明确开启后，`send_mail` 通过全部权限校验即执行真实发送。AgentMailBridge 不判断自然语言意图，也不自动回复、定时发送或建立草稿。

确认时重新解析 Client token，检查 active/paused/revoked、`mail.send`、发件账号、源邮件读取范围、附件目录、到期时间、文件大小和 SHA-256。同一 Client 与 idempotency key 只执行一次；重复确认返回既有结果。Provider 结果不确定时记录 `delivery_unknown`，不会自动重发。

回复优先 Reply-To，否则使用 From；回复全部排除当前发件身份并对 To/Cc 去重，不推断 Bcc。转发只附加请求明确选择的原附件，并可混合使用已授权本地附件。兼容 `submit_result` 保持固定 `OWNER_GMAIL`，不会扩张或继承通用发件权限。
