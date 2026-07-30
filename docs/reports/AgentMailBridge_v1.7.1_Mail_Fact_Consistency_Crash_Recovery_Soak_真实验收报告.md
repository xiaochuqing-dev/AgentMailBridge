# AgentMailBridge v1.7.1 邮件事实一致性、崩溃恢复与长期运行真实验收报告

验收日期：2026-07-30 至 2026-07-31

总体结论：CONDITIONALLY PASS。邮件事实一致性、发件恢复、Sent 对账、checkpoint、临时资料治理、GUI、自动化、真实 QQ/163、真实安装版 Codex CLI/Claude Code/Hermes、构建和安装生命周期均有实际通过证据。已知 P0 为 0。安装器和双 EXE 均为 NotSigned，是 P1 发布限制。Codex Desktop v1.7.1 独立新任务、物理 Windows 多次睡眠恢复和受控多小时 soak 本次未完成；7 至 14 天仅为后续 dogfood 计划，不计入本次实测。

## 1. 起始 HEAD

开始时本地 `master` 与 `origin/master` 均为 `98d2c95f55518cac9d9aff7c192f1d7aa693ac78`，工作区干净。v1.7.0 最终报告、当前 schema、发布物和全量基线均在 Research Gate 中记录。

## 2. 最终 HEAD

核心实现提交为 `0b3448baf901c9077f345aad41b6203bb182c039`。包含产品文档、69 项报告与 7 张矩阵的首次完整推送 HEAD 为 `18821153185f0f63fde1a99282c295810e4eaeb2`；GitHub `refs/heads/master` 已实际核对为同一 SHA。其后的提交仅回填本次推送状态。

## 3. 本阶段目标

停止扩展大功能，保证邮件移动、网络失败、进程中断、SMTP 不确定边界、Sent 延迟和清理操作后，系统仍能解释唯一事实、目录归属、发送状态、恢复材料与人工处理要求。

## 4. 产品边界

保持 Windows-first、local-first、single-user 和一个 Provider-neutral MCP。未增加 Gmail send、Outlook、新 Provider、新 Agent、内置 AI、自动回复、Web API、SaaS、删除/移动/已读修改或 GUI 框架重写；Gmail OAuth scope 仍严格为 `gmail.readonly`。

## 5. Research Gate

Research Gate 已在编码前完成并提交为 `2bd6bababaaf7aae0dd8125c92e3a55d63c5367b`。报告位于 `docs/research/AgentMailBridge_v1.7.1_邮件一致性与恢复_开源调研.md`，包含问题、许可证、RFC、复用决策、供应链、打包影响和最小自定义范围。

## 6. 开源复用清单

直接复用现有 IMAPClient、Python email/smtplib、python-email-validator、SQLite transaction/WAL/backup/integrity_check、Windows Credential Manager/ACL 与现有原子写入辅助。参考 Proton Bridge、OfflineIMAP3、isync、Thunderbird、notmuch、JMAP、MailKit、RQ/Celery 的状态与恢复模式，不复制不兼容代码。

## 7. 禁止重复造轮子执行结果

未重写 IMAP、MIME、SMTP、地址校验、SQLite、Credential 或文件锁核心。项目自定义代码只负责本产品特有的永久事实、多目录 membership、不可幂等 SMTP 恢复、保守 Sent 对账、两阶段快照清理和受限修复编排。

## 8. 许可证核对

IMAPClient 为 New BSD，MailKit/aiosmtplib 为 MIT，python-email-validator 为 Unlicense，JMAP 为 Apache-2.0，SQLite 为 Public Domain，Python 为 PSF。GPL 的 Proton Bridge、OfflineIMAP3、isync、notmuch 仅参考设计，不复制代码。Claude Code 下划线 server key 兼容方案参考官方仓库 issue `anthropics/claude-code#80065`，只实现最小配置迁移。

## 9. mail fact 模型

