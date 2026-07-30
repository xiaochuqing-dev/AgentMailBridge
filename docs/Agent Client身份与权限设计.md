# Agent Client 身份与权限设计

## 产品边界

v1.7.1 正式重点支持 Codex、Claude Code 和 Hermes。普通 Claude Desktop 只保留兼容代码。AgentMailBridge 提供 11 个确定性工具，不内置模型、不理解自然语言、不自动挑选邮件、不自动重发未知结果，也不建立通用 IAM、团队角色或远程认证系统。

权限以 Client 为边界并默认拒绝。读取、发件和兼容结果提交互相独立；读取账号、邮箱目录、发件账号、完整资料输出目录和本地附件目录分别支持动态 `all` 与显式 `selected`。旧 Client 迁移后保持 selected 且默认没有 `mail.send`。通用发件只有 `confirm` 和 `autonomous` 两种模式。

## Research Gate

调研日期为 2026-07-27。

- MCP 2025-06-18 stdio 规范要求 Client 启动子进程，stdin/stdout 使用逐行 UTF-8 JSON-RPC，stdout 不得混入日志；本机 stdio 可通过进程环境传递本地凭据。
- Claude Code 官方文档确认 local、project、user 三种 scope；user/local 位于用户配置，project 使用项目根目录 `.mcp.json`，stdio 支持 command、args 和 env。
- Claude Desktop 官方本地服务器文档确认 Windows 配置位于 `%APPDATA%\Claude\claude_desktop_config.json`，配置为 `mcpServers` JSON，保存后需完整重启。
- OpenAI Codex 官方文档确认 CLI、IDE 和桌面形态共享 `config.toml`；用户级默认位于 `~/.codex/config.toml`，可信项目可使用 `.codex/config.toml`，stdio 使用 `mcp_servers.<id>` 的 command、args、env。
- Hermes 官方文档和当前 0.19.0 安装确认 Windows 配置位于 `%LOCALAPPDATA%\hermes\config.yaml`，`mcp_servers` 支持 command、args、env；CLI 提供 `hermes mcp add/list/test`，交互会话提供 `/reload-mcp`。
- CC-Switch 的公开文档、发布记录和关键配置管理实现用于验证“先备份、保留无关配置、按 server id 合并、原子写入、恢复”的工程方向。其 License 为 MIT；本项目只借鉴架构策略，没有复制源码。
- Claude Code 官方仓库问题 #80065 记录了带连字符 MCP server 名在工具暴露与路由之间不一致的 NotFound 故障；本项目仅对 Claude Code 使用 `agent_mail_bridge` 键，并在受管写入时迁移旧键。其他 Client 继续使用 `agent-mail-bridge`。

官方来源：

- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- https://modelcontextprotocol.io/docs/tutorials/security/authorization
- https://code.claude.com/docs/en/mcp
- https://modelcontextprotocol.io/docs/develop/connect-local-servers
- https://developers.openai.com/codex/mcp/
- https://developers.openai.com/codex/config-reference/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- https://hermes-agent.nousresearch.com/docs/user-guide/windows-native
- https://github.com/NousResearch/hermes-agent
- https://github.com/farion1231/cc-switch
- https://github.com/anthropics/claude-code/issues/80065

## 身份模型

`agent_clients` 保存稳定不透明 `client_id`、Client 类型、显示名称、状态、配置方式与位置、Credential 引用、token SHA-256、最近调用、撤销时间、权限模式以及账号/资料目录范围模式。`agent_client_permissions` 保存 capability、account 和 workspace 的确定性 allow/deny 事实。`agent_client_config_backups` 保存外部配置修改前后的路径、Hash、状态和恢复时间。

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

