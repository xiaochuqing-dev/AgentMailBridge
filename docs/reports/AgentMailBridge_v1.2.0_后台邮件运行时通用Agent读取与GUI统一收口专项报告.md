# AgentMailBridge v1.2.0 后台邮件运行时、通用 Agent 读取与 GUI 统一收口专项报告

## 结论

本专项已完成 v1.2.0 实现、自动化回归、独立 stdio 协议验证、真实 Hermes Agent 读件与回邮闭环、GUI 预验收整改、文档、敏感扫描、clean build、Defender、覆盖安装、卸载保留、重装和桌面快捷方式验证。P0 为 0，P1 为 0。发布文件未做 Authenticode 签名，用户指出两项 GUI 问题后明确要求修复并直接收尾、未进行第二轮截图确认，这两项列为 P2。最终判定为 CONDITIONALLY PASS。

## 基线、定位与架构

1. 开始前 HEAD：本地 master 与 origin/master 一致，基线为 `02d81bb528ce4182311ebbf306a509f910f4560c`。
2. v1.1.0 能力复核：Gmail API/IMAP、QQ SMTP、自动收件、重试、统一 Mail Package、raw.eml、Mail Facts、邮件详情、完整发件、受控 submit_result、Windows 双 EXE、installer 与 ZIP 均保留复用。
3. 产品差异化：AgentMailBridge 仍是本地优先、Provider 无关的单用户邮件桥，不扩展为 Gmail MCP、Agent 平台、知识库、RAG、项目管理或多租户服务。
4. GUI 非必经：邮件读取由按需启动的本地 stdio MCP 直接访问用户目录中的 SQLite 与统一归档；主窗口未打开或完全退出时，已归档邮件仍可读。
5. 后台边界：GUI/托盘继续承担自动收件、重试和通知；MCP 不建立监听端口、不注册启动项、不显示托盘图标，stdin 关闭即退出。

## MCP 读取能力

6. 读取总开关：新增 `MCP_MAIL_READ_ENABLED`，默认关闭；GUI 可一次性启用并持久化，关闭时读取工具稳定返回 `read_access_disabled`，submit_result 不受影响。
7. 工具清单：tools/list 统一暴露 `submit_result`、`search_mails`、`get_mail`、`read_mail_resource`、`prepare_mail_resources`、`list_agent_workspaces`、`get_mail_sync_status` 共 7 个工具。
8. search_mails：支持 latest、today、yesterday、recent_days、date_range、all，支持结构化字段、newest/oldest、limit/offset、ensure_fresh、allow_cached 和未来账号兼容字段。
9. 搜索语义：query 多词按 AND 处理，覆盖主题、发件人、收件人、CC/BCC、完整可读正文、资源名称、链接文字、域名、URL 和自然状态；资源多处命中仍一封一行。
10. get_mail：返回有界正文、正文状态、时间、收发件人、会话、资源清单、Hash、可用性和真实 raw.eml 描述；长正文支持 offset、max_chars、next_offset、has_more。
11. read_mail_resource：resource_id 必须属于指定邮件；文本、preview、csv_preview、raw 均有范围上限，不允许路径穿越、任意文件读取、附件执行或修改。
12. 文本编码：支持 UTF-8、BOM、GBK/GB2312、GB18030、Big5 等可靠回退；扩展名与字节特征共同阻止伪装二进制按文本读取。
13. CSV/TSV：使用流式解析，支持逗号、Tab、分号、引号及字段内换行，返回编码、分隔符、列名、指定行范围、截断状态、大小和 SHA-256。
14. 图片：PNG、JPG、WebP、GIF、BMP 返回格式、宽高、大小、Hash、资源标识和所属邮件；不引入 OCR。
15. PDF/Office/二进制：PDF、DOCX、XLSX、PPTX、ZIP、EXE 和未知二进制返回类型与安全描述，可受控准备但不执行、不解压。
16. 链接：返回 URL、显示文字、分类、来源、下载状态及已下载资源关系；普通链接保持离线，不由 AgentMailBridge 自动访问。
17. prepare_mail_resources：由产品将指定资源原子复制到授权工作区的邮件子目录，保留文件名，安全处理冲突，复制前后大小与 SHA-256 不一致即失败。
18. 准备边界：目标必须位于授权工作区，阻止 `..`、绝对路径、符号链接和 junction 逃逸；原归档不变，并生成邮件说明文件。
19. list_agent_workspaces：仅返回 GUI 明确授权工作区的稳定标识、完整显示路径、可用状态与默认状态，不枚举无关目录。
20. get_mail_sync_status：返回自动收件状态、上次检查/成功、最近结果、下次检查、重试数、跨进程同步状态、本地数据年龄和 freshness。
21. ensure_fresh：数据足够新时直接查询；过期时调用既有收件服务；其他进程刷新时有限等待或返回 `sync_in_progress`；失败缓存会明确标注 stale/cached 与同步错误。
22. 跨进程互斥：新增 Windows/POSIX 字节锁，具有超时、进程崩溃自动释放和普通只读查询不受阻塞的边界；同一时刻只允许一个刷新任务。
23. submit_result 兼容：名称、参数、固定 OWNER_GMAIL、授权根、request_id 幂等、速率限制、受控 staging 和源文件至发送归档 Hash 链均保留。
24. 通用配置：GUI 只生成一份标准 stdio MCP JSON；Codex、Claude Code、Hermes、OpenCode 等说明复用同一 server 和工具，不创建客户端专用业务实现。
25. 工具元数据：所有输出均使用 structuredContent 加简洁 text，稳定 error_code、有界文本和 UTF-8；MCP stdout 仅含 JSON-RPC，诊断只进 stderr/文件日志。

