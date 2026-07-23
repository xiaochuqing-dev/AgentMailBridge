# AgentMailBridge v1.4.3 Provider Validation & Hardening 真实 E2E 验收报告

报告日期：2026-07-23

结论：CONDITIONALLY PASS

本结论表示 v1.4.3 协议硬化、自动化、预检、构建和静态制品验收通过；不表示 QQ、163、Generic 已完成真实账号 E2E，也不表示它们已经成为正式支持 Provider。

## 1. 基线 HEAD

开发基线：`e5aafa922ad39123c9636c71016becf9adb776e4`

基线提交：`docs: record v1.4.2 delivery status`

开发分支：`master`

## 2. 本阶段目标

本阶段不新增大型 Provider，不扩展 Gmail OAuth scope，不实现 Gmail send、Outlook 或 Unified Inbox。目标是建立真实 Provider 验证入口，硬化 IMAP/SMTP 的目录、UIDVALIDITY、重试和错误分类，建立 Full Suite Preflight，并完成可重复的 Windows 候选制品验收。

## 3. v1.4.2 已完成能力

v1.4.2 已具备统一账号模型、按账号隔离的 runtime、Generic IMAP/SMTP、QQ/163 profile、Mail Package、raw.eml、Mail Facts、附件 Hash、自动收件、重试、MCP 按 account_id 查询和 Windows 打包。

## 4. v1.4.2 验收缺口

QQ、163 和独立 Generic 第三方服务器没有真实账号 E2E；真实目录、服务端错误、重连、风控和安装升级行为尚无证据。原有 retry 标识未绑定 UIDVALIDITY，IMAP 国际化目录和网络异常分类仍有硬化空间，完整测试前也缺少统一预检。

## 5. Research Gate

本阶段先核对成熟规范和客户端资料，再实施最小修改：

- RFC 9051：UID 仅在 mailbox 当前 UIDVALIDITY 代际内稳定；SPECIAL-USE 属可选能力。
- RFC 8314：邮件提交与访问优先使用 TLS，隐式 TLS 端口是成熟部署方式。
- RFC 5321：SMTP 4xx 表示可重试临时失败，5xx 表示永久失败。
- RFC 3463：增强状态码 5.3.4 表示消息对接收系统过大。
- Thunderbird ISPDB：QQ 与 163 使用完整邮箱地址作为用户名，IMAP 993 与 SMTP 465 使用隐式 TLS。
- QQ 官方资料：第三方客户端使用授权码，不把网页登录密码写入应用。

主要来源：

- https://www.rfc-editor.org/rfc/rfc9051.html
- https://www.rfc-editor.org/rfc/rfc8314.html
- https://www.rfc-editor.org/rfc/rfc5321.html
- https://www.rfc-editor.org/rfc/rfc3463.html
- https://autoconfig.thunderbird.net/v1.1/qq.com
- https://autoconfig.thunderbird.net/v1.1/163.com
- https://service.mail.qq.com/detail/0/75

163 官方资料在本次调研时没有形成稳定、可直接引用的完整配置依据，因此以 Thunderbird ISPDB 和通用 RFC 为主要依据；没有把未经真实验证的差异写成 Provider quirk。

## 6. QQ 真实 E2E

状态：NOT_TESTED。

当前环境没有专用 QQ 测试账号和授权码。未执行真实登录、收件、增量、发件、回收、附件、重启或错误注入，也未使用用户生产邮箱冒险测试。受控验证脚本已经支持显式网络确认、真实发件二次确认和脱敏 JSON 证据，可在用户提供专用测试账号后执行。

## 7. 163 真实 E2E

状态：NOT_TESTED。

原因和安全处理与 QQ 相同。未把自动化结果伪称为真实服务器通过。

## 8. Generic 真实 E2E

状态：NOT_TESTED。

当前环境没有 QQ/163 之外的专用标准 IMAP/SMTP 测试账号，因此没有证明第三方服务器兼容矩阵。Generic Core 保持 implementation ready。

## 9. Provider Validation Matrix

| Provider | Auth | Login | Folder | Receive | Incremental | Send | Attachment | Restart | Error | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| Gmail | OAuth，readonly | 已有正式接收路径 | 已有 | 已有 | 已有 | Planned | 接收已有 | 已有 | 自动化回归 | Receive supported |
| QQ | 授权码实现 | 自动化 PASS，真实 NOT_TESTED | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | implementation_ready_e2e_required |
| 163 | 授权码实现 | 自动化 PASS，真实 NOT_TESTED | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | implementation_ready_e2e_required |
| Generic-Test | Credential 实现 | 自动化 PASS，真实 NOT_TESTED | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | 自动化 PASS | implementation_ready_e2e_required |
| Outlook | 未实现 | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | planned |

