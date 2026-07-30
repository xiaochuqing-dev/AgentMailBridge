# AgentMailBridge v1.7.1 邮件一致性与恢复开源调研

调研日期：2026-07-30

调研对象：`origin/master` 与本地 `HEAD` 均为 `98d2c95f55518cac9d9aff7c192f1d7aa693ac78`，工作区开始时干净。

本报告是 v1.7.1 的 Research Gate。结论先于实现冻结：优先复用现有依赖、Python 标准库、SQLite 官方能力和已经通过真实 E2E 的项目模块；GPL 项目仅用于理解长期运行模式，不复制代码；没有足够收益时不增加运行依赖。

## 1. 当前基线

- 产品版本：1.7.0。
- 已有发布物：1.7.0 installer、Windows x64 ZIP 与 checksums。
- migration：`multi_account_core_v1=3`、`agent_integration_permission_v1=2`、`mail_flow_v17=1`、`unified_mail_archive_v1=1`，状态均为 completed。
- v1.7.0 最终报告记录 634 passed、2 skipped；v1.7.1 开工基线以本次实际全量 pytest 输出为准。
- 核心文件：`application_service.py` 5779 行，`main_window.py` 8048 行，`database.py` 5463 行，`imap_sync.py` 961 行，`outbound_mail.py` 930 行，`send_requests.py` 577 行。

## 2. 当前问题清单

1. `mail_packages` 已是永久事实，`mail_package_mailboxes` 已能保存多个目录，但 membership 缺少当前存在、移除时间、观察来源和对账状态，无法解释移动、Label 移除和服务器删除。
2. 目录角色目前会直接决定同步邮件的 direction。Gmail 多 Label、Sent 后移动到自定义目录、外部客户端发件和方向冲突都需要独立证据。
3. `claim_for_send` 只有一次条件状态更新，没有 durable lease、owner、heartbeat、过期恢复和阶段事件。
4. SMTP 接受、数据库状态和本地 archive 之间仍有崩溃窗口。启动时没有统一扫描非终态请求。
5. `sent_archive_failed` 保留了 MIME，但没有正式的只重建归档、不再次 SMTP 的恢复服务。
6. Sent 映射能记录 Message-ID 命中，但没有完整证据等级、ambiguous 候选、长期未匹配和 external outbound 对账记录。
7. IMAP 目录失败路径会以 `last_uid=0` 和空 checkpoint 写失败状态，存在清零最后成功进度的风险。
8. checkpoint 兼容镜像和 mailbox 权威状态分两次提交，第二次失败时可能出现进度不一致。
9. UIDVALIDITY 变化会清零单目录 UID，但缺少明确的 reconciliation 状态、旧代际记录和可继续分段重扫游标。
10. 发件快照、取消/过期请求和工作副本缺少统一 dry-run、保留期限、受保护状态和清理审计。
11. 现有一致性扫描偏文件层，尚未覆盖 membership、方向冲突、send request/outbound、Sent mapping、stale lease、orphan snapshot 和 checkpoint 异常。
12. 维护页尚不能集中解释同步、发件、事实一致性和可安全清理空间。

## 3. 主要子系统复用决策表

