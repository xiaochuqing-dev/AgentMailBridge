# AgentMailBridge MCP 使用说明

AgentMailBridge MCP 是产品内部按需启动的本机 stdio 接口，不监听端口、不常驻、不创建快捷方式。Agent 客户端打开会话时启动进程，stdin 关闭后进程自动退出。

## 安装版

最终命令为安装目录内的：

`AgentMailBridgeMCP.exe`

路径可能包含空格、中文用户名或中文目录，必须作为一个完整 command 参数。推荐直接在 GUI“Agent 接口”页面复制配置。通用 JSON：

```json
{
  "mcpServers": {
    "agent-mail-bridge": {
      "command": "C:\\Users\\<用户>\\AppData\\Local\\Programs\\AgentMailBridge\\AgentMailBridgeMCP.exe",
      "args": []
    }
  }
}
```

内部 EXE 不需要 GUI 正在运行，也不创建窗口或托盘图标。

## 源码开发版

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

## 协议和工具

已验证生命周期：`initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`。唯一工具为 `submit_result`：

```json
{
  "file_path": "C:\\允许目录\\report.md",
  "title": "可选标题",
  "request_id": "stable-request-001"
}
```

`request_id` 用于重试幂等；同一成功请求再次调用返回 duplicate。每个 stdio 进程限制一分钟五次提交。

## 安全边界

- 收件人固定为 `OWNER_GMAIL`，Agent 不能指定或修改。
- 文件必须位于 DATA_ROOT 或 ALLOWED_SEND_ROOTS；越界返回 `path_not_allowed`。
- GUI 用户明确选择任意文件的入口不授权 MCP 访问该目录。
- MCP 不能读取 credentials.json、token.json、Credential Manager、邮件正文、任意目录或 Gmail。
- stdout 只输出 JSON-RPC；诊断信息不得污染协议。

发布验收已覆盖中文/空格路径、独立多次启动退出、真实固定收件人发送、duplicate、本地 sent 归档哈希、越界拒绝、审计及无僵尸进程。真实发送只确认 SMTP 接受和本地归档，不据此宣称 Gmail 已最终收到。
