# AgentMailBridge v1.2.1 Gmail OAuth 首次配置可靠性与 GUI 无阻塞紧急修复报告

## 结论

本次 Hotfix 已完成 OAuth 状态机、Qt 后台 Worker、可取消和有限时回环、严格 Desktop 凭据校验、Token 原子提交、Gmail Profile 与账号匹配、并发锁、退出清理、结构化错误、GUI 整改、自动化回归、源码真实 OAuth、clean build、覆盖安装和安装版现有 Token 连接验证。

已知功能缺陷 P0=0、P1=0。真实人工取消和真实人工超时未执行，只有自动化证据；安装版未重复一次完整浏览器 OAuth；本机 Defender 不可用；发布文件未签名。最终判定为 CONDITIONALLY PASS，不伪报这些未执行项。

## 基线与真实问题

1. 实际开始 HEAD：本地 master 与 origin/master 均为 `3f68311d646681dfd1e85448d0a736f7e1aeae82`。需求提示词描述的功能基线是 `67a6b96e4ca6e541da34bee3bc2c6187d0431d86`；实现以已同步且已包含瘦身提交的 `3f68311` 为真实起点。
2. 用户问题：系统浏览器能进入 Google 授权，但旧 GUI 在等待回调时进入 Windows“未响应”，且没有可靠取消、超时、阶段反馈或故障定位。
3. 原始根因：旧授权入口在 Qt GUI 主线程同步调用阻塞式本地回环流程；等待浏览器、Token 交换和 API 请求期间 Qt 事件循环不能继续处理。
4. 二次验收根因：早期测试夹具只禁止 dotenv 读取，没有重定向默认配置写入，GUI 测试曾把假测试账号写入真实源码配置。回调、Token 交换和 Profile 实际完成后，账号匹配保护因此正确拒绝保存，用户看到的现象像“没有轮询到 Token”。现已同时隔离运行目录和配置写入路径，并加入回归。

## 状态机、线程与回环

5. 状态机：实现 IDLE、VALIDATING_CREDENTIALS、PREPARING_CALLBACK、CALLBACK_READY、OPENING_BROWSER、WAITING_FOR_USER、CALLBACK_RECEIVED、EXCHANGING_TOKEN、VERIFYING_GMAIL、AUTHORIZED、AUTHORIZED_UNVERIFIED、CANCELLING、CANCELLED、TIMED_OUT、FAILED，并拒绝非法跳转。
6. Worker 架构：OAuth 使用 QObject + QThread；Gmail API、IMAP 和 QQ 连接测试使用后台任务。Worker 不直接访问 QWidget，UI 只通过 Signal/Slot 更新。
7. GUI 主线程证据：专项测试以 QTimer heartbeat 覆盖 OAuth 等待和 Gmail API 慢请求，证明事件循环持续运行；源码中未使用 QApplication.processEvents 伪装异步。
8. 会话隔离：每次授权有唯一 session_id；旧会话进度和完成信号不能更新新会话。重复点击、授权中连接测试和两个窗口由同进程活动会话门禁阻止。
9. 本地回环：受控 WSGI 服务只绑定 `127.0.0.1` 和系统随机端口，绑定成功后才生成 redirect URI 和打开浏览器；不依赖 localhost DNS 或 IPv6 优先级。
10. 超时：默认 300 秒，等待截止时间使用单调时钟；自动化覆盖到达 TIMED_OUT、服务器关闭、端口释放和可重试。
11. 取消：使用线程事件和内部唤醒请求中断 handle_request，不使用 QThread.terminate 或强杀线程；自动化覆盖取消、立即重试和端口释放。
12. 回调校验：只接受预期路径和单值参数，使用常量时间比较 state；缺 code、错误回调、重复参数和 state mismatch 均有稳定错误码。
13. 生命周期：成功、拒绝、失败、取消、超时、窗口关闭和应用退出都进入 finally 清理服务器、授权 URL、state、锁和线程引用。连续 20 次回环测试未发现端口残留。
14. 浏览器体验：自动打开失败时会话继续，用户可重新打开同一 URL、复制仅存于内存的授权链接或取消；不使用 OOB 验证码流程，也不自动操作 Google 页面。
15. 代理诊断：OAuth 子流程增量合并 `127.0.0.1`、localhost 和 `::1` 回环绕过项，结束后恢复原环境；不清空代理，也不记录可能含凭据的代理 URL。