`mail_packages` 保持唯一永久事实，raw.eml、资源、Hash、package_id、account_id 和 resource_id 不因目录变化重建。新邮件身份使用账号、Provider、raw/内容指纹等确定证据，Message-ID 不再单独充当唯一键。

## 10. membership 多对多

现有 `mail_package_mailboxes` 升级为正式 membership，记录 account、mailbox、UID/UIDVALIDITY、first/last seen、currently_present、removed_at、来源和对账状态。同一事实可同时位于 Inbox、Sent、Archive、Label 和自定义目录。

## 11. direction 证据

direction 与 mailbox 分离，证据来自本地 outbound、SMTP accepted、Provider Sent、Inbox、Provider metadata 和迁移事实。冲突进入 reconciliation/health issue，不按当前目录名静默覆盖。

## 12. Gmail 多 Label

自动化覆盖一封消息多个 labelIds、新 Label 发现、明确空 labelIds 和重复扫描。结果只增减 membership，不复制 package，也不扩大 selected mailbox 权限。真实 Gmail 仍只读，本次未使用自动化 OAuth。

## 13. IMAP 目录移动

Inbox 到 Archive、Sent 到自定义目录通过完整 UID 快照和 membership 对账自动化验证。旧位置标记 server absent，新位置建立归属；package、direction、raw 和 Hash 保持不变。本次没有新增独立真实服务器移动证据。

## 14. server deletion

周期 UID 快照自动化验证服务器删除：仅把 membership 更新为 `currently_present=0` 并记录 removed_at，不删除本地永久事实。失败或 partial 快照不能据此标记缺失。

## 15. send state machine

状态覆盖 pending_confirmation、cancelled、expired、ready_to_send、acquiring_lease、sending、smtp_accepted、sent_archive_pending、sent_waiting_reconciliation、sent_reconciled、definitely_not_sent、delivery_unknown、sent_archive_failed、recovery_required 和 failed，并持久记录阶段事件。

## 16. lease

发送使用 SQLite `BEGIN IMMEDIATE` 条件领取 durable lease，保存 owner、process/session、attempt、acquired/heartbeat/expires、current stage、固定 Message-ID 和 MIME Hash。双执行者与两个真实子进程只能有一个获得执行权。

## 17. heartbeat

只有当前且未过期 owner 能短事务续租；过期 owner 不能重新取得所有权。SMTP 网络调用仍受 Provider timeout 限制，heartbeat 不持有长事务。

## 18. stale lease

DATA 前的 stale lease 可确定为未发送；DATA 已开始则进入 delivery_unknown。启动恢复跳过活跃 lease，过期不等于允许重发。自动化覆盖 stale owner、恢复领取与状态分类。

## 19. startup recovery

初始化扫描非终态请求，按本地 outbound、固定 MIME/Message-ID、数据库结果和 Sent mapping 分类。恢复可重复执行，不修改已完成事实，也不把未知结果送回自动发送队列。

## 20. SMTP 接受后崩溃

故障注入覆盖 SMTP callback/Persistence 失败、accepted 后 archive 前中断和 outbound 发布后数据库失败。SMTP 尝试保持 1；后续只用固定 MIME 恢复事实或对账，不第二次调用 SMTP。

## 21. delivery_unknown

DATA 已开始但没有确定 accepted/not-sent 证据时持久化 delivery_unknown，保留 MIME 与附件快照，不自动重发。用户可重新对账、标记已发送/未发送，或用新 idempotency key 创建新请求。

## 22. sent_archive_failed

Provider 已接受但本地 package 发布失败时保存 sent_archive_failed。恢复校验固定 MIME Hash 后只发布原 bytes；归档失败不会触发新的 SMTP。

## 23. Sent 对账

先要求 account ownership 一致，再使用 provider id、raw Hash、唯一 Message-ID 加 Header/内容/附件指纹和有界时间窗。真实 confirm、Claude Code autonomous、Hermes autonomous 均形成 Sent mapping，SMTP 各 1 次。

