# AgentMailBridge 阶段 3 至 5 完整闭环实现报告

## 执行摘要

阶段 3、阶段 4、阶段 5 均通过门禁。项目现已具备真实 Gmail API 收件、QQ SMTP 回传、受控本机 MCP、Windows 托盘常驻和可恢复的日常运行基础。

## 开始前状态

开始前已完成 IMAP、Gmail API、QQ SMTP、SQLite、ApplicationService 和 PySide6 第二批次界面。MCP、托盘、通知、开机启动、首次向导、单实例和安全退出尚未完成。

开发前存档提交为 613c149。真实数据库已使用 SQLite 在线备份接口备份，路径为 AgentMailBridgeData/backups/agent_mail_bridge_AMB-20260710-222514_before_stage3.db，大小 57344 字节，源库和备份库 integrity_check 均为 ok。

## 阶段 3 实现与真实验收

修复了 Gmail API 客户端未应用网络超时、配置写入失败后运行时状态不回滚、PySide6 QRunnable 完成信号可能丢失、完成回调可能跨线程更新界面，以及高 DPI 下右侧状态文本截断的问题。

真实 Gmail API 诊断通过。真实收取第一次保存 1 封可信自发自收邮件，第二次收取产生重复记录而未重复保存。正文可用 UTF-8 读取、保存路径在 DATA_ROOT 内、SQLite WAL 和旧记录均保持完整。

QQ SMTP 诊断和真实发件通过。真实 Gmail 收到回传附件，Gmail API 下载附件的 SHA-256 与源文件一致；sent 归档、SQLite 状态 sent、相同 request_id 返回 duplicate 均已验证。

IMAP 直连和 SOCKS5 均在 Gmail 993 TLS 握手阶段被当前网络中断。该结果归为环境端口限制，不是代码或凭据错误；Gmail API HTTPS 收件已作为可用后端通过。

真实 Windows GUI 验证了 100%、125%、150% 缩放、中文字体、页面切换、脱敏密码框、手动收件、自动任务互斥、失败提示和恢复。自动收件在真实 IMAP 网络失败后显示具体原因，切回 Gmail API 后恢复。

阶段 3 检查点：bb0b0cd，标签 v0.3.0-real-chain-validated。

## 阶段 4 MCP 设计与实现

新增 agent_mail_bridge.mcp_server。本实现使用本机 stdio、逐行 UTF-8 JSON-RPC，支持 initialize、notifications/initialized、ping、tools/list 和 tools/call。唯一工具为 submit_result(file_path, title=None, request_id=None)。

MCP 始终调用 ApplicationService.submit_result，复用 QQ SMTP、SQLite、sent 归档和 request_id 幂等。新增 mcp_calls 审计表，记录成功、重复、路径拒绝、输入拒绝和频率限制。

安全边界：工具 schema 没有收件人参数；只允许 DATA_ROOT 或 ALLOWED_SEND_ROOTS；不能读取或修改凭据和邮箱配置；不监听网络端口；每个 stdio 进程每 60 秒最多 5 次提交。

真实 Codex Agent 已调用 submit_result，返回 success。随后 Gmail 实际收到邮件，附件哈希一致，sent 归档存在，SQLite 状态为 sent。相同 request_id 的真实 stdio 复验返回 duplicate，DATA_ROOT 外路径返回 path_not_allowed，GUI Agent 接口页可显示审计记录。

MCP 文档位于 docs/MCP使用说明.md，包含 Codex 和 Claude Code 命令、状态说明和安全边界。

阶段 4 检查点：e979a1e，标签 v0.4.0-mcp-loop-validated。

## 阶段 5 桌面常驻与稳定性

新增 Windows 桌面运行时模块。系统托盘支持隐藏、恢复、手动检查、刷新与正常退出；通知对新邮件和收件失败做限频提示，不对空检查提示。

自动收件改为单次定时器，避免休眠后堆积。失败按有限退避调度，OAuth 需要授权时暂停。窗口拒绝在退出期间启动新任务，等待当前任务安全完成后关闭定时器、线程池、数据库连接和托盘。

新增 QLockFile 单实例锁，按 DATA_ROOT 隔离。新增当前 Windows 用户 Run 项开关，默认关闭且可恢复。新增首次配置向导，在缺少 Gmail 地址时收集数据目录、收件方式和可选 QQ 身份，不明文显示授权码。

真实 Windows 验收：系统托盘可用；窗口可隐藏并恢复；同一数据目录第二实例被拒绝，释放后可再次获取锁；两次失败自动收件的第二次延迟大于第一次；开机启动可启用并清除。

阶段 5 检查点：本报告随阶段 5 代码提交，标签为 v0.5.0-daily-use-validated。

## 测试与质量

最终自动化结果：201 passed。编译检查通过，pip check 为 No broken requirements found。测试使用隔离 DATA_ROOT、OAuth 文件和环境变量，不读取真实密钥。

新增覆盖包括 MCP 生命周期、协议输出、受控提交、审计、路径拒绝、幂等、频率限制、GUI 后台任务回调和配置保存回滚。

日志轮转采用 2 MB 单文件、保留 5 个文件。扫描已跟踪和未忽略仓库文件，未发现 Gmail 地址、QQ 地址、应用专用密码或 QQ 授权码泄露。

## 修改文件

主要新增：agent_mail_bridge/mcp_server.py、agent_mail_bridge/desktop_runtime.py、agent_mail_bridge/ui/setup_wizard.py、docs/MCP使用说明.md、tests/test_mcp.py。

主要修改：ApplicationService、SQLite、邮件发送错误码、Gmail API 授权、PySide6 主窗口、GUI 启动入口、README、pyproject 和相关测试。

## 限制与后续观察

当前 IMAP 993 仍受本机网络限制，建议继续使用 Gmail API。未制作正式安装包。首次向导只提供最小配置，OAuth 授权和高级网络参数仍在主窗口完成。

需要用户长期观察：连续数日自动收件、Windows 休眠/唤醒、网络变化与 OAuth 失效后的真实恢复。系统托盘、单实例、开机启动和安全退出已通过短周期实机验证。

下一批建议：Windows 打包、干净机器安装验证、密钥环替代 .env，以及在 993 可用网络下补充 IMAP 真实附件验收。

## 最终判断

项目已达到单用户 Windows 日常可用状态：真实 Gmail API 收件和 QQ SMTP 回传可用，本地 Agent 可经受控 MCP 回传，桌面常驻基础能力、自动收件保护、审计和安全退出均已实现。长期运行结论仍需真实日常环境持续观察。

参考：MCP stdio、生命周期和 tools 规范来自 https://modelcontextprotocol.io/specification/2025-11-25/basic/transports、https://modelcontextprotocol.io/specification/2025-03-26/basic/lifecycle、https://modelcontextprotocol.io/specification/2025-11-25/server/tools 。
