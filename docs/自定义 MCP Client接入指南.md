# 自定义 MCP Client 接入指南

自定义 Client 使用 Manual/Assisted Setup。GUI 创建 Client 并选择 capability、账号和工作区后，可复制最小 stdio JSON 或 TOML 示例。

配置必须包含：

- command：安装版 `AgentMailBridgeMCP.exe`，源码版为当前 Python
- args：安装版为空，源码版为 `-m agent_mail_bridge.mcp_server`
- env：`AGENT_MAIL_BRIDGE_CLIENT_ID` 与 `AGENT_MAIL_BRIDGE_CLIENT_TOKEN`

第二个 env 值是可单独撤销的 AgentMailBridge scoped token，不是邮箱密码或 OAuth Token。不要提交到 Git、日志、报告或团队共享配置。若目标 Client 支持从安全环境引用秘密，应优先使用该能力；否则按本机同用户边界保护配置文件。

完成后运行 GUI“测试”，确认 initialize、tools/list 和七工具。真实搜索、正文与资源读取还需开启全局邮件读取总开关，并明确授权 capability、账号和工作区。

不支持安全自动合并的格式不会被 AgentMailBridge 强行修改。撤销 Client 会立即拒绝旧 token；手动 Client 的外部配置需由用户在对应程序中删除。
