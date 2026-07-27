# Claude Code 接入指南

v1.6.0 正式支持 Claude Code，当前验证版本为 2.1.220。先在 GUI 的“Agent 接入”点击“连接 Claude Code”，默认使用推荐权限；普通 Claude Desktop 聊天客户端是另一个产品入口，只保留兼容配置，不属于本指南的验收范围。

user 配置由 Claude Code 保存于用户配置；project 配置位于项目根目录 `.mcp.json`。AgentMailBridge 只合并 `mcpServers.agent-mail-bridge`，保留其他 server 和未知字段，写前备份并检测并发修改。JSON 会由标准库统一缩进。

配置写入后重启 Claude Code 或新建会话，可用 `/mcp` 或 `claude mcp list` 查看连接，再回到 GUI 点击“测试”。真实 PASS 必须实际调用 search、get、resource read 和完整邮件资料准备，并查询一封 2024 历史邮件；实际读取仍受总开关、Client capability、账号和资料目录权限限制。

Client 暂停或撤销后，后续调用立即拒绝。撤销时可预览并仅移除 AgentMailBridge 自己的配置项；需要恢复时使用该 Client 的配置备份。

当前版本未验证时自动配置退化为辅助方式，不覆盖 `~/.claude.json` 或 `.mcp.json`。配置预览不展示其他 MCP Server 的值。

官方依据：https://code.claude.com/docs/en/mcp