| 子系统 | 当前实现 | 调研库/项目 | 许可证 | 可直接复用 | 可参考模式 | 不采用原因 | 最终方案 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| IMAP 协议、目录和编码 | IMAPClient 3.x | IMAPClient、MailKit、OfflineIMAP3、isync、Thunderbird | New BSD / MIT / GPL-2+ / GPL-2 / MPL-2.0 | IMAPClient | 每目录状态、SPECIAL-USE 优先 | 不复制 GPL；不再建协议栈 | 保持 IMAPClient Adapter，只扩展本地事实 |
| Gmail Label | Gmail API readonly label list | JMAP Email/Mailbox、Proton Bridge | Apache-2.0 / GPL-3.0 | 现有 Google API client | 一个 Email 对应 mailboxIds 集合 | 不实现 JMAP；不复制 Proton | 把 Label 观察写入正式 membership |
| 永久邮件事实 | `mail_packages`、manifest v2 | notmuch、JMAP、Thunderbird | GPL-3+ / Apache-2.0 / MPL-2.0 | 现有 package/archive | 内容事实与位置分离、多位置关联 | notmuch 不负责本项目 canonical archive | 保留唯一 package，扩展现有 mapping 为 membership |
| MIME 与 SMTP | `EmailMessage`、`smtplib`、一次构建 | CPython、aiosmtplib、MailKit | PSF / MIT / MIT | Python 标准库 | exact bytes、明确阶段 | 当前边界同步且在线程中执行；async 库无收益 | 不换库，固化 MIME 和阶段事实 |
| 地址校验 | email-validator | python-email-validator | Unlicense | 当前依赖 | Unicode、IDNA、无 DNS 校验 | 无 | 继续直接复用 |
| 发送租约 | `BEGIN IMMEDIATE` + status | RQ、Celery、SQLite 事务 | BSD-2 / BSD-3 / Public Domain | SQLite 事务 | owner、TTL、heartbeat、abandoned cleanup | RQ/Celery 需要 Redis/broker，仍不能消除 SMTP 不确定性 | 单行 SQLite lease + 条件状态迁移 |
| 崩溃恢复 | 终态与 raw snapshot | RQ started registry、Celery ack、transactional outbox | BSD-2 / BSD-3 / 设计模式 | 现有 request/raw/outbound | 启动扫描、过期不直接重放、outbox | 通用队列的自动重投不适合不可幂等 SMTP | 项目内最小恢复协调器，未知结果绝不重发 |
| Sent 对账 | `sent_server_mappings` | Proton Bridge、notmuch、JMAP | GPL-3 / GPL-3+ / Apache-2.0 | 现有线程与 fingerprint helpers | 多证据、候选、状态变化 | 外部实现身份模型不同 | 确定性 evidence ranking + ambiguous 记录 |
| checkpoint | mailbox + account JSON | OfflineIMAP3、isync、Thunderbird、RFC 9051 | GPL-2+ / GPL-2 / MPL-2.0 / RFC | 现有 IMAPClient 状态 | UIDVALIDITY 只失效单目录、最后成功游标独立 | 不复制实现 | mailbox state 成为权威，兼容镜像提交后更新 |
| SQLite 备份与校验 | `sqlite3.backup`、integrity_check | SQLite 官方 | Public Domain | 当前标准库绑定 | WAL、事务、backup、quick/integrity check | 无 | 继续直接复用；日常健康用轻量检查 |
| 文件原子写入 | `os.replace`、fsync、有界 WinError 5/32 重试 | Python 标准库、现有 v1.7 验证 | PSF / 项目 MIT | 当前 helper | 同目录临时文件、原子发布 | 不新增 filelock/atomicwrites | 复用现有 helper，不再造一套 |
| 跨进程互斥 | `ProcessLock`、SQLite writer lock | Windows msvcrt、SQLite | PSF / Public Domain | 当前已验证封装 | OS 释放锁、数据库条件领取 | Send lease 不是普通文件互斥 | 收件继续 ProcessLock，发件只用 durable DB lease |
| 清理与保留 | 技术日志清理、备份列举 | RQ registry cleanup、SQLite、标准库 | BSD-2 / Public Domain / PSF | 现有路径/Hash/DB 能力 | dry-run、保护活跃/恢复材料、审计 | 不引入调度框架 | 只清理产品管理对象的最小 retention service |
| 一致性扫描 | `maintenance.scan_consistency` | SQLite、notmuch fsck 思路 | Public Domain / GPL-3+ | 当前扫描与 Hash helper | 只读优先、候选不自动合并 | 不复制 GPL | 拆出领域扫描器并保留维护编排 |
| Windows 凭据与 ACL | Credential Manager、ctypes、ACL helper | Microsoft 官方 DPAPI/ACL 文档 | 平台 API | 当前实现 | 当前用户边界、失败保留旧值 | 不改变凭据链 | 本阶段不新增秘密存储 |

## 4. 开源项目与许可证核对

### ProtonMail/proton-bridge

- 仓库：https://github.com/ProtonMail/proton-bridge
- 2026-07-30 GitHub 元数据显示仍活跃，默认分支 master，许可证 GPL-3.0。
- `syncservice` 与 `imapservice` 把同步状态、消息构建和事件处理分层，适合参考“状态持久化与运行编排分离”。
- 决策：只参考服务分层、分阶段同步和失败报告；不复制任何 GPL 代码，不引入其本地 IMAP server 架构。

### OfflineIMAP/offlineimap3

- 仓库：https://github.com/OfflineIMAP/offlineimap3
- COPYING 明确为 GPL-2.0-or-later。
- `folder/Base.py` 为每个 folder 保存 UIDVALIDITY；缓存值与服务器值不一致时不继续把旧 UID 当成当前代际。
- 决策：参考单目录 UIDVALIDITY 门禁和临时文件写入；不复制代码。历史 package 永久保留，只有该 mailbox checkpoint 进入 reconciliation。

### isync/mbsync