## 10. Provider quirks

没有在缺少真实证据时建立大型 quirk framework。QQ/163 的 host、port、TLS、完整邮箱用户名规则继续收口在 Provider Profile；Generic Core 没有新增散落的 provider if/elif。新的真实差异必须由 E2E 证据驱动后再加入小型 profile override。

## 11. Folder / SPECIAL-USE

目录发现现在能解码 IMAP modified UTF-7 bytes，并保留服务端 delimiter、flags 和 SPECIAL-USE 事实。SPECIAL-USE 缺失时不臆造目录角色。真实 QQ/163 中文目录、Sent、Drafts、Trash、Junk 仍待真实 E2E。当前不扩大为 Sent 同步；完成真实目录矩阵后再决定是否实现最小 INBOX + Sent。

## 12. UID / UIDVALIDITY

重试身份升级为 `mailbox + UIDVALIDITY + UID`。发现 UIDVALIDITY 变化时，只清理对应账号和 mailbox 的失效技术重试，不删除历史、raw.eml、Mail Package 或附件。对同代际旧版 `mailbox:uid` 和纯 UID 重试保留兼容读取，避免升级后丢失可恢复任务。

## 13. Retry / reconnect

到期重试会按当前 UIDVALIDITY 代际合并进扫描；成功后清理兼容 retry 标识。单邮件失败只持久化安全 error code 或异常类型，不保存服务端完整响应。连接重置、断开、不可用、超时、TLS、认证和限流均有独立安全分类。

## 14. Scheduler / isolation

多账号 scheduler、backoff、disabled/re-enabled 和失败隔离沿用 v1.4.2 架构并通过相关自动化回归。真实 QQ 失败不影响 163/Gmail、重启后恢复间隔等跨真实服务器行为仍为 NOT_TESTED。

## 15. SMTP

SMTP 分类按 RFC 语义硬化：4xx 为临时可重试，5xx 为永久失败；认证、发件人、收件人、断线、服务不可用和消息过大单独分类。552 和增强状态码 5.3.4 可识别为消息过大。用户可见错误不回显授权码或完整敏感服务端响应。

## 16. Attachment

附件、中文文件名、0 字节附件、大小与 SHA-256 审计路径由既有自动化覆盖。真实 QQ/163/Generic 附件发件和回收未执行，状态为 NOT_TESTED。

## 17. Mail Package / raw / Hash

本阶段没有改变一个邮件一个正式 archive object 的边界。raw.eml 仍来自实际 IMAP `BODY.PEEK[]` 或 Gmail raw；资源保持 package-relative；源、staged、pre-SMTP 和 sent archive 的 size/SHA-256 核验继续保留。相关回归通过。

## 18. MCP

继续使用一个 provider-neutral MCP。account_id filter、get_mail、resource、sync status、ensure_fresh 和固定 OWNER_GMAIL 的 submit_result 边界未扩张。最终 packaged MCP smoke 通过，stdout 协议和 UTF-8 路径回归通过。

## 19. GUI

本阶段没有视觉结构调整，只更新版本和必要说明。没有使用浏览器或 Computer Use 执行 OAuth。100%、125%、150% DPI 深浅色人工截图未重做，标记 NOT_TESTED。

## 20. 安装 / 升级 / 卸载

安装器成功构建，版本资源一致。由于当前没有 Windows VM、Sandbox 或独立测试用户，未执行 v1.4.1/v1.4.2 真实覆盖升级、Credential/OAuth/用户数据保留和卸载保留验证，状态为 NOT_TESTED。为保护用户当前安装与数据，没有在本机生产用户环境执行破坏性验证。

## 21. Full Suite Preflight

最终执行通过。版本、Provider 状态、schema version、硬编码断言、`git diff --check`、`compileall` 和目标测试全部通过；目标测试结果为 86 passed。

## 22. targeted tests

v1.4.3 新增测试与 v1.4.2 Generic 定向回归：17 passed。新增覆盖国际化目录、错误脱敏、连接重置、SMTP 临时/永久/超大邮件以及 UIDVALIDITY 失效重试清理。

## 23. final full pytest

命令：`python -m pytest -q`