## 24. ambiguous 候选

重复 Message-ID、多个同等级 request 或证据不足时记录 ambiguous，不任选候选、不自动合并。自动化覆盖重复 Message-ID 和多个 request 的模糊 Sent 观察。

## 25. external client outbound

无本地 request 的 Sent 观察可形成 external outbound package，但不伪造 Client、确认模式或 idempotency key。该路径有自动化通过证据；本次没有新增网页/手机独立真实发送证据。

## 26. checkpoint 保护

每个 account_id + mailbox_id 独立保存最后成功与当前尝试。网络、SQLite 或落盘失败不清零最后成功 checkpoint；只有事实和 membership 成功提交后才能推进。

## 27. UIDVALIDITY

UIDVALIDITY 变化只使受影响目录进入 reconciliation/full rescan，历史 package、membership 历史、raw 和 Hash 不变，其他目录继续。迁移和故障注入均覆盖该边界。

## 28. 单目录失败隔离

目录级 attempt、failure count 和 checkpoint 独立。单目录失败、单封损坏邮件与 partial 结果不阻断其他目录，也不把失败目录伪报为完整快照。

## 29. 快照保留

pending、sending、delivery_unknown、sent_archive_failed、recovery_required 和活跃 lease 均保护恢复材料。cancelled、expired、definitely_not_sent 或已完成并超过保留期的请求才可能成为清理候选。

## 30. 清理 dry-run

预览只返回脱敏 ID、类别、估算字节和原因。执行时重新校验状态、lease、ownership、路径与 Hash；两阶段移动到 DATA_ROOT 隔离区，事务提交后删除，失败可恢复或回滚。

## 31. 工作副本

相同 package/manifest/Hash 继续复用；用户修改不覆盖，删除副本不影响永久事实。健康和一致性扫描可发现失效记录与孤立候选，但安全清理不删除用户工作副本。

## 32. 一致性扫描

只读扫描覆盖数据库/package、manifest identity、raw/resource Hash、相对路径、ownership、membership、direction、request/outbound、Sent mapping、checkpoint、lease、线程、snapshot、工作副本和 secret canary。修复基于最新扫描、一次单项、先在线备份、白名单动作并审计。

## 33. 运行健康 GUI

六档浅/深色 100%、125%、150% 截图和 150% 紧凑窗口均 PASS。隔离数据包含 130 条问题、12 个目录、6 条 delivery_unknown、4 条 archive recovery 和大容量数字；几何证据无横向滚动或控件溢出，代表截图人工抽查无明显遮挡和主题串色。

## 34. 大邮件

自动化覆盖大附件、50+ 附件、同名/零字节/Unicode 文件名和超限附件隔离。发布前进行容量检查；局部资源失败不会删除已建立的邮件事实。

## 35. 损坏 MIME

损坏 MIME、缺失类型、异常 Date、极长 References 和无 Message-ID 均有定向覆盖。实际观察时间不被异常声明时间覆盖，线程只使用唯一确定证据。

## 36. 磁盘不足

故障注入在 staging 前模拟 ENOSPC，写入在发布前失败，不留下半成品或推进数据库事实。容量预检集成到原子写入路径。

## 37. 文件锁

仅对 Windows WinError 5/32 执行有限短退避；其他错误立即失败。故障注入验证锁解除后原子替换成功，重试有界。

## 38. SQLite busy

SQLite busy 故障保持 request 与 lease 原状态，不误推进发送阶段。事务失败回滚，后续可由正常恢复流程重新处理。

## 39. 双进程

两个真实 Python 子进程并发领取同一 request 的测试只有一个成功，另一个不能获得 lease；没有重复 SMTP 权限。

## 40. Windows 睡眠恢复

系统时间回拨和未来 membership snapshot 时间已自动化验证，避免长期停止对账。物理 Windows 多次睡眠、休眠和唤醒本次未完整执行，不能宣称 PASS，列为后续 dogfood 条件项。

