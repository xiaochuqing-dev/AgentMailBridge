# Hermes 接入指南

AgentMailBridge v1.7.2 正式支持 Windows Hermes 入口。读取与发件分别授权；Hermes 只能使用当前 Client 获准的账号、邮箱目录和本地附件目录，且只能按用户选择使用发送前确认或自主发送。结果不确定发件保持不可自动重发。

在 GUI 的“Agent 接入”点击“连接 Hermes”，默认使用推荐权限：所有当前及以后新增邮箱、正文/附件/邮件图片读取、按需刷新、所有当前及以后新增资料目录和完整邮件资料准备开启，结果提交关闭。完全信任仍受固定收件人、DATA_ROOT、ownership 与 Hash 边界约束。

Windows 配置默认位于 `%LOCALAPPDATA%\hermes\config.yaml`。AgentMailBridge 只合并 `mcp_servers.agent-mail-bridge`，使用 round-trip YAML 保留注释、未知字段和其他 server。预览只显示自身条目的 command、args、被隐藏的 env 名、脱敏目标和恢复说明；应用前备份并检查 Hash/mtime，写入采用原子替换。

当前版本可用以下命令核对：

```powershell
hermes --version
hermes mcp list
hermes mcp test agent-mail-bridge
```

已有 Hermes 会话可执行 `/reload-mcp`，或重启会话。真实验收必须由 Hermes 模型实际调用 `search_mails`、`get_mail`、`read_mail_resource` 和 `prepare_mail_resources` 的 complete 模式，并验证 2024 历史查询、目录拒绝、暂停、撤销和审计；配置生成或 tools/list 不能替代。

若未检测到 Hermes 或版本未验证，GUI 只提供安全辅助配置和最小复制方式，不盲目修改 YAML。Hermes 的模型凭据、其他 server secret 和 AgentMailBridge scoped token 不得进入预览、日志或报告。

官方资料：

- https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- https://hermes-agent.nousresearch.com/docs/user-guide/windows-native
- https://github.com/NousResearch/hermes-agent