## 审计与数据库

26. 统一审计：新增增量 `mcp_audit_events`，覆盖搜索、读邮件、读资源、准备、同步状态和发送；旧 `mcp_calls` 保持兼容并在 GUI 统一查询。
27. 审计最小化：记录工具、操作、目标摘要、状态、error_code、时长、返回字节数、缓存与同步事实，不保存正文全文、附件内容或凭据。
28. 数据库迁移：迁移幂等且只增量建表/索引，不重置 SQLite，不修改现有 Mail Package、资源、业务历史、重试和自动收件状态。
29. DATA_ROOT 安全：每次 MCP 读取都会重新验证正式 DATA_ROOT、package 根和 package-relative 路径，数据库中被篡改的逃逸路径也会被拒绝。

## GUI 统一收口

30. 左侧 Agent/MCP 入口：收件和发件共用一个独立入口；最终按用户反馈与历史、文件与数据、设置、关于合并为同一导航卡片，消除脱离感和多余空隙。
31. 独立页面：集中展示 MCP 状态、读取开关、同步状态、统一配置、两个简短示例、工作区授权和最近 MCP 调用，不复制右侧健康面板。
32. 旧交付指令：删除主题输入、长文本框、复制长交付指令和旧方案说明；收件页、发件页继续只承担各自业务职责。
33. 最近 MCP 调用：数据源统一为新审计与旧发送记录，操作产品化为搜索邮件、读取邮件、读取附件、准备资源、查询同步和发送结果。
34. 整行视觉：表格采用 NoSelection、NoFocus、整行统一背景和仅横向轻分隔；没有竖向网格、border-left、单元格焦点竖线或每格独立 Hover。
35. 无字符分隔：正式 GUI 不使用字符 `|` 或 `｜` 表达调用列，调用时间、操作、目标、状态和详情由真实列与留白组织。
36. 发件资源竖线整改：附件与链接表使用专用 `compactResourceTable` 浅色/深色样式，关闭单元格选择、焦点和交替行背景，截图箭头所示青色竖条根因已移除。
37. 路径与目标：DTO 保留完整路径，主行优先显示文件名和有意义父目录；完整路径可在详情、复制和打开动作中使用，不压缩为无意义的盘符省略。
38. MCP 调用详情：提供结构化的调用时间、tool_name、目标、状态、error_code、request_id、Hash/准备路径等安全诊断字段，不回显秘密和正文。
39. 全局刷新：顶栏统一刷新当前页、右侧服务状态、今日统计、连接健康、快捷提示、同步状态和最近调用；刷新本地页面不擅自触发网络收件。
40. 全局样式根因：移除表格 item 的竖向边界、selection/focus 污染和浅色 Hover 泄漏；专用业务表在浅色与深色主题中均保持整行一致。
41. 用户 GUI 验收：用户在首轮桌面 GUI 中指出导航入口空隙和发件资源表竖条两项问题，并明确要求修复后直接收尾。两项均完成结构修复与自动化回归；按用户要求未再次暂停做第二轮截图确认，因此不伪称二次目视通过。

## 测试与真实闭环

