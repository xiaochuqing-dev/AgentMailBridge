# Claude Code 接入指南

v1.5.0 支持 Claude Code user 和 project 配置。先在 GUI 的“Agent 接入”点击“连接 Claude Code”，选择能力、邮箱账号和工作区，再查看脱敏预览并确认。

user 配置由 Claude Code 保存于用户配置；project 配置位于项目根目录 `.mcp.json`。AgentMailBridge 只合并 `mcpServers.agent-mail-bridge`，保留其他 server 和未知字段，写前备份并检测并发修改。JSON 会由标准库统一缩进。

配置写入后重启 Claude Code 或新建会话，可用 `/mcp` 或 `claude mcp list` 查看连接，再回到 GUI 点击“测试”。PASS 必须显示 initialize、tools/list 和七工具；实际读取仍受总开关、Client capability、账号和工作区权限限制。

Client 暂停或撤销后，后续调用立即拒绝。撤销时可预览并仅移除 AgentMailBridge 自己的配置项；需要恢复时使用该 Client 的配置备份。

官方依据：https://code.claude.com/docs/en/mcp
