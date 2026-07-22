# AgentMailBridge v1.4.0 Multi-Account Core 多邮箱架构地基重构报告

总体结论：CONDITIONALLY PASS。统一账号、Provider 边界、账号级 ownership、可回滚迁移、统一 MCP 与 GUI 基础结构已落地，核心实现、模拟多账号、打包、ZIP、安装器生成、秘密扫描和 Defender 均通过。真实 Gmail/IMAP/SMTP 网络 E2E、当前已安装 v1.3.0 的覆盖升级和真实用户库迁移未执行；三个 EXE 未签名。未创建 Tag 或 GitHub Release。

## 1. 开始前 HEAD / 基线版本

开始前分支为 `master`，HEAD 与 `origin/master` 均为 `2975575deeaa5db02d002170166861e7fae58f66`，工作树干净，产品版本为 1.3.0。远端提交在实现完成前再次 fetch，未发现分叉。

## 2. 当前旧架构问题复盘

v1.3.0 的 Gmail 收件、QQ 发件、Mail Package、Mail Facts、MCP 七工具和安全链路成熟，但账号仍隐含在 `.env` 单值、`account_ref`、全局同步状态和全局重试键中。同类型账号无法安全保存相同 Message-ID，收件、发件、同步、邮箱目录和归档缺少统一一等账号外键。

## 3. 开源社区参考与不重复造轮子说明

调研了 Mailspring、Cypht、Nextcloud Mail、email-mcp、Thunderbird 桌面账号设计和 Thunderbird Android。借鉴“底层账号独立、上层统一视图”、账号/邮箱目录/身份分层、Provider 模块化、统一 MCP 与引导式配置边界。现有 Gmail API/IMAP、Google OAuth、QQ SMTP、Mail Package、Mail Facts、SQLite、MCP 与 Qt 页面全部复用，没有重写邮件协议，也没有复制外部业务代码。

## 4. 参考项目与借鉴点

- Mailspring：https://github.com/foundry376/Mailspring 。借鉴本地同步引擎、账号/文件夹/会话/消息边界和统一收件视图。
- Cypht：https://github.com/cypht-org/cypht 。借鉴多 Provider/协议模块聚合与组合视图。
- Nextcloud Mail：https://github.com/nextcloud/mail 。借鉴多账号统一视图和复用成熟邮件库而非自写协议。
- email-mcp：https://github.com/codefuturist/email-mcp 。借鉴 Account/Provider/Service 分层、Provider 检测与一个 MCP 管理多个账号的方向。
- Thunderbird Account Configuration：https://developer.thunderbird.net/thunderbird-development/codebase-overview/account-configuration 。借鉴账号容器、incoming server、identity、SMTP server 分离。
- Thunderbird Android：https://github.com/thunderbird/thunderbird-android 。借鉴多个账号与 Unified Inbox 并存的产品结构。

## 5. 许可证与代码复用边界

AgentMailBridge 继续使用 MIT。Mailspring 为 GPL-3.0，Cypht 为 LGPL-2.1，Nextcloud Mail 为 AGPL-3.0，email-mcp 为 LGPL-3.0，Thunderbird Android 为 Apache-2.0；Thunderbird 桌面资料仅参考公开设计文档。上述项目只用于架构研究，没有复制代码、资源或实现片段，因此未把 copyleft 义务引入本仓库。协议层继续使用 Python 标准库、Google 官方 API/OAuth 客户端和项目既有依赖。

## 6. Multi-Account Core 最终设计

数据流为 `统一产品/MCP → MailAccount → Provider Adapter → 既有 Gmail/QQ 实现`。底层事实按账号隔离，上层默认统一查询；Provider Adapter 只声明认证、可用能力、已实现能力和后端，不创建未使用的大型框架。

## 7. MailAccount 模型

`MailAccount` 包含稳定 `account_id`、provider、规范化邮箱地址、显示名、认证类型、收件/发件开关、启用状态、`data_namespace`、capabilities、非秘密 provider settings 和来源。ID 由 provider 与规范化地址计算，不含邮箱明文。账号表不保存密码、授权码、OAuth URL、Client Secret 或 Token。

## 8. Provider / Adapter 抽象

