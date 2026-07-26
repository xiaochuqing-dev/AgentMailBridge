# AgentMailBridge v1.5.0 Agent Integration & Permission Foundation 真实验收报告

## 1. 基线 HEAD

基线为 master 的 d8cf0e28a14d086dd352af8f652a8afb89c0b8e9，产品版本为 1.4.5。本阶段直接在 master 开发，未创建分支、Tag 或 GitHub Release。

## 2. 本阶段目标

在 GUI 中连接 Claude Code、Codex、Claude Desktop 和自定义 MCP Client，为每个 Client 建立独立身份、能力、账号与工作区权限，并让 Agent 调用现有七个确定性事实工具。

## 3. 产品边界

AgentMailBridge 仍是本地优先、单用户、Agent-neutral 的邮件事实与资源后端；不内置模型，不理解自然语言，不做智能路由、任务规划、Gmail Send、Outlook 或通用 IAM。

## 4. 当前 Provider 与事实体系

QQ、163 收发保持 supported；Gmail receive 保持 supported，OAuth scope 仍严格为 gmail.readonly；Generic IMAP/SMTP 保持 implementation ready / E2E required；Gmail send 与 Outlook 仍为 planned。Mail Package、raw.eml、resource ownership、SHA-256、受控准备与七个 MCP 工具均保留。

## 5. Research Gate

2026-07-26 在实现前检查 MCP stdio 与本地授权边界、Claude Code/Claude Desktop/Codex 当前配置方式，并检查 CC-Switch 的备份、按 server 合并与恢复策略。未复制第三方源码。

## 6. 调研来源与日期

