# AgentMailBridge v1.6.0 Agent Ecosystem、历史邮件与完整资料交付真实验收报告

验收日期：2026-07-27

## 1. 基线 HEAD

基线为 master 的 `907176c0be86aad358a4c2262a90bc07bcccf783`，开始验收时与 `origin/master` 一致。

验收状态：PASS

## 2. 本阶段目标

完成 Codex、Claude Code、Hermes 正式接入，产品化历史邮件导入，并把正文、原始邮件、资源与来源作为一个受控完整资料包交付。

验收状态：PASS

## 3. 产品边界

产品仍为本地优先、Windows 优先、单用户邮件桥。MCP 仍是按需启动的 stdio 子进程；未扩展为 SaaS、通用邮件客户端或 Agent 编排平台。

验收状态：PASS

## 4. 当前 Provider 状态

QQ、163 为正式支持；Gmail receive 保持正式支持且 OAuth scope 仍仅为 `gmail.readonly`；Generic IMAP/SMTP 保持 implementation ready / E2E required；Gmail send、Outlook/Microsoft 仍为未来范围。

验收状态：PASS

## 5. v1.5.0 基线

使用基线提交导出的 v1.5.0 源码构建旧版 EXE 和隔离安装器，并生成含 5 个账号、4 个邮件包、1 条 outbound、4 条 scheduler 状态和 1 个旧 Client 的真实升级基线。

验收状态：PASS

## 6. Research Gate

实现前核对了官方配置格式、scope、热加载和 Windows 路径；CC-Switch 仅作为配置备份、轮换和保留未知字段的公开参考，未复制其代码。

