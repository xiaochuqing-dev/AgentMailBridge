# Windows 安装、升级与卸载

运行 `AgentMailBridge-1.2.1-Setup.exe`。默认安装到 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需管理员权限。开始菜单和可选桌面快捷方式只指向 `AgentMailBridge.exe`；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式或开机启动项。

从 v1.2.0 升级到 v1.2.1 前，正常退出主窗口和托盘，再运行新版安装器覆盖安装。v1.2.1 只替换程序文件并修复 Gmail OAuth 首次配置；配置、credentials.json、token.json、Credential Manager、SQLite、邮件归档、工作区和日志继续位于安装目录外。安装器清理旧安装目录 `_internal` 时不会触碰 `%LOCALAPPDATA%\AgentMailBridge` 用户目录。

首次 Gmail OAuth 必须使用 Desktop app JSON。本地回调监听 `127.0.0.1` 随机端口；企业代理、安全软件或防火墙需要允许浏览器访问本机回环。覆盖升级后，已有匹配 Client ID 且包含 refresh token 的 Token 应继续有效；凭据更换导致 Client ID 不匹配时会明确要求重新授权，不会反复刷新旧 Token。

从 v1.1.0 升级到 v1.2.0 前，正常退出主窗口和托盘，再运行新版安装器覆盖安装。稳定 AppId 只替换程序文件；配置、收件规则、OAuth、Credential Manager、DATA_ROOT、SQLite、received、send、sent、backups 和工作区授权位于安装目录外并保留。v1.2.0 不重建数据库，只增量新增统一 MCP 审计表和非敏感邮件读取配置；旧 `mcp_calls`、邮件、资源、发送记录和自动收件状态全部保留。

统一邮件归档升级新增 `mail_packages`、`mail_resources`、`trusted_domains` 和迁移元数据；邮件级发件升级新增 `outbound_messages`、`outbound_resources`、`outbound_links`，并给 `sent_files` 补充关联字段。首次需要任一迁移时，程序会先创建并校验 `before_mail_models` SQLite 在线备份，再幂等回填旧收件与旧发件记录。旧文件、旧业务记录和兼容查询不会删除；历史上没有保存的正文或 raw 不会伪造。重复启动不会重复创建同一旧发件邮件。

新邮件目录位于 `DATA_ROOT\received\mail\年\月\日\<package>`，目录内包含实际可用的 `raw.eml`、正文分层、附件/内嵌图片/下载目录和相对路径 `manifest.json`。安装目录仍只读，覆盖升级和卸载不会删除这些目录。首次 v1.2.0 启动会幂等创建 `mcp_audit_events`，邮件读取开关默认关闭；应检查自动收件状态、Agent/MCP 页面、日志概览和“文件与数据”一致性扫描结果。

从 Windows“已安装的应用”卸载时，程序、Qt、快捷方式、安装记录和失效开机启动值会删除；配置、OAuth、凭据和用户数据默认保留。重新安装后可继续识别。

v1.2.0 发布验收必须覆盖：完整 pytest、clean build、主 EXE packaged self-test、七工具 MCP packaged smoke、真实 Agent 读取、事实搜索、文本/CSV/图片/文档资源、工作区准备、Hash 闭环、秘密扫描、v1.1.0 覆盖安装、数据保留、桌面快捷方式目标、快捷方式实际启动、版本、浅色/深色整行表格、100%/125%/150% DPI、单实例、托盘、历史记录、文件与数据、设置和独立 Agent/MCP 页面。

MCP packaged smoke 还必须以 UTF-8 向 `AgentMailBridgeMCP.exe` 写入 initialize、tools/list、每个 tools/call、malformed JSON、未知 method 和 EOF；验证读取开关关闭/开启、中文正文、附件、prepare Hash 和兼容 submit_result。不得通过修改控制台 code page 或手工 Copy-Item 规避问题。安装后桌面快捷方式仍只能指向 `AgentMailBridge.exe`，MCP EXE 不创建快捷方式。

安装器和 EXE 未签名时可能触发 SmartScreen。公开发布仍需独立无 Python Windows 环境和最终第三方许可复核；不得因此自动创建 GitHub Release。
