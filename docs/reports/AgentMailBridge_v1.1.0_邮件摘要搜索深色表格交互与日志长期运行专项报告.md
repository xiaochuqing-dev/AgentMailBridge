# AgentMailBridge v1.1.0 邮件摘要、搜索、深色表格交互与日志长期运行专项报告

## 结论

本专项的实现、自动化测试、真实中文收发、邮件事实搜索、日志压力模拟、三档 DPI 截图、敏感扫描、clean build、Defender 扫描、覆盖安装、安装版 MCP 回归和 master 推送均已完成。P0 为 0，P1 为 0。正式产物未签名、Gmail 网页会话未登录、用户在 2026-07-17 明确免除最终安装版人工界面验收，这三项列为 P2，最终判定为 CONDITIONALLY PASS；不影响已验证的邮件、Hash、数据库、构建和安装结论。

## 基线、版本与根因

1. 开始前 master / HEAD：本地与 `origin/master` 一致，基线为 `a35d48c472c0423c79cb498ad0d08b176949286b`。
2. v1.0.0 能力复核：单 Gmail、单 QQ、Gmail API/IMAP、QQ SMTP、自动收件、受控 MCP、Mail Package、raw.eml、邮件详情、历史、Files & Data、完整手动发件、Windows installer 和 ZIP 均可复用，本轮没有重做这些架构。
3. v1.1.0 升级范围：仅收口邮件列表摘要、整行视觉、深色 Hover、邮件事实搜索、中文解码、自动收件日志降噪、app_events 保留、日志管理和发布版本。
4. 版本来源审计：`agent_mail_bridge/version.py`、GUI、About、CLI、MCP serverInfo、GUI/MCP EXE metadata、Inno Setup、README、CHANGELOG、installer 与 ZIP 文件名均统一为 1.1.0。
5. 收件摘要根因：旧 GUI 把较长 `body_summary` 直接放入主表并按文本动态增高，列表承担了正文阅读职责，长邮件可形成约 200～260 px 的异常行。
6. 最近发送摘要根因：旧实现同样截取长正文并动态增高，正文挤压主题、时间、状态和操作列。

## 共享摘要与整行交互

7. 共享摘要构建器：新增 `mail_summaries.py`，收件与发件复用正文压缩、Markdown 清理、资源事实、Tooltip 和行高规则。
8. 正文摘要策略：合并空白与排版换行，列表正文预览约 36 个用户可读字符、最多两行。该长度由真实 100%/150% DPI 截图校准，目的是在现有内容列宽下让资源事实稳定可见；数据库正文和 raw.eml 不变。
9. 资源数量展示：附件、邮件图片、链接、下载数量放在摘要首行，0 项省略；有正文时仍不会隐藏资源事实。
10. 主题与摘要原则：正文是可缩略摘要；主题优先展示并受控在两行视觉空间，完整主题保留在 Tooltip 和详情。
11. 行高策略：收件和最近发送统一固定为 74 px，长正文、长主题和资源数量不再把行撑到 200 px 以上。
12. Tooltip：包含完整主题、发件人、资源事实与最多 600 字符的正文预览，并提示双击查看完整邮件，避免 Tooltip 覆盖整个窗口。
13. 深色 Hover 根因：全局 `QTableWidget::item:hover` 使用浅色背景，深色主题没有完整覆盖专用邮件表。
14. 单元格分块根因：QTableWidget 默认按 item 绘制 hover、selection、focus 和列边界，使一封邮件看起来像多个独立格子。
15. 整行视觉实现：收件和最近发送使用专用 `mailRecordTable` 样式，显式定义浅色/深色 Hover、透明 selection、无 focus 框和无竖向分隔；其他业务表不受影响。
16. 交互保持：表格采用 NoSelection/NoFocus，但双击非按钮区域仍打开邮件或发送详情，真实按钮仍可点击、Tab 可达、Enter/Space 可触发。

## Mail Facts 搜索与中文编码

