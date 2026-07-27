# Claude Desktop 兼容接入说明

普通 Claude Desktop 聊天客户端不是 AgentMailBridge v1.6.0 的重点支持对象，状态固定为 `SKIPPED_BY_SCOPE`。推荐使用 Claude Code；两者不得混为同一客户端或用 Claude Code PASS 代替 Desktop PASS。

为避免破坏存量用户，Windows `%APPDATA%\Claude\claude_desktop_config.json` 的已有兼容适配器继续保留在“其他 Agent”。如用户自行使用，AgentMailBridge 仍只合并 `mcpServers.agent-mail-bridge`，写前备份并检测并发变化。

本阶段不做专门开发、真实 E2E 或阻断验收，也不因该入口单独重复运行长测试。配置损坏、路径无法识别或版本不确定时安全退化为辅助配置；撤销仍只移除 AgentMailBridge 自身项。
