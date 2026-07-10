# AgentMailBridge MCP 使用说明

AgentMailBridge 提供一个仅限本机的 stdio MCP 服务。它只有 submit_result 工具，用于把允许目录内的本地结果文件交给现有 ApplicationService，再由 QQ SMTP 发送到固定 OWNER_GMAIL。

## 启动与配置

在项目根目录执行：

```powershell
python -m agent_mail_bridge.mcp_server
```

Codex：

```powershell
codex mcp add agent-mail-bridge -- python -m agent_mail_bridge.mcp_server
```

对应 config.toml 示例：

```toml
[mcp_servers.agent-mail-bridge]
command = "python"
args = ["-m", "agent_mail_bridge.mcp_server"]
```

Claude Code：

```powershell
claude mcp add agent-mail-bridge -- python -m agent_mail_bridge.mcp_server
```

如果 Agent 的工作目录不是项目根目录，应在 Agent 配置中设置项目目录，或使用已安装的 agent-mail-bridge-mcp 命令。

## submit_result

输入：file_path 为必填本地文件路径；title 为可选邮件标题，最多 200 个字符；request_id 为可选幂等标识，最多 128 位，仅允许字母、数字、点、下划线、冒号和连字符。

不支持 recipient、to、邮箱地址、SMTP 参数或凭据参数。收件人始终来自本地 OWNER_GMAIL。

主要状态：success、failed、duplicate、sent_archive_failed、file_not_found、path_not_allowed、configuration_error、invalid_input、rate_limited。

相同 request_id 在已发送、归档失败或发送中的情况下不会再次发信。SMTP 明确失败后仍可使用同一 request_id 安全重试。

## 安全边界

服务只通过 stdin/stdout 通信，不监听 TCP 端口。Agent 只能提交 DATA_ROOT 或 ALLOWED_SEND_ROOTS 内的文件，不能读取 Gmail token、credentials.json、Gmail 应用专用密码或 QQ 授权码，不能修改邮箱配置、删除文件、扫描任意目录或指定任意收件人。

每个 stdio 进程每 60 秒最多提交 5 次。每次有效提交、参数拒绝和频率限制都会写入 mcp_calls 审计表；GUI 的 Agent 接口页可以查看最近调用。

## 常见错误

file_not_found：文件不存在或不是普通文件。

path_not_allowed：文件不在 DATA_ROOT 或 ALLOWED_SEND_ROOTS 中。

configuration_error：QQ SMTP 或固定收件人配置不完整。

duplicate：相同 request_id 已处理或正在处理，不会重复发信。

sent_archive_failed：SMTP 已成功，只有本地 sent 归档失败；不要重复提交。

rate_limited：一分钟内已提交 5 次，等待频率窗口恢复。

调试时先运行 python -m agent_mail_bridge diagnose-qq-smtp，再在 GUI Agent 接口页和日志页核对 request_id、状态和错误代码。不要把 .env、credentials.json 或 token.json 交给 Agent。

协议实现参考 MCP 官方 stdio、生命周期和 tools 规范；Codex 配置命令以本机 codex mcp add --help 输出为准。
