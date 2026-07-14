# AgentMailBridge v1.0.0 核心链路可靠性与自动收件稳定性专项整改报告

日期：2026-07-14

## 1. 开始前最新 master 状态

开始前执行了 `git pull --ff-only origin master`、状态检查和最近 30 条提交检查。本地 master 与 origin/master 同为 `5639790`，工作树干净。整改前完整基线为 311 项测试通过。随后打开旧正式安装版并核对安装目录、桌面快捷方式、自动收件、Codex MCP 和 Claude Code MCP 状态。

## 2. 现有 MCP 架构审计

保留本机按需 stdio、固定 `OWNER_GMAIL`、只读 Gmail scope、allowed roots、request_id 幂等、速率限制、mcp_calls 审计、QQ SMTP 和发送归档。MCP 仍不监听网络端口，stdin 关闭后退出，不扩展为通用邮箱 MCP。

## 3. 真实 Agent 调用问题复现

旧审计记录和现有调用链表明，调用方需要自行确保编码、JSON-RPC 行格式以及文件已位于允许目录；旧 mcp_calls 还保留一次源仓库报告被 `path_not_allowed` 拒绝、随后人工放入允许目录才成功的事实。本轮使用正式安装版 MCP 和 Agent 生成的中文空格路径文件重新验收，不再执行手工 Copy-Item。

## 4. Windows UTF-8 根因

旧入口依赖 Python 文本流继承的 Windows 编码状态，未在进程边界显式固定 stdin/stdout/stderr；首条 BOM 也没有统一剥离。不同宿主、ACP/OEM code page 和 PowerShell 写入方式会造成中文或首条 JSON 解析不一致。

## 5. JSON-RPC 修复

启动时将 stdin、stdout、stderr 明确重配为 UTF-8；首条输入兼容 BOM；stdout 仅输出逐行 JSON-RPC，每条响应立即 flush；诊断留在 stderr/文件日志。malformed JSON、非法 method 和单请求异常返回结构化错误，EOF 正常结束进程。initialize、tools/list、tools/call、成功、失败、BOM、malformed、非法 method、stdout purity、flush 和 EOF 均由源码及 packaged smoke 覆盖。

## 6. staging 原设计问题

旧链路只验证调用方传入路径，Agent 往往需要先把结果文件搬到 Data/send，再调用 MCP。产品没有形成 source、受控副本、MIME 读取源和归档之间的完整字节证据。

## 7. 新受控 staging

`submit_result` 现在完成路径授权、普通文件/类型/大小检查、source size/Hash、请求级 staging 路径、临时文件原子复制和 os.replace、staged size/Hash 复核，再把经过验证的 staging 文件交给发送链路。失败原因和阶段状态写入 mcp_calls。

## 8. 路径安全边界

MCP 仍只能读取 `DATA_ROOT`、`ALLOWED_SEND_ROOTS` 和已有明确批准范围；GUI 选择过的全局文件不会扩大 MCP 信任。测试覆盖允许源、越界源、源消失、复制失败和 Hash 不一致。没有引入全盘读取、任意收件人或网络服务。

## 9. source / staged / archive Hash

发送链路记录并校验 source、staged、attachment_pre_smtp 和 sent_archive 的 SHA-256 与字节数。MIME 只读取已验证的 send 副本，SMTP 成功后归档并重新计算 Hash；任一发送前不一致会阻止发信。

## 10. MCP 返回结构

成功结果包含 status、request_id、filename、size_bytes、source_sha256、staged_sha256、attachment_pre_smtp_sha256、sent_archive_sha256 和 send_status。duplicate 作为幂等成功处理，不再写成 ERROR 事件。

## 11. 真实 loopback E2E

使用正式安装版 `AgentMailBridgeMCP.exe` 两次执行真实固定收件人发送。第一次在 GUI 可见状态下由自动收件回收；第二次在窗口关闭到托盘后回收。每次相同 request_id 的第二次调用均返回 duplicate，QQ SMTP 没有重复发送。没有点击“立即收取”。

## 12. source / received size 和 SHA-256

