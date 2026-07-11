# AgentMailBridge GUI 使用说明

AgentMailBridge 0.9.0 的普通用户入口只有 `AgentMailBridge.exe`。首次启动完成 Gmail、固定收件人、数据目录及可选 QQ 发件配置；秘密值进入 Windows Credential Manager，输入框不会回显旧值。

主要页面：

- 仪表盘：账号、OAuth、自动收件、最近状态和快捷操作。
- 收邮件：Gmail API/IMAP 收取和历史记录。
- 发邮件：用户明确选择的全局文件；该能力不会扩大 MCP 白名单。
- 高级设置：QQ 授权码、网络、开机启动、OAuth JSON 导入、显式授权和诊断。
- Agent 接口：内部 MCP 安装状态、固定收件人、允许目录、调用记录及 Codex/Claude/通用 JSON 配置复制。
- 数据维护：数据库状态、备份、一致性检查和脱敏报告。

安装版点击“导入 OAuth JSON”会验证 Desktop OAuth 文件并复制到当前用户受控 OAuth 目录；替换必须明确确认，现有 token 不会静默删除。首次向导的“导入旧版 .env”只处理用户主动选择的文件。

关闭主窗口默认进入托盘；托盘可恢复或正常退出。开机启动默认关闭，启用后使用 `AgentMailBridge.exe --background`。内部 MCP 不显示 GUI、托盘或开机启动项。

错误统一显示操作状态和脱敏信息。日志、诊断报告及错误详情不得出现邮箱密码、QQ 授权码、token 内容或不必要的私人绝对路径。
