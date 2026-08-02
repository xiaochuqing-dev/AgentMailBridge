# AgentMailBridge v1.7.2 Reliability Audit & Release Candidate 真实验收报告

结论：CONDITIONALLY PASS / RELEASE CANDIDATE

本轮未发现未修复 P0/P1。Sent 对账和本地恢复不再仅凭 Message-ID 自动确认物理邮件或发送事实，最终源码、完整测试、最终 clean build、安装生命周期、三种真实 Agent 和一次最终安装版 QQ→163 自主发送均已通过。由于 Authenticode 未签名、7 至 14 天 dogfood、Windows 物理睡眠/真实断网、GUI confirm 用户动作、网页/手机 external outbound 与服务器目录移动尚未完成，本报告不判正式发布 PASS。

## 1. 执行日期和环境

- 日期：2026-08-03，Asia/Shanghai。
- 系统：Windows 11 家庭中文版，Microsoft Windows NT 10.0.26200.0，x64。
- Python：3.11.15。
- 真实 Agent：Codex CLI 0.146.0、Claude Code 2.1.220、Hermes 0.19.0。
- GitHub 仓库：xiaochuqing-dev/AgentMailBridge。

## 2. 起始本地与远端 HEAD

- 起始本地 master：a3269824ac122df85602fe338013eb0dea244246。
- 起始 origin/master：a3269824ac122df85602fe338013eb0dea244246。
- 起始工作区干净，无需保存或覆盖用户改动。

## 3. 工作区和 worktree 策略

- 按 AGENTS.md 直接在 master 开发，没有新建发布分支、Tag、PR 或 GitHub Release。
- 仅为真实 v1.7.1 基线构建和隔离升级创建 detached 临时 worktree。
- 每个临时 worktree 删除前均确认 tracked/untracked 变化为 0；生成的 build/dist/release 为已知 ignored 文件。
- 最终只保留主 worktree；含真实工作副本、旧版副本、隔离安装器和脱敏临时证据的专用目录均按精确路径删除。

## 4. 起始与最终分支状态

- 起始：master 加远端旧分支 codex/account-workspace-installer。
- 核心提交：94d82ceb33f95affc5b6f786bf5f276272ffd718，已普通 fast-forward 推送。
- 旧分支删除后，远端只保留 master；最终报告提交状态见第 30 节。

## 5. 旧远端分支比较与删除依据

- 旧分支 HEAD：c5c5c2aa07f0f5fe3ed69cd86888f6e3e2d47ed9。
- 删除前 merge-base/ancestor 核验通过；与核心 master 比较为 master 独有 67、旧分支独有 0。
- 旧分支是 master 祖先且无未吸收提交或文件内容，因此在 master 首次成功推送后删除远端分支和本地跟踪分支。

## 6. 本阶段边界与明确未做事项

- 只做 v1.7.2 可靠性审计、Sent 对账/恢复修复、Red Team、版本、发布脚本、文档、构建与验收。
- 未新增 Provider、SaaS、Web API、Agent 编排、GUI 重写、MCP 工具、权限类型、数据库 migration 或运行依赖。
- Gmail 仍严格为 gmail.readonly；Gmail send、Outlook/Microsoft 和普通 Claude Desktop 不进入本版。
- 未创建 Tag、PR、GitHub Release，未自动化 OAuth，未删除/移动服务器私人邮件。

## 7. 外部调研

本轮未进行外部网络调研。任务给定的仓库代码、AGENTS.md、现有测试、v1.7.0/v1.7.1 设计与真实报告已足以确定风险和验收标准；没有需要用不稳定外部资料补足的实现未知点。

## 8. Sent 对账根因

旧 find_sent_candidate 把 exact_message_id 排在 raw Hash 前，并在同账号恰好只有一个 Message-ID 候选时直接 matched；已有 package 的 From、To/Cc、Subject 与指纹并未约束这一路径。reconcile_send_request_locally 也可因唯一 Message-ID 把 delivery_unknown 或恢复请求推进为 sent/sent_reconciled。Message-ID 可被复用、伪造或由不同物理邮件共享，因此唯一候选不是唯一事实。

## 9. 修复后的证据模型