## 凭据、Token 与 Gmail 验证

16. Desktop 凭据：只接受顶层唯一 installed 节点，严格校验 UTF-8、大小、必需字段、Client ID 格式、Google HTTPS 端点和本地回环 redirect URI。
17. Web 凭据：Web application、installed/web 混合、字段缺失、非法类型、远程 HTTP 端点和非回环 redirect URI均明确拒绝。
18. 凭据保存：先验证，再用同目录唯一临时文件、flush、fsync 和 os.replace 原子替换；进程锁保护并发更新，失败保留旧文件。
19. Token 原子性：state 校验、最小 scope、Client ID、refresh token 和账号条件满足后才提交；同样使用落盘后原子替换。失败授权、账号不匹配和保存失败不会提前删除旧 Token。
20. Scope：始终且仅使用 `https://www.googleapis.com/auth/gmail.readonly`，未增加发送、修改或完整邮箱权限。
21. refresh token：显式授权以取得长期 refresh token 为门禁；缺失时返回 refresh_token_missing。刷新、Gmail Profile 和连接测试均不在 GUI 线程执行。
22. Gmail Profile：Token 交换后读取 `users.getProfile(userId=me)`，验证 API、scope 和账号。网络、代理、API 未启用或临时服务错误可进入 AUTHORIZED_UNVERIFIED。
23. AUTHORIZED_UNVERIFIED：安全保留已取得 Token，并允许直接“重新验证 Gmail API”，不会再次打开浏览器。
24. 账号不匹配：预期账号在会话开始时取稳定快照；实际账号只以脱敏形式提示，不静默改绑，不保存候选 Token。
25. 并发：同进程活动会话门禁和系统级文件锁共同保护 Token；锁有有限等待，进程退出后由操作系统释放。
26. 错误模型：覆盖 credentials、browser、callback、timeout、cancel、state、Token、refresh、network、proxy、API、scope、profile、account 和 lock 等稳定 error_code，并提供原因、下一步、可重试性和脱敏技术详情。
27. 隐私：日志只记录阶段、错误码、耗时和非敏感回环事实，不记录授权 URL、query、state、code、Client Secret、Token、完整回调或真实账号。

## GUI 与视觉验收

28. Gmail API 页面保留现有设计，只增加凭据类型、目标账号摘要、阶段、轻量进度、取消、重开浏览器、复制链接、重新验证、清除 Token 和连接测试。
29. 按用户最后反馈移除页面滚动条，默认窗口调整为 760×740、最小 680×700；打开即能看到底部三个常用操作。清除 Token 位于授权状态右侧，底部操作保持单行且不重叠。
30. 亮色、深色以及 100%、125%、150% 六组截图几何检查均为 overlap=False、clipped=[]、outside=[]。截图保存在忽略目录，未进入 Git 或发布产物。

## 自动化与真实验收

31. 完整 pytest：479 passed，耗时 1274.26 秒。OAuth/Gmail 专项：146 passed，耗时 8.87 秒。恢复正式构建解释器依赖后又执行 OAuth、Gmail、GUI 和 Windows 打包聚焦回归：163 passed，耗时 8.84 秒。测试隔离相关 47 项通过；完整和聚焦测试前后真实源码配置文件均保持一致。
32. 其他源码验证：compileall 通过；git diff --check 通过，仅有既有 LF/CRLF 提示；Gmail API 真实诊断通过；只读收件 smoke 使用 gmail_api、limit 1，扫描 0、保存 0、错误 0、退出码 0。
33. 人工 OAuth E2E：由用户亲自在系统浏览器完成 Google 账号选择和同意。源码 GUI 实际收到回调，完成 Token 交换、Gmail Profile、账号匹配和“测试 API 连接”；用户随后明确反馈成功。独立真实诊断继续复用该 Token，证明 Token 持久化可用。
34. 人工取消/超时 E2E：未执行。取消、超时、端口释放、重新授权、浏览器重开和复制链接均有自动化证据，但本报告不把它们写成人工 E2E。

