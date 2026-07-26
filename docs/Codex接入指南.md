# Codex 接入指南

v1.5.0 支持 Codex user 和可信 project 配置。先在 GUI 的“Agent 接入”点击“连接 Codex”，选择能力、邮箱账号和工作区，再确认脱敏 TOML 预览。

用户级配置默认位于 `~/.codex/config.toml`；项目级配置位于项目根目录 `.codex/config.toml`，只应在可信项目使用。AgentMailBridge 只更新 `[mcp_servers.agent-mail-bridge]` 及其 env table，保留其他 TOML section；写前备份并核对 Hash 与 mtime。

配置完成后重启或 reload Codex。Codex CLI、IDE 和桌面形态在同一 Codex host 上共享该配置；可用 `codex mcp list` 检查，再在 AgentMailBridge GUI 中运行连接测试。

若 Codex 服务额度、登录或外部策略阻断真实会话，配置与本地 stdio 测试通过不能标记为真实 Agent E2E PASS。Client 暂停、权限修改、token 轮换和撤销都在下一次 MCP 调用生效。

官方依据：https://developers.openai.com/codex/mcp/ 与 https://developers.openai.com/codex/config-reference/
