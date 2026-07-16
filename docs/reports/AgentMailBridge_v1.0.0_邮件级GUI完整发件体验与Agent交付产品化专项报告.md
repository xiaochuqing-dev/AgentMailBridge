# AgentMailBridge v1.0.0 邮件级 GUI、完整发件体验与 Agent 交付产品化专项报告

## 结论

本专项已完成代码实现、自动化回归和真实收发第一轮闭环。发布构建、安装版验收、最终 Agent 报告交付与 Git 推送证据在本报告收口时补齐。

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
30. 未授权 repo 实测返回 `path_not_allowed`、`not_sent`，未产生 outbound；授权后闭环在最终收口补证。
31. 授权拒绝盘符根、用户主目录、Windows、Program Files、ProgramData、AppData、产品数据和敏感文件；真实路径解析继续阻止目录逃逸。

## 响应式与视觉

32. 最近发送改为一封 outbound 一行，主题和正文列伸展并吃满剩余宽高。
33. 修复根因是移除固定单文件列思维，改用伸展表头、动态行高和垂直 splitter。
34. 长主题、正文和文件名使用换行及动态行高，不生成省略号隐藏核心信息。
35. normal、maximize、restore、手动 resize 与 100% / 125% / 150% DPI 截图证据在安装版最终 QA 后补齐。

## 自动化与真实 E2E

36. 完整 pytest：378 passed、1 skipped，耗时 693.12 秒；跳过项仅为当前 Windows 账户无创建符号链接权限。
37. 真实 Gmail API 收取：`success`，扫描 1、接收 1、保存 1、失败 0；形成 package `pkg_8d101800f85f774780eb86a6`，raw 可用且归档 ready。
38. 真实人工发件：outbound `out_079baa69bcbb47c5bf0b32d92abb1a9d`，一次 SMTP 发送成功，3 附件、2 链接；自动收回后对应 package 为 1 封、3 附件、2 链接、7 个资源。
39. 真实 Agent MCP E2E：待最终报告定稿、正式工作区授权和安装版 MCP 执行后补齐 success / duplicate / 收回 / Hash。
40. GUI 截图：开始前证据已保存到 gitignored `qa-artifacts/mail-gui-phase2/before`；完成后矩阵待收口。

## 发布交付

41. README、GUI、MCP、安全、Windows 安装、发布清单和 CHANGELOG 已同步，最终数值待收口。
42. 秘密扫描待构建前和构建后执行。
43. Git commit / push 待最终收口。
44. clean build 待最终收口。
45. installer / ZIP 待最终收口。
46. Defender / Authenticode 待最终收口。
47. 本机覆盖安装待最终收口。
48. 桌面快捷方式待最终收口。
49. installer / ZIP SHA-256 待最终收口。
50. 当前剩余项均为发布验收步骤；最终判定待全部真实证据完成后给出。

## 数据安全证据

真实用户库迁移前主库 SHA-256 为 `3ec28767e0bf2f0d9f72a884000c73accc7a2e1d336ecc7613f5f60c9c76c54c`。迁移备份 `agent_mail_bridge_20260716_134109_010527_before_mail_models.db` 状态 valid，SHA-256 为 `9637ebafd8d029a309d720b0aa9f9681fdd16bfcff9c125b2a1b0cf7212488d6`。迁移并 checkpoint 后主库 SHA-256 为 `113d96b4ca80570a289f1b6e81cacce911e4f3b128d4ba75a5ae6eed9675a55e`，`PRAGMA integrity_check` 为 ok。
