# AgentMailBridge v1.3.0 复杂邮件 MCP 验收、收发语义、历史补扫与邮件详情统一整改报告

验收日期：2026-07-22

总体结论：CONDITIONALLY PASS。v1.3.0 功能、兼容、真实 Gmail API、真实安装版 MCP、Windows 覆盖安装、数据保全、clean build、ZIP、安装包、Defender 和 SHA-256 均完成；没有 P0/P1。真实 IMAP、GUI 向不同外部测试邮箱实际投递、卸载保留和 Authenticode 签名因当前环境或证书不可用而未完成，均已如实标记。

## 1. 开始前 HEAD

开始前 master 为 `fd0a5c720ea23f97d930d7466e2382ffecb49656`，版本 1.2.1，本地与 origin/master 一致。正式构建所用实现 HEAD 为 `1075fc58ddb12eef32788eacc48054b2f90f48e3`；最终报告提交后的最终 HEAD 以 Git 记录和最终回复为准。

## 2. v1.2.1 基线测试

基线执行完整 pytest：478 passed，1 skipped，0 failed，耗时 1028.65 秒。基线 OAuth 热修复、GUI 非阻塞和既有邮件能力保持可用。

## 3. 前置复杂邮件 MCP 验收

结果 PASS。真实 1.2.1 安装版 stdio MCP 返回协议 `2025-06-18` 和 7 个工具；目标 package 唯一，18 个资源包括 9 个附件、1 个内联图片、5 个链接；Markdown/TXT/JSON/CSV、PNG/PDF/DOCX/XLSX、0 字节 DAT、raw.eml、大小、SHA-256、stdout 纯净和 EOF 退出均通过。详见同目录前置基线报告。

## 4. 当前机器真实 Gmail 与目标邮件匹配说明

验收从当前安装版配置和正式 DATA_ROOT 读取事实，当前账号已脱敏且与目标邮件真实收件账号一致；没有从旧提示词、截图或发件文件推断账号。后端为 gmail_api，Token 可复用，scope 严格保持 `gmail.readonly`，没有重新打开浏览器授权。

## 5. 历史漏收真实问题复盘

根因是旧规则首次返回 rule_skipped 后，普通增量扫描的 lookback 窗口继续前移，而产品没有显式历史重扫入口。规则后来放宽也无法重新覆盖旧日期，因此形成“provider 有、本地没有”的永久漏收表象。

## 6. self_only 根因

旧默认把“只收自己发给自己的邮件”当作普遍安全默认，并在多个层重复按发件人过滤；这既不符合 Inbox 收件直觉，也把收件规则和防回流混为一谈。

## 7. 默认收件语义整改

新安装默认 `ALL_SCANNED`，Gmail API/IMAP 共用统一规则判断。自动收取、立即收取和历史补扫共享同一业务处理层；正常 `no_changes` 仍是健康结果，不增加失败或回退。

## 8. legacy 配置迁移

缺少显式模式的旧隐式 self_only 安全迁移为 `all_scanned`；已有显式 self_only/custom 保留用户选择。迁移原子、幂等，并持久化 `RECEIVE_RULE_CONFIG_VERSION=2` 与来源标记。真实覆盖升级后模式仍为 all_scanned，旧配置和秘密未丢失。

## 9. 防回流设计

防回流改为精确的本机 outbound header、outbound_id 和本地发送历史交叉验证，不再仅凭 From 地址判断。自定义 Header 不含秘密；用户从 QQ 网页发送的同地址邮件不会被误判为本机外发；submit_result 的 request_id 幂等保持不变。

## 10. 历史补扫

GUI 支持最近 24 小时、7 天、30 天和自定义日期，默认应用当前规则。实现直接查询当前 provider，不受普通 lookback 限制；分页和 scan_cap 有界，共用进程内/跨进程收件锁，支持取消、进度、有限重试、partial 保留和逐封故障隔离。

真实 Gmail API 窄范围补扫首次扫描 2 封，新增 1 封本地缺失历史邮件、重复 1 封；第二次扫描新增 0、重复 2，目标复杂邮件始终只有 1 个 package，数据库 quick_check 为 ok。

## 11. rule_skipped 重新评估

新增轻量 receive_rule_evaluations 事实，保存 provider/message 标识、结果、原因、规则指纹和 scan_id，不保存正文或附件。rule_skipped 只代表本次规则不接受，不进入永久去重黑名单；规则变化后历史补扫可重新评估。隔离测试覆盖“先拒绝、越过 lookback、放宽规则、历史补扫恢复、再次补扫去重”。

## 12. GUI 任意收件人

GUI 手动发件现在接受一个用户明确输入的合法 recipient，拒绝空值、多收件人、CR/LF 注入和非法地址；outbound 事实记录实际 To。自动化层通过。当前没有与 OWNER_GMAIL 不同且已获明确授权的安全测试地址，因此真实外部投递标为 NOT_TESTED，没有向陌生地址发信。