测试文件为 UTF-8 Markdown，包含中文、英文、多行、空格和特殊字符，大小 4895 字节。source size 与 received size 均为 4895；两端 SHA-256 均为 `27d79ed9710327ce037476c99e354f1cc67a7f1c8c38a893d0504f29ad83fc90`。真实 Gmail loopback 字节一致。

## 13. 自动收件旧架构

旧实现主要依赖 GUI 单次 QTimer 和内存失败计数，启动可能等待完整周期；窗口、长暂停、网络恢复和旧失败项之间缺少统一持久状态。旧数据库日志显示同一 Gmail 资源的坏附件 `123.t t` 在多轮中反复失败。

## 14. 自动收件漏检根因

固定最近 N 条、单页结果、缺少重叠回看、启动延迟检查以及长时间事件循环暂停都可能形成漏检窗口。单项异常被放大为整轮失败时，还会错误影响后续调度。

## 15. scheduler 新状态机

新增 auto_receive_state，持久化 enabled、interval、last_check_at、last_success_at、last_result、last_error、consecutive_global_failures、next_check_at 和 checkpoint。成功、no_changes、partial 与全局失败分别驱动正常周期或退避周期。

## 16. 启动立即检查

自动收件已启用时，正式 GUI 启动约 3 秒后检查。正式安装版数据库记录验证了启动时间与首轮检查时间差约 3 秒。

## 17. 周期检查

默认周期 60 秒，最低 30 秒，可选 1、3、5、10 分钟。手动和自动共用 ApplicationService.receive、规则、去重、保存和错误语义，并由同一任务锁防止并发。

## 18. overlapping lookback

Gmail API 每轮加入约 30 分钟 after 时间窗并完整处理 page token，直到安全 scan cap；IMAP 使用 locale-independent SINCE 和 BODY.PEEK。重复扫描依赖 Message-ID/唯一约束，不重复保存。

## 19. checkpoint

每次健康完成后持久化 last_check、last_success 和 checkpoint；应用重启从 SQLite 恢复，不只依赖内存。由于重叠回看与去重为主要防漏机制，checkpoint 不作为会导致永久漏信的硬切断边界。

## 20. sleep / network recovery

15 秒 watchdog 检测 next_check_at 已逾期后立即补偿。真实安装版将 next_check 模拟为超期 10 分钟，18 秒内完成补偿检查。网络验收使用仅对测试进程生效的断路代理：首轮进入 30 秒退避，代理恢复后同一 GUI 进程下一轮成功，失败计数归零并恢复 60 秒周期。未执行会中断当前会话的实体睡眠。

## 21. poison message

使用用户邮箱中真实既有坏附件 `123.t t` 验收。它每次仍产生附件下载失败，但每轮状态为 partial，不触发全局连接退避；终态后的下一轮为 no_changes、failed=0，正常轮询不再被污染。

## 22. retry state

receive_retries 持久化 backend、resource_id、message_id、attachment_id、retry_count、last_error、last_attempt_at、next_retry_at 和 terminal_status。真实坏附件依次达到第 2、3、4、5 次，最终 retry_count=5、terminal_status=needs_attention。到期项即使离开 30 分钟普通回看窗口，也会按 Gmail 资源 ID 或 IMAP UID 单独重试。

## 23. global backoff

连接级失败采用 30 秒、60 秒、120 秒、300 秒、最长 900 秒退避；成功、no_changes 或 partial 会恢复正常周期。OAuth、网络和后端整体错误不写入单邮件重试表。

## 24. no_changes 语义

无新邮件、全部重复和全部不匹配均保持健康 no_changes，不增加错误统计、不写 ERROR、不触发退避、不停止 scheduler。partial 保留成功工作，只作为警告。

## 25. 自动收件可观察性

收件页展示正常运行/正在检查/连接退避、上次检查、上次成功、下次检查、最近结果、待重试和需要处理数量。状态来自 SQLite 与真实 scheduler；托盘运行时继续更新。

## 26. 窗口最大化 / 还原

标题栏使用统一线性最小化、最大化、还原和关闭图标。真实鼠标点击最大化与还原、双击标题栏切换、关闭到托盘及托盘恢复均通过。normal geometry 被限制在当前屏幕工作区。根据用户复核，收件页删除重复 Gmail 管理卡、扩大中央区，正常窗口无外层滚动，文件表和最近日志不重叠；用户已确认最终截图。