- 强确定证据：精确 request/package/outbound 关联、账号一致的既有 Provider mapping、账号一致的 raw MIME SHA-256。
- 完整复合证据：唯一规范化 Message-ID 加 From、To/Cc 集合、Subject、内容/附件指纹和绝对不超过七天的时间窗口。
- Message-ID、内容指纹、主题或时间单独均不能自动合并。
- 两条强证据指向不同事实时返回 ambiguous/manual_review；明确强证据可覆盖冲突的弱 Message-ID，并保存非敏感 decision_reason。
- 缺 Header、指纹或有效时间时保守 unmatched；系统时间回拨使用绝对差值，不会放大弱证据。

## 10. send_recovery 审计

- 本地恢复只接受 request.package_id、outbound.request_id、raw Hash 或完整复合证据。
- 仅 Message-ID 命中时保持原状态并记录 unresolved，不调用 SMTP、不增加 smtp_attempt_count。
- delivery_unknown 不进入自动发送；smtp_accepted/sent_archive_failed 只恢复固定 MIME 的本地归档。
- GUI 单项一致性修复复用同一无副作用决策，不绕过保守规则。

## 11. 关键代码变更

- 新增 reconciliation_evidence.py：纯决策数据结构、强弱证据冲突处理、七天绝对时间边界和 request 指纹生成。
- send_reconciliation.py：移除 Message-ID 强证据优先级，为 package 与 send_request 收集完整复合事实并审计原因/候选数。
- send_recovery.py：统一证据模型，阻止 weak Message-ID 推进状态或触发 SMTP。
- mail_processing.py：把真实观察时间传入对账。
- 发布脚本：生命周期默认 1.7.1→1.7.2，隔离升级前创建并验证 before_v1_7_2_upgrade 备份，失败时先卸载随机 AppId 测试实例再清理临时目录。
- 真实 Agent 脚本：读取场景加入 sync.ensure_fresh，提示词使用当前版本。

## 12. 结构偿债范围

只提取了一个同领域纯函数小模块，数据库查询和状态写入仍由既有 send_reconciliation/send_recovery 编排。复用了地址解析、Hash、归档指纹、SQLite、备份和审计；未建立第二套映射表、框架或 migration，也未改写历史 raw.eml、Hash、package_id、resource_id 或 account_id。

## 13. Red Team 场景矩阵

17 项全部 PASS：

1. 单一旧 package 复用 Message-ID 且 raw 不同，unmatched。
2. 相同 Message-ID、不同收件人，unmatched。
3. 相同 Message-ID/Subject、附件 Hash 不同，unmatched。
4. raw Hash 指向 A、弱 Message-ID 指向 B，按规则选择 A 并审计原因。
5. Provider mapping 与 raw Hash 指向不同 package，ambiguous。
6. 唯一 Message-ID 加完整复合证据且在窗口内，matched。
7. delivery_unknown 仅 Message-ID 命中，不推进、不发 SMTP。
8. 本地恢复用精确 raw Hash，成功且不重发。
9. 本地恢复用窗口内完整复合证据，成功。
10. Sent 回流用完整 request 复合证据恢复 delivery_unknown，不重发。
11. 多个 Message-ID 候选，ambiguous。
12. 一致性扫描保留 ambiguous，不自动合并。
13. 仅内容指纹不能充当物理身份。
14. external outbound 复用 Message-ID 仍形成独立且幂等事实。
15. 不同 account_id 的相同 Message-ID/raw 不越界。
16. 时间窗口绝对、有界，时间回拨不提升弱证据。
17. 既有 outbound、Provider、raw 强路径继续兼容。

## 14. P0、P1、P2

- 已修复 P0 风险：弱 Message-ID 可能错误合并物理邮件、把 delivery_unknown 提升为已发送。
- 未解决 P0：0。
- 未解决产品 P1：0。
- 未解决产品代码 P2：0。
- 发布证据缺口：NotSigned、长期 dogfood、物理睡眠/断网、confirm 用户点击、external outbound/服务器目录移动，均在已知限制中单列，不伪装为产品 PASS。

## 15. 自动化测试结果

- Red Team：17 passed。
- Release Candidate 静态不变量：8 passed。
- Red Team + v1.7.1 consistency/recovery：57 passed。
- 相关 consistency/fault/mail flow/archive：89 passed。
- Full Suite Preflight：204 passed；version、Provider、schema、diff check、compileall、targeted pytest 全部 PASS。
- 最终完整 pytest -q -rs：721 passed、1 skipped、0 failed，2006.44 秒。
- 唯一 skip：当前 Windows 无创建测试符号链接权限，WinError 1314；不是断言失败或新增 skip。
- pip check：无损坏依赖；quick_check=ok；foreign_key_check=0；python -m agent_mail_bridge --version=1.7.2。