- 官方项目：https://isync.sourceforge.io/
- GitHub 镜像参考：https://github.com/Coool/isync ，标识 GPL-2.0。
- 成熟做法是为 channel/folder 保存同步状态与 UID 代际，而不是使用一个全账号游标。
- 决策：只参考每目录状态文件和可继续同步思路；不复制 GPL 代码，也不增加外部进程依赖。

### Thunderbird Desktop

- 官方仓库：https://github.com/thunderbird/thunderbird-desktop
- 源文件通常以 MPL-2.0 头部授权；仓库包含长期维护的 IMAP folder cache、离线同步、UIDVALIDITY 与消息数据库。
- 决策：参考 folder cache、离线事实与网络状态分层；不移植其 C++/XPCOM 数据库模型。

### notmuch

- 镜像：https://github.com/notmuch/notmuch
- COPYING 为 GPL-3.0-or-later。
- API 明确：同一 Message-ID 的新文件加入现有 message，并把多个 filename 与该 message 关联；这验证了“一个逻辑事实、多位置引用”的成熟方向。
- 限制：notmuch 在最后一个 filename 删除时会移除 message，而 AgentMailBridge 要永久保留事实，因此不能直接照搬生命周期。
- 决策：只参考唯一事实和多位置集合；不复制 GPL 代码，不引入 Xapian。

### JMAP Email/Mailbox

- 仓库：https://github.com/jmapio/jmap
- 许可证 Apache-2.0；正式规范为 RFC 8620/RFC 8621。
- Email 的 `mailboxIds` 是集合；移动是修改集合，不是重建 Email。`blobId` 表示原始 RFC 5322 octets，`receivedAt` 与消息头时间分开。
- 决策：直接采用其概念边界作为本地模型参考，但不实现 JMAP 协议。

### IMAPClient

- 仓库：https://github.com/mjs/imapclient
- COPYING 为 New BSD，项目已经依赖并打包。
- 直接提供 LIST/XLIST、modified UTF-7、readonly select、UIDVALIDITY/UIDNEXT 与特殊目录 helper。
- 决策：继续直接复用，不新增 IMAP parser、目录编码或 UID FETCH 实现。

### MailKit

- 仓库：https://github.com/jstedfast/MailKit
- MIT。
- 参考点是 SPECIAL-USE/XLIST 优先、名称回退仅作兼容；以及 SMTP/IMAP 阶段和异常边界。
- 决策：不引入 .NET 依赖，只参考角色识别与测试矩阵。

### Python email、mailbox、smtplib

- 来源：https://docs.python.org/3/library/email.message.html 和 https://docs.python.org/3/library/smtplib.html
- Python Software Foundation License。
- 决策：MIME 继续用 `EmailMessage` 构建一次并以 SMTP policy 固化 bytes；SMTP 继续使用标准库。`mailbox` 只作为本地格式参考，不改变项目 package 格式。

### python-email-validator

- 仓库：https://github.com/JoshData/python-email-validator
- Unlicense。
- 决策：继续直接复用 Unicode/IDNA/语法规范化，`check_deliverability=False`，不手写地址正则。

### aiosmtplib

- 仓库：https://github.com/cole/aiosmtplib
- MIT，项目活跃。
- 决策：不采用。当前 SMTP 位于明确 worker/thread 边界，迁移到 asyncio 会扩大 Qt、Provider Adapter 和打包复杂度，却无法消除“服务器接受后进程崩溃”的语义窗口。

### RQ 与 Celery

- RQ：https://github.com/rq/rq ，BSD-2-Clause。
- Celery：https://github.com/celery/celery ，BSD-3-Clause。
- RQ StartedJobRegistry 使用到期时间识别 abandoned execution；Celery 文档明确晚确认任务可能重复执行，任务必须幂等。
- 决策：只参考 lease/heartbeat/abandoned cleanup、执行事件和终态保留。SMTP 不是天然幂等任务，不能采用通用队列的自动 redelivery；引入 Redis/broker也违背 local-first 小产品边界。

### SQLite 官方机制

- 事务：https://www.sqlite.org/lang_transaction.html
- WAL：https://www.sqlite.org/wal.html
- 在线备份：https://www.sqlite.org/backup.html
- 完整性检查：https://www.sqlite.org/pragma.html#pragma_integrity_check
- SQLite 代码为 Public Domain。
- 决策：`BEGIN IMMEDIATE` + 条件更新取得单一执行权；短事务更新 heartbeat；使用当前 `sqlite3.backup`；日常状态避免每次完整 integrity_check，异常或用户操作再执行完整检查。

### Windows DPAPI/ACL