## 文档、安全、构建与安装

35. 文档：README、CHANGELOG、AGENTS、GUI、MCP、安全诊断、Windows 安装升级、Gmail OAuth 配置故障排查和本报告已同步。测试写入隔离事故及门禁也已记录。
36. 敏感扫描：开始时 Git 跟踪文件 137 个，本次只新增 OAuth 状态机、OAuth 专项测试、配置排查文档和最终报告 4 个任务文件；禁止跟踪运行文件 0。项目正式 scanner 对 dist/release 的 265 个文件通过。补充扫描以 7 个真实账号/OAuth 标记在内存中匹配，源码文本、dist/release 和 ZIP 成员均为 0 命中；`.env`、credentials、Token、SQLite、日志和 QA 图片均未进入 Git 或产物。
37. clean build：首次尝试由 discovery 门禁发现构建解释器缺少正式依赖，未生成错误 release。按 requirements-build 安装声明依赖并通过 pip check 后，从头清理并重建成功；PyInstaller 6.21.0、GUI self-test、MCP smoke、Inno Setup 6.7.3、ZIP、checksums 和秘密扫描退出码均为 0。产物只包含一份 `gmail.v1.json`。
38. 发布产物：`AgentMailBridge.exe`、`AgentMailBridgeMCP.exe`、`AgentMailBridge-1.2.1-Setup.exe`、`AgentMailBridge-1.2.1-Windows-x64.zip` 和 `checksums.sha256` 均已生成，GUI、MCP 和 installer 版本均为 1.2.1。
39. SHA-256：installer 为 `b4e5e2362b36a0da5f2d85ee133bc350e5312ba383ee14741cf40103922519e0`；ZIP 为 `9e833f75ef80917b7bcd29b4c40a6952bb4ede48f7ae3205e5337191345a94dd`；与 checksums.sha256 逐项一致。
40. 覆盖安装：installer 静默覆盖退出码 0。安装版和源码人工验收所用配置、credentials、Token 以及 SQLite 的存在性、长度和 SHA-256 均保持一致，用户目录文件数量不变。安装目录 GUI/MCP Hash 与 dist 一致。
41. 安装版验证：GUI packaged self-test 和 MCP smoke 通过；安装版 1.2.1 使用本次真实有效 Token 通过 Gmail API 无界面诊断，正式 Token 未变化。桌面和开始菜单快捷方式均指向 1.2.1 GUI，MCP 快捷方式数量为 0。
42. 安装版浏览器 OAuth：未重复执行。源码真实浏览器 OAuth 已由用户通过，安装版验证覆盖二进制、Google discovery、凭据后端、MCP 和现有 Token Gmail API 连接，但不将其写成安装版完整浏览器授权 E2E。
43. Defender：本机没有 Defender PowerShell cmdlet、MpCmdRun 或已登记的杀毒产品，无法执行扫描；状态记为 UNAVAILABLE。
44. Authenticode：GUI EXE、MCP EXE 和 installer 均为 NotSigned。公开分发前仍建议使用可信代码签名证书。
45. Git：本报告完成后创建清晰本地提交；按用户最终指令不 push、不 force push、不创建 GitHub Release。origin/master 继续停在 `3f68311`，另一台电脑只有在用户后续明确允许推送后才能拉到本提交。

## 剩余风险与判定

46. 功能 P0=0、功能 P1=0。没有已知会阻止 Desktop 凭据、OAuth 回调、Token 保存或 Gmail API 连接的代码缺陷。
47. 验证证据缺口 P1=1：真实人工取消/超时没有执行。自动化覆盖充分，但不等价于人工 Windows E2E。
48. 发布 P2=2：Defender 在本机不可用；Authenticode 未签名。另有安装版完整浏览器 OAuth 未重复执行的证据边界，安装版现有 Token 连接已通过。
49. 最终判定：CONDITIONALLY PASS。v1.2.1 Hotfix 已具备实际使用和本地安装条件；若要判定完整 PASS，应补一次人工取消或人工短超时、一次安装版完整浏览器 OAuth，并在具备 Defender 的环境扫描；公开发布还应完成代码签名。
