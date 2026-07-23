# AgentMailBridge v1.4.2 Generic IMAP/SMTP、QQ/163 双向收发验收报告

日期：2026-07-23

结论：CONDITIONALLY PASS。实现、自动化、构建、打包与安全扫描通过；QQ、163 和任意 Generic 真实服务器 E2E 因无独立测试凭据为 NOT_TESTED，因此不得对外宣称已完成真实 Provider 正式验收。

## 1. 基线 HEAD

开发基线为 `10eea93478544a5959df21da1b143ac82ec97d02`，分支为 `master`，远端为 `origin`。

## 2. 本阶段目标

建立 Provider-neutral Runtime Config，接通 Generic IMAP 持续收件和 Generic SMTP 正式发件，使 QQ 与 163 复用同一 Core 获得完整收发能力，同时保持 Gmail、统一归档、MCP 和安全边界不回归。

## 3. v1.4.1 技术债

v1.4.1 Generic IMAP 暂借 Gmail 字段，Generic SMTP 暂借 QQ 字段，只支持连接测试和目录发现。该 shim 已在新 Generic 路径移除；旧 Gmail/QQ 字段仅留在原 Provider 兼容 Adapter。

## 4. Research Gate

调研了 Thunderbird/ISPDB、Thunderbird Android ImapSync、Mailspring-Sync、Cypht、Nextcloud Mail/Horde、IMAPClient、email-mcp、better-email-mcp、RFC 9051、RFC 6154、Python smtplib 和 QQ 官方授权码说明。只借鉴架构和协议经验，没有复制外部项目业务源码。

## 5. Community Research Matrix

完整矩阵见 `docs/Generic IMAP SMTP 与 QQ 163 双向收发设计.md`。核心决策是：账号和 SMTP identity 分离；checkpoint 属于 account + mailbox；UIDVALIDITY 改变必须重置 UID 游标；Message-ID 只负责归档去重；大邮箱采用 polling、有界 scan cap 和小批次 fetch；单邮件失败与账号连接失败分开；目录解析交给 IMAPClient；SMTP 复用标准库。

## 6. Provider-neutral Runtime Config

新增内存态 `IncomingRuntimeConfig` 与 `OutgoingRuntimeConfig`，包含 backend、username、secret、host、port、security、timeout、mailbox 和 UID overlap。secret 不参与 repr。AccountRuntimeRouter 按 account_id 构造配置，Generic 不再覆写 gmail_address、gmail_app_password、qq_email 或 qq_auth_code。

## 7. Generic IMAP 架构

新增共享 `imap_sync.py`。QQ、163 与 Generic 经 Router 取得账号配置和 Credential，再使用 IMAPClient 登录、选择 mailbox、搜索 UID、批量读取真实 `BODY.PEEK[]`，最后进入既有 `normalized_mail_from_raw` 与 `process_normalized_mail`。

## 8. UID / UIDVALIDITY

checkpoint 保存 mailbox 级 UIDVALIDITY、UIDNEXT、HIGHESTMODSEQ、last_uid、reset count 和策略名。UIDVALIDITY 变化时只把该 mailbox 的 UID 游标重置为 0，再依靠账号级 Message-ID/provider id 唯一事实去重；不移动 package、不改 raw.eml、不重算 Hash。

## 9. 增量同步

增量搜索从 last_uid 向前保留有限 UID overlap，新 UID 受 scan cap 限制，fetch 批次为 25。成功的新 UID 推进 checkpoint；到期 retry 可脱离普通 overlap 窗口补入。初次、连续三轮 scan cap 推进、重复同步和 UIDVALIDITY reset 已自动化覆盖。

## 10. Folder / SPECIAL-USE

同步前刷新 LIST 目录并复用现有 RFC 6154 role 映射；目录刷新失败只记录告警，已配置 INBOX 仍继续。发现目录时合并 checkpoint，不覆盖已有 last_uid。Sent/Drafts/Trash/Junk/Archive 只按 SPECIAL-USE 或保守 profile 识别，不把同名目录错误合并。

## 11. Retry / reconnect