## 13. MCP 固定收件人安全边界

PASS。MCP submit_result schema 不含 recipient，服务仍只使用 OWNER_GMAIL；GUI 的手动 recipient 能力没有进入 MCP。真实安装版 tools/list 和源码测试均确认该边界。

## 14. Header 解码

From、To、CC、BCC、Reply-To 使用 RFC 2047/地址结构解析，展示 decoded display name 与规范地址；解码失败也保留 address。真实复杂邮件 MCP 返回的联系人没有暴露编码串，隔离测试覆盖所有 Header、多地址、折行和异常编码。

## 15. decoded/raw 事实模型

raw.eml 和原始 Header 原样保留；mail_packages 增加 raw header、contacts_json、provider identity 和 outbound origin 事实。GUI/MCP 使用人类可读联系人，搜索同时覆盖 display name 和 normalized address，未假设永远只有一个账号。

## 16. 邮件正文区域

详情页改为纵向 splitter，正文优先并设置约 240px 的安全最小高度；1080p 默认达到约 340–400px 级别的可视空间。正文只读、有界、可滚动，列表仍只显示严格受限摘要，HTML 不执行脚本。

## 17. 资源分区

邮件内容、邮件中的图片、附件、链接与下载动态分区；空分区隐藏，非零图片/附件/链接/下载计数始终可见。长文件名、路径、时间、0 字节与动作信息不再被无意义省略；用户界面不暴露内部 resource enum。

## 18. 链接无标题识别

URL 继续从正文和 HTML 离线识别，不依赖主题或正文出现“链接”提示词。测试覆盖无提示词的 http/https URL、去重和 5 个真实复杂邮件链接；默认不联网、不自动下载、不放宽 trusted domains。

## 19. 链接展示优化

display_name 按可读标题、文件名、路径语义和域名生成，避免笼统“链接/下载”及低信息量 view/report。真实 MCP 的 5 个链接均具有非空、非通用显示名。

## 20. 数据库迁移

v1.3 联系人、provider identity、outbound origin、规则评估和索引迁移在短事务内执行，失败回滚；升级前自动创建数据库备份。真实 1.2.1 库覆盖升级后业务计数保持 22 个 package、85 个资源，迁移列和唯一索引存在，quick_check、integrity_check、foreign_key_check 均通过。历史补扫随后按预期新增 1 个真实 package。

## 21. 自动化测试结果

最终 clean build 内完整 pytest：529 passed，1 skipped，0 failed，耗时 1623.97 秒。此前新增专项回归 133 passed；Qt 暗色资源区修复后 GUI 相关 48 passed；打包 MCP 去 Qt 依赖修复后专项 19 passed。所有结论均来自实际执行。

## 22. 真实 E2E

已执行真实 1.2.1 前置 MCP、1.3.0 已安装 MCP、真实 Gmail API 历史补扫、覆盖安装和数据保全。真实 MCP 完成搜索、详情、4 类文本读取、CSV preview、6 类二进制 preview/prepare、0 字节、raw.eml、Hash、UTF-8/BOM、flush、stdout purity 和 EOF。真实 IMAP与不同外部 recipient 投递未执行。

## 23. 实现后 MCP 再验收

PASS。安装版 1.3.0 MCP 协议为 `2025-06-18`，7 工具顺序兼容；目标搜索唯一命中，18/9/1/5/0 资源计数与基线一致。准备 6 个资源到本次进程明确授权的 QA 工作区，源/目标大小和 SHA-256 全部一致；持久化 MCP 读件开关保持 false。

## 24. 历史补扫真实/模拟验证

真实 Gmail API：首次恢复 1 封缺失历史邮件，第二次零新增并全部 duplicate，目标 provider 在两个 scan_id 中均有评估事实。原先跨设备漏收样本属于另一 Gmail，未冒充当前账号样本；其“先规则拒绝后恢复”场景由 Gmail API/IMAP 隔离回归覆盖。7 天、30 天、分页、上限、取消、partial、retry 和 rule bypass 均有自动化证据。

## 25. GUI 结构验收

浅色/深色、100%/125%/150% 共 6 张邮件详情截图已人工检查，分辨率分别为 1500×950、1875×1188、2250×1425。暗色资源区在首轮检查发现并修复后重拍通过；最大化/还原、标题栏双击、几何约束、空分区、历史补扫和发件 recipient 由 Qt 回归覆盖。

## 26. 文档

README、GUI 使用、CLI/MCP、邮件事实查询、安全诊断、Windows 安装升级、发布清单、版本与安装脚本均更新到 1.3.0，并明确默认收件、lookback 与补扫、GUI/MCP 发件边界、联系人、资源、链接和后续路线。