Gmail Adapter 当前实现 receive/archive/mail_facts，继续使用 Gmail API 或 IMAP；QQ Adapter 当前实现 send/outbound_archive，继续使用 SMTP。Generic IMAP/SMTP 与 Microsoft Adapter 仅登记 planned 边界。Gmail 发件、QQ 收件、163 和 Outlook 未被误报为可用。

## 9. Gmail / QQ 现有能力如何迁移

现有 Gmail 配置映射为 receive-enabled 正式账号，认证类型根据 Gmail API/IMAP 选择；现有 QQ 配置映射为 send-enabled 正式账号。GUI 保存账号后同步模型，CLI 初始化、应用启动、收件、发件、自动任务和归档链路均写入稳定账号归属；原协议代码和安全语义未重写。

## 10. 数据库迁移

新增 `mail_accounts`、`mailboxes`、`account_sync_states`。`mail_packages/received_messages/received_files/receive_retries/receive_rule_evaluations/outbound_messages/sent_files` 增加账号字段。v1.3 `received_messages` 与 `receive_retries` 在同一 `BEGIN IMMEDIATE` 事务中安全重建为账号复合唯一键；索引与 ownership 回填在提交前完成，迁移元数据为 `multi_account_core_v1`。

## 11. 旧数据保全

需要迁移时先创建 `before_v1_4_multi_account` 在线备份。迁移不移动文件、不重新生成 raw、不重算或伪造历史事实。专项 fixture 验证旧 package、outbound、sent、retry、sync 映射，重复初始化不增行，故障注入整体回滚，`raw.eml` SHA-256 前后相同。真实用户数据库本次未打开或修改。

## 12. account ownership

每个 package 带 `account_id/mailbox_id`，manifest 升级为 v2；兼容收件行、收件文件、规则评估和有限重试带账号；发件邮件与发送文件带 `from_account_id`；同步状态按账号保存。同一 Message-ID 可在两个 Gmail 账号各自归档，同账号内仍保持去重。

## 13. GUI 多账号基础结构

左侧收敛为统一“邮箱账号”可滚动列表，Gmail 和 QQ 卡不再以“收件/发件”作为固定标题，而显示真实当前能力；稳定账号 ID 与数据命名空间可通过 Tooltip 观察。“添加邮箱账号”保留未来 Provider 入口，但明确尚不开放第二个同类型账号或未接通 Provider。原 Gmail API/IMAP、QQ 配置页和网络线程边界保持。

## 14. MCP 兼容与未来扩展

仍只有一个 `AgentMailBridgeMCP` 和七个工具。`search_mails` 新增可选 `account_id`，保留 `account_ref`；省略时跨账号统一查询，指定时严格过滤。`get_mail_sync_status` 返回当前收件账号与已启用账号摘要。`submit_result` schema 不含 recipient，目标仍固定 `OWNER_GMAIL`；路径白名单、只读 Gmail scope、UTF-8/stdout purity、审计和 Hash 链不变。指定发件账号属于下一阶段，没有提前扩大 MCP 权限。

## 15. 自动化测试结果

正式 build 的完整 pytest 首轮结果为 534 passed、1 skipped、1 failed，耗时 1568.67 秒。唯一失败是 Windows 清理性能测试临时目录时后台线程本地 SQLite 句柄未显式释放；在后台维护线程 `finally` 中关闭连接后，失败用例单独复跑为 1 passed（2.52 秒）。其余已通过用例未重复运行。源码 compileall、版本命令、变更空白检查均通过。

## 16. 真实 E2E / 模拟多账号验证

模拟多账号 PASS：两个 Gmail、两个 QQ、稳定 ID、Provider 区分、同步隔离、同 Message-ID package 隔离、outbound 隔离、查询过滤、迁移幂等、回滚、旧数据和 Hash 均覆盖。真实本地进程 E2E PASS：dist 与 ZIP 中 GUI self-test、MCP initialize/tools/list/tools/call、七工具、UTF-8/BOM、malformed JSON、中文路径、stdout purity 和 EOF。真实 Gmail API、IMAP、QQ SMTP 网络 E2E 为 NOT_TESTED，本阶段没有触发 OAuth 或发送邮件。

