# AgentMailBridge MCP 使用说明

AgentMailBridge MCP 是发件能力中的内部按需 stdio 接口，不监听端口、不常驻、不创建快捷方式。Agent 客户端打开会话时启动，stdin 关闭后自动退出。

在 GUI 发件页标题区进入“Agent 发件 / MCP”，查看状态、固定收件人、最近调用、自检、完整交付指令和允许工作区，并复制 Codex、Claude Code 或通用 JSON 配置。

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

安全边界：收件人固定为 `OWNER_GMAIL`；文件必须位于 `DATA_ROOT` 或用户在界面明确授权的工作区；GUI 全局文件选择不会扩大 MCP 白名单；磁盘根目录、用户主目录、Windows、Program Files、ProgramData、AppData、产品数据目录和秘密文件不能授权或提交；MCP 不能读取凭据、OAuth、邮件正文、任意目录或修改邮箱设置；stdout 只输出 JSON-RPC。工作区变更从下一次 MCP 进程启动开始生效。

Windows 正式版对 stdin、stdout 和 stderr 明确使用 UTF-8，不依赖 ACP/OEM code page；兼容首条 UTF-8 BOM。每条响应完整写出并立即 flush，普通日志不会进入 stdout，stdin EOF 后进程退出。中文目录、中文文件名、空格路径、中文或中英文标题均可直接提交。

交付时 Agent 应自行识别用户本轮真正需要的最终文件，生成稳定 request_id，直接把原始文件路径传给 `submit_result`。不得运行 PowerShell Copy-Item、cp、另存临时副本或要求用户填写最终路径。遇到 path_not_allowed 时，提示用户在 GUI 授权原文件所在工作区；遇到 transport closed 时重新建立 MCP 会话后用同一 request_id 重试。通过验证后，AgentMailBridge 自己原子复制到 `DATA_ROOT/send/staging/mcp`，再验证源文件与 staging 的字节数和 SHA-256；之后校验 SMTP 附件读取副本与 sent 归档。

成功的 `structuredContent` 至少包括 status、request_id、filename、size_bytes、source_sha256、staged_sha256、attachment_pre_smtp_sha256、sent_archive_sha256 和 send_status。正常成功时四个 SHA-256 应相同；SMTP 已接受但归档失败时会返回部分完成，不能盲目重发。

Codex 与 Claude Code 配置只需把 command 指向正式安装目录的 `AgentMailBridgeMCP.exe`，args 为空。通用 JSON 同理。安装升级后应重新执行 initialize、tools/list、malformed JSON、未知 method、中文文件 submit_result 和 EOF smoke。