17. 旧搜索范围：仅过滤 GUI 内存中的主题、发件人和短正文摘要，无法稳定命中收件人、完整正文、附件和链接。
18. 新搜索范围：收件搜索改为调用只读 Mail Facts 服务，覆盖主题、发件人显示名/地址、To、CC、BCC、完整可读正文、资源名称、URL、archive/parse 状态自然文案。
19. 附件、链接和收件人：真实搜索已分别通过中文附件名、CID 图片名、链接显示文字、域名、URL 路径、收件人和抄送人命中同一邮件。
20. 去重与性能：每个关键词通过 package 字段加 `EXISTS` 资源子查询组合，多个资源同时命中不会 JOIN 放大；多词按 AND 处理，GUI 使用 250 ms debounce 和有限结果集。
21. 中文乱码定位：真实 E2E 暴露 RFC 2047 编码中文片段与 ASCII 标记相邻时，旧 `decode_header` 手工拼接会丢失必要空格；正文侧还需要在缺失或错误 charset 时选择可信解码。
22. 四层证据：Gmail 服务端连接器与项目 Gmail raw API 显示主题、正文、HTML、CID 和长中文附件名正确；raw.eml 为真实原始字节；SQLite 保存的事实、资源名和 Hash 正确；GUI 截图无 `????` 或替换字符。Gmail 网页因浏览器会话未登录未做目视截图，列为 P2。
23. 编码矩阵：UTF-8、GBK、GB2312、Big5、无 charset、错误 charset、RFC 2047、RFC 2231 与折行混合 Header 均有自动化验证。
24. 中文内容：真实复杂邮件包含中文长主题、中英文正文、Emoji、HTML、3 个附件、1 个长中文附件名、1 张 CID 图片、普通链接和文件样式链接，解析与展示未出现无故乱码。

## 日志长期运行与管理

25. 文件日志轮转：保留既有 RotatingFileHandler，单文件约 2 MB、最多 5 个历史文件，本轮未改变该安全边界。
26. app_events 膨胀根因：旧实现每次自动检查开始和无变化完成都 INSERT，SQLite 技术事件会按分钟无限增长，而自动收件状态表已经能表达健康心跳。
27. 自动日志降噪：automatic=True 且无新增/失败时不再写开始、完成和去重扫描永久事件，后端细节仅保留在 DEBUG 文件日志。
28. 事件保留边界：新增邮件、部分完成、真实失败、首次 backoff、网络恢复、手动收取、发件、MCP、配置和维护事件保留；普通自动 no_changes 心跳不保留。
29. 保留配置：普通事件支持 7/30/90 天，WARNING/ERROR 支持 30/90/180 天，硬上限支持 5000/10000/20000 条，非法值回退默认。
30. 默认时间策略：普通技术事件 30 天，WARNING/ERROR/FAILED 90 天。
31. 数量策略：默认超过 10000 条后批量降至约 8000 条；测试以缩小边界验证 101→80，算法比例与正式配置一致。
32. 清理触发：应用初始化异步清理，watchdog 每 24 小时检查一次，日志管理页支持立即清理；单次失败允许后续重试且不会制造递归日志风暴。
33. WAL/VACUUM 边界：清理使用短事务，先按时间再按数量；不在 GUI/日志写入线程做大删除，不自动执行阻塞性的 VACUUM，文件日志轮转与 SQLite 保留相互独立。
34. 日志管理页面：新增日志概览、组合筛选、保留设置、分页、导出、详情、立即清理、清除日常检查和清空全部技术日志。
35. 日志概览：显示总数、今日错误、日常检查数、预计过期数和最近清理时间。
36. 类型筛选：支持事件类型、级别、时间范围和多词搜索组合。
37. 日常检查筛选：默认隐藏日常检查，用户可勾选显示；清除日常检查只删除匹配的 app_events。
38. 分页：每页最多 150 条，支持加载更多，查询始终使用 LIMIT/OFFSET，不一次性加载全表。
39. 导出：当前筛选可导出 UTF-8 BOM CSV，诊断导出继续可用；token、密码、授权码和敏感值按规则脱敏。
40. 危险操作：立即清理、清除日常检查和清空全部技术日志分别产品化；清空全部技术日志有二次确认。
41. 不误删证明：清理代码只操作 app_events；自动化测试核对 mail_packages、mail_resources、outbound_messages、sent_files、mcp_calls、retry state 与邮件文件均不变。覆盖安装前后业务数据也保持不变，真实 E2E 后仅按预期新增记录。

## 自动化、真实 E2E 与 GUI QA