## 27. 敏感扫描

正式构建 scanner 对 dist/release 共 279 个文件通过，禁止文件名和已配置秘密标记命中为 0；ZIP 成员也检查。最终补充扫描覆盖 Git 跟踪文件、测试 fixture、docs/reports、运行日志、dist、release 和 ZIP，使用真实 OAuth/Token/Credential 标记仅在内存匹配，不输出值；`.env`、credentials、token、SQLite、raw.eml、邮件、附件和日志均未进入 Git 或发布包。

## 28. Git commits

本轮实现提交为：

- `e13b31f` feat: fix receive semantics and add historical rescan
- `bac9566` feat: allow explicit GUI recipients and decode mail addresses
- `0bb7fdd` ui: improve mail detail resources and link presentation
- `4d3d05e` docs: finalize v1.3.0 verification
- `1075fc5` fix: keep packaged MCP independent from Qt

最后一项来自 clean build 实际捕获的 packaged MCP Qt 导入问题，并增加了无 PySide 环境回归。

## 29. push

master 已推送到 origin/master；最终专项报告也随收口提交并推送。未创建 GitHub Release。

## 30. clean build

从清理 build/dist/release 开始执行正式 Windows 构建脚本，顺序完成 529/1 pytest、PyInstaller、GUI packaged self-test、MCP smoke、Inno Setup、ZIP、checksums 和 secret scan，退出码 0。打包 MCP 的首次失败没有被忽略，修复并推送后重新从头 clean build 成功。

## 31. installer / ZIP

生成 `AgentMailBridge-1.3.0-Setup.exe` 和 `AgentMailBridge-1.3.0-Windows-x64.zip`。ZIP 独立解压后 GUI self-test 和 MCP 7 工具 smoke 均通过；安装器完成 1.2.1→1.3.0 覆盖安装，退出码 0。安装目录 GUI/MCP 与 dist Hash 一致。

## 32. Defender

PASS。Microsoft Defender、实时保护均启用，签名版本 1.455.256.0；对 release 目录执行 CustomScan，耗时约 4.43 秒，新增检测 0。

## 33. Authenticode

NOT_TESTED / NotSigned。dist GUI、dist MCP、安装后 GUI、安装后 MCP 和 Setup.exe 的状态均为 NotSigned；当前没有代码签名证书，未伪造签名通过。该项列为发布治理 P2。

## 34. 覆盖安装

PASS。安装器稳定 AppId 覆盖 1.2.1，安装后两个 EXE 均为 1.3.0；GUI packaged self-test、MCP smoke 和真实 MCP 均通过。桌面与开始菜单两个快捷方式都只指向主 GUI，无 MCP 快捷方式；无 Run 启动项、服务、计划任务或遗留 MCP 进程。卸载保留测试为 NOT_TESTED，以避免无必要卸载当前可用安装。

## 35. 数据保留

PASS。安装前后未启动程序的严格摘要对比完全一致：配置、OAuth credentials/token、Windows Gmail/QQ 凭据、SQLite、received/send/sent、复杂邮件 package、raw.eml、附件、工作区配置、日志和备份均保留。首次 1.3 初始化仅按设计新增迁移标记和有效 pre-v1.3 备份；22 个 package、85 个资源、22 条收件业务记录不变，MCP 读件持久开关仍为 false。随后历史补扫新增 1 封属于明确验收行为，不是安装器改写。

## 36. SHA-256

- Setup.exe：`7d5f20751eccfd8ece47a8bdd599ef58434c2b6dbdede55f4de690e2afb0d564`，40971588 bytes
- Windows-x64.zip：`c3d3cf7a5727a9791a70d2579ca19a7498bb60b4d201ccb77d74073e21eda8fa`，64072112 bytes
- GUI EXE：`84616c8b9c7ec73059e44821e56ec60cc2a0cab86b901f6874f377336f5a62e5`，6812784 bytes
- MCP EXE：`7878ec61c0434ab53715493ff81b2851a17cf951e2af119150973aaa4bb8484d`，6821941 bytes

`checksums.sha256` 复算一致；安装版 GUI/MCP 与 dist 对应 Hash 完全相同。

## 37. P0/P1/P2

P0：0。P1：0。P2：4 个验收/发布环境缺口，分别为未签名、真实 IMAP 未执行、GUI 向不同安全外部地址真实投递未执行、卸载保留未执行；均不是本轮已发现功能回归。

## 38. PASS / CONDITIONALLY PASS / FAIL

最终判定 CONDITIONALLY PASS。46 项矩阵中 43 项 PASS、1 项 PARTIAL、2 项 NOT_TESTED、0 项 FAIL。核心收件语义、历史恢复、MCP 安全、复杂资源、数据库、构建、安装和数据保全可以交付；条件项不阻塞本地 1.3.0 使用，但公开签名发布前应补齐证书。