- 来源：Microsoft Credential Manager、DPAPI、`icacls` 和文件安全官方文档。
- 决策：继续使用现有 Credential Manager 与当前用户 ACL。邮件恢复、对账和扫描只处理无秘密事实，不读取或返回 Credential 值。

## 5. RFC 与协议依据

- RFC 5321：SMTP server 接受 DATA 后只证明服务器接受了责任，不证明最终送达；客户端断线可能无法确定服务端是否已接受。
- RFC 5322：Message-ID、Date、In-Reply-To、References 与公开 Bcc 语义。
- RFC 6154：SPECIAL-USE 是 Sent、Drafts、Junk、Trash、Archive 等目录角色的首选依据。
- RFC 9051：UID 只在同一 UIDVALIDITY 内稳定；UIDVALIDITY 变化必须隔离旧代际。
- RFC 8621：Email 与 mailboxIds 集合分离；原始 blob、声明时间、接收时间和 Mailbox 状态不是同一事实。
- RFC 6532：国际化邮箱头与 Unicode 数据必须由成熟 parser/validator 处理。

## 6. 数据模型决策

1. `mail_packages` 保持唯一永久事实，package、raw、resource 和历史 Hash 不因目录变化重建。
2. 不新建重复的第二套 mapping 表。把现有 `mail_package_mailboxes` 升级为正式 membership：增加 `currently_present`、`removed_at`、`source`、`reconciliation_status`、`last_verified_at` 和观察代际；旧行无损回填为当前存在。
3. `mail_packages.mailbox_id/mailbox_ref` 仅保留首个/兼容位置，不再作为当前唯一目录事实，也不因后续目录观察覆盖。
4. direction 继续保存在 package，但新增可审计方向证据。证据优先级：本地 outbound / 已有 outbound mapping；Provider 明确 sent metadata；已配置账号匹配 From + Sent membership；Sent SPECIAL-USE/Gmail SENT；其他确定性复合证据。
5. direction 冲突不静默覆盖，写入 reconciliation record 和 health issue。目录移动本身不能改变已经确定的 direction。
6. server presence 是 membership 的当前状态；本地永久事实删除是完全不同的用户数据生命周期，本阶段不提供后者。

## 7. 发件状态机与租约决策

状态语义冻结为：

`pending_confirmation` / `cancelled` / `expired` / `ready_to_send` / `acquiring_lease` / `sending` / `smtp_accepted` / `sent_archive_pending` / `sent_waiting_reconciliation` / `sent_reconciled` / `definitely_not_sent` / `delivery_unknown` / `sent_archive_failed` / `recovery_required` / `failed`。

- lease 由 SQLite 单行和 `BEGIN IMMEDIATE` 条件领取实现，字段含 owner/session、acquired/heartbeat/expires、attempt_no、stage、fixed Message-ID 和 MIME Hash。
- 每次迁移写入 send attempt event；事件不含正文、地址明文集合、附件内容或凭据。
- `smtp_attempt_count` 只在真正进入 SMTP 前增加，不把 MIME 构建失败算成 SMTP 尝试。
- lease 过期只触发恢复，不代表可自动重发。
- stage 明确在 SMTP 前且没有尝试事实时可判为 `definitely_not_sent`；SMTP 调用开始后但没有确定结果时只能进入 `delivery_unknown`。
- SMTP 接受后先持久化 `smtp_accepted`；后续只重试本地 archive/Sent 对账，永不再次 SMTP。
- heartbeat 使用短 SQLite 更新；阻塞 SMTP 由现有网络 timeout 约束，heartbeat 线程不持有长事务。

## 8. 启动恢复决策

启动恢复按证据顺序：正式 outbound package、request/outbound 行、固定 Message-ID/MIME Hash、Sent mapping、只读 Sent 查询。恢复结果只能是确定已发送、确定未发送、等待归档/对账或 `delivery_unknown`。

- raw snapshot 与附件 snapshot 在 request 解决前均受保护。
- `sent_archive_failed` 从既有 raw bytes 恢复 package，禁止重建不同 MIME。
- `delivery_unknown` 可由后续 Sent 强证据解决，但不会自动 SMTP。
- 手动“基于原内容新建请求”必须创建新 idempotency key 和新 request_id。

## 9. Sent 对账决策

证据等级：`exact_provider_id`、`exact_message_id`、`exact_raw_hash`、`exact_content_attachment_fingerprint`、`deterministic_composite`、`ambiguous`、`unmatched`。

只有一个强候选时自动合并。多个候选、方向冲突或时间窗外复合命中写入 `ambiguous`，保留所有候选摘要但不含正文。Sent 中没有本地 request/package 的邮件作为 external outbound 新事实归档。Sent membership 与 outbound direction 分开保存。