## 16. QQ/163 真实复核

- 最终安装版 MCP 的三种真实 Agent 均读取授权账号、Inbox/Sent、邮件和资源。
- Claude Code 最终 autonomous：恰好 1 个新请求、状态 sent、smtp_attempt_count=1；QQ→163 真实到达，2 个附件 Hash 一致，Sent mapping 成立，outbound raw Hash 一致。
- QQ SMTP 认证诊断 PASS；QQ/163 继续为正式支持。
- 本轮未执行 confirm 的 GUI 用户点击。产品不允许 MCP 确认，自动点击也会越过用户确认边界，因此明确标为 NOT_EXECUTED_USER_ACTION_REQUIRED。

## 17. external outbound 与服务器目录移动

- 自动化 Red Team 已验证 external outbound 在复用 Message-ID 时仍形成独立、幂等事实。
- 本轮未从网页/手机真实发件，也未在服务器移动邮件到 Archive/自定义目录；这些会改变外部服务器状态并需要额外人工场景，标记 NOT_EXECUTED。

## 18. Codex Desktop

- 使用独立新任务，任务 ID 019fc433-24c3-7b43-b125-e0e162c0e953。
- 临时 Managed Client 初次因缺 sync.ensure_fresh 出现两次权限拒绝；只补该读取刷新能力，不开 mail.send，再重试一次。
- 完成账号/目录/工作区/健康、Inbox/Sent 限定搜索、get_mail、read_mail_resource 和 complete prepare。一次 prepare 参数探测为 invalid_input，随后完整调用成功且未覆盖工作副本。
- 暂停 Client 后下一次健康调用立即 client_disabled；随后恢复原配置并撤销临时身份。
- 在线 Sent refresh 当时失败，搜索如实使用本地归档，没有伪造在线成功。该 Desktop 验收发生在最后一个 Provider-vs-weak-Message-ID 细化前；该细化不触及 Desktop/配置/MCP schema，最终安装版 stdio 已由三种真实 Agent 重新覆盖。

## 19. Codex CLI、Claude Code、Hermes

- Codex CLI 0.146.0：最终安装版自然任务 PASS，进程 310.80 秒，六类必需读取工具、Inbox/Sent、资源读取和完整准备通过。
- Claude Code 2.1.220：最终安装版读取 PASS，146.59 秒，六类工具、完整准备 7 个资源；自主发送 PASS，157.91 秒。
- Hermes 0.19.0：首次进程和连接 PASS，但只完成五类工具并遗漏 complete prepare，严格判 FAIL；唯一一次重试 98.80 秒，六类工具和完整准备 7 个资源 PASS。
- 验收 Client 为既有 v1.7 验收身份。清理时发现 Codex 基线没有受管条目，因此保持该复用身份 revoked；Hermes YAML Hash 未变化。Claude 基线条目 token 已陈旧，受控恢复中保留原 Client ID/权限、轮换一次 scoped token、原子更新受管条目，其他 JSON 语义完全不变，11 工具连接重新 PASS，send_mode 恢复 confirm。邮箱凭据未读取或修改。

## 20. 短期 soak、睡眠与网络

- 实测短期样本：180.0 秒，18 次服务重建/只读状态采样，failures=[]。
- 前后 SQLite quick_check 均为 ok，foreign_key_check 均为 0，邮件事实计数未下降。
- Windows 物理睡眠/唤醒与真实网络断开未执行；不得把进程等待或模拟异常写成物理证据。

## 21. 7 至 14 天 dogfood

未完成。长期计划只存在于 docs/长期运行验证计划.md，不能计入本轮实测时长。

## 22. clean build 与 packaged smoke

- 发现最后一个强证据细化晚于较早构建后，主动判旧 Hash 作废，并从最终源码重新 clean build。
- 最终 clean build -SkipTests：125.5 秒，PASS。
- 构建内 Full Suite Preflight、GUI packaged self-test、11 工具 MCP packaged smoke、UTF-8/BOM/EOF/stdout、默认拒绝与 secret exclusion 均 PASS。
- 第一次最终重建清理被两个旧 dist MCP 进程锁文件阻断；核对其精确 executable path 与已撤销验收上下文后只终止对应进程，再重建成功。这是环境占用，不是构建断言失败。

