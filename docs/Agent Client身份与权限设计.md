# Agent Client 身份与权限设计

## 产品边界

v1.5.0 把 Claude Code、Codex、Claude Desktop 和自定义 MCP Client 建模为本机 Agent Client。AgentMailBridge 仍只提供七个确定性事实工具，不内置模型、不理解自然语言、不自动挑选邮件，也不建立通用 IAM、团队角色或远程认证系统。

## Research Gate

调研日期为 2026-07-26。

- MCP 2025-06-18 stdio 规范要求 Client 启动子进程，stdin/stdout 使用逐行 UTF-8 JSON-RPC，stdout 不得混入日志；本机 stdio 可通过进程环境传递本地凭据。
- Claude Code 官方文档确认 local、project、user 三种 scope；user/local 位于用户配置，project 使用项目根目录 `.mcp.json`，stdio 支持 command、args 和 env。
- Claude Desktop 官方本地服务器文档确认 Windows 配置位于 `%APPDATA%\Claude\claude_desktop_config.json`，配置为 `mcpServers` JSON，保存后需完整重启。
- OpenAI Codex 官方文档确认 CLI、IDE 和桌面形态共享 `config.toml`；用户级默认位于 `~/.codex/config.toml`，可信项目可使用 `.codex/config.toml`，stdio 使用 `mcp_servers.<id>` 的 command、args、env。
- CC-Switch 的公开文档、发布记录和关键配置管理实现用于验证“先备份、保留无关配置、按 server id 合并、原子写入、恢复”的工程方向。其 License 为 MIT；本项目只借鉴架构策略，没有复制源码。

官方来源：

- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- https://modelcontextprotocol.io/docs/tutorials/security/authorization
- https://code.claude.com/docs/en/mcp
- https://modelcontextprotocol.io/docs/develop/connect-local-servers
- https://developers.openai.com/codex/mcp/
- https://developers.openai.com/codex/config-reference/
- https://github.com/farion1231/cc-switch

## 身份模型

`agent_clients` 保存稳定不透明 `client_id`、Client 类型、显示名称、状态、配置方式与位置、Credential 引用、token SHA-256、最近调用和撤销时间。`agent_client_permissions` 保存 capability、account 和 workspace 的确定性 allow/deny 事实。`agent_client_config_backups` 保存外部配置修改前后的路径、Hash、状态和恢复时间。

每个 Client 创建独立随机 scoped token。完整 token 的管理副本保存在 Windows Credential Manager；SQLite 只保存 SHA-256。配置协议必须把 token 传给 stdio 子进程时，只写 AgentMailBridge scoped token 和 client_id，不写邮箱密码、QQ/163 授权码、Gmail OAuth Token 或 Client Secret。列表、日志、审计和普通诊断不返回完整 token。

stdio 启动身份来自 `AGENT_MAIL_BRIDGE_CLIENT_ID` 与 `AGENT_MAIL_BRIDGE_CLIENT_TOKEN`。Client 不能通过工具参数伪造身份。Server 在 initialize、tools/list 和每次 tools/call 重新读取数据库状态并校验 token Hash，因此暂停、权限变更、轮换和撤销立即生效。

## 权限链

每次调用必须依次通过：

1. `MCP_MAIL_READ_ENABLED` 全局读取总开关
2. Client 存在且 token 匹配
3. Client 为 active、enabled 且未撤销
4. 工具对应 capability 已 allow 且未 deny
5. 邮箱 `account_id` 位于 Client allowlist
6. 目标 `workspace_id` 位于 Client allowlist
7. 既有 DATA_ROOT、package/resource ownership、路径、大小和 SHA-256 校验

capability 固定为 `mail.search`、`mail.get`、`resource.read`、`resource.prepare`、`sync.status`、`sync.ensure_fresh`、`workspace.list`、`result.submit`。账号和工作区使用精确 ID allowlist。无权限记录即拒绝；unknown/anonymous Client 默认拒绝，不建立隐式 legacy 放行。

只有一个授权账号时，省略 account_id 可安全收窄到该账号；授权范围等于全部当前启用账号时可使用统一视图；其他多账号组合必须显式指定账号，防止隐式扩大范围。

`submit_result` 仍固定发往 `OWNER_GMAIL`，不接受任意 recipient、from_account、Gmail send 或 Outlook send。

## 稳定拒绝

身份与权限错误包括 `agent_access_disabled`、`unknown_client`、`client_disabled`、`client_revoked`、`client_auth_failed`、`capability_denied`、`account_denied`、`workspace_denied`。配置错误包括 `config_not_found`、`config_parse_failed`、`config_changed_concurrently`、`config_backup_failed`、`config_write_failed`、`config_restore_failed`、`unsupported_client_version`、`client_not_installed`、`connection_test_failed`。

## 配置安全

Managed Setup 先解析现有配置并生成脱敏预览，用户确认后才写入。应用前同时核对原文件 SHA-256 与 mtime；格式损坏或并发变化时拒绝写入。原文件先复制到产品数据目录下的 Client 专属备份；新内容使用同目录临时文件、flush、fsync 和原子替换，写后复核 Hash，失败时恢复原内容。重复应用不产生第二个 server 项；撤销只移除 `agent-mail-bridge` 项；恢复同样拒绝覆盖已被外部再次修改的配置。

JSON 合并保留未知字段和其他 MCP Server，但标准库序列化会统一缩进，这是已明确的格式库限制。Codex TOML 只替换自身 table，保留其他 section 和文本布局。配置格式不受支持或无法安全定位时退化为 Assisted/Manual，不强行写入。

## 本地威胁模型

这是 Windows 单用户、同一登录用户下的权限分区，不是操作系统级强隔离。同一用户下能读取目标 Client 配置或注入其进程的恶意程序，可能取得 scoped token；该 token 仍只能访问 AgentMailBridge 内显式授予的 capability、账号和工作区，并可单独暂停、轮换或撤销。邮箱真实凭据和 OAuth Token 从不交给 Client。

## 迁移与审计

旧数据库在 v1.5.0 schema 变更前创建 `before_v1_5_agent_permissions` 在线备份。三张表、审计增量列和 migration metadata 在单一事务中幂等创建；不移动 package、不改 raw.eml、不重算 Hash、不改变 account_id，也不创建匿名放行 Client。

审计沿用 `mcp_audit_events`，增加 client_id、client_type、显示名快照、capability、account_id、workspace_id、deny reason 和 correlation id。审计不保存完整正文、附件、邮箱凭据、OAuth Token、Client token 或自然语言对话。
