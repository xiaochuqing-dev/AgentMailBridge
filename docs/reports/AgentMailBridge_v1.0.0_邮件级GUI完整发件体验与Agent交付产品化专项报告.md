# AgentMailBridge v1.0.0 邮件级 GUI、完整发件体验与 Agent 交付产品化专项报告

## 结论

本专项全部完成。邮件级收件、完整发件、Agent 工作区交付、自动化回归、真实收发、GUI 矩阵、clean build、覆盖安装、快捷方式和发布安全验收均通过，最终判定 PASS。

## 基线与根因

1. 开始前 master / commit：`master` 与 `origin/master` 一致，基线为 `6fbc77e`。
2. 第一阶段归档地基复核：真实库已有 16 个邮件 package、43 个邮件资源，raw、正文、附件、链接和 Mail Facts 可直接复用。
3. 文件级 GUI 根因：收件、历史和发送仍从兼容文件表组装行，导致一封多附件邮件拆行；发件只围绕一个 file_path；最近发送列宽未把主题和内容设为主要伸展列。

## 邮件级收件与详情

4. “今日收到文件”已改为“今日收到邮件”，一封邮件一行。
5. Inbox 使用只读 Mail Facts 查询，支持当日范围和关键词。
6. 主表显示主题、发件人、内容摘要、收取时间、状态、操作。
7. 状态转换为已收取、部分完成、需要处理等自然文案。
8. 新增邮件详情二级页。
9. 正文优先安全可读文本，不执行 HTML。
10. 内嵌图片按邮件图片展示并保留安全路径检查。
11. 附件以文件名、大小、状态和真实按钮展示。
12. 链接与下载按网页、文件链接等自然类别展示。
13. 邮件信息保留完整发件人、收件时间等事实，不暴露内部枚举。
14. 新增同会话邮件按时间顺序展示及详情跳转。

## 历史与文件

15. 历史记录按收件邮件和发件邮件展示；已有 outbound 的 MCP 审计不再重复成第二条业务记录。
16. Files & Data 新增“所属邮件”，新收件资源以 `mail_resources` 为权威来源，未映射旧文件继续兼容。
17. 邮件详情可定位附件，文件行可返回所属邮件，实现双向导航。

## 完整发件模型

18. 新增 outbound mail model：`outbound_messages`、`outbound_resources`、`outbound_links`。
19. 启动检测发件模型迁移，真实库迁移前创建并校验 `before_mail_models` 备份。
20. 5 条旧 `sent_files` 已幂等回填为 5 封 Agent outbound，重复初始化不重复创建，不伪造旧正文。
21. 手动发件支持正文或仅正文发送。
22. 支持 0 至多个附件、同路径去重、同名不同路径共存、0 字节和长 Unicode 文件名。
23. 支持 0 至多个显式链接。
24. 一次确认构造并发送一个 MIME；正文、链接和全部附件进入同一邮件。
25. `submit_result` 继续保持固定收件人、request_id 幂等、受控 staging 和四段 Hash 审计，并映射到一个 outbound。

## Agent 交付产品化

26. Agent / MCP 入口已移动到发件页标题右侧，不新增一级导航。
27. 页面提供可复制的完整交付指令。
28. 指令不要求用户填写路径，Agent 根据任务上下文识别最终文件。
29. 工作区授权由 GUI / Service 管理并持久化到 `ALLOWED_SEND_ROOTS`，下一 MCP 会话生效。
30. 未授权 repo 实测返回 `path_not_allowed`、`not_sent`，未产生 outbound；授权后正式安装版 MCP 从 repo 原路径发送成功。
31. 授权拒绝盘符根、用户主目录、Windows、Program Files、ProgramData、AppData、产品数据和敏感文件；真实路径解析继续阻止目录逃逸。

## 响应式与视觉

32. 最近发送改为一封 outbound 一行，主题和正文列伸展并吃满剩余宽高。
33. 修复根因是移除固定单文件列思维，改用伸展表头、动态行高和垂直 splitter。
34. 长主题、正文和文件名使用换行及动态行高，不生成省略号隐藏核心信息。
35. normal、maximize、restore、手动 resize 均通过；手动高度由 1019 缩至 763 后可用。100% 由正式安装版 Windows 控制实测，125% / 150% 由相同代码与真实数据在隔离 Qt 缩放进程生成 1650×1125、1980×1350 截图并人工检查。

## 自动化与真实 E2E

