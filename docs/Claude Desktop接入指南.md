# Claude Desktop 接入指南

Windows Claude Desktop 配置位于 `%APPDATA%\Claude\claude_desktop_config.json`。在 GUI 的“Agent 接入”中创建 Claude Desktop Client，选择权限，查看脱敏预览并确认写入。

AgentMailBridge 只合并 `mcpServers.agent-mail-bridge`，保留其他 server 和未知字段，写前保存原文件及 Hash。保存后必须完全退出并重新启动 Claude Desktop，再回到 GUI 点击“测试”。

配置损坏、路径无法识别、并发修改或版本不确定时会拒绝覆盖并退化为辅助配置。未安装 Claude Desktop 时，状态只能记为 PACKAGED_CONFIG_PASS 或 NOT_TESTED，不能写成真实 Client PASS。

暂停或撤销后后续调用立即拒绝；撤销只移除 AgentMailBridge 项，配置备份可手动恢复。

官方依据：https://modelcontextprotocol.io/docs/develop/connect-local-servers