## 41. Codex Desktop

本次无法创建符合要求的 v1.7.1 独立 Codex Desktop 新任务，因此未沿用 v1.7.0 Desktop PASS，也未用 CLI 或 tools/list 代替。当前项为验收缺口，不构造虚假证据。

## 42. Codex CLI 安装版

Codex CLI 0.146.0 使用 packaged MCP 完成真实自然语言读取：Inbox/Sent、历史范围、6 次成功工具调用、6 个准备资源、完整 package 与源归档不变；外部进程 359.11 秒退出码 0。v1.7.0 的超时问题在当前版本闭环。

## 43. Claude Code

Claude Code 2.1.220 packaged read 为 PASS：6 次工具调用和 6 个准备资源。autonomous send 为 PASS：7 次能力调用、1 个 request、SMTP 1 次、真实送达、2 个附件 Hash 一致、Sent mapping 与 outbound raw 均通过；外部进程分别 168.39 秒和 95.61 秒。

## 44. Hermes

Hermes 0.19.0 packaged read 为 PASS；autonomous send 为 PASS：组合读写 8 次成功调用、SMTP 1 次、真实送达、2 个附件 Hash 一致、Sent mapping 与 outbound raw 均通过；外部进程分别 136.86 秒和 208.41 秒。

## 45. QQ 真实 E2E

QQ Inbox/Sent 目录发现、真实新建/回复/回复全部/转发链路、confirm/autonomous 相关投递、outbound raw、Sent mapping、去重和暂停/恢复/撤销参与 12 项真实邮件流检查，整体 PASS。

## 46. 163 真实 E2E

163 Inbox/Sent 与 QQ 互为隔离测试端，真实送达和附件计数、回复/转发、自身地址排除、Sent mapping 与去重均 PASS。未删除服务器邮件或改变私人邮件状态。

## 47. 故障注入矩阵

自动化覆盖 before lease、after lease、MIME 后、before SMTP、DATA started、accepted 后、archive 前后、startup、双进程、磁盘、文件锁、SQLite busy、cleanup transaction、损坏/大 MIME、时间与地址边界。详见矩阵 A；所有未知或 accepted 边界均证明不自动重发。

## 48. 持续运行实际时长

本次没有单独计时、可审计的受控多小时产品 soak，因此不能给出虚假的持续运行 PASS。最长连续自动化进程是最终全量 pytest 2427.02 秒；五次真实安装版 Agent 外部进程合计 968.38 秒，但两者都不是后台邮件长期 soak。

## 49. 7 至 14 天后续 dogfood 计划

`docs/长期运行验证计划.md` 已定义每天的同步、真实 Agent、confirm/autonomous、目录移动、Sent 回流、清理、扫描、物理睡眠和指标记录。该计划尚未执行，绝不计入本次验收。

## 50. migration

`mail_consistency_v171` schema version 3 使用增量、单事务、幂等迁移并在升级写入前备份。旧 package_id/account_id/resource_id、raw、附件和 Hash 保持不变，旧 membership/outbound/Sent 数据保留。

## 51. 覆盖升级

随机 AppId、隔离路径执行 1.7.0 到 1.7.1 真实覆盖升级，v1.7.1 表与 schema、迁移备份、旧 Client 默认无 send 扩权、账号/OAuth/Credential/数据库/package Hash 全部通过。

## 52. 卸载

隔离卸载移除程序文件但保留用户数据和 Credential；5 个账号、4 个 package、1 个 outbound、4 个 scheduler state、4 个 raw、4 个附件和 2 个 OAuth JSON 事实保持。

## 53. 重装

同一隔离数据上重装后 GUI packaged self-test、MCP initialize/tools/sync/UTF-8 stdout/EOF 和数据恢复通过；测试安装与测试 Credential 已清理，生产安装未触碰。

## 54. targeted tests

