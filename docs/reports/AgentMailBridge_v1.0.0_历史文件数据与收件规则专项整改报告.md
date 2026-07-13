# AgentMailBridge v1.0.0 历史文件数据与收件规则专项整改报告

## 结论

本轮专项结果为 PASS。账号卡片、今日文件、收件规则、历史记录、文件与数据、数据概览、测试、GUI QA、敏感信息扫描、GitHub 推送、Windows 构建、本机覆盖安装、桌面快捷方式、单实例与托盘均完成实际验收。未创建 GitHub Release。

## 开始前状态与最新 master 基线

- 执行 `git pull --ff-only origin master`，结果为已是最新。
- 开始前位于 `master`，工作区干净并跟踪 `origin/master`。
- 开始前最新提交为 `1f3bd43`，同时检查了最近 20 条提交。
- 完整 pytest 基线为 295 项通过，用时 235.19 秒。
- 实际打开旧安装版 GUI 复现：账号卡片标题与邮箱空间不足；今日文件仍有路径列；历史页平铺数据库字段；文件页错误拼装大小。
- 原安装位置为 `%LOCALAPPDATA%\Programs\AgentMailBridge\AgentMailBridge.exe`；桌面 `AgentMailBridge.lnk` 已指向该正式安装目录，不指向源码、Python 或旧 dist。

## 账号卡片问题与修复

Gmail 与 QQ 卡片改为标题、职责/邮箱、状态分层布局，状态标签不再与标题争抢空间；邮箱允许合理换行且完整保留。两张卡片统一尺寸和对齐，左栏适度加宽，没有通过缩小字体或省略号掩盖问题。Windows 100%、125%、150% DPI 均未发现标题、状态或邮箱裁切。

## 今日文件改造

今日文件主表最终为“文件名、大小、收取时间、操作”四列，删除保存路径显示列。路径仍保留在内部数据和 `UserRole` 中，继续支持搜索、双击安全预览、默认程序打开和复制完整路径。文件名列优先伸展并允许换行，行高按长文件名调整；时间只显示 `HH:mm:ss`。打开与复制路径按钮等高，操作容器使用垂直居中布局，三档 DPI 均未发现按钮偏下。

## 收件规则旧模型、新模型与迁移

旧模型只有 `AUTO_RECEIVE_ONLY_SELF_MAIL` 布尔值。新模型增加 `RECEIVE_RULE_MODE`、发件人/域名、主题关键词和仅含附件配置，支持：

- `self_only`：仅本人邮件。
- `all_scanned`：当前扫描范围内全部邮件。
- `custom`：发件人或域名、主题关键词、仅含附件。

自定义模式中不同分类使用 AND，同一分类内部使用 OR；发件人和域名去空格、去重、忽略大小写并执行基本格式校验；主题关键词去空格、去重并执行大小写无关包含匹配；未启用分类不参与过滤；空自定义规则禁止保存。

未设置新模式时，旧 `true` 无损映射为 `self_only`，旧 `false` 无损映射为 `all_scanned`。GUI 保存新配置时同步旧布尔键，不需要用户手工修改 `.env`。规则校验或配置写入失败时，不修改当前内存中的有效规则。

## API、IMAP、手动与自动统一规则

规则匹配位于 `process_normalized_mail` 共用业务处理入口，在去重和保存之前执行。Gmail API 与 Gmail IMAP 都先归一化为 `NormalizedMail` 再进入该入口；手动收件与自动收件都调用同一个 `ApplicationService.receive`，不存在后端或触发方式各自实现规则的分叉。

## 历史记录产品化

历史记录主表改为“类型、摘要、时间、状态、操作”，不再直接展示完整 request_id、绝对路径和原始数据库状态。收件摘要优先主题，发件摘要优先原始文件名或主题，Agent / MCP 摘要优先 title 或文件名；状态统一中文化。查看详情使用结构化弹窗，包含类型、摘要、完整时间、中文状态、原始状态、request_id、关联文件、完整路径、错误详情、source 和 backend。有关联文件时可定位，无关联文件时按钮禁用且页面不报错。

## 文件与数据 0 B 根因及修复

根因是旧页面从 `received_messages` 业务邮件记录拼装文件列表，并错误推导 `size_bytes`。本轮明确职责：

- `received_messages` 只负责“发生过什么”的业务历史。
- `received_files` 是真实收件文件、路径、大小、类型、MIME 和状态的权威来源。

新增统一受管文件查询和 DTO，通过 `ApplicationService.get_managed_files` 返回收件文件、已发送归档和 Agent 结果。UI 不再从 `history_rows` 拼装文件。

收件文件直接查询 `received_files` 并关联邮件主题与后端。发送归档来自 `sent_files`；旧记录缺少大小时，只对 `DATA_ROOT` 或 `ALLOWED_SEND_ROOTS` 中确认安全且存在的文件执行 `Path.stat().st_size`，不读取文件内容，不因单个文件缺失中断页面。Agent / MCP 记录按 request_id、路径和发送关联归一化，避免同一真实文件重复展示。

大小语义已区分：真实空文件显示 `0 B`，未知大小显示 `—`，文件不存在显示“文件已不存在”。主表不显示绝对路径，完整路径只进入结构化文件详情和复制路径操作。文件详情同时展示类型、来源、大小、时间、状态、存在性、MIME、request_id 和 SHA-256。

