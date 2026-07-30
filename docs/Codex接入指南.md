# Codex 接入指南

v1.7.1 把 Codex Desktop 与 Codex CLI 作为推荐入口。先在 GUI 的“Agent 接入”连接 Codex，再用人话分别授权读取账号/目录、发件账号、确认或自主发送和本地附件目录。桌面真实任务必须实际调用工具，不能用 CLI 或配置存在替代。结果不确定发件只能查询、对账或由 GUI 用户处理，Agent 不得自动重发。

用户级配置默认位于 `~/.codex/config.toml`；项目级配置位于项目根目录 `.codex/config.toml`，只应在可信项目使用。AgentMailBridge 只更新 `[mcp_servers.agent-mail-bridge]` 及其 env table，保留其他 TOML section；写前备份并核对 Hash 与 mtime。

OpenAI 官方资料确认 Codex CLI、IDE 和桌面版共享 `~/.codex/config.toml`。因此同一用户只创建一个 Codex Client，不为桌面版重复生成 token 或配置。配置完成后重新载入或重启 Codex；可先用 `codex mcp list` 核对，再在桌面版或 CLI 真实调用 `search_mails`、`get_mail`、`read_mail_resource` 和完整模式的 `prepare_mail_resources`。

v1.7.1 已按官方配置参考真实验证 Codex CLI 0.146.0；0.145.0 继续兼容。其他版本仍按未知版本处理，不自动写入 TOML。

若 Codex 服务额度、登录或外部策略阻断真实会话，配置与本地 stdio 测试通过不能标记为真实 Agent E2E PASS。Client 暂停、权限修改、token 轮换和撤销都在下一次 MCP 调用生效。

真实验收必须查询本地归档、读取资源、准备完整邮件资料，并验证暂停/撤销后的下一次调用被拒绝；仅有 `tools/list` 不能算真实 PASS。未知版本显示“当前版本未完成自动配置验证”，只提供辅助配置，不修改现有 TOML。

官方依据：https://learn.chatgpt.com/docs/extend/mcp?surface=cli 与 https://developers.openai.com/codex/config-reference/
