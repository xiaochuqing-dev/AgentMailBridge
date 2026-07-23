# AgentMailBridge v1.4.4 QQ + 163 Real Provider Closure 真实双向收发验收报告

验收日期：2026-07-24

## 1. 基线 HEAD

基线分支为 `master`，基线 HEAD 与执行前 `origin/master` 均为 `4f2e4c683f7f9c1e665e3823275ce4db54193d34`。未创建新分支、Tag 或 GitHub Release。

## 2. 本阶段目标

以低频真实网络测试关闭 QQ、163 的登录、目录、收件、增量、发件、真实投递、富 MIME、归档、Hash、调度隔离和核心错误路径，将满足门槛的 Provider 从 implementation ready 升级为 supported。

## 3. 当前用户可用测试条件

本机已有一个 QQ 账号和一个 163 账号，二者的 IMAP/SMTP 授权码均存入 Windows Credential Manager。报告、源码、测试、日志和构建产物不记录邮箱地址或授权码。

## 4. 安全凭据边界

真实验证脚本只接受 account_id、显式网络确认和显式真实发件确认，不接受密码参数。凭据只从 account-specific Credential Manager 槽读取，不回显；未自动删除远端邮件，也未执行验证码、风控绕过或高频登录。

## 5. Research Gate

163 在 LOGIN 成功后 SELECT INBOX 返回 Unsafe Login。修复前核对了 [RFC 2971 IMAP ID](https://www.rfc-editor.org/rfc/rfc2971)、[网易邮箱客户端帮助](https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2eda07326646e6eb0)、[Mozilla 兼容记录](https://bugzilla.mozilla.org/show_bug.cgi?id=1105573) 和 [isync 兼容记录](https://sourceforge.net/p/isync/bugs/73/)。结论是增加 Profile 驱动的 RFC 2971 ID，且只发送真实产品名与版本。

## 6. QQ Real E2E

PASS。真实 IMAP/SMTP 登录、7 个目录发现、首次收件、第二次无变化增量、SMTP 发件、附件、目标实际到达、回收归档、重启后的 Credential Manager 复用均通过。最终 pending retry 和 needs_attention 均为 0。

## 7. 163 Real E2E

PASS。加入最小 IMAP ID quirk 后，真实 IMAP/SMTP 登录、6 个目录发现、收件、增量、发件、附件、目标实际到达、回收归档及重连均通过。

## 8. QQ → 163 Interop Matrix

| 路径 | SMTP | 目标真实到达 | IMAP 回收 | raw/package/facts | 附件与 Hash | ownership | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| QQ → 163 | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| 163 → QQ | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

## 9. QQ → QQ / 163 → 163

| 路径 | 中文主题与正文 | 附件 | 回收与去重 | 结果 |
| --- | --- | --- | --- | --- |
| QQ → QQ | PASS | PASS | PASS | PASS |
| 163 → 163 | PASS | PASS | PASS | PASS |

四条互发路径均验证三个附件，其中包含中文名、多附件和 0-byte 文件。

## 10. Provider Validation Matrix

| Provider | Auth | Login | Folder | Receive | Incremental | Send | Real Delivery | Attachment | Restart | Error | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Gmail | PARTIAL | FAIL | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | PASS | receive supported；本机 Token 刷新失败 |
| QQ | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | supported |
| 163 | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | supported |
| Generic | PASS | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | PASS | PASS | implementation_ready_e2e_required |
| Outlook | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | NOT_TESTED | planned |

Gmail 行只描述本机本轮状态，不改变既有 Gmail receive 正式支持结论；Gmail send 仍未实现。

## 11. Real Folder Matrix

| Provider | 真实目录 | delimiter | SPECIAL-USE/角色事实 |
| --- | --- | --- | --- |
| QQ | INBOX、Sent Messages、Drafts、Deleted Messages、Junk、其他文件夹、其他文件夹/QQ邮件订阅 | `/` | 服务端能力含 XLIST，但 LIST 未返回 Sent/Drafts/Trash/Junk SPECIAL-USE；除 INBOX 外保持 other，不臆造角色 |
| 163 | INBOX、草稿箱、已发送、已删除、垃圾邮件、病毒文件夹 | `/` | 返回 SPECIAL-USE；Drafts、Sent、Trash、Junk 正确识别，病毒文件夹保持 other |

QQ INBOX checkpoint 为 UIDVALIDITY 1624886883、UIDNEXT 225；163 INBOX 为 UIDVALIDITY 1、UIDNEXT 0。以上是服务端本轮实际返回事实。

## 12. Provider quirks

163 quirk 收口为 `ProviderProfile.imap_id_enabled`，登录后、任何目录操作前发送 `name=AgentMailBridge` 和版本；不发送用户或设备信息。QQ 暴露的是兼容 GUI 新授权码只更新 legacy 槽的问题，修复为只同步精确匹配 QQ 账号的 IMAP/SMTP 槽。没有在业务层散落 Provider 分支。

## 13. Generic Core 是否保持纯净

PASS。QQ 与 163 继续共享 Generic IMAP、Generic SMTP、parser、archive、retry、scheduler 和 Mail Facts。差异只位于 Profile flag 与兼容账号凭据同步层；Generic 独立第三方 E2E 仍为 NOT_TESTED。

## 14. UID / UIDVALIDITY

PASS。真实发现记录了 UIDVALIDITY、UIDNEXT 和 UID；自动化继续覆盖 overlap、checkpoint、UIDVALIDITY 代际变化和旧 retry 清理。未执行破坏性的远端 UIDVALIDITY 操作。

## 15. Incremental

PASS。QQ、163 连续第二次同步均返回健康的 no_changes。历史补扫覆盖 2026-07-23 至 2026-07-24：QQ 扫描 10、重复 10、失败 0；163 扫描 9、重复 9、失败 0。

## 16. Retry / Backoff

PASS。真实 QQ 初次恢复授权后发现三封旧邮件的非 ASCII Header 对象解析异常；单邮件隔离保证后续邮件继续处理。统一字符串化 Header 后再次真实收件为 scanned 13、saved 3、duplicates 10、failed 0，retry 归零。退避、有限重试和 UID 代际由自动化覆盖。

## 17. Scheduler / Isolation

PASS。最终强制调度检查 3 个账号：QQ no_changes、163 no_changes、Gmail oauth_error，整体 partial；Gmail 失败没有阻断 QQ 或 163。此前的 QQ 凭据错误同样未阻断 163。

## 18. SMTP

PASS。QQ、163 均通过正确 From/from_account_id、纯文本、中文主题、HTML、附件、多附件、中文文件名和 0-byte 文件验证；SMTP 返回成功后继续验证真实投递，不以 SMTP 接受作为唯一证据。

## 19. Real Delivery

PASS。QQ → 163、163 → QQ、QQ → QQ、163 → 163 均在目标 IMAP 服务端找到唯一测试标记，并由 AgentMailBridge 同步为正式归档。

## 20. Attachments

PASS。互发矩阵验证中文名、多附件、普通附件和 0-byte 文件；QQ 与 163 还分别通过 HTML、inline PNG 与三个附件组成的富 MIME 邮件。

## 21. raw.eml / Mail Package / Mail Facts / Hash

PASS。每条真实链路均验证实际 raw.eml、单一 package、Mail Facts、资源 ownership、账号 ownership、附件 size/SHA-256、发送前/发送归档 Hash 以及接收侧 Hash。

## 22. MCP

PARTIAL。Provider 验证通过 ApplicationService 的 account_id 搜索和 get_mail 核验真实归档。打包 MCP 的 UTF-8 stdio、七工具列表、默认拒绝、固定收件人与 EOF 退出 smoke 为 PASS。本机全局邮件读取 opt-in 仍关闭，因此真实 QQ/163 归档的 MCP search/get/read/prepare 未擅自开启，记为 NOT_TESTED。

## 23. GUI

PASS（非视觉）。QQ 兼容配置页更新授权码后同步精确账号 Credential Manager 槽的回归通过。无视觉布局变更，100%/125%/150% 深浅色截图为 NOT_TESTED。

## 24. 安装 / 升级 / 卸载

NOT_TESTED。已生成安装包并通过编译验证，但当前没有 Windows Sandbox、VM 或隔离用户，未在用户现有生产环境执行真实覆盖升级和卸载保留测试。

## 25. targeted tests

PASS。Full Suite Preflight 的定向回归为 91 passed，覆盖 Provider、Generic、多账号、GUI 凭据同步和 Windows 版本元数据。

## 26. Full Suite Preflight

PASS。版本、Provider 状态、schema、硬编码版本断言、`git diff --check`、compileall 和 targeted pytest 全部通过。

## 27. final full pytest

PASS。`578 passed, 1 skipped, 0 failed`，耗时 1629.53 秒。

## 28. clean build

PASS。执行 `scripts/build_windows.ps1 -SkipTests`，PyInstaller GUI 与 MCP 构建、build verification 均通过。

## 29. packaged smoke

PASS。打包 GUI 自检和 packaged MCP smoke 通过。

## 30. installer

PASS。生成 `release/AgentMailBridge-1.4.4-Setup.exe`，大小 38,414,189 bytes。

## 31. ZIP

PASS。生成 `release/AgentMailBridge-1.4.4-Windows-x64.zip`，大小 60,752,680 bytes。

## 32. checksums

PASS。

| 产物 | SHA-256 |
| --- | --- |
| AgentMailBridge-1.4.4-Setup.exe | `bb44571d8eb455bfd3010e53157603086a5a5e1aa62e41f2b48dd9de0ad59cf5` |
| AgentMailBridge-1.4.4-Windows-x64.zip | `c7a1a2b2215084d89712b48f75e6c670aaa845c37d1c6ac395f744a6f170edc6` |

## 33. secret scan

PASS。标准扫描检查 309 个 dist/release 文件和禁止文件名，0 命中；因为源码 `.env` 没有配置旧 secret marker，另从 Credential Manager 内存读取当前 QQ/163 两个实际凭据值，对 477 个 Git 候选与产物文件同时检查 UTF-8/UTF-16LE 字节，0 命中。扫描过程不输出凭据。

## 34. Defender

PASS。Microsoft Defender Antivirus 与实时保护均启用；分别扫描 release 与 dist/AgentMailBridge，0 新检出。

## 35. Authenticode

安装包、AgentMailBridge.exe 和 AgentMailBridgeMCP.exe 均为 `NotSigned`。状态已核验但不能表述为签名通过；若公开分发，应先配置代码签名证书。

## 36. P0 / P1 / P2

P0：0。P1：真实安装/覆盖升级/卸载保留未在隔离环境执行；公开分发产物未签名。P2：真实 MCP 正文读取因全局 opt-in 关闭而未测；本机 Gmail Token 需用户自行重新授权；视觉截图未测。

## 37. PASS / CONDITIONALLY PASS / FAIL

QQ + 163 Real Provider Closure：PASS。v1.4.4 本地验收产物：CONDITIONALLY PASS，条件是正式分发前完成隔离安装/升级/卸载和代码签名。FAIL：0。

## 38. QQ 正式支持状态

`supported`。真实门槛已覆盖 login、folder、receive、incremental、restart、send、real delivery、attachment、archive/raw/Hash/ownership 和核心错误恢复。

## 39. 163 正式支持状态

`supported`。真实门槛已覆盖，RFC 2971 ID 兼容已以最小 Profile quirk 收口。

## 40. Generic 状态

`implementation_ready_e2e_required`。自动化通过，但没有独立于 QQ/163 的第三方真实服务器和账号，不升级正式支持。

## 41. 已知限制

Gmail send 与 Outlook/Microsoft 未实现；Gmail OAuth scope 仍严格为 gmail.readonly。本机 Gmail Token 当前刷新失败。真实安装生命周期、MCP 全局 opt-in 后的真实读取和视觉截图未执行。构建产物未签名。

## 42. Sent / Folder 下一阶段建议

本阶段只采集真实目录事实，不扩大为全 Folder Sync。下一阶段可先设计 account-aware、mailbox-aware 的 INBOX + Sent 只读同步，QQ 需使用有证据的名称 fallback，163 可使用 SPECIAL-USE；不得污染现有 INBOX checkpoint。

## 43. 下一阶段路线建议

优先完成隔离安装生命周期与签名，然后再进入 Gmail Full Account / Gmail Send Permission Model；保持只读用户无破坏升级、最小权限和 Agent 发件权限独立。

## 44. commits

代码、测试、版本、文档和本报告将在当前 `master` 上按逻辑提交；最终 commit 以本报告所在 Git 历史为准，不创建 Tag。

## 45. push status

报告生成时为 PENDING。完成最终检查与提交后推送 `origin/master`，推送结果将在最终状态提交中更新。