36. 完整 pytest：378 passed、1 skipped，耗时 693.12 秒；跳过项仅为当前 Windows 账户无创建符号链接权限。
37. 真实 Gmail API 收取：`success`，扫描 1、接收 1、保存 1、失败 0；形成 package `pkg_8d101800f85f774780eb86a6`，raw 可用且归档 ready。
38. 真实人工发件：outbound `out_079baa69bcbb47c5bf0b32d92abb1a9d`，一次 SMTP 发送成功，3 附件、2 链接；自动收回后对应 package 为 1 封、3 附件、2 链接、7 个资源。
39. 真实 Agent MCP E2E：正式安装版 `AgentMailBridgeMCP.exe` 从授权 repo 原路径交付本报告；request_id `packaged-real-5dffcf3cf6d54c53ad141168870fdf83` 首次 success、再次 duplicate，只生成 outbound `out_a380a7a7d12b5e4ab952037dd68b80b0`。源、staging、pre-SMTP、sent archive Hash 均为 `2dee86b7393243ae1b03f4f87a95d29c3b16429df4991507c4e414e3221cc84b`。Gmail API 收回 package `pkg_4e5dbdbb6330fae758282ab2`，附件大小 5667、Hash 相同、raw available、archive ready。
40. GUI 截图：开始前位于 gitignored `qa-artifacts/mail-gui-phase2/before`；完成后位于 `qa-artifacts/mail-gui-phase2/after/{100,125,150}`，覆盖收件、详情、会话、历史、所属邮件、发件、Agent、深色、最大化、还原、resize 和快捷方式启动。

## 发布交付

41. README、GUI、MCP、安全、Windows 安装、发布清单和 CHANGELOG 已同步。
42. 构建前、构建内和构建后秘密扫描通过：864 个产物文件、0 个已配置秘密标记命中；Git 未跟踪 `.env`、OAuth、数据库、邮件、日志或 QA 图片。
43. 实现提交 `87e7497676e5ed169acea576e5146165dbb9cad4` 已推送 `origin/master`；本报告最终证据随后单独提交并再次推送。
44. clean build 通过；构建内 pytest 为 378 passed、1 skipped，用时 787.56 秒；总构建用时 938.7 秒。packaged GUI self-test、MCP UTF-8 smoke 和 build verification 均 PASS。
45. 已生成 `AgentMailBridge.exe`、`AgentMailBridgeMCP.exe`、portable ZIP 和 Inno Setup 6.7.3 installer。
46. Defender Antivirus 与实时保护开启；对 release、dist 自定义扫描前后历史检测均为 5，本项目新增检测 0。GUI、MCP、installer Authenticode 均为 NotSigned。
47. 本机覆盖安装退出码 0；配置、credentials、token、SQLite 的安装前后长度和 SHA-256 完全一致。安装目录双 EXE 与 dist Hash 一致，安装版 GUI self-test、MCP smoke 和真实 E2E 通过。首次安装尝试因两个旧 MCP 会话占用 EXE 返回 5，关闭旧会话后正常覆盖，未触碰用户数据。
48. 桌面快捷方式目标为当前安装目录 `AgentMailBridge.exe`；通过链接实际启动后只有 1 个主进程、1 个窗口，第二次启动仍为单实例，关闭窗口后主进程继续在托盘运行。
49. installer SHA-256：`1684834b481709972d02a0eaca7a394fd8ded9705a888d90f9a6eb336e976471`；ZIP SHA-256：`c1853c8041f3382308a17930aaf0dc0f80aa693ecfcd9cd0f3566529716bd9dc`。
50. P0：0，P1：0。P2：当前产物未做 Authenticode 代码签名；不影响本机验收，公开发布前仍建议使用正式证书。请求范围内最终判定 PASS，未创建 GitHub Release。

## 数据安全证据

真实用户库迁移前主库 SHA-256 为 `3ec28767e0bf2f0d9f72a884000c73accc7a2e1d336ecc7613f5f60c9c76c54c`。迁移备份 `agent_mail_bridge_20260716_134109_010527_before_mail_models.db` 状态 valid，SHA-256 为 `9637ebafd8d029a309d720b0aa9f9681fdd16bfcff9c125b2a1b0cf7212488d6`。迁移并 checkpoint 后主库 SHA-256 为 `113d96b4ca80570a289f1b6e81cacce911e4f3b128d4ba75a5ae6eed9675a55e`，`PRAGMA integrity_check` 为 ok。