批量 fetch 异常时降级为逐 UID；单封缺失、fetch 或归档异常进入 `mailbox:uid` 有限 retry，不阻断后续邮件。认证、TLS、timeout、disconnect 和普通连接错误使用稳定账号级错误分类，交给既有 per-account scheduler/backoff。没有无限重试。

## 12. Generic SMTP

现有 MIME、收件人校验、附件 staging、Hash、outbound 和 sent archive 保留。底层发送改为读取 OutgoingRuntimeConfig，支持 SSL/TLS 与 STARTTLS，执行 EHLO、TLS 升级、认证和 send_message。

错误区分 connect、tls、auth、recipient_rejected、sender_rejected、timeout、temporary、server_unavailable、message_too_large 和 send。错误文案不包含凭据或完整服务端敏感响应。

## 13. QQ receive/send

QQ 使用 `imap.qq.com:993 SSL` 和 `smtp.qq.com:465 SSL`，完整邮箱地址为用户名，授权码分别写入账号级 IMAP/SMTP credential slot。receive/send、统一归档、历史补扫、目录发现、Mail Facts 与 outbound ownership 已接通并自动化通过。

真实 QQ 登录、收信、增量、发信、回收和附件：NOT_TESTED。

## 14. 163 receive/send

163 使用 `imap.163.com:993 SSL` 和 `smtp.163.com:465 SSL`，完整邮箱地址为用户名，账号级授权码进入 IMAP/SMTP credential slot。创建、收发 capability、SMTP 发件与 from_account_id/outbound ownership 自动化通过。

163 个人邮箱官方帮助页本次无法稳定取得，默认端点参考 Thunderbird ISPDB。真实登录、收信、增量、发信、回收和附件：NOT_TESTED。

## 15. Account ownership

收件 account_ref 改为读取真实 Incoming Runtime username；正式 account_id 仍由 runtime_account_id 决定。发件的 outbound_messages、sent_files 和资源事实写入明确 from_account_id、真实 From 与 Provider 账号引用。同 Message-ID 在不同账号仍可独立存在。

## 16. Scheduler

QQ、163、Generic 的 receive capability 进入既有逐账号 scheduler、锁、retry 和 backoff。一个账号的连接失败不占用其他账号锁，单封坏邮件不升级为全局失败。no_changes 与 partial 语义保持。

## 17. GUI

新增 163 账号类型；QQ 与 163 显示 receive + send，使用共享授权码编辑器；Generic 按实际 IMAP/SMTP host 显示能力。账号页继续提供后台连接测试、目录发现、启停和保守移除；收件页和发件页复用既有 account_id 选择。

人工 100%、125%、150% 深浅色截图：NOT_TESTED。本阶段没有进行大范围视觉重构。

## 18. MCP

仍只有一个 AgentMailBridge MCP 和七个工具。QQ、163、Generic 的 package 可按 account_id 进入 search/get/resource/sync status；ensure_fresh 只同步指定账号。`submit_result` schema 不增加发件账号或 recipient，固定 `OWNER_GMAIL` 边界不变。

## 19. 安全

Gmail OAuth scope 仍严格且唯一为 `gmail.readonly`。IMAP/SMTP 只允许 SSL/TLS 或 STARTTLS。凭据仅存 Windows Credential Manager，不进入 SQLite、checkpoint、日志、诊断、报告或 Git。MCP 路径白名单、stdout purity、Hash 和审计边界保持。

## 20. 自动化测试

最终冻结代码全量结果：564 passed、1 skipped，1562.63 秒。

重点 v1.4.2、多账号、GUI/迁移回归：45 passed。首次完整发布运行发现两个旧 1.4.1 版本断言及 Windows 数字版本四元组未同步；修正后定向 2 passed，并重新执行全量得到上述最终结果。

覆盖 initial/incremental、UID overlap、UIDVALIDITY、scan cap、单邮件失败、SSL/STARTTLS、recipient reject、163 outbound ownership、schema v3 QQ 迁移，以及既有 Gmail、MCP、GUI、归档、附件与安全回归。真实网络 timeout/rate limit/Provider quirks 仍属于 E2E 缺口。

## 21. 真实 E2E

QQ：NOT_TESTED。

163：NOT_TESTED。

Generic 第三方服务器：NOT_TESTED。

