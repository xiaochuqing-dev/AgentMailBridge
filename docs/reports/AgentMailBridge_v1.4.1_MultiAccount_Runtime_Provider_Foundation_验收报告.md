# AgentMailBridge v1.4.1 Multi-Account Runtime & Provider Foundation 验收报告

总体结论：CONDITIONALLY PASS。Multi-Account Runtime、账号 CRUD、Credential/OAuth 隔离、逐账号调度、Generic Provider Foundation、GUI、统一 MCP、迁移、全量回归、clean build、portable overlay、秘密扫描、Defender 和哈希均通过。真实外部邮箱联网、多账号真实凭据 E2E、已安装旧版覆盖安装与 Authenticode 签名仍按事实标记 NOT_TESTED 或未满足；未创建 Tag 或 GitHub Release。

## 1. 基线 HEAD 与阶段目标

开始前分支为 `master`，HEAD 与 `origin/master` 均为 `97be87855afc7cc5bb26a3a42e9b96fd34b33c6c`，工作树干净，产品版本为 1.4.0。本阶段目标是把 v1.4.0 的账号 ownership 数据地基升级为真正由 `account_id` 驱动的运行时，同时建立 Generic IMAP/SMTP 的安全协议基础。

## 2. 上阶段遗留问题

v1.4.0 已有 MailAccount、Provider Adapter、mailboxes、account_sync_states 和主要业务事实 ownership，但收件仍主要使用单个 Gmail 配置，发件仍主要使用单个 QQ 配置，OAuth、Credential、锁、同步状态和 GUI 操作没有真正按账号隔离。上阶段完整回归曾有一个 Windows 临时目录 SQLite 句柄失败，单项修复已通过；本阶段重新执行最终全量回归。

## 3. 开源社区调研与 Research Gate

正式实现前研究了 Thunderbird Account Manager/Autoconfig、Mailspring per-account Sync Engine、Cypht 多 Provider 模块、email-mcp 的 account/service 分层、Nextcloud Mail 多账号与成熟库复用，以及 Google、Microsoft、IMAP RFC、IMAPClient 和 Python smtplib 官方资料。完整 Community Architecture Research Matrix 与资料链接见 `docs/Multi-Account Runtime 与 Provider Foundation 设计.md`。

共同成熟经验是：稳定账号身份与服务器属性分离；凭据、连接、同步、错误和退避按账号隔离；统一收件箱属于上层查询而不是底层状态合并；IMAP UID、UIDVALIDITY、SPECIAL-USE 和国际化目录交给成熟协议库；账号移除与本地历史删除分开确认。

## 4. Community Architecture Research Matrix 摘要

| 问题 | 社区成熟解 | AgentMailBridge 决策 |
| --- | --- | --- |
| 账号身份 | Thunderbird、Mailspring、Nextcloud Mail 均使用稳定账号实体 | provider + 规范化地址生成稳定不透明 account_id，地址不可原地改写 |
| 运行时 | Mailspring 每账号实例，Thunderbird 每 incoming server 独立 | 单进程轻量 Router + 每账号 context/锁，复用既有 Gmail/QQ 实现 |
| Credential/OAuth | 按账号/server identity 隔离 | Credential Manager key、OAuth 目录、Token、锁均按 account_id |
| Scheduler | 每账号状态、错误和退避 | account_sync_states 独立保存；协调器只聚合，不覆盖账号状态 |
| IMAP | 成熟客户端库解析协议与目录 | 使用 New BSD IMAPClient；不自写 LIST/UID/SPECIAL-USE 解析 |
| 删除 | 停止账号与删除本地邮件分开 | soft remove；默认保留 package、raw、附件、outbound 和审计 |
| Unified Inbox/MCP | 底层账号独立，上层统一查询 | 一个 SQLite、一个 MCP；查询可按 account_id 过滤 |

## 5. 最终架构决策与不重复造轮子

调用链为 `GUI / MCP / ApplicationService → account_id → AccountRuntimeRouter → Provider Adapter → 既有 Backend`。Router 负责读取账号、检查 enabled/removed/capability、装配只属于该账号的内存配置和凭据。Gmail API/IMAP 收件、QQ SMTP 发件、Mail Package、Mail Facts、历史补扫、发送归档与安全校验继续复用，没有复制协议实现。

没有引入“一账号一进程”的桌面重型结构，也没有建设 SaaS、Provider-specific MCP 或最终 Unified Inbox。SQLite 仍统一保存本机事实，但所有运行事实和数据 ownership 按 account_id 分开。

## 6. 许可证边界