## 17. clean build

先执行一次完整 clean build；测试发现上述句柄问题后停止。修复与目标复跑通过后，使用 `build_windows.ps1 -SkipTests` 仅继续未执行步骤，避免重复完整测试。PyInstaller GUI/MCP、build verification、packaged smoke、Inno Setup、ZIP、checksums 和 secret scan 最终退出码均为 0。

## 18. installer / ZIP

生成 `AgentMailBridge-1.4.0-Setup.exe`（40,987,396 bytes）和 `AgentMailBridge-1.4.0-Windows-x64.zip`（64,111,801 bytes）。ZIP 独立解压后 GUI self-test 与 MCP smoke 通过。当前已安装 v1.3.0 且有两个外部 MCP 进程占用安装目录，本次未强杀进程，实际覆盖安装与卸载保留标记 NOT_TESTED；安装器编译和静态安装边界测试通过。

## 19. 数据保留

隔离迁移测试确认数据库 facts、raw 和 Hash 保留，文件系统未移动。构建、ZIP smoke 和 Defender 只处理构建产物/隔离目录。真实配置、OAuth、Credential Manager、邮件、日志、备份及安装目录未被修改；真实用户库迁移为 NOT_TESTED。

## 20. 敏感扫描 / Defender

tracked forbidden-file 检查 PASS；dist/release/ZIP secret scan PASS，共检查 279 个产物文件，未读取或输出秘密值。Defender 与实时保护启用，签名版本 1.455.263.0；对 release 与 dist 执行 CustomScan，命令成功且无检测错误。GUI EXE、MCP EXE、Setup.exe 的 Authenticode 均为 NotSigned，没有伪称签名通过。

## 21. 已知限制

- GUI 暂不新增第二个同类型账号，认证文件/凭据仍由当前 Gmail/QQ 兼容配置管理。
- 只正式接通 Gmail 收件与 QQ 发件；其他能力仅有 Adapter 边界。
- `ensure_fresh` 仍同步当前已配置 Gmail 收件账号；多账号调度编排属于后续。
- 真实覆盖安装、真实用户库迁移、卸载保留和外部网络 E2E 未执行。
- Authenticode 未签名；未做本阶段新增界面的人工六档截图，100%/125%/150% 布局由 Qt 自动化覆盖。

## 22. P0 / P1 / P2

P0：无。P1：公开交付前，在外部 MCP 进程退出后完成 v1.3→v1.4 覆盖安装、真实库迁移摘要和安装版 MCP 复验。P2：补代码签名证书、人工 DPI/深浅色截图；后续阶段再实现多账号认证存储、真实新增账号与多账号调度。

## 23. PASS / CONDITIONALLY PASS / FAIL

Multi-Account Core 代码、模拟迁移、隔离、兼容、构建、ZIP、秘密扫描和 Defender 为 PASS。整体为 CONDITIONALLY PASS，条件是补做真实已安装环境升级/迁移验证与签名治理；没有 FAIL 或阻断本阶段代码合并的问题。

## 24. 下一阶段建议

按同一 Adapter 边界依次完成 Gmail 收+发、QQ 收+发、Generic IMAP/SMTP（优先覆盖 163 等标准邮箱）、Microsoft/Outlook，再开放真正的多账号添加向导与 Unified Inbox。每个账号需独立认证存储、同步任务、失败退避与发送权限；不要为 QQ、163、Yahoo 分别复制 IMAP/SMTP 实现。

## 25. Git commits / push 情况

核心实现提交：`e4a94bec4c0606e8a011805f26363cb58e458329`（`feat: establish v1.4 multi-account core`）。文档与本报告将在独立提交中记录并推送到 `origin/master`；最终 HEAD 与 push 结果以任务最终交付输出和本报告后续状态补记为准。没有 force push、Tag 或 GitHub Release。

## 产物 SHA-256

- Setup：`c3df1c097639b40262ec311329ea1e29893cf7727a09c8f8fc7cf2f7d1d83ba0`
- Portable ZIP：`05d269da712030b694bfe5ab32067c430f63aef7b7c27ec746b74fc82e699c12`
