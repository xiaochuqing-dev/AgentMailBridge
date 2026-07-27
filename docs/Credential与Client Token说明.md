# Credential 与 Client Token 说明

邮箱秘密与 Agent Client 身份是两条不同的凭据链。

邮箱密码、Gmail 应用专用密码、QQ/163 授权码和 Generic IMAP/SMTP secret 位于 Windows Credential Manager 的账号凭据槽。Gmail OAuth credentials 与 token 位于账号专属 OAuth 目录，scope 始终只有 `gmail.readonly`。这些内容不会写入 Agent 配置。

每个 Agent Client 另有独立随机 scoped token。管理副本位于 Windows Credential Manager，SQLite 只保存 SHA-256 与不含秘密的 credential reference。它只能代表该 Client 在 AgentMailBridge 内获准的 capability、账号和资料目录，不能推导或读取邮箱凭据。

stdio Client 启动时通过 `AGENT_MAIL_BRIDGE_CLIENT_ID` 和 `AGENT_MAIL_BRIDGE_CLIENT_TOKEN` 传递身份。若目标 Client 只能在本地配置中保存 env，配置文件会包含 scoped token；这属于同一 Windows 用户边界内的有限秘密，不应提交到 Git、共享给团队或写入日志。GUI 预览、列表、审计、诊断和报告隐藏完整 token。

暂停 Client 会保留 token 但立即拒绝调用；撤销会删除 Credential Manager 管理副本并立即拒绝旧配置。撤销外部配置时只移除 `agent-mail-bridge` 项，其他 MCP Server 不受影响。

v1.6.0 的 token 轮换协调 Credential、SQLite Hash 和已受管外部配置。Credential 更新成功但数据库失败时恢复旧 Credential；数据库成功但配置应用失败时恢复旧 token，配置写入也回滚原文件。成功时新 token 与配置一起生效并提示 reload；无法完成恢复时返回 `token_rotation_rollback_failed`，不会把双失效伪装为成功。

外部配置预览不显示 token 值或其他 MCP Server 内容，只列出将隐藏的 env 名称。配置备份可能包含第三方 Client 原有秘密，只保存在当前用户 DATA_ROOT，最多 20 份和 90 天，并至少保留最近一个有效副本。

若 Windows Credential Manager 不可用，正式运行不会把 token 降级写入 SQLite 明文。测试环境可使用进程内隔离存储，但不能据此声称生产凭据持久化已验证。
