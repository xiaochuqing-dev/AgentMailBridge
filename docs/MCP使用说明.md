# AgentMailBridge MCP 使用说明

AgentMailBridge MCP 是发件能力中的内部按需 stdio 接口，不监听端口、不常驻、不创建快捷方式。Agent 客户端打开会话时启动，stdin 关闭后自动退出。

在 GUI“发件 > Agent 发件 / MCP”中查看状态、允许目录、固定收件人、最近调用记录、自检，并复制 Codex、Claude Code 或通用 JSON 配置。

安装版命令为安装目录内的 `AgentMailBridgeMCP.exe`；源码开发版命令为：

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

唯一工具为 `submit_result`：

```json
{
  "file_path": "C:\\允许目录\\report.md",
  "title": "可选标题",
  "request_id": "stable-request-001"
}
```

`request_id` 用于幂等重试；重复成功请求返回 duplicate。每个 stdio 进程限制一分钟五次提交。

安全边界：收件人固定为 `OWNER_GMAIL`；文件必须位于 `DATA_ROOT` 或 `ALLOWED_SEND_ROOTS`；GUI 全局文件选择不会扩大 MCP 白名单；MCP 不能读取凭据、OAuth、邮件正文、任意目录或修改邮箱设置；stdout 只输出 JSON-RPC。