Thunderbird Autoconfig 为 MPL-2.0，Mailspring Sync 为 GPL-3.0，Cypht 为 LGPL-2.1，email-mcp 为 LGPL-3.0，Nextcloud Mail 为 AGPL-3.0。本项目只借鉴架构、错误隔离与测试思想，没有复制其业务源码、资源或配置数据库。新增 IMAPClient 3.x 为 New BSD 许可，已加入依赖与 THIRD_PARTY_NOTICES；SMTP 继续使用 Python 标准库。

## 7. Account Runtime 与 Provider Adapter

新增 AccountRuntimeRouter 和 AccountRuntimeContext。收件、历史补扫、GUI 手动发件、连接测试、目录发现、OAuth 状态与授权均可通过 account_id 获取对应运行配置。runtime_account_id/runtime_provider 只存在于内存副本，不写入 `.env` 或数据库秘密字段；归档、retry、package 与 outbound 继续写入稳定账号 ownership。

Provider Registry 只声明真实能力。Gmail 正式实现 receive/archive/mail_facts；QQ 正式实现 send/outbound_archive；Generic 只实现 connection_test/folder_discovery；Microsoft 仍为 planned。业务入口在 capability 校验后才调用后端，未接通能力返回稳定错误。

## 8. Account CRUD 与移除策略

GUI 与服务层支持 create/read/update、enable/disable 和 soft remove。同一 provider + email 使用稳定 account_id；provider 与 email_address 创建后不可原地变更。移除会停止收发和调度，但保留 mail package、raw.eml、附件、发件、重试、审计和 ownership。Credential 与 OAuth Token 只有用户勾选时才清理；不提供静默物理删除历史数据的快捷方式。相同稳定身份重新创建时恢复原记录，不制造 orphan ownership。

## 9. per-account Credential

新账号使用 `account:<account_id>:<secret_kind>` Windows Credential Manager target，不含邮箱明文。IMAP 和 SMTP secret kind 分开；数据库、provider_settings、日志、MCP 和报告均不保存或回显秘密。旧 Gmail/QQ key 只允许精确匹配的兼容账号迁移，第二个同 Provider 账号不能 fallback。用户显式删除兼容账号凭据时同时清理对应旧 key，避免被再次回填。

## 10. per-account OAuth

Gmail Desktop credentials 与 token 位于 OAuth 根目录的 `accounts/<account_id>/`。旧单账号 credentials/token 只向精确匹配的 legacy Gmail 一次性原子复制，原文件保留；迁移完成标记防止用户清除 Token 后又被旧文件自动恢复。导入、替换、授权、取消、清除和状态查询均接受 account_id，一个账号的 Token 操作不影响其他账号。现有 Desktop-only 校验、超时、可取消、回调关闭、账号核对和 Token 原子替换逻辑全部复用。

## 11. Scheduler、Retry、Backoff 与错误隔离

每个账号独立保存 enabled、interval、last check/success/result/error、failure count、next check 和 checkpoint，并使用独立线程锁与 `receive-<account_id>.lock` 进程锁。协调器顺序运行到期账号以减少 SQLite 写竞争，每个账号异常都转换为局部失败后继续。成功、no_changes 和 partial 恢复正常间隔；认证或连接失败按 30、60、120、300、600、900 秒退避；cancelled 不增加失败计数。

全局 GUI 协调器状态与账号状态分开保存。修改全局间隔只更新各账号指定配置字段，不抹掉 last result/retry/backoff；协调器的下一次唤醒取各账号最早 next_check。停用账号会停止其状态，重新启用时按全局自动收件开关重新加入调度。

## 12. 多账号隔离证据

自动化验证两个同 Provider Gmail 可同时存在，稳定 ID、Credential、内存配置、OAuth 路径、Token 清除、同步状态、backoff、锁和路由互不冲突；两个 QQ 使用独立 SMTP secret namespace。账号 A 抛出普通失败或未分类异常后，账号 B 仍继续运行；未分类异常不把详情写入结果。真实第二 Gmail/QQ 的外部网络登录未执行，标记 NOT_TESTED。

## 13. Gmail send 决策与 OAuth scope

Google 官方文档确认 Gmail API `messages.send` 需要 `gmail.send`、`gmail.compose` 或更宽权限；SMTP XOAUTH2 通常需要完整 `mail.google.com` scope。项目安全不变量要求 scope 必须且只能是 `gmail.readonly`。因此 v1.4.1 没有扩大 scope、没有要求存量用户重新授权，也没有把 Gmail send 误报为完成。

Gmail send 状态为 NOT_IMPLEMENTED / planned。这是安全边界下的明确架构决策，不是测试遗漏。后续如单独立项，必须先完成权限模型、重新授权迁移、GUI 与 MCP 发件权限分离以及真实发送/回收 E2E。