v1.7.1 故障恢复与一致性专项为 52 passed；完整定向集为 107 passed；存储、配置、工作区和兼容回归为 67 passed。最终 Preflight 再运行 204 项定向回归，23.96 秒全部通过。

## 55. full pytest

最终完整 pytest 实际执行结果为 695 passed、2 skipped、0 failed，耗时 2427.02 秒。之后只完成真实 Agent 证据与报告收口，没有再修改产品源码。

## 56. build

clean Windows build `-SkipTests` 实际通过，版本、双 EXE 资源和 installer 元数据均为 1.7.1。构建不是用旧 dist 覆盖，随后执行 packaged smoke。

## 57. packaged smoke

GUI packaged self-test、MCP 11 工具、UTF-8/BOM/EOF、stdout 纯净、默认拒绝与 sync status 均通过；隔离安装、升级和重装也重复验证 initialize/tools/EOF。

## 58. installer

生成 `AgentMailBridge-1.7.1-Setup.exe`，大小 52,209,192 bytes。安装、升级、卸载保留与重装恢复均使用真实安装器执行。

## 59. ZIP

生成 `AgentMailBridge-1.7.1-Windows-x64.zip`，大小 86,279,448 bytes。ZIP 与 installer 均位于 gitignored release，不提交 GitHub。

## 60. checksums

安装器 SHA-256：`ab925e152ac1b856f3283f2a9364a9a38673d05a191b92f8e869cc6d23a2b42c`。

ZIP SHA-256：`cd473675d294c2c5c528f293478b4168c340d71b2f4b901d16753b2f41558975`。

实际文件与 `release/checksums.sha256` 一致。

## 61. secret scan

dist/release/ZIP secret scan PASS，共检查 337 个产物文件；Git 暂存路径也排除 `.env`、OAuth、数据库、artifacts、release、dist、邮件和附件。真实 Agent 证据的 secret 检查均 PASS，私人正文、地址、附件内容与路径不进入本报告。

## 62. Defender

对 release 和 dist 执行 Microsoft Defender 自定义扫描，未发现新增威胁，结果 PASS。

## 63. Authenticode

安装器、`AgentMailBridge.exe` 和 `AgentMailBridgeMCP.exe` 实际均为 NotSigned。没有证书，不能表述为可信签名发布；这是当前唯一已知 P1 发布风险。

## 64. P0 / P1 / P2

P0：0。未发现重复真实发送、accepted 后重发、永久事实丢失、错误自动合并、secret 泄露、越权、迁移丢失、清理唯一恢复资料或私人数据进入 Git。

P1：1。正式发布物无 Authenticode 可信签名。

P2/验收缺口：Codex Desktop v1.7.1 新任务未执行；物理睡眠/受控多小时 soak 未完成；网页/手机 external outbound 与真实目录移动未新增独立证据；Generic 仍缺独立第三方 E2E；普通 Claude Desktop 为范围外。

## 65. PASS / CONDITIONALLY PASS / FAIL

实现、自动化、真实 QQ/163、Codex CLI、Claude Code、Hermes、GUI、构建、生命周期与安全扫描均通过。由于 NotSigned P1 和上述诚实披露的验收缺口，整体为 CONDITIONALLY PASS，不升级为完整 PASS。

## 66. 已知限制

Gmail 只读；Generic 为 implementation ready / E2E required；Outlook/Microsoft、Gmail send 和普通 Claude Desktop 产品化不在范围。delivery_unknown 需要 Sent 或用户确定证据，系统不会自动重发。ApplicationService、MainWindow 和 database 仍为较大的旧编排文件，本阶段已拆出 9 个领域模块但未做无关重构。

## 67. 下一阶段建议

先完成代码签名，再按现有 dogfood 计划补 Codex Desktop 新任务、物理睡眠/网络恢复、多日托盘、网页/手机 external outbound 和真实目录移动。只有这些条件完成且无新 P0/P1，才考虑把候选结论升级为 PASS；不要新增 Provider 或功能范围。

## 68. commits