## 23. installer、ZIP 与 checksums

- AgentMailBridge-1.7.2-Setup.exe：52,225,433 bytes；SHA-256 5257b764177885f8a0fe513b8e2c51a83f383700d9eef221ab975e391089a336。
- AgentMailBridge-1.7.2-Windows-x64.zip：86,298,822 bytes；SHA-256 ba37eb713ab2527a01fe7637baf480383d0ae1c9c8982e951ffe503833ca8579。
- AgentMailBridge.exe：SHA-256 741af4576f978a28837089b9b5622d1d603911f5f087e8ef1be308fd704ca906。
- AgentMailBridgeMCP.exe：SHA-256 0c25107e3f4ed82dd519f6711cce6e5dff4be9456aca5e1e834c165ff24e4948。
- checksums.sha256 与 installer/ZIP 重新计算值一致；GUI、MCP、installer 的 FileVersion/ProductVersion 均为 1.7.2。

## 24. 升级、卸载与重装

- 从干净 detached a3269824 构建真实 v1.7.1 基线，再与最终 v1.7.2 制品生成同一随机 AppId 的隔离生命周期安装器。
- 21/21 检查 PASS：旧安装、覆盖升级、升级前在线备份及校验、数据库完整性、schema、旧 Client 默认无扩权、账号/权限、OAuth、Credential、package/raw/附件 Hash、outbound、Sent mapping、调度、卸载保留、重装恢复和最终无测试安装残留。
- 四阶段计数均保持：accounts=5、outbound=1、packages=4、scheduler=4、agent_clients=1、raw=4、attachments=4、OAuth JSON=2。

## 25. secret scan、Defender 与 Authenticode

- source/Git secret scan：337 文件，PASS；staged 再扫描无 forbidden path 或高置信 secret 模式。
- build 内 dist/release/ZIP secret exclusion PASS；.env、credentials、token、数据库、日志、邮件和附件未进入制品或 Git。
- 最终 Defender：Antivirus/Realtime 均启用，定义 1.455.474.0；release 扫描 4.23 秒、dist 0.58 秒，检测前后均为 0。
- installer、GUI EXE、MCP EXE Authenticode 均为 NotSigned，如实保留。
- 脱敏诊断导出未检出 secret、高置信密钥或私人完整路径。

## 26. 已知限制

- 制品未签名，不应标记正式发布 PASS。
- 7 至 14 天 dogfood、物理睡眠/断网、真实 external outbound/目录移动未做。
- confirm 真实发送仍需要 GUI 用户亲自点击。
- 最终 pytest 的符号链接逃逸用例因当前 Windows 特权不足跳过；其他路径安全用例通过。
- Desktop 的在线 Sent refresh 当次失败，使用本地归档完成读取；未声称在线 refresh PASS。

## 27. 最终结论

CONDITIONALLY PASS。核心可靠性缺陷已修复，未解决 P0/P1 为 0；自动化、构建、生命周期、真实 Agent 和一次最终真实发送均通过，但发布证据缺口仍存在。

## 28. Release Candidate 判断

达到 Release Candidate。没有达到“正式签名并完成长期/人工外部场景”的正式发布标准，不发布 GitHub Release。

## 29. 下一步边界

只允许继续 7 至 14 天 dogfood、GUI confirm/人工 Desktop 复核、签名或 Developer Preview、网页/手机 external outbound、服务器目录移动和真实用户验证。不得以本报告为理由继续扩功能。

## 30. commits 与 push 状态

- 94d82ceb33f95affc5b6f786bf5f276272ffd718：fix: harden v1.7.2 Sent reconciliation evidence，已推送 origin/master。
- c259d31b7a5c4b370202a8e6f6d34f92fd33fd23：docs: add v1.7.2 reliability acceptance report，已推送 origin/master。
- 报告提交推送后复核：本地 HEAD、origin/master 和 GitHub 远端 master 均为 c259d31b7a5c4b370202a8e6f6d34f92fd33fd23；远端只保留 master，主 worktree 干净且无临时 worktree。
- 当前改动仅记录上述已完成事实；其纯文档收口提交将在随后普通推送，并在最终用户交付中报告最终 HEAD。