## 10. checkpoint 与 UIDVALIDITY 决策

- `mailbox_sync_states` 是权威 checkpoint，`account_sync_states.checkpoint` 只保留兼容镜像。
- 保存成功事实与推进 `last_uid` 必须遵守顺序；数据库提交失败不能推进兼容镜像。
- 失败只更新 last attempt/error/failure count，不修改 last successful UID/checkpoint。
- UIDVALIDITY 变化保存旧代际摘要，标记该 mailbox `reconciliation_required`，重扫 cursor 可分段继续；不删除历史 package。
- 单目录异常独立记录，其他目录继续同步。

## 11. 临时资料与清理决策

- 只清理 AgentMailBridge 管理的 request snapshot/staging/work copy record，不删除用户原附件或正式 package。
- `delivery_unknown`、`sent_archive_failed`、`smtp_accepted`、`recovery_required` 永久保护，直到明确解决。
- cancelled/expired/definitely_not_sent 使用短期保留；sent_reconciled 使用可配置保留；failed 根据恢复证据保护。
- 默认 dry-run，返回数量、总大小、预计释放空间和保护原因；执行使用同一计划重新校验后逐项删除并审计。
- 失败不会改变正式事实，单项文件锁不阻断其余安全清理。

## 12. Windows 存储与锁决策

- 收件跨进程互斥继续复用现有 `ProcessLock`，它已通过 Windows `msvcrt.locking` 和进程退出释放验证。
- 发件不把文件锁当 durable truth，使用 SQLite lease。
- 文件写入继续复用同目录临时文件、flush/fsync、`os.replace` 和 WinError 5/32 有界重试。
- 不引入 portalocker、filelock、atomicwrites 或新的任务队列依赖。

## 13. 一致性扫描决策

默认扫描只读，覆盖 package/manifest/raw/resource Hash、membership ownership、direction evidence、request/outbound/Sent mapping、stale lease、orphan snapshot/work copy、duplicate candidate 和 checkpoint 异常。修复前必须预览、数据库备份、单项选择、事务和审计；ambiguous 永不自动合并，任何修复都不触发 SMTP。

## 14. 供应链与打包影响

- 新增第三方运行依赖：0。
- 保持 PyInstaller 当前 hidden import 与许可收集边界。
- 直接复用依赖的许可证已经进入现有第三方 notices；本阶段文档新增的是设计参考，不把 GPL 源码或二进制打入产品。
- aiosmtplib、Celery、RQ、notmuch、Proton Bridge、Thunderbird 与 isync 均不进入 requirements、dist 或 installer。

## 15. 最终复用清单

直接复用：IMAPClient、email-validator、Python email/smtplib/hashlib/pathlib/sqlite3、现有 Provider Adapter、现有 exact MIME 构建、现有 Mail Package/manifest、现有 SQLite backup、现有原子写入、现有 ProcessLock、现有权限与 Hash 校验。

只参考设计：Proton Bridge 分层同步、OfflineIMAP/isync/Thunderbird 每目录代际、notmuch 一事实多位置、JMAP Email/mailboxIds、RQ/Celery lease 与 abandoned cleanup、MailKit SPECIAL-USE、transactional outbox。

## 16. 最小自定义实现清单

1. membership presence/reconciliation 字段与本项目旧数据迁移。原因：这是 AgentMailBridge package_id/account_id/mailbox_id 的专属组合，没有通用库能直接表达。
2. direction evidence 与冲突记录。原因：需结合本地 outbound、现有账号、Gmail Label、IMAP role 和项目审计。
3. SQLite send lease、attempt event 与启动恢复协调。原因：SMTP 不确定性和 exact raw archive 是本产品专属状态；通用队列自动重投反而不安全。
4. Sent evidence ranking 与 ambiguous record。原因：需复用当前 package、outbound、provider mapping 和附件 Hash。
5. snapshot retention plan、health aggregation 和一致性领域检查。原因：只允许操作本产品受管对象，并需遵守权限、路径、Hash 与永久事实边界。

## 17. Research Gate 自检

- 已覆盖提示词列出的成熟项目、标准库、RFC、SQLite 与 Windows 方案。
- 已逐项核对许可证，GPL/AGPL 代码不会复制到 MIT 项目。
- 已说明直接复用、只参考和必须自定义的边界。
- 已评估维护状态、Windows 打包、依赖体积和供应链影响。
- 最终决策不新增重复协议、SMTP、队列、锁、迁移或索引框架。

结论：Research Gate 通过，可以进入定向实现。
