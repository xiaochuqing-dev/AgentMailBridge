# Windows 安装、升级与卸载

运行 `AgentMailBridge-1.3.0-Setup.exe`。默认安装到 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需管理员权限。开始菜单和可选桌面快捷方式只指向 `AgentMailBridge.exe`；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式或开机启动项。

从 v1.2.1 升级到 v1.3.0 前，正常退出主窗口和托盘，再运行新版安装器覆盖安装。程序文件会替换，`%LOCALAPPDATA%\AgentMailBridge` 下的 `.env`、OAuth credentials/token、SQLite、邮件 package、raw.eml、附件、工作区和日志不会删除，Windows Credential Manager 中的 Gmail IMAP/QQ SMTP secret 也不由安装器清理。

首次 v1.3.0 启动会先为需要升级的 SQLite 创建在线备份，再在同一可回滚事务中增加联系人、outbound origin、provider 唯一约束和规则评估结构；随后从真实 raw.eml 或旧 Header 幂等回填 decoded 联系人，不改写 raw。没有显式 `RECEIVE_RULE_MODE` 的旧隐式 self-only 默认会原子写入 v2 迁移标记并切换为 `all_scanned`；显式 self_only/custom 保留。升级后应核对收件偏好、历史补扫入口、发件 To、复杂邮件详情和数据库 `quick_check`。

从 v1.2.0 升级到 v1.2.1 时，稳定 AppId 只替换程序文件并修复 Gmail OAuth 首次配置；配置、credentials.json、token.json、Credential Manager、SQLite、邮件归档、工作区和日志继续位于安装目录外。安装器清理旧安装目录 `_internal` 时不会触碰 `%LOCALAPPDATA%\AgentMailBridge` 用户目录。

首次 Gmail OAuth 必须使用 Desktop app JSON。本地回调监听 `127.0.0.1` 随机端口；企业代理、安全软件或防火墙需要允许浏览器访问本机回环。覆盖升级后，已有匹配 Client ID 且包含 refresh token 的 Token 应继续有效；凭据更换导致 Client ID 不匹配时会明确要求重新授权，不会反复刷新旧 Token。

从 v1.1.0 升级到 v1.2.0 前，正常退出主窗口和托盘，再运行新版安装器覆盖安装。稳定 AppId 只替换程序文件；配置、收件规则、OAuth、Credential Manager、DATA_ROOT、SQLite、received、send、sent、backups 和工作区授权位于安装目录外并保留。v1.2.0 不重建数据库，只增量新增统一 MCP 审计表和非敏感邮件读取配置；旧 `mcp_calls`、邮件、资源、发送记录和自动收件状态全部保留。

统一邮件归档升级新增 `mail_packages`、`mail_resources`、`trusted_domains` 和迁移元数据；邮件级发件升级新增 `outbound_messages`、`outbound_resources`、`outbound_links`，并给 `sent_files` 补充关联字段。首次需要任一迁移时，程序会先创建并校验 `before_mail_models` SQLite 在线备份，再幂等回填旧收件与旧发件记录。旧文件、旧业务记录和兼容查询不会删除；历史上没有保存的正文或 raw 不会伪造。重复启动不会重复创建同一旧发件邮件。

新邮件目录位于 `DATA_ROOT\received\mail\年\月\日\<package>`，目录内包含实际可用的 `raw.eml`、正文分层、附件/内嵌图片/下载目录和相对路径 `manifest.json`。安装目录仍只读，覆盖升级和卸载不会删除这些目录。首次 v1.2.0 启动会幂等创建 `mcp_audit_events`，邮件读取开关默认关闭；应检查自动收件状态、Agent/MCP 页面、日志概览和“文件与数据”一致性扫描结果。

从 Windows“已安装的应用”卸载时，程序、Qt、快捷方式、安装记录和失效开机启动值会删除；配置、OAuth、凭据和用户数据默认保留。重新安装后可继续识别。

v1.3.0 发布验收覆盖完整 pytest、clean build、主 EXE packaged self-test、七工具 MCP packaged smoke、复杂邮件读取与资源 Hash、历史补扫、联系人解码、秘密扫描、v1.2.1 覆盖安装和数据保留。覆盖安装后还要验证 GUI/MCP 版本、复杂邮件仍可读、数据库 quick_check、桌面/开始菜单快捷方式只指向 GUI，以及 portable ZIP 可独立启动。

MCP packaged smoke 还必须以 UTF-8 向 `AgentMailBridgeMCP.exe` 写入 initialize、tools/list、每个 tools/call、malformed JSON、未知 method 和 EOF；验证读取开关关闭/开启、中文正文、附件、prepare Hash 和兼容 submit_result。不得通过修改控制台 code page 或手工 Copy-Item 规避问题。安装后桌面快捷方式仍只能指向 `AgentMailBridge.exe`，MCP EXE 不创建快捷方式。

安装器和 EXE 未签名时可能触发 SmartScreen。公开发布仍需独立无 Python Windows 环境和最终第三方许可复核；不得因此自动创建 GitHub Release。