42. 自动化：整改后的源代码全量为 403 passed、1 skipped；clean build 内再次为 403 passed、1 skipped，耗时 925.19 秒。聚焦 v1.2 与兼容回归为 19 passed。
43. GUI 回归：新增测试固定 Agent/MCP 导航同卡片、附件/链接表 NoSelection/NoFocus 和专用无竖线样式；最终完整套件通过。
44. 独立协议 E2E：真实 stdio 子进程验证 initialize、BOM、中文路径/标题、tools/list、全部 tools/call、flush、EOF、错误响应和 stdout 纯净。
45. GUI 非必经 E2E：在 GUI 退出后，正式归档搜索、邮件正文和附件读取仍通过；MCP 进程按需启动并在 EOF 后退出。
46. 真实 Agent E2E：本机已安装 Hermes Agent 临时接入同一标准 MCP，成功搜索 1 封目标邮件、读取 512 字正文并读取附件；测试后已移除临时客户端配置。
47. 真实后台收件与回邮闭环：Hermes 调用 submit_result 发送真实附件，Gmail API 自动收回，随后 Hermes 搜索、get_mail、read_mail_resource 均成功，回收附件 SHA-256 与发送源一致。
48. 搜索 E2E：latest/today/yesterday/recent_days/date_range、newest/oldest、分页、主题/正文/发件人/收件人/资源/链接命中和一封一条去重均有自动化；正式归档目标搜索实测命中 1 条。
49. CSV E2E：中文 CSV、引号、字段内换行、行范围和截断通过；大文件路径使用流式读取，未用一次性全量加载替代。
50. 图片与文档资源 E2E：PNG 宽高、PDF/Office/EXE 安全描述、二进制拒绝文本解码、raw.eml 分段和受控资源准备 Hash 均通过自动化。
51. ensure_fresh 与互斥：新鲜缓存、过期触发、同步失败缓存标识、锁竞争超时、持锁进程退出后恢复均通过；外部真实 Gmail 回收证明既有收件服务可被闭环使用。
52. 已安装 MCP：安装目录中的 1.2.0 MCP 暴露 7 个工具，正式数据搜索命中 1 条，正文 97 字、附件读取和协议纯净通过；submit_result 以既有 request_id 返回 duplicate、Hash 一致且未重发。

## 文档、安全、Git、构建与安装

53. 文档：README、CHANGELOG、AGENTS、GUI 使用说明、MCP 使用说明、安全与诊断、Windows 安装升级、统一邮件归档、邮件事实查询及两份 v1.2 专项说明均已同步。
54. 敏感扫描：真实账号、密码/授权码、OAuth 敏感值、用户绝对路径和真实 E2E 标记的 Git 文件命中均为 0；`.env`、credentials/token、SQLite、raw.eml、manifest、邮件、附件、日志、build/dist/release 均未进入 Git。
55. Git：实现提交为 `45898d1`，打包门禁修复提交为 `cfebe73`，均已正常推送 origin/master；本报告使用独立文档提交，未 force push、未创建 GitHub Release。
56. clean build：从干净提交 `cfebe73` 执行项目正式脚本，完成清理、完整 pytest、PyInstaller 双 EXE、GUI self-test、MCP smoke、Inno Setup、ZIP、checksums 和敏感扫描，退出码 0。
57. 打包门禁修复：首次构建正确发现旧 smoke 仍断言只有 submit_result；更新为 7 工具并增加默认读取关闭验证后，重新 clean build 全部通过，没有绕过门禁。
58. EXE 与发布产物：GUI EXE、MCP EXE、installer、ZIP 均为 1.2.0；生成 `AgentMailBridge-1.2.0-Setup.exe`、`AgentMailBridge-1.2.0-Windows-x64.zip` 和 `checksums.sha256`。
59. Defender：Microsoft Defender Antivirus、实时保护和最新签名均启用；installer 与 ZIP 分别执行自定义扫描，退出码均为 0，未发现威胁。
60. Authenticode：GUI EXE、MCP EXE 和 installer 的实际状态均为 NotSigned；未将无签名伪报为已签名。
61. 覆盖安装：静默覆盖安装退出码 0；4 个核心配置/OAuth/数据库文件哈希未变，用户文件总数保持 148，双 EXE ProductVersion 均为 1.2.0。
62. 卸载保留与重装：卸载退出码 0，安装目录双 EXE 清除；148 个用户文件、4 个核心文件哈希和 2 个 Windows Credential Manager 目标均保留。随后重装退出码 0，数据与凭据计数仍一致，正式 SQLite quick_check 为 ok 且邮件包、资源均存在。
63. 快捷方式：桌面 AgentMailBridge 快捷方式仅指向正式 GUI，桌面和开始菜单中的 MCP 快捷方式数量为 0；已从快捷方式实际启动安装版 1.2.0，MCP 未常驻。
64. SHA-256：installer 为 `57b94731a814c88b2b3a233ce4d3c39dd616741f6f71aa1cde4ad1c710ad7898`；ZIP 为 `c04b831a99fcfccabc9a523546c20aee92a233921235c7d51674e1f71923b702`，与 checksums.sha256 逐项一致。
65. 剩余问题：P0=0，P1=0，P2=2。P2 一为正式产物未做 Authenticode 签名；P2 二为按用户“修复后直接收尾”指示未做修复后的第二轮截图目视确认。建议公开分发前补代码签名；GUI 两项已有代码结构和自动化证据。
66. 最终判定：CONDITIONALLY PASS。通用 Agent 无需打开 GUI 即可搜索和读取邮件，文本/CSV/图片/文档/二进制/资源准备/同步状态/跨进程协调/统一审计/submit_result/真实闭环/构建安装均完成；两个 P2 已明确披露，没有伪造验证结论。