- `2bd6bababaaf7aae0dd8125c92e3a55d63c5367b`：Research Gate 与复用决策。
- `0b3448baf901c9077f345aad41b6203bb182c039`：邮件事实一致性、恢复、GUI、迁移、测试和发布脚本。
- `18821153185f0f63fde1a99282c295810e4eaeb2`：产品文档、发布检查清单和最终专项报告。

## 69. push status

PASS。2026-07-31 首次普通 fast-forward push 已成功：`98d2c95..1882115 master -> master`。随后使用 `git ls-remote` 核对 GitHub `refs/heads/master` 为 `18821153185f0f63fde1a99282c295810e4eaeb2`。本段回填以独立文档提交再次普通 push；第二次推送结果由最终交付状态记录。全程不建分支、不 force push、不改写历史、不创建 Tag、PR、GitHub Release 或 Release Assets。

## A. 发件恢复矩阵

| 故障点 | SMTP 尝试 | 实际送达 | 本地事实 | 最终状态 | 自动重发 | 恢复结果 | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lease 前中断 | 0 | 否 | request 保留 | ready_to_send | 否 | 可重新领取 | PASS |
| lease 后、DATA 前中断 | 0 | 否 | snapshot 保留 | definitely_not_sent | 否 | stale lease 分类 | PASS |
| MIME 后、SMTP 前中断 | 0 | 否 | 固定 MIME 保留 | definitely_not_sent | 否 | 可创建新请求 | PASS |
| DATA started 后结果不明 | 1 | 不确定 | request/snapshot 保留 | delivery_unknown | 否 | 等待 Sent 或人工结论 | PASS |
| SMTP accepted 后 archive 前中断 | 1 | Provider 已接受 | 固定 MIME 可恢复 | sent_archive_pending | 否 | 只恢复归档 | PASS |
| outbound 发布后 DB 失败 | 1 | Provider 已接受 | package 可核验 | recovery_required | 否 | 启动恢复对账 | PASS |
| sent_archive_failed | 1 | Provider 已接受 | raw snapshot 保留 | sent_reconciled | 否 | 原 bytes 归档 | PASS |
| autonomous 幂等重放 | 1 | 1 次 | outbound + Sent | sent | 否 | 返回既有结果 | PASS |

## B. 邮件事实矩阵

| 场景 | 唯一 fact | memberships | direction | server presence | duplicate | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Gmail 多 Label | 1 | 多条当前归属 | 独立证据 | 当前 | 无 | PASS |
| Inbox 到 Archive | 1 | 旧 absent + 新 current | 不改变 | 可审计 | 无 | PASS |
| Sent 到自定义目录 | 1 | 多位置历史 | outbound 保持 | 可审计 | 无 | PASS |
| 服务器删除 | 1 | 标记 absent | 不改变 | absent | 无 | PASS |
| 重复 Message-ID 的不同物理邮件 | 多个真实事实 | 各自归属 | 各自证据 | 各自记录 | 不错误合并 | PASS |
| external Sent | 1 | Sent membership | outbound | current | 无 | PASS |

## C. 同步矩阵

| 账号 | 目录 | checkpoint 保留 | UIDVALIDITY | 失败隔离 | 继续同步 | Status |
| --- | --- | --- | --- | --- | --- | --- |
| QQ | Inbox/Sent/自定义 | 是 | 每目录 | 是 | 是 | PASS |
| 163 | Inbox/Sent/自定义 | 是 | 每目录 | 是 | 是 | PASS |
| Gmail | 多 Label | 是 | Provider label state | 是 | 是 | PASS |
| 隔离 IMAP | UIDVALIDITY 变化目录 | 是 | 只失效该目录 | 是 | 分段恢复 | PASS |
| 隔离 IMAP | 单目录故障 | 是 | 不改 | 是 | 其他目录继续 | PASS |

## D. Sent 对账矩阵