## 数据概览与维护

原单行概览升级为六个小型卡片：数据库状态、数据库大小、收件文件占用、已发送归档占用、Agent 结果占用和备份占用。备份统计新增真实总字节数。创建、验证、恢复、扫描、打开备份目录和维护报告继续复用原有安全实现，没有重写恢复与备份逻辑。

## 测试

- 专项开发阶段测试：29 项通过，后续相关测试 51 项通过。
- 最终完整 pytest：311 项通过，用时 327.83 秒。
- clean build 内再次完整 pytest：311 项通过，用时 342.92 秒。
- Python `compileall` 与 `git diff --check` 通过。
- 新增测试覆盖账号卡片、今日四列与按钮对齐、旧配置迁移、三种模式、发件人/域名/关键词/附件、AND/OR、空规则、API/IMAP、手动/自动、历史摘要与详情、中文状态、真实收件大小、旧发送 stat、MCP 去重、0 B/未知/不存在、文件详情和数据概览。

## GUI QA

真实 Windows Qt 后端完成 100%、125%、150% DPI，实际 DPR 分别为 1.0、1.25、1.5；同时检查深色主题。检查范围包含收件页、账号卡片、今日文件、收件规则编辑器及自定义展开、历史记录、历史详情、文件与数据、文件详情和数据概览。QA 截图位于 gitignored 的 `test-output/qa`，未进入 Git。

实际检查未发现标题或邮箱裁切、按钮偏下、生成省略号、主表无效截断路径、错误 0 B、原始英文状态污染、raw dict 详情、重复一级路由或明显 Demo 占位。

## 文档更新

已更新 `README.md`、`CHANGELOG.md`、`AGENTS.md`、`.env.example`、`docs/GUI使用说明.md`、`docs/安全与诊断说明.md` 和 `docs/Windows安装与升级说明.md`。AGENTS 已加入业务历史与真实文件职责分离、统一收件规则、旧配置兼容、路径展示、状态本地化及 0 B 语义不可破坏规则。

## 敏感信息扫描

提交前检查了 `git status`、`git diff`、`git diff --cached`、`git diff --check` 和 `git ls-files`。110 个跟踪文件中，禁止跟踪的 `.env`、credentials、token、SQLite、日志、QA 截图、build、dist 和 release 数量为 0；高风险 OAuth、私钥和邮箱密码模式命中为 0。`.env`、build、dist、release 和 test-output 均保持忽略。

构建后 `scripts/secret_scan.py` 检查 864 个 dist/release 文件，禁止路径和已配置秘密标记均未命中。Defender 已对 release 目录执行自定义扫描，检出 0 项。

## Git commit 与 push

功能、测试和文档提交为 `431a4917fef674bb86d10ba95301d79ea5805ad1`，已通过普通 `git push origin master` 推送。未 force push、未改写历史、未创建 GitHub Release。本报告完成后作为独立文档提交继续正常推送 master。

## Windows build

沿用现有构建体系完成 clean build，生成主 `AgentMailBridge.exe` 和内部 `AgentMailBridgeMCP.exe`。主程序 packaged self-test、MCP stdio smoke、构建文件排除检查、安装器和哈希生成均通过。

- 安装器：`release/AgentMailBridge-1.0.0-Setup.exe`
- 安装器 SHA-256：`e4ea150f13522e2c033e9d629361b8bc2eff965e8be9278cffe07a214d3a0e34`
- Portable ZIP：`release/AgentMailBridge-1.0.0-Windows-x64.zip`
- ZIP SHA-256：`062be72d358f8fffb1f0719c4a4f75c222f01974067869dafb30b82e6d5b1365`

主 EXE、内部 MCP EXE 和安装器的 Authenticode 状态均为 `NotSigned`，与现有安全文档一致。

## 本机安装与桌面快捷方式

退出旧安装版托盘进程后，使用新安装器静默覆盖 `%LOCALAPPDATA%\Programs\AgentMailBridge`，安装器退出码为 0。安装前后 14 个用户状态文件的数量、大小和修改时间摘要完全一致；配置、OAuth、数据库和现有 received 文件保持不变，空的 sent 与 backups 目录未被删除。安装后主程序和内部 MCP 均存在，版本为 1.0.0。

桌面快捷方式名称为 `AgentMailBridge.lnk`，目标为 `%LOCALAPPDATA%\Programs\AgentMailBridge\AgentMailBridge.exe`，工作目录为同一正式安装目录，使用主 EXE 自带图标；不指向 dist、Python 或源码。

实际通过桌面快捷方式启动后，逐页验证账号卡片、今日四列、时间、打开/复制路径、自定义规则、历史页与详情、真实文件大小、文件详情、数据概览。重复启动快捷方式始终只有一个正式进程；关闭主窗口后进程保留且窗口隐藏，再次启动快捷方式可从托盘恢复同一实例。

## 剩余 P0、P1、P2

- P0：无。
- P1：无。
- P2：当前产物未做商业代码签名；公开发布前仍建议在独立无 Python 的干净 Windows 环境执行最终安装/卸载复核和第三方许可复核。本轮未获授权也未创建 GitHub Release。

## 最终判定

PASS。本轮提示词要求的功能、兼容、安全、测试、GUI QA、文档、扫描、GitHub push、Windows 构建、本机安装和桌面快捷方式验收均已完成。