参考：[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Claude Code MCP](https://code.claude.com/docs/en/mcp)、[Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)、[Hermes Windows](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)、[CC-Switch](https://github.com/farion1231/cc-switch)。

验收状态：PASS

## 7. Codex 官方资料与测试版本

Codex CLI 与桌面端共享用户级或项目级 `config.toml` 的 MCP 配置语义。实际测试版本为 Codex CLI 0.145.0。

验收状态：PASS

## 8. Claude Code 官方资料与测试版本

按官方 stdio MCP 和 local/project/user scope 语义实现托管配置。实际测试版本为 Claude Code 2.1.220。

验收状态：PASS

## 9. Hermes 官方资料与测试版本

按官方 `config.yaml`、stdio MCP 和 Windows Native 路径实现 round-trip YAML 合并。实际测试版本为 Hermes Agent 0.19.0。

验收状态：PASS

## 10. 普通 Claude Desktop 跳过说明

普通 Claude Desktop 只保留存量兼容入口，未进行专项开发或长时间真实 E2E；不以 Claude Code 的结果替代它。

验收状态：SKIPPED_BY_SCOPE

## 11. Agent 接入页调整

推荐入口固定为 Codex、Claude Code、Hermes；Claude Desktop 下沉为“其他 Agent”。6 组 100%、125%、150% DPI 明暗主题截图均通过，三行卡片无横向滚动。

验收状态：PASS

## 12. 推荐 / 完全信任 / 自定义模式

推荐模式开放读取、搜索、获取和资料准备；完全信任模式额外开放结果提交；自定义模式保留逐能力、账号和资料目录控制，技术细节默认隐藏。

验收状态：PASS

## 13. 动态所有邮箱

动态范围按调用时的有效账号集合求值，新账号自动纳入；selected 范围保持显式白名单，停用或移除账号不会绕过校验。

验收状态：PASS

## 14. 动态所有资料目录

动态范围只覆盖调用时已登记的 Agent 资料目录，不扩展到任意文件系统路径；selected 范围继续按 workspace_id 校验。

验收状态：PASS

## 15. 旧 Client 迁移

v1.5 Client 事务迁移到 schema v2，默认保持 selected/selected，不因升级扩大账号或目录权限；迁移前自动建立数据库备份。

验收状态：PASS

## 16. “工作区”术语调整

用户界面改用“资料目录”等可理解术语，底层 workspace_id 与授权顺序保持不变。

验收状态：PASS

## 17. 历史邮件导入体验

提供 2024、最近一年、全部和自定义范围；支持季度分段、进度、扫描/保存/去重/跳过/失败计数、取消、继续和重启后状态恢复。

验收状态：PASS

## 18. 2024 真实历史补扫

对一个真实 QQ 历史账号执行 2024-01-01 至 2024-12-31 的网络补扫，4 个季度分段全部完成；成功选取一封 2024 邮件验证正文、raw、ownership 和资源 Hash。

验收状态：PASS

## 19. 历史邮件数量和分段

补扫前后均为 12 个归档包；本次扫描 12、匹配 12、去重 12、新增 0、失败 0、规则跳过 0，共 4 段。重复补扫未制造重复包。

验收状态：PASS

## 20. 完整邮件资料结构

原子发布目录包含 `邮件正文.md`、`原始邮件.eml`、`邮件信息.json`、`原始归档manifest.json`、`完整资料manifest.json`、`附件`、`邮件内图片`、`下载文件`。

验收状态：PASS

## 21. 正文

正文由正式归档事实生成，真实闭环中 HTML 与可读正文均可用，未把私人正文写入证据或本报告。

验收状态：PASS

## 22. raw.eml

完整资料使用归档中真实取得的 raw bytes，不重建、不伪造；2024 历史邮件和新测试邮件均验证 raw 存在。

验收状态：PASS

## 23. attachments

QQ→163 真实 MIME 邮件包含 3 个附件，其中覆盖普通文本、结构化文件和零字节文件；复制前后大小与 SHA-256 一致。

验收状态：PASS

## 24. inline images

真实 MIME 邮件包含一张内嵌 PNG，归档归属、资料复制和 Hash 校验均通过。

验收状态：PASS

## 25. downloads

完整资料固定创建下载文件目录；只交付已属于该 package 的受控下载资源，不在资料准备阶段联网或扩大 trusted domain。

验收状态：PASS

## 26. 来源说明

邮件信息、原始归档 manifest 与完整资料 manifest 分别记录邮件事实、原始资源来源和交付清单，路径均为资料包相对路径。

验收状态：PASS

## 27. Hash

真实完整资料包共 9 个文件；源文件、复制文件、manifest 记录的大小与 SHA-256 一致，发布前后原始归档快照不变。

验收状态：PASS

## 28. ownership

package_id、account_id、资源记录与规范化路径必须同时匹配；路径穿越、符号链接、junction、跨账号和跨 package 均被拒绝。

验收状态：PASS

## 29. 资料准备目录权限

目标必须是当前 Client 获准的资料目录；原子 staging 只发生在目标父目录内，Client A 无法使用 Client B 独占目录。

验收状态：PASS

## 30. Codex CLI 状态

真实 Codex 进程完成 7 工具发现、当前邮件搜索、2024 历史搜索、get、read 和完整资料准备；退出码 0，未暴露 Client token。

验收状态：PASS

## 31. Codex 桌面版真实 E2E

桌面端与 CLI 共用的 MCP 配置、打包 MCP、权限、连接和真实工具链已验证。当前安全工具边界明确禁止自动操纵 Codex Desktop 新任务，因此未伪造“桌面新会话热加载”证据。

验收状态：CONDITIONALLY PASS

## 32. Claude Code 真实 E2E

真实 Claude Code 进程完成当前邮件、2024 历史、get、read、完整资料、7 工具发现和 selected 账号拒绝；退出码 0，未暴露 token。

验收状态：PASS

## 33. Hermes 真实 E2E

真实 Hermes 进程完成当前邮件、2024 历史、get、read、完整资料和 7 工具发现；配置保留未知字段，撤销、恢复与轮换后重连通过。

验收状态：PASS

## 34. 三条真实用户场景

开发需求邮件、历史资料查询、完整资料交付均由真实 Agent 调用、真实归档和受控资料目录完成，不以 fixture-only 结果代替。

验收状态：PASS

## 35. 跨邮箱搜索

同一 provider-neutral MCP 可在获准范围内搜索 163 当前测试邮件和 QQ 2024 历史邮件；资源匹配不重复邮件行。

验收状态：PASS

## 36. 真实测试邮件生成

仅发送 1 封带 HTML、内嵌图片、3 个附件的验收邮件，复用已确认结果避免重复发件；未使用任意 MCP 收件人能力。

验收状态：PASS

## 37. 真实测试邮件收取

QQ SMTP 到 163 IMAP 的真实投递和收取已确认，轮询 8 次后进入正式统一归档；单封失败隔离与 ownership 均保持。

验收状态：PASS

## 38. 配置预览秘密脱敏

预览按结构只显示 AgentMailBridge 自身项，环境值与 token 全部脱敏；不会打印同文件中的其他 provider、模型或用户 secret。

验收状态：PASS

## 39. 备份保留策略

托管配置备份最多保留 20 份、90 天，并始终保留最近一份有效备份；备份使用当前用户可读写边界。

验收状态：PASS

## 40. Token 轮换失败恢复

Credential、数据库哈希和 Client 配置采用协调轮换；任一步失败均回滚到旧 token、旧哈希与旧配置，真实 Agent 在成功轮换后可重连。

验收状态：PASS

## 41. submit_result Client 目录限制

`submit_result` 保持旧参数兼容，同时新增 Client 资料目录授权；目标还必须继续通过 ALLOWED_SEND_ROOTS、ownership 和 Hash 校验。

验收状态：PASS

## 42. 未验证版本安全退化

只对已验证版本提供托管写入；未知或未来版本退化为 assisted，不猜测配置格式，也不覆盖用户文件。

验收状态：PASS

## 43. 撤销和恢复

暂停或撤销在下一次 MCP 调用立即生效，无需重启其他 Client；恢复、重新应用配置和撤销后清理自身项均通过。

验收状态：PASS

## 44. 审计

审计记录 Client、工具、结果、模式、资料包 Hash 和原子发布事实，不记录完整正文、附件内容、token 或邮箱 secret。

验收状态：PASS

## 45. 数据库与 migration

Agent schema v2 与历史导入表按事务、幂等方式升级；数据库 integrity 为 ok，迁移备份存在，旧 Client 权限未扩大。

验收状态：PASS

## 46. 覆盖升级

随机 AppId 隔离安装中，v1.5.0 安装后覆盖到 v1.6.0；账号、包、outbound、scheduler、OAuth 文件、Credential、Client 和 Hash 全部保留。

验收状态：PASS

## 47. 卸载

隔离卸载移除了程序文件，但保留数据库、OAuth、配置、Credential、邮件包和 Client；未静默删除用户数据。

验收状态：PASS

## 48. 重装恢复

同一隔离目录重装 v1.6.0 后，打包 GUI/MCP 探针、数据、Client、包 Hash 和 Credential 全部恢复；临时安装与测试 Credential 已清理。

验收状态：PASS

## 49. targeted tests

定向集合实际执行 123 项，全部通过。

验收状态：PASS

## 50. Full Suite Preflight

版本、Provider 状态、schema、`git diff --check`、compileall 和 123 项定向测试全部通过。

验收状态：PASS

## 51. final full pytest

实际执行最终全量测试一次：610 passed、1 skipped，耗时 1828.65 秒；未因 Claude Desktop 重复运行。

验收状态：PASS

## 52. pip check

实际执行 `python -m pip check`，结果为 No broken requirements found。

验收状态：PASS

## 53. clean build

清理 build/dist/release 后从当前源码构建 v1.6.0，PyInstaller GUI 与 MCP 均成功，版本资源一致。

验收状态：PASS

## 54. packaged smoke

打包 GUI self-test 和打包 MCP smoke 均通过；MCP 保持 7 个工具、UTF-8 stdio、stdout 仅协议数据。

验收状态：PASS

## 55. installer

生成 `AgentMailBridge-1.6.0-Setup.exe`，大小 44,453,499 bytes；Inno Setup 6.7.3 编译成功。

验收状态：PASS

## 56. ZIP

生成 `AgentMailBridge-1.6.0-Windows-x64.zip`，大小 70,253,101 bytes。

验收状态：PASS

## 57. checksums

安装器 SHA-256 为 `1d2815f652374db1ed3e2cfe401db09eb06124b57104a3f5e1f4208024b26f2a`；ZIP SHA-256 为 `e0f81676189ad3f72f98f696345ba64a20ece4aa17602715daaa5a368cdbfb76`，与 `checksums.sha256` 一致。

验收状态：PASS

## 58. secret scan

dist、release 和 ZIP 共检查 327 个文件；禁止文件名、secrets 路径和归档条目均未命中。本次源码目录没有可供比对的已配置 secret marker，扫描器如实报告 0 个 marker。

验收状态：PASS

## 59. Defender

Microsoft Defender 与实时保护均启用，签名版本 1.455.368.0；对 release 目录执行自定义扫描，0 个新检测。

验收状态：PASS

## 60. Authenticode

安装器、GUI EXE 和 MCP EXE 的实际状态均为 NotSigned。功能与本地验收不受影响，但在可信签名完成前不得表述为可信公开发布。

验收状态：CONDITIONALLY PASS

## 61. P0 / P1 / P2

P0：未发现 secret/token 泄露、账号或目录越权、路径/ownership/Hash 绕过、源包修改、配置不可恢复或私人资料入库。

P1：Codex Desktop 新任务真实调用受当前安全自动化边界限制；公开发布缺可信代码签名。

P2：普通 Claude Desktop 未测试；未知未来 Agent 版本只提供 assisted。

验收状态：CONDITIONALLY PASS

## 62. PASS / CONDITIONALLY PASS / FAIL

核心实现、真实 QQ→163 邮件、QQ 2024 历史、Codex CLI、Claude Code、Hermes、测试、构建和生命周期均通过。因 Codex Desktop 新会话证据与 Authenticode 尚未闭环，最终结论为有条件通过。

验收状态：CONDITIONALLY PASS

## 63. 已知限制

Gmail send、Outlook、Unified Inbox 不在本阶段；Generic 在独立第三方 E2E 前不升级为正式支持；超大历史范围需要按季度或更短自定义范围继续；未签名安装器可能触发 Windows 信誉提示。

验收状态：CONDITIONALLY PASS

## 64. 下一阶段建议

先观察真实 Agent 搜索频率、历史邮件价值和完整资料使用率，再从更多 Agent、Gmail Full Account、Outlook、Local API/CLI/Event、Sent/全文件夹同步中选择一个方向。

验收状态：PASS

## 65. commits

已生成 `a4c275b feat: deliver scoped agent history workflows`、`385e3be test: validate real agents history and lifecycle`、`164984f docs: publish v1.6 integration and delivery guides`。本报告提交与最终 push status 提交将在 Git 收尾后补记。

验收状态：CONDITIONALLY PASS

## 66. push status

当前本地 master 已完成前三个普通提交；本报告尚未提交和推送，最终状态将在 push status 提交中更新。

验收状态：CONDITIONALLY PASS

## A. Agent 验收矩阵

| Agent | Install Detect | Config | Real Tool Discovery | Search | Get | Read | Complete Package | 2024 History | Deny | Revoke | Audit | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex CLI | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Codex Desktop | PASS | PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | PASS | PASS | PASS | CONDITIONALLY PASS |
| Claude Code | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Hermes | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Claude Desktop | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE |

## B. 邮件能力矩阵

| Provider | New Mail | 2024 History | Body | raw.eml | Attachments | Inline Images | Complete Package | Agent Search | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| QQ | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| 163 | PASS | CONDITIONALLY PASS | PASS | PASS | PASS | PASS | PASS | PASS | CONDITIONALLY PASS |
| Gmail | PASS | CONDITIONALLY PASS | PASS | PASS | PASS | PASS | CONDITIONALLY PASS | PASS | CONDITIONALLY PASS |

163 与 Gmail 的条件项表示本阶段没有分别执行其 2024 全年真实补扫；Gmail 也没有单独重复完整资料真实 E2E。共享实现、既有正式归档和回归均通过，但未用实现就绪替代本阶段真实证据。

## C. 用户体验矩阵

| 场景 | Agent | Mail Scope | Complete Package | Final User Outcome | Status |
| --- | --- | --- | --- | --- | --- |
| 开发需求邮件 | Codex CLI | 当前获准邮箱 | PASS | 找到当前真实需求邮件并读取资源 | PASS |
| 历史资料查询 | Claude Code | 获准 QQ 2024 历史 | PASS | 找到历史邮件并取得正文、raw 与资源 | PASS |
| 完整资料交付 | Hermes | 获准邮箱与资料目录 | PASS | 原子生成完整资料包且原始归档不变 | PASS |