结果：573 passed，1 skipped，0 failed，耗时 1740.13 秒。

补充检查：`python -m pip check` 返回 No broken requirements；`python -m agent_mail_bridge --version` 返回 `agent_mail_bridge 1.4.3`。

## 24. build

命令：`powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SkipTests`

结果：clean build 通过，GUI packaged self-test、MCP packaged smoke 和 build verification 通过。构建脚本即使使用 `-SkipTests` 也会运行不含测试执行的 Preflight，避免跳过版本和状态一致性检查。

## 25. installer

文件：`release/AgentMailBridge-1.4.3-Setup.exe`

大小：38,410,156 bytes

SHA-256：`D30F781DE6BE63482E367795AD952C16D897073B31FE49A8A12F28E2FA7785F0`

FileVersion：1.4.3

## 26. ZIP

文件：`release/AgentMailBridge-1.4.3-Windows-x64.zip`

大小：60,746,812 bytes

SHA-256：`9B7F4FF23070C40C36EF661DE71797CD3EED3C95B2B81B356CCC9F02C4E22A01`

ZIP 条目：316；禁止项：0。

## 27. checksums

`release/checksums.sha256` 已生成，内容与最终 installer 和 ZIP 重新计算结果一致。

## 28. secret scan

结果：PASS。扫描 309 个文件，0 个已配置 secret marker；Git 跟踪文件和 ZIP 中均未发现 `.env`、credentials.json、token.json、数据库、日志、邮件或附件数据。

## 29. Defender

Microsoft Defender AntivirusEnabled=True，RealTimeProtectionEnabled=True，签名版本 1.455.277.0。对最终 release 与 dist 自定义扫描前后检测数均为 3，新增检测 0。

## 30. Authenticode

安装包、`AgentMailBridge.exe` 和 `AgentMailBridgeMCP.exe` 状态均为 NotSigned。未伪称已签名。

## 31. P0/P1/P2

P0：0。

P1：

1. QQ 真实账号完整 E2E 未执行。
2. 163 真实账号完整 E2E 未执行。
3. Generic 第三方真实服务器 E2E 未执行。
4. v1.4.1/v1.4.2 覆盖升级与卸载保留未在隔离环境执行。

P2：

1. Windows 制品未签名。
2. 本阶段未重做 DPI 与深浅色人工截图。
3. Sent 目录真实矩阵和最小同步方案待真实验证后决定。

## 32. PASS / CONDITIONALLY PASS / FAIL

CONDITIONALLY PASS。

通过范围是 v1.4.3 代码硬化、自动化、Preflight、clean build、packaged smoke、校验和、secret scan 和 Defender。真实 Provider 与真实安装验收存在 P1 缺口，因此不能给出整体 PASS。

## 33. Provider 正式支持状态

Gmail 接收继续为正式稳定能力，OAuth scope 仍严格为 `gmail.readonly`，发送仍为 planned。

QQ、163、Generic 继续为 `implementation_ready_e2e_required`，未升级为正式支持。

Outlook/Microsoft 继续为 planned。

## 34. 已知限制

缺少专用真实账号和隔离 Windows 验收环境；没有真实服务端目录、风控、速率限制、回收链路或升级保留证据；制品未签名；Sent 同步未进入本阶段。

## 35. 下一阶段建议

先准备专用 QQ、163 和一个非 QQ/163 的 Generic 测试账号，在低频、非破坏性条件下执行受控验证脚本并保存脱敏证据。随后在 Windows Sandbox、VM 或独立测试用户完成安装、覆盖升级和卸载保留。只有四项 P1 全部闭环且无新阻断，才评估 Provider 状态升级；在此之前不进入 Gmail send 或 Outlook。

## 36. commits

实现提交：`28f08e73b352e3623fd792ba1ece022b462cf2b4`，`feat: harden provider validation for v1.4.3`

验收文档提交：`5c3ee22fe53976b073647d311cf0bc45f197068c`，`docs: record v1.4.3 validation evidence`

最终 push 状态补记使用独立文档提交。

## 37. push status

状态：PASS。实现与包含本报告的验收文档已通过普通 `git push origin master` 推送到 `https://github.com/xiaochuqing-dev/AgentMailBridge.git`，远端 `master` 已更新到 `5c3ee22fe53976b073647d311cf0bc45f197068c`。最终状态补记提交完成后继续普通推送，并再次核对远端 HEAD。

本阶段没有创建 Tag、GitHub Release 或公开 Release Assets。