| 来源 | 本地 request | outbound fact | Sent 副本 | 匹配证据 | 是否合并 | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GUI confirm | 有 | 有 | 有 | account + raw/Header/指纹 | 是 | PASS |
| Claude Code autonomous | 有 | 有 | 有 | account + raw/附件 Hash | 是 | PASS |
| Hermes autonomous | 有 | 有 | 有 | account + raw/附件 Hash | 是 | PASS |
| external client | 无 | 新建 | 有 | account + Sent observation | 不与无证据事实合并 | PASS |
| delivery_unknown 回流 | 有 | 可恢复 | 有 | 唯一强证据 | 是 | PASS |
| ambiguous 多候选 | 多个 | 不改 | 有 | 证据同级 | 否 | PASS |

## E. 存储矩阵

| 状态 | 快照保留 | 可清理 | 恢复需要 | 实际清理 | Status |
| --- | --- | --- | --- | --- | --- |
| pending_confirmation | 是 | 过期后 | 是 | 未清理活跃项 | PASS |
| delivery_unknown | 长期保留 | 否 | 是 | 0 | PASS |
| sent_archive_failed | 必须保留 | 否 | 是 | 0 | PASS |
| cancelled/expired | 短期保留 | 到期后 | 否 | 两阶段故障注入通过 | PASS |
| sent_reconciled | 保留期内 | 到期后 | 否 | dry-run 与执行通过 | PASS |
| 用户工作副本 | 用户控制 | 不由本清理器处理 | 否 | 0 | PASS |

## F. Agent 矩阵

| Agent | Inbox | Sent | 完整事实 | confirm | autonomous | 故障后查询 | packaged | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex Desktop | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS | SKIPPED_BY_SCOPE | CONDITIONALLY PASS | CONDITIONALLY PASS | CONDITIONALLY PASS |
| Codex CLI 0.146.0 | PASS | PASS | PASS | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | PASS | PASS | PASS |
| Claude Code 2.1.220 | PASS | PASS | PASS | SKIPPED_BY_SCOPE | PASS | PASS | PASS | PASS |
| Hermes 0.19.0 | PASS | PASS | PASS | SKIPPED_BY_SCOPE | PASS | PASS | PASS | PASS |
| 普通 Claude Desktop | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE | SKIPPED_BY_SCOPE |

Codex Desktop 行的 CONDITIONALLY PASS 表示 v1.7.0 既有能力未发现回归，但 v1.7.1 本次没有独立新任务实测；它不是当前 PASS 证据。

## G. 开源复用矩阵

| 子系统 | 调研项目 | 许可证 | 复用方式 | 自定义范围 | Status |
| --- | --- | --- | --- | --- | --- |
| IMAP/目录 | IMAPClient、OfflineIMAP3、isync、Thunderbird | BSD/GPL/MPL | 直接复用 IMAPClient；只参考 GPL 模式 | membership/checkpoint 适配 | PASS |
| 邮件事实 | notmuch、JMAP、Thunderbird | GPL/Apache/MPL | 参考事实与位置分离 | 永久 package 规则 | PASS |
| MIME/SMTP | Python email/smtplib、MailKit、aiosmtplib | PSF/MIT | 直接复用标准库 | 固定 bytes 与阶段事实 | PASS |
| 地址 | python-email-validator | Unlicense | 直接复用 | 权限与 own-address 集合 | PASS |
| lease/recovery | SQLite、RQ、Celery/outbox | Public Domain/BSD | 复用事务，参考 lease | 不可幂等 SMTP 分类 | PASS |
| Sent 对账 | Proton Bridge、JMAP、notmuch | GPL/Apache | 只参考证据与状态模型 | 确定性 ranking | PASS |
| 存储/清理 | SQLite、Python 原子写入、Windows ACL | Public Domain/PSF/平台 API | 直接复用现有能力 | 两阶段产品清理 | PASS |
| Claude Code 配置 | anthropics/claude-code#80065 | 公开 issue | 采用官方 workaround | 单 Client key 迁移 | PASS |