原因是没有独立安全测试账号。未使用用户重要邮箱，也没有执行真实外发。Adapter 状态为 `implementation_ready_e2e_required`，不能写成正式线上通过。

## 22. Gmail regression

Gmail API/IMAP 收件、多账号 OAuth、scheduler、统一 package、Mail Facts 与 MCP 回归均包含在全量测试中。没有新增 Gmail send，没有改动唯一 `gmail.readonly` scope。

## 23. Build

`scripts/build_windows.ps1` clean build PASS。Python 3.11.15、PyInstaller 6.21.0、IMAPClient 3.1.0。GUI packaged self-test PASS，MCP packaged smoke PASS，build verification PASS。

GUI、MCP 与 Setup 的 FileVersion/ProductVersion 均为 1.4.2。

## 24. Installer

生成 `release/AgentMailBridge-1.4.2-Setup.exe`，大小 38,404,097 bytes。

SHA-256：`084d7a6694a70fa2efcf243f767cc9538f10ca75df2575e4f2de8b137bbbe012`

真实安装、v1.4.1 覆盖升级与卸载保留：NOT_TESTED，缺少隔离 Windows 测试环境。

## 25. ZIP

生成 `release/AgentMailBridge-1.4.2-Windows-x64.zip`，大小 60,735,773 bytes。

SHA-256：`09863db71d41cf2349270569ca8a8252b078dc26d53d1e630fe1ac222660ad03`

## 26. Checksums

`release/checksums.sha256` 已生成并与现场重新计算结果一致。

## 27. Secret scan

dist/release/ZIP 扫描 PASS：309 个文件，0 个当前配置 secret marker。Git tracked forbidden path 数量为 0。`.env`、credentials.json、token.json、数据库、日志、邮件和附件未进入产物或提交范围。

## 28. Defender

Windows Defender 对 release 与 dist 执行 CustomScan：PASS。历史检测总数 3，扫描后仍为 3，无新增检测。

## 29. Authenticode

AgentMailBridge.exe：NotSigned。

AgentMailBridgeMCP.exe：NotSigned。

AgentMailBridge-1.4.2-Setup.exe：NotSigned。

当前无签名证书，可能出现 SmartScreen 未知发布者提示。

## 30. P0 / P1 / P2

P0：0。

P1：3 个验收缺口，分别为 QQ 真实 E2E、163 真实 E2E、Generic 真实服务器兼容矩阵，均为 NOT_TESTED。

P2：3 个发布缺口，分别为真实安装/升级/卸载、人工 DPI 深浅色截图、Authenticode 签名。

## 31. 最终判定

CONDITIONALLY PASS。

代码、自动化、构建、打包和安全扫描满足交付条件；因为核心 Provider 真实 E2E 未执行，不能判定完全 PASS，也不能把 QQ/163 标为已完成真实正式支持。

## 32. 已知限制

- 仅 polling + UID checkpoint；没有启用 IDLE、CONDSTORE、QRESYNC。
- 只同步配置的 mailbox，默认 INBOX；远端 Sent 不自动追加副本。
- Provider 限流、特殊目录、中文目录和安全策略依赖真实服务器验证。
- Gmail send 与 Outlook/Microsoft 保持 planned。
- MCP 不提供任意账号或任意收件人发送。

## 33. 下一阶段

先用独立 QQ、163 和至少一个 Generic 测试账号完成真实 E2E 矩阵并沉淀安全 Provider quirks；通过后再评估是否把 Adapter 状态升级为正式。Gmail send 权限模型和 Outlook/MSAL/PKCE 应分别立项，不在本阶段夹带实现。

## 34. Commits

- `65174d7 feat: enable generic IMAP SMTP mail providers`
- `316ab14 test: cover generic mail provider runtime`
- `f3a7690 docs: document v1.4.2 design and validation`

本节的交付状态补记使用独立文档提交承载，避免改写以上已验收提交。

## 35. Push status

PASS。2026-07-23 已正常推送 `10eea93..f3a7690` 到 `origin/master`，未使用 force push。当前状态补记将在随后一个文档提交中同步到同一主分支。不创建 Tag、GitHub Release 或 Release Assets。
