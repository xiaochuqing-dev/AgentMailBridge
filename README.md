# AgentMailBridge

AgentMailBridge v1.0.0 是面向个人用户的本地优先 Windows 邮箱桥接工具。它通过 Gmail 收取邮件附件，通过 QQ SMTP 将文件发送到固定 Gmail，并向 Codex、Claude Code 等本地 Agent 提供受控的 stdio MCP 提交能力。

项目不提供多租户、任意收件人、通用 Gmail MCP、遥测或云同步。邮箱凭据、OAuth、数据库、邮件附件和归档由用户保留在本机。

## 正式版界面

- 顶部主工作区只有“收件”和“发件”。
- 左侧已有 Gmail、QQ 账号卡片用于管理当前账号；“添加邮箱账号”仅展示未来扩展说明，不新增第二个同类型账号。
- 左侧底部只有“历史记录”“文件与数据”“设置”“关于”。
- Agent / MCP 位于发件页；高级设置是“设置 > 高级设置”的二级页面。
- 历史记录管理收件、发件和 Agent / MCP 业务行为；文件与数据管理实际文件、存储概览、备份、恢复和一致性扫描。
- 今日文件、最近日志和最近发送记录采用内部滚动，不使用分页。

## Windows 安装

运行 `AgentMailBridge-1.0.0-Setup.exe`。默认安装到 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需 Python、Git 或管理员权限。桌面和开始菜单只指向 `AgentMailBridge.exe`；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式、托盘或开机启动项。

安装版数据位置：

- 配置：`%LOCALAPPDATA%\AgentMailBridge\Config\.env`
- OAuth：`%LOCALAPPDATA%\AgentMailBridge\OAuth`
- 数据、SQLite、日志和归档：`%LOCALAPPDATA%\AgentMailBridge\Data`
- 缓存：`%LOCALAPPDATA%\AgentMailBridge\Cache`

覆盖升级和普通卸载不会静默删除配置、OAuth、Credential Manager 凭据或用户数据。

## 邮箱配置

Gmail API 与 Gmail IMAP 使用互斥的条件配置页。Gmail API 页负责选择并验证 `credentials.json`、导入受控 OAuth 目录、授权和连接测试；Gmail IMAP 页只管理 Google 生成的应用专用密码。QQ 账号页管理 QQ 地址、SMTP 授权码和连接测试。

Gmail IMAP 密码和 QQ SMTP 授权码保存在 Windows Credential Manager。界面只显示固定掩码和配置状态，不回显旧值。Gmail OAuth scope 固定为 `gmail.readonly`。

## Agent / MCP

在“发件 > Agent 发件 / MCP”复制 Codex、Claude Code 或通用 JSON 配置。MCP 按需启动，stdin 关闭后退出，只提供 `submit_result`：

```json
{
  "file_path": "C:\\允许目录\\report.md",
  "title": "可选标题",
  "request_id": "stable-request-001"
}
```

收件人固定为 `OWNER_GMAIL`，文件必须位于 `DATA_ROOT` 或 `ALLOWED_SEND_ROOTS`。Agent 不能读取凭据、OAuth、邮件正文、任意扫描目录或修改邮箱配置。

## 开发与构建

需要 Python 3.11 或更高版本：

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python -m agent_mail_bridge --version
python -m agent_mail_bridge.gui
python -m agent_mail_bridge.mcp_server
```

Windows 构建：

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

流程会清理旧构建、运行 pytest、构建 GUI 与内部 MCP、执行 packaged smoke 和秘密排除扫描，并生成：

- `release\AgentMailBridge-1.0.0-Setup.exe`
- `release\AgentMailBridge-1.0.0-Windows-x64.zip`
- `release\checksums.sha256`

详细说明见 `docs/GUI使用说明.md`、`docs/MCP使用说明.md`、`docs/安全与诊断说明.md`、`docs/Windows安装与升级说明.md` 和最终发布验收报告。