capability 固定为 `mail.search`、`mail.get`、`resource.read`、`resource.prepare`、`sync.status`、`sync.ensure_fresh`、`workspace.list`、`result.submit`。推荐模式启用前七项、关闭 `result.submit`，并动态覆盖所有当前及以后新增邮箱和资料目录；完全信任模式启用全部能力但仍不能突破固定收件人和文件边界；自定义模式使用显式选择。deny 始终优先。旧 Client 的显式账号和目录迁移为 `selected`，不会因升级自动扩权。

只有一个授权账号时，省略 account_id 可安全收窄到该账号；授权范围等于全部当前启用账号时可使用统一视图；其他多账号组合必须显式指定账号，防止隐式扩大范围。

`submit_result` 仍固定发往 `OWNER_GMAIL`，不接受任意 recipient、from_account、Gmail send 或 Outlook send；文件还必须同时属于当前 Client 获准的资料目录和全局允许目录。

## 稳定拒绝

身份与权限错误包括 `agent_access_disabled`、`unknown_client`、`client_disabled`、`client_revoked`、`client_auth_failed`、`capability_denied`、`account_denied`、`workspace_denied`。配置错误包括 `config_not_found`、`config_parse_failed`、`config_changed_concurrently`、`config_backup_failed`、`config_write_failed`、`config_restore_failed`、`unsupported_client_version`、`client_not_installed`、`connection_test_failed`。

## 配置安全

Managed Setup 先解析现有配置，但预览只展示 AgentMailBridge 项的 command、args、env 名称、隐藏值、脱敏目标、备份和重启说明，不回显整个配置或其他 MCP Server 的秘密。应用前同时核对原文件 SHA-256 与 mtime；格式损坏或并发变化时拒绝写入。原文件先复制到产品数据目录下的 Client 专属备份；新内容使用同目录临时文件、flush、fsync 和原子替换，写后复核 Hash，失败时恢复原内容。重复应用不产生第二个 server 项；Claude Code 使用 `agent_mail_bridge` 并迁移旧的 `agent-mail-bridge`，撤销时同时移除这两个受管别名；其他 Client 只管理 `agent-mail-bridge`。恢复同样拒绝覆盖已被外部再次修改的配置。

JSON 合并保留未知字段和其他 MCP Server，但标准库序列化会统一缩进，这是已明确的格式库限制。Codex TOML 只替换自身 table，保留其他 section 和文本布局。Hermes 使用 round-trip YAML 合并，保留注释、未知设置和其他 server。备份最多保留 20 份和 90 天，至少保留最近一个有效副本；备份可能包含第三方 Client 原有秘密，只位于当前用户数据目录并收紧文件权限。未验证的新 Client 版本退化为 Assisted，不盲目写入。

token 轮换以旧 token 为恢复点：Credential 更新后数据库失败会恢复旧 Credential；数据库成功后受管配置应用失败会恢复旧 token，并由配置原子写入自行恢复原文件。任何回滚无法完成都会返回独立错误并提示先暂停 Client。

## 本地威胁模型

这是 Windows 单用户、同一登录用户下的权限分区，不是操作系统级强隔离。同一用户下能读取目标 Client 配置或注入其进程的恶意程序，可能取得 scoped token；该 token 仍只能访问 AgentMailBridge 内显式授予的 capability、账号和工作区，并可单独暂停、轮换或撤销。邮箱真实凭据和 OAuth Token 从不交给 Client。

## 迁移与审计

v1.6.0 在 Agent Integration schema v2 变更前创建 `before_v1_6_agent_ecosystem` 在线备份，然后在单一事务中增加动态范围字段和 `history_import_runs`。旧 Client 默认保持 `selected`；迁移幂等、失败回滚，不移动 package、不改 raw.eml、不重算 Hash、不改变 account_id/package_id/resource_id。v1.5.0 的 `before_v1_5_agent_permissions` 基线备份规则继续保留为历史升级链证据。

审计沿用 `mcp_audit_events`，增加 client_id、client_type、显示名快照、capability、account_id、workspace_id、deny reason 和 correlation id。审计不保存完整正文、附件、邮箱凭据、OAuth Token、Client token 或自然语言对话。