## 39. 下一阶段 v1.4.x

v1.4.x 聚焦 GUI 视觉质感和 Gmail OAuth/IMAP 新手教程入口，不改变本轮收发语义、安全边界或统一归档模型。

## 40. 后续 v2.x Route B

v2.x 再进入多邮箱 Route B，先设计 account/mailbox/thread 隔离、迁移和 UI ownership；本轮没有提前引入 Outlook、QQ/163 收件、多租户、SaaS、RAG 或 Agent 编排。

## 最终回归矩阵（46 项）

1. PASS：v1.2.1 OAuth 回归。真实 Token 无浏览器复用，Gmail API readonly 查询成功。
2. PASS：Gmail API receive。真实历史补扫新增 1 封缺失历史邮件。
3. NOT_TESTED：IMAP receive。当前真实账号使用 gmail_api；IMAP PEEK、分页与去重由自动化通过。
4. PASS：新安装默认 ALL_SCANNED。配置默认和新安装测试通过。
5. PASS：legacy 配置迁移。隐式旧默认、显式模式、幂等和真实配置迁移通过。
6. PASS：self_only 高级规则。保留为明确高级选择并统一执行。
7. PASS：custom rule。发件人、主题、附件组合与 legacy 语义测试通过。
8. PASS：自动增量收件。调度、overlap、no_changes、partial、retry 与恢复测试通过。
9. PASS：手动立即收取。与历史补扫文案和行为边界测试通过。
10. PASS：历史补扫 24h。UI 范围、provider 查询和真实单日窄范围通过。
11. PASS：历史补扫 7d/30d 或模拟。日期范围、分页、scan_cap 测试通过。
12. PASS：旧 rule_skipped 恢复。隔离场景通过，真实补扫另恢复 1 个缺失 package。
13. PASS：去重。真实第二次补扫新增 0、重复 2，目标 package 唯一。
14. PASS：outbound origin。精确 Header、本地历史交叉验证与非本机同地址测试通过。
15. PARTIAL：GUI 任意 recipient。完整自动化通过；不同安全外部地址真实投递 NOT_TESTED。
16. PASS：MCP fixed owner。真实 schema 无 recipient，OWNER_GMAIL 边界保持。
17. PASS：From 解码。真实 MCP 和 RFC 2047 测试通过。
18. PASS：To/CC/BCC/Reply-To 解码。结构化多地址和异常回退测试通过。
19. PASS：今日收到邮件列表。真实 MCP today 查询及 GUI 列表回归通过。
20. PASS：邮件详情正文区域。六档截图和最小高度回归通过。
21. PASS：inline image 分区。真实 1 个内联图和 GUI 分区通过。
22. PASS：attachment 分区。真实 9 个附件和动态分区通过。
23. PASS：link/download 分区。真实 5/0 计数和动作布局通过。
24. PASS：空分区隐藏。Qt 回归与截图通过。
25. PASS：无“链接”提示词 URL 识别。离线 fixture 通过。
26. PASS：link display_name。真实链接无通用名，规则测试通过。
27. PASS：Markdown read。真实 MCP 有界读取通过。
28. PASS：TXT read。真实 MCP 有界读取通过。
29. PASS：JSON read。真实 MCP 严格文本读取通过。
30. PASS：CSV preview。真实列和行分页预览通过。
31. PASS：PNG/PDF/DOCX/XLSX prepare。真实安装版 MCP 准备与 Hash 通过。
32. PASS：0 字节 DAT。预览、准备、大小和空内容 Hash 通过。
33. PASS：Hash。源、事实、准备目标和发布物复算一致。
34. PASS：raw.eml 保留。真实 bytes 有界读取，覆盖安装摘要不变。
35. PASS：MCP 7 tools 兼容。基线、dist、ZIP、安装版均通过。
36. PASS：stdout purity。BOM、中文、malformed JSON 与协议输出通过。
37. PASS：EOF exit。真实安装版 stdin 关闭后退出码 0。
38. PASS：database migration。真实旧库升级、约束和完整性检查通过。
39. PASS：backup/restore。3 个真实备份均有效；确认、恢复和失败回滚自动化通过。
40. PASS：packaged GUI。clean build、ZIP 和安装版 self-test 通过。
41. PASS：packaged MCP。隔离 smoke 与真实复杂邮件 E2E 通过。
42. PASS：installer overwrite。1.2.1→1.3.0 实际覆盖安装通过。
43. PASS：user data preserve。严格与语义摘要对比通过。
44. PASS：secret scan。Git、fixture、报告、日志、dist/release/ZIP 均通过。
45. PASS：Defender。release CustomScan 新增检测 0。
46. NOT_TESTED：Authenticode。所有待签名 EXE 状态为 NotSigned，当前无证书。