42. 自动化测试：最终源代码全量为 391 passed、1 skipped，耗时 912.24 秒；clean build 内再次为 391 passed、1 skipped，耗时 906.86 秒。跳过项为 Windows 当前账户符号链接权限限制。
43. 真实中文收件 E2E：自动轮询 2 次发现两封测试邮件；复杂邮件形成 1 个 package、3 个附件、1 张邮件图片、2 个链接、HTML 和真实 raw.eml，乱码检查通过。
44. 真实手动发件 E2E：一次 SMTP 形成 1 个 outbound、3 个附件、2 个链接；Gmail 实际收到并自动回收，附件 Hash 全部一致。
45. Agent/MCP 回归：未授权路径返回 path_not_allowed；授权工作区从原路径 success；相同 request_id duplicate；源、staged、pre-SMTP、sent archive 与 loopback Hash 一致。安装版 packaged MCP 也完成真实 success/duplicate，并以 automatic=True 收回 1 个附件，SHA-256 为 `690ad77d04a83231b69c33da8b5a2f3b40335039cf0a55997e6df156a72b4df5`。
46. 真实搜索：主题标记、中文正文、中文长附件名、CID 图片名、域名和 URL 路径分别查询时均只返回 1 封邮件。
47. 长时间模拟：等价模拟 1440 次每分钟无变化检查，app_events 增量为 0；偶发业务事件仍保留；时间清理、101→80 数量清理、异步上限收缩和业务数据不变均通过。
48. UI 矩阵：源代码真实 Qt 数据截图覆盖浅色、深色、Hover、搜索、日志、Normal、maximize、restore、手动 resize、100%/125%/150% DPI；邮件表无白闪、无竖向分块，资源数量可见。最终安装版人工界面复核由用户明确免除，列为 P2。
49. QA 截图：位于 gitignored `qa-artifacts/v1.1.0-e2e/screenshots/{100,125,150}`，共 18 张；不进入 Git。

## 文档、安全、Git、构建与安装

50. 文档更新：README、CHANGELOG、AGENTS、GUI 使用说明、安全与诊断、Windows 安装升级、邮件事实查询和本报告均已同步 v1.1.0。
51. 敏感扫描：项目脚本扫描 864 个构建文件通过；暂存区的真实账号、OAuth token、私钥、秘密值和用户绝对路径命中均为 0；Git 未跟踪 `.env`、credentials.json、token.json、SQLite、raw.eml、邮件、日志、release 或 QA 截图。
52. Git：实现提交为 `096525d84817d660eb4262af430cc4ac05b6b262`，已正常推送 `origin/master`；本报告作为独立 docs 提交推送，未 force push、未创建 GitHub Release。最终 HEAD 以本报告提交后的仓库状态和最终回复为准。
53. clean build：总耗时 1037.8 秒，项目原生脚本完成清理、全量 pytest、PyInstaller、packaged self-test、MCP smoke、build verification、Inno Setup、ZIP、checksums 和秘密扫描。
54. EXE：`dist/AgentMailBridge/AgentMailBridge.exe` 与 `AgentMailBridgeMCP.exe` 均为 FileVersion/ProductVersion 1.1.0；GUI self-test、MCP UTF-8/BOM/EOF/安全拒绝 smoke 通过。
55. 发布产物：已生成 `release/AgentMailBridge-1.1.0-Setup.exe`、`release/AgentMailBridge-1.1.0-Windows-x64.zip` 和 `release/checksums.sha256`。
56. Defender / Authenticode：Windows Defender Antivirus 与实时保护开启；release 和 dist 自定义扫描前后历史检测均为 5，本项目新增检测 0。GUI、MCP 和 installer Authenticode 均为 NotSigned。
57. 覆盖安装：v1.0.0→v1.1.0 静默覆盖安装退出码 0。配置、OAuth、Credential Manager、SQLite、18 个 package、53 个资源、7 个 outbound、13 条 MCP 审计、目录文件数及自动收件状态在首次业务运行前完全保留；真实安装版 E2E 后按预期变为 19 个 package、56 个资源、8 个 outbound、15 条 MCP 审计，数据库 integrity_check 为 ok。
58. 桌面快捷方式：`AgentMailBridge.lnk` 存在，目标和工作目录均为正式安装目录，不指向源码、dist 或 Python；第二次启动前后正式进程数均为 1，单实例通过。最终可视交互验收按用户指示免除。
59. SHA-256：installer 为 `1bc37dc7d8b3bae48e0973fa25d07e6af7efa8ec15c401c617ed2dabe5847f40`；ZIP 为 `76edbbce0ef06469055b3d24e312e3896560543d9b6356393cf8b728af305ac4`，与 checksums.sha256 一致。
60. 剩余问题：P0=0，P1=0，P2=3：正式产物未做 Authenticode 签名；Gmail 网页会话未登录；最终安装版人工界面验收由用户免除。签名建议在公开分发前补齐，其余两项已有 Gmail 服务端/raw/SQLite/Qt 截图与安装版后台证据覆盖。
61. 最终判定：CONDITIONALLY PASS。核心功能、可靠性、安全边界、真实收发、搜索、长期日志、构建、安装、Hash 和数据保留均通过；未验证项已明确列出，没有伪造证据。