- MCP stdio transport 与 UTF-8：[MCP Transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- 本地 stdio 凭据边界：[MCP Local Authorization](https://modelcontextprotocol.io/docs/tutorials/security/authorization)
- Claude Code MCP 与 scope：[Claude Code MCP](https://code.claude.com/docs/en/mcp)
- Claude Desktop Windows 配置：[Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
- Codex MCP 配置：[OpenAI Codex MCP](https://developers.openai.com/codex/mcp/)
- CC-Switch：2026-07-26 检查 GitHub 主仓库与 MIT License，仅借鉴配置管理策略。

## 7. Client Identity 设计

新增稳定不透明 client_id、client_type、状态、配置方式、scope、最近调用与撤销时间。MCP 从受控启动环境解析 client_id 和独立 token，不接受工具参数伪造身份。

## 8. Client token / 本地威胁模型

每个 Client 使用独立 ambc_ scoped token；数据库只存 SHA-256，Windows Credential Manager 保存 GUI 管理副本。token 可轮换、暂停和撤销，不可推导邮箱凭据，不进入日志、报告或审计。同一 Windows 用户可读取自己的 Client 配置，因此这是最小权限与可撤销边界，不宣称同用户强隔离。

## 9. Permission Model

权限固定为 mail.search、mail.get、resource.read、resource.prepare、sync.status、sync.ensure_fresh、workspace.list、result.submit，并叠加 account allowlist、workspace allowlist 与 active/paused/revoked 状态。

## 10. 全局 Gate 兼容

授权顺序为 Global Gate AND Client Valid AND Client Enabled AND Capability AND Account AND Workspace AND 既有 DATA_ROOT/ownership/Hash 检查。MCP_MAIL_READ_ENABLED 保留且默认 false；匿名或 legacy 调用不会自动获得权限。

## 11. 数据库与 migration

新增 agent_clients、agent_client_permissions、agent_client_config_backups，并扩展 MCP 审计字段。迁移在正常数据库升级前创建 before_v1_5_agent_permissions 备份，单事务、幂等；不移动 package、不重写 raw.eml、不重算 Hash、不改变 account_id。

## 12. GUI 信息架构调整

原“Agent / MCP”合并重构为唯一的“Agent 接入”一级入口。页面包含总开关、四类连接入口、Client 表、权限/配置/测试/恢复/暂停/撤销、工作区和审计筛选。Windows Qt 100%、125%、150% 浅色与 150% 深色截图均无横向溢出，中文、表格和操作按钮可读。

## 13. Claude Code 配置方式

支持用户级 JSON 与项目级 .mcp.json，采用 stdio command/args/env；检测到 2.1.220 时为 managed_supported，无法安全写入时退化为 assisted。配置应用后按官方行为重新启动或 reload。

## 14. Codex 配置方式

支持用户级 config.toml 与项目级 .codex/config.toml，写入 mcp_servers.agent-mail-bridge 的 command、args 和最小 env。检测到 codex-cli 0.145.0 时为 managed_supported；CLI、IDE 与 Desktop 共用 Codex MCP 配置语义。

## 15. Claude Desktop 配置方式

支持 Windows claude_desktop_config.json 的 mcpServers 合并、备份与恢复，并提示完全退出后重启。当前机器未安装 Claude Desktop，真实客户端调用未执行。

## 16. Custom MCP 配置方式

提供 stdio command、args、必要 env、Client profile ID，以及可复制/导出的 JSON/TOML 示例。Custom 保持 manual_only，不夸大为一键支持。

## 17. Managed / Assisted / Manual 能力矩阵

| Client | Managed | Assisted | Manual | 当前检测 |
| --- | --- | --- | --- | --- |
| Claude Code | 支持用户/项目配置 | 支持 | 支持 | 已安装，可管理 |
| Codex | 支持用户/项目 TOML | 支持 | 支持 | 已安装，可管理 |
| Claude Desktop | 格式与路径稳定时支持 | 支持 | 支持 | 未安装 |
| Custom MCP | 不适用 | 配置片段 | 支持 | manual_only |

## 18. 配置预览

GUI 应用前展示脱敏 diff 摘要；完整 scoped token 不在预览中显示。预览记录原文件 hash、mtime、目标类型和 scope。

## 19. 配置备份

每次受管写入前创建原文件备份并记录 original_hash、applied_hash、路径和状态；原文件不存在时也保留可撤销语义。

## 20. 幂等合并

JSON/TOML 只更新 AgentMailBridge server 项，保留其他 MCP server 与未知字段；重复执行不产生重复项。Claude Code 用户/项目 fixture、Claude Desktop JSON 和 Codex Unicode TOML 均通过。

## 21. rollback

写入使用同目录临时文件、flush、fsync 和 os.replace；异常时保留或恢复原文件。外部 hash/mtime 变化、损坏 JSON/TOML 和写入失败均拒绝覆盖；手动恢复备份 PASS。

## 22. revoke

撤销立即使 token 失效；受管配置移除只删除 AgentMailBridge 自己的项，不删除其他 server。恢复 Client 需要重新连接，不允许旧 token 复活。

## 23. Client status

实现 active、paused、revoked，以及 managed_supported、assisted_supported、manual_only、not_installed、configuration_conflict、connected、test_failed 等配置/连接状态。

## 24. 真实 Claude Code E2E

Claude Code 2.1.220 使用真实可执行程序和隔离邮件归档完成 search_mails、get_mail、read_mail_resource、prepare_mail_resources。四工具审计齐全，准备文件 SHA-256 匹配，输出未暴露 Client token，结果 PASS。

## 25. 真实 Codex E2E

codex-cli 0.145.0 使用真实可执行程序和隔离 MCP 配置完成同一四工具链。隔离源码运行所需 server env 显式传递后，握手、审计和准备文件 SHA-256 全部 PASS，输出未暴露 Client token。

## 26. 真实 Claude Desktop E2E

当前机器未安装 Claude Desktop，状态 NOT_TESTED；没有把 fixture 结果写成真实 Client PASS。

## 27. 未安装 Client 的隔离配置验证

Claude Desktop 使用隔离 HOME/config fixture 验证空文件、既有 server、未知字段、备份、幂等合并、冲突、损坏文件拒绝、移除与恢复，状态 PACKAGED_CONFIG_PASS。Custom 配置生成状态 PACKAGED_CONFIG_PASS。

## 28. MCP tools/list

源码真实 stdio 与打包 AgentMailBridgeMCP.exe 均返回恰好七工具；UTF-8、BOM、中文路径、flush、EOF 与 stdout purity 回归通过。

## 29. search/get/read/prepare

Claude Code 与 Codex 均完成真实四工具事实链；自动化另覆盖分页、资源读取边界、受控准备、ownership、路径和 Hash 拒绝。

## 30. account permission matrix

Client A 仅允许账号 A、Client B 仅允许账号 B；交叉访问返回 account_denied。Client A 暂停或撤销不影响 Client B；多账号省略 account_id 时不进行不安全猜测。结果 PASS。

## 31. workspace permission matrix

授权 workspace 可准备资源，未授权 workspace、驱动器根目录、用户目录、AppData、DATA_ROOT 越界和错误 ownership 均拒绝。pre-SMTP/准备前 Hash 不一致继续阻断。结果 PASS。

## 32. global gate

总开关关闭时所有有效 Client 的 read 类能力返回 agent_access_disabled；总开关开启但 Client 无 capability 时仍返回 capability_denied。结果 PASS。

## 33. unknown Client deny

缺少身份、未知 client_id、错误 token 与匿名 legacy 调用在 initialize/tools/list/tools/call 均默认拒绝并审计。结果 PASS。

## 34. revoke immediate deny

同一运行期内撤销后下一次调用立即返回 client_revoked；旧 token 轮换后立即返回 client_auth_failed。结果 PASS。

## 35. Client isolation

状态、token、能力、账号和工作区均按 client_id 独立存储；一个 Client 的暂停、撤销或权限修改不改变其他 Client。结果 PASS。

## 36. submit_result invariant

submit_result 保持固定 OWNER_GMAIL 收件人与既有 staging、大小、Hash、发送归档安全边界；未开放任意 recipient、from_account、Gmail send 或 Outlook send。

## 37. audit

mcp_audit_events 记录 client_id、client_type、display_name snapshot、tool、capability、account_id、workspace_id、status、deny reason、timestamp 与 correlation id。GUI 可按 Client、工具和成功/拒绝筛选。

## 38. secret redaction

审计、GUI 预览、连接测试、真实 E2E 输出与诊断均不记录 Client token、邮箱密码、QQ/163 授权码、Gmail OAuth Token、正文或附件内容。

## 39. 配置安全

备份 Hash、预览、幂等、未知字段保留、并发冲突、损坏配置拒绝、原子替换、失败 rollback、只移除自身项、恢复和临时文件清理均有自动化证据。邮箱 secret 和 OAuth Token 不进入 Agent 配置。

## 40. 升级 / 卸载 / 重装

随机 AppId 和隔离用户目录真实执行 1.4.5 安装、1.5.0 覆盖升级、卸载、1.5.0 重装。5 个账号、4 个 package、1 个 outbound、4 个 scheduler 状态、4 个 raw.eml、4 个附件、2 个 OAuth JSON 及其 Hash 全部保留；Agent migration 备份存在，卸载后程序移除、用户数据保留，重装恢复 PASS，生产安装未触碰。

## 41. targeted tests

最终定向运行 Client/权限/配置/真实 E2E 命令与生命周期测试：18 passed。

## 42. Full Suite Preflight

109 passed；version、Provider status、schema、git diff --check、compileall 与 targeted pytest 全部 PASS。

## 43. final full pytest

596 passed，1 skipped，0 failed，用时 2106.26 秒。

## 44. pip check

No broken requirements found，PASS。

## 45. clean build

Windows clean build -SkipTests PASS；PyInstaller 主 EXE/MCP EXE、GUI packaged self-test 与 build verification 均 PASS。仅有既有 pycparser 可选 lextab/yacctab 警告。

## 46. packaged smoke

打包 MCP 使用显式隔离 Client 身份完成 initialize、七工具、总开关拒绝、UTF-8、stdout purity、flush 与 EOF，PASS。

## 47. installer

AgentMailBridge-1.5.0-Setup.exe 生成成功，大小 44,034,314 bytes。

## 48. ZIP

AgentMailBridge-1.5.0-Windows-x64.zip 生成成功，大小 69,560,129 bytes。

## 49. checksums

checksums.sha256 已生成。安装器 SHA-256 为 E23C6A8D6EBE936DAA9344EA413DA7FDEF01D6B798EA40E2F0692DE2624C24FA；ZIP SHA-256 为 1257394304019312B9D5FB9D5A5A64E38A5C562B9A3AB297BB5B4664D1B08236。

## 50. secret scan

构建流程扫描 319 个纳入范围的文件，未检出配置的 secret marker，PASS。

## 51. Defender

Microsoft Defender 服务、病毒防护与实时防护均启用；release 与 dist 自定义扫描完成，项目范围 threat detection 为 0，PASS。

## 52. Authenticode

AgentMailBridge.exe、AgentMailBridgeMCP.exe 与安装器均为 NotSigned。未伪造签名结论。

## 53. P0 / P1 / P2

P0：0。P1：1，公开发布仍缺可信代码签名。P2：1，Claude Desktop 因本机未安装仅完成 PACKAGED_CONFIG_PASS。未发现权限绕过、配置不可恢复、workspace/ownership/Hash 绕过或 secret 泄露。

## 54. PASS / CONDITIONALLY PASS / FAIL

最终判定 CONDITIONALLY PASS。功能、安全、两个已安装真实 Agent、自动化、打包、扫描与生命周期均通过；NotSigned 是唯一 P1，未安装 Claude Desktop 是诚实记录的 P2。

## 55. 已知限制

同 Windows 用户不构成强安全隔离；Client 配置可能包含仅代表 AgentMailBridge 有限权限的 scoped token。Claude Desktop 未真实联调。边缘未来版本若配置格式变化会安全失败并退化为 assisted。Gmail 仍只读，Outlook 未实现。

## 56. 下一阶段建议

先真实使用本地 Client 权限地基并收集反馈；随后只按实际需求选择 Local API/CLI/Event Foundation、Gmail Full Account、Outlook Full Account 或 Sent Sync，不在 v1.5.0 提前扩展。

## 57. commits

功能提交 c2036e1：Client identity、权限、配置适配、GUI、数据库迁移与 1.5.0 版本。测试提交 6d1c4aa：权限/配置/MCP/lifecycle 自动化、真实 Claude Code/Codex E2E 与 GUI DPI QA。文档与验收报告提交 6a3de65。

## 58. push status

2026-07-26 已将功能、测试与文档提交普通 push 到 origin/master，远端到达 6a3de65。本节状态回填由独立收口提交承载并继续普通 push；最终核验要求本地 master 与 origin/master 一致。全程未 force push、改写历史、创建 Tag 或 GitHub Release。

## Client 验收矩阵

| Client | Install Detect | Config Mode | Backup | Apply | Test | Identity | Search | Get | Read | Prepare | Deny | Revoke | Audit | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code 2.1.220 | PASS | managed | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Codex 0.145.0 | PASS | managed | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Claude Desktop | PASS（未安装识别） | assisted | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | NOT_TESTED | PACKAGED_CONFIG_PASS | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS |
| Custom MCP | PASS | manual | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PASS | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PACKAGED_CONFIG_PASS | PASS | PASS | PASS | PACKAGED_CONFIG_PASS |
