# Claude Code 接入指南

v1.7.2 正式支持 Claude Code。先在 GUI 的“Agent 接入”连接 Claude Code，再明确设置读取账号/目录、发件账号、确认或自主发送和本地附件目录。普通 Claude Desktop 是另一个入口，只保留兼容配置。Claude Code 可查询恢复状态，但不能确认或自动重发 `delivery_unknown`。

user 配置由 Claude Code 保存于用户配置；project 配置位于项目根目录 `.mcp.json`。Claude Code 专用键为 `mcpServers.agent_mail_bridge`；AgentMailBridge 会原子迁移旧的 `agent-mail-bridge` 键，保留其他 server 和未知字段，写前备份并检测并发修改。JSON 会由标准库统一缩进。

Claude Code 2.1.220 存在带连字符 MCP server 名的工具路由问题：工具会以连接符转下划线后的名称暴露，但调用仍按旧 server 名查找并返回 NotFound。Claude Code 专用下划线键依据官方公开问题 anthropics/claude-code#80065 的 workaround；Codex、Hermes、Claude Desktop 和通用 MCP JSON 仍使用 `agent-mail-bridge`，不改变其兼容语义。

配置写入后重启 Claude Code 或新建会话，可用 `/mcp` 或 `claude mcp list` 查看连接，再回到 GUI 点击“测试”。真实 PASS 必须实际调用 search、get、resource read 和完整邮件资料准备，并查询一封 2024 历史邮件；实际读取仍受总开关、Client capability、账号和资料目录权限限制。

Client 暂停或撤销后，后续调用立即拒绝。撤销时可预览并仅移除 AgentMailBridge 自己的配置项；需要恢复时使用该 Client 的配置备份。

当前版本未验证时自动配置退化为辅助方式，不覆盖 `~/.claude.json` 或 `.mcp.json`。配置预览不展示其他 MCP Server 的值。

官方依据：https://code.claude.com/docs/en/mcp

兼容问题依据：https://github.com/anthropics/claude-code/issues/80065