## 14. Generic IMAP/SMTP Foundation

新增静态 Provider Profile、TLS-only 配置校验、连接超时、IMAP/SMTP 凭据隔离、SMTP 仅认证不发送、IMAP LIST 目录发现、RFC 6154 SPECIAL-USE 映射、capability 读取以及 INBOX UIDVALIDITY/UIDNEXT/HIGHESTMODSEQ checkpoint。IMAP 使用 IMAPClient，SMTP 使用 smtplib；不允许 plain transport，也拒绝 password/token/client_secret 等进入 provider_settings。

Generic 正式持续 receive/send、IDLE、QRESYNC、完整 UID 增量、Provider quirks 和大邮件 E2E 尚未实现，状态保持 experimental foundation / planned。QQ 收件、163、Yahoo 和自建邮箱不会因为存在 profile 就被标记为正式支持。

## 15. Microsoft / Outlook 决策

Microsoft 官方资料确认 Exchange Online IMAP/SMTP 需要 Entra OAuth 与协议权限，Graph Mail.Send 需要独立发件权限，桌面应用应使用系统浏览器授权码 + PKCE。Outlook 不能被当成“服务器 + 密码”的 Generic 特例。本阶段只保留 planned Adapter；MSAL/PKCE、个人与组织租户、管理员同意、Graph/协议路线和真实租户 E2E 留待独立阶段。

## 16. GUI

“添加邮箱账号”已从演示入口变为真实创建页，可创建第二个 Gmail、第二个 QQ 和 Generic foundation 账号。账号卡显示 Provider、地址、真实 capability、启用、认证和最近状态；旧 Gmail/QQ 卡也进入按账号管理页。账号页支持 enable/disable、凭据更新、连接测试、IMAP 目录发现、按账号 OAuth、清除 Token 和保守移除。

收件页可选择实际收件账号，手动收件、连接测试与历史补扫使用同一选择；发件页只列出已启用且正式接通 send 的账号。OAuth 和网络测试继续在后台 Worker 运行，关闭窗口或应用会取消并等待会话。自动化覆盖 100%、125%、150% 和现有深色交互回归；本阶段未执行人工六档截图，标记 PARTIAL。

## 17. MCP

仍只有一个 AgentMailBridge MCP 和七个工具。`get_mail_sync_status` 新增可选 account_id，省略时保持兼容；既有 search_mails account_id 过滤继续使用。`submit_result` schema、固定 OWNER_GMAIL、路径白名单、staging、幂等、Hash 链和审计未改变，不接受 recipient 或 from_account_id。GUI 选择发件账号没有扩大 Agent 权限。

## 18. 数据迁移与旧数据保全

Multi-Account schema version 从 1 升为 2，mail_accounts 新增 removed_at。需要迁移时继续先做正常升级备份，再以事务补列和更新迁移元数据。隔离 fixture 从 v1.4 schema 1 升级到 v1.4.1，验证 raw.eml、中文附件、SHA-256、OAuth 原文件、按账号 OAuth 副本、旧 Credential 与历史 ownership 保持；不移动 package，不重写 raw，不重算历史 Hash。

## 19. v1.3/v1.4 覆盖升级

隔离 portable overlay 为 PASS：先解压 1.4.0 ZIP，确认 GUI/MCP 文件版本为 1.4.0 且旧 GUI packaged self-test 退出码 0；随后在同一目录覆盖解压 1.4.1 ZIP，确认双 EXE 版本为 1.4.1，新 GUI self-test 与 MCP packaged smoke 均通过。测试使用固定临时目录、隔离 HOME/DATA_ROOT、禁用 dotenv 和真实 Credential Store。

真实已安装 v1.3/v1.4 通过安装器覆盖、真实用户数据库摘要、注册表/快捷方式与卸载保留未执行，原因是缺少一次性 Windows VM/隔离用户环境，且不得影响当前用户真实安装、凭据和数据，标记 NOT_TESTED。数据库迁移 fixture 与 portable overlay 不冒充真实安装器覆盖。

## 20. 最终 pytest、compile 与 E2E

冻结代码最终全量 pytest 为 555 passed、1 skipped，耗时 1731.28 秒（28:51），无失败。跳过项为现有平台条件项。重点核心、GUI、MCP、调度和兼容组合回归为 115 passed；新增 v1.4.1 账号、OAuth、Credential、Provider、升级和 UI 测试也包含在最终全量结果中。compileall、pip check、`python -m agent_mail_bridge --version`（1.4.1）与 `git diff --check` 通过。