## 27. 自动化测试

最终完整测试为 325 项通过，耗时 353.41 秒。新增覆盖 UTF-8/BOM/flush/EOF、受控 staging、Hash mismatch、分页回看、到期资源脱离回看窗口重试、IMAP UID、poison 隔离、状态持久化、scheduler/backoff/watchdog、托盘、并发锁、最大化/还原和 UI 几何。

## 28. 真实 Windows 验收

正式安装版 GUI/MCP、真实 QQ SMTP、Gmail API 自动回收、托盘回收、网络断开恢复、长暂停补偿、坏附件终态、单实例和真实窗口控制均完成。当前 Windows 为 150% DPI，浅色/深色截图通过并获用户确认；100%/125%/150% 另由 Qt 回归覆盖。当前只有一台显示器，未伪造多显示器验收。

## 29. 文档更新

已更新 README、CHANGELOG、AGENTS、MCP 使用说明、GUI 使用说明、安全与诊断说明、Windows 安装与升级说明以及本报告。文档同步说明 UTF-8、staging、Hash、scheduler、回看、重试、退避、托盘、可观察状态和窗口控制。

## 30. 敏感信息扫描

`scripts/secret_scan.py` 扫描 864 个文件，0 个配置秘密标记；git diff/check、tracked files、构建产物和忽略目录另行审计。真实邮箱配置、OAuth、SQLite、邮件、附件、日志、E2E 文件和 QA 截图均留在用户目录或 gitignore 路径，不进入报告和提交。Defender 对 release 和 dist 的范围内检测为 0；系统历史检测数不作为本项目检测结果。

## 31. Git commit / push

实现提交 `e713f766f2302226a1bae30767865174b16eec72` 已正常 push 到 `origin/master`。本段状态通过后续文档提交补记；未 force push、未改写历史、未创建 GitHub Release。

## 32. clean build

最终代码执行 clean build，构建 GUI EXE、MCP EXE、portable ZIP 和 Inno Setup installer。packaged GUI self-test、packaged MCP UTF-8 smoke、secret scan 和 build verification 均通过。最终全量测试在相同代码上另行执行并通过。

## 33. 本机安装

退出旧 GUI/托盘进程后，使用新 installer 覆盖正式当前用户安装。配置、Credential Manager、OAuth、SQLite、received、sent 和 backups 均保留；数据库 integrity_check=ok。正式 GUI 与 MCP 产品版本均为 1.0.0。

## 34. 桌面快捷方式

桌面快捷方式指向 `%LOCALAPPDATA%\Programs\AgentMailBridge\AgentMailBridge.exe`，无参数，工作目录为正式安装目录；不指向 Python、源码或 dist。实际启动最新构建，单实例、正常窗口、自动收件、托盘、最大化/还原均通过。MCP 不存在桌面快捷方式。

## 35. installer / ZIP 路径和 SHA-256

`release/AgentMailBridge-1.0.0-Setup.exe`，45986769 字节，SHA-256：`93798383e23c332dca225a6c47afeda279f0d348699e0a8062967129828b4c3f`。

`release/AgentMailBridge-1.0.0-Windows-x64.zip`，79092140 字节，SHA-256：`57513027e0a02e0d6a224479528caca7915cf7c47fc1a1a6df10b85701069bbb`。

## 36. 剩余 P0 / P1 / P2

P0：无。

P1：无。

P2：当前机器只有一台显示器，未做实体多显示器拖动；没有执行会中断当前任务的真实 Windows 睡眠，只完成真实超期补偿；Claude Code 当前未配置 AgentMailBridge MCP，已验证通用 JSON 配置与 Codex 正式配置；构建产物未进行商业代码签名。

## 37. PASS / CONDITIONALLY PASS / FAIL

结论：CONDITIONALLY PASS。

核心链路、真实 Gmail loopback、字节 Hash、无人值守自动收件、托盘、网络恢复、长暂停补偿、真实坏附件隔离、最终测试、构建、安装和桌面快捷方式均通过。条件仅为当前环境无法完成的实体多显示器、真实系统睡眠和未配置 Claude Code 三项 P2 验收；不存在阻断使用的 P0/P1。