packaged E2E 为 PASS：clean dist 与 portable overlay 均通过 GUI self-test；MCP initialize、ping、tools/list、七工具顺序、tools/call、UTF-8 BOM、中文内容、malformed JSON、未知 method、读取默认关闭、path_not_allowed、stdout purity 和 EOF 通过。真实 Gmail API/IMAP、QQ SMTP、Generic 外部服务器、多账号真实 OAuth/收发均未执行，统一标记 NOT_TESTED。

## 21. clean build、packaged MCP、installer 与 ZIP

最终全量测试通过后执行 `build_windows.ps1 -SkipTests`，避免重复 28 分钟测试但不跳过任何打包门禁。clean build 的 PyInstaller 双 EXE、GUI packaged self-test、MCP packaged smoke、版本验证、单一 Gmail discovery document、ZIP、Inno Setup 6.7.3 installer、checksums 和 secret scan 均退出码 0。

产物为 `AgentMailBridge-1.4.1-Setup.exe`（41,198,729 bytes）和 `AgentMailBridge-1.4.1-Windows-x64.zip`（64,493,428 bytes）。GUI、MCP 和 Setup 的 FileVersion/ProductVersion 均为 1.4.1。安装器生成 PASS；真实安装执行仍为 NOT_TESTED。

## 22. checksums、secret scan、Defender 与 Authenticode

checksums 文件与独立 SHA-256 复算一致：

- Setup：`3eb71068e594e24abb8b2e588cea6eda5c1897b0a276e5091d190788ca55e5fd`
- Portable ZIP：`3f494bcfa89f0cd00f7e09bc0f6fbbe331d674b4eb3d4829c055937f5ed374d9`

dist/release/ZIP secret scan 为 PASS，检查 287 个产物文件；当前隔离构建环境没有非空真实 secret marker，因此 marker 数为 0，文件名与归档路径排除仍执行。另以 Git tracked forbidden-file scan 检查 154 个路径，未发现 `.env`、credentials.json、token.json、数据库、raw 邮件或秘密目录。IMAPClient 3.1.0 dist-info、AUTHORS 与 New BSD COPYING 已进入包。

Windows Defender Antivirus 与实时保护均启用，签名版本 1.455.277.0。对 release 和 dist/AgentMailBridge 执行 CustomScan，命令成功；系统历史检测记录总数扫描前后均为 3，无新增。本任务没有判断或删除既有历史检测记录。

Authenticode 事实为：AgentMailBridge.exe NotSigned、AgentMailBridgeMCP.exe NotSigned、Setup.exe NotSigned。没有把未签名写成 PASS。

## 23. P0 / P1 / P2 与最终判定

P0：无。

P1：在公开交付前使用隔离 Windows VM/测试用户执行真实已安装 v1.3/v1.4→v1.4.1 覆盖、用户库迁移摘要、OAuth/Credential 保留、快捷方式、安装版 MCP 与卸载保留；使用用户自有测试账号完成第二 Gmail、第二 QQ 和至少一个 Generic 服务器的真实认证/失败隔离 E2E。不得使用浏览器自动化完成 OAuth。

P2：配置代码签名证书；补人工 100%/125%/150% 深浅色六档截图；评估裁剪未使用的打包可选模块。

自动化、构建与安全门禁没有阻断问题，整体判定 CONDITIONALLY PASS。条件是上述 P1 在正式公开发布前完成；Gmail send 因 readonly 安全不变量而未实现，不作为伪失败处理。

## 24. 已知限制与下一阶段建议

已知限制：Gmail send 未实现；Generic 只有连接与目录基础；Microsoft、163、QQ receive 尚未正式上线；真实多账号网络 E2E、真实安装覆盖和签名未完成；Unified Inbox 最终产品化不在本阶段。

下一阶段建议先建立可撤销的 Gmail 发件权限方案评审；若坚持唯一 readonly scope，则优先把 Generic IMAP 持续同步做成经过真实账号验证的独立阶段，先完成 UIDVALIDITY、分页、有限 retry、断线恢复和 package E2E，再逐 Provider 上线。Microsoft 应保持独立 MSAL/PKCE 阶段。不要为 QQ、163、Yahoo 各复制一套协议逻辑。

## 25. commits、GitHub push 与 Release

实现提交为 `ebf474c`（`feat: activate multi-account runtime foundation`）和 `6ea0164`（`feat: expose multi-account controls in GUI and MCP`）。最终文档提交与 GitHub push 状态在提交后回填。要求正常推送 `origin/master`，不得 force push。未获得用户明确批准，不创建 Tag、GitHub Release 或公开 Release Assets。
