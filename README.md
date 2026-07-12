# AgentMailBridge

AgentMailBridge 是面向个人用户的本地优先 Windows 邮箱桥接工具。用户在一个桌面程序中管理邮箱、OAuth、允许目录和本地数据；Codex、Claude Code 等 Agent 通过受控 stdio MCP，在任务完成后把结果文件发送到用户固定邮箱。

项目不依赖中心化云服务，不提供多租户、任意收件人、通用 Gmail MCP、遥测或云同步。用户自行掌握邮箱、凭据、OAuth 文件、数据库和邮件归档。

当前版本：0.9.0。当前发布结论为内部候选产物 CONDITIONALLY PASS；尚未在真正无 Python 的独立 Windows Sandbox/VM 中完成最终复核，因此暂不建议公开发布。

## 普通 Windows 用户

### 安装

运行 `AgentMailBridge-0.9.0-Setup.exe`。安装器默认按当前用户安装到：

`%LOCALAPPDATA%\Programs\AgentMailBridge`

不需要管理员权限、Python、Git 或源码目录。开始菜单只创建 AgentMailBridge 主程序入口；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式、托盘图标或开机启动项。

### 首次配置

1. 启动 AgentMailBridge。
2. 在首次向导点击“配置 Gmail 收件账号”，选择 Gmail API 或 Gmail IMAP；两种方式使用独立配置界面。
3. Gmail API 用户直接在界面选择 `credentials.json`，程序验证后自动导入受控 OAuth 目录，再完成浏览器授权和连接测试；无需手动复制 OAuth 文件。
4. Gmail IMAP 使用 Google 生成的应用专用密码；QQ SMTP 使用 QQ 邮箱生成的授权码。两类 secret 保存到 Windows Credential Manager，界面只显示固定掩码和“已配置”状态，不回显旧值。
5. 旧版用户可在首次向导点击“导入旧版 .env”；只导入用户明确选择的文件，秘密值写入并回读验证成功后才从旧文件清空。

主界面顶部只有“收件 / 发件 / 高级设置”。默认进入收件工作台；账号新增与修改统一从左上角“添加邮箱账号”或 Gmail、QQ 账号卡片进入。收件页不显示账号 secret，发件页不显示 QQ 认证配置，高级设置不再放账号级认证。

安装版默认持久化位置：

- 非敏感配置：`%LOCALAPPDATA%\AgentMailBridge\Config\.env`
- OAuth：`%LOCALAPPDATA%\AgentMailBridge\OAuth`
- 数据、SQLite、日志和归档：`%LOCALAPPDATA%\AgentMailBridge\Data`
- 缓存：`%LOCALAPPDATA%\AgentMailBridge\Cache`

这些目录与安装目录分离，覆盖升级不会替换。普通卸载默认保留配置、OAuth、Credential Manager 凭据和用户数据；卸载会删除程序、快捷方式、安装记录及失效的开机启动项。

### Agent MCP 配置

MCP 是内部按需启动接口。Agent 客户端需要时启动 `AgentMailBridgeMCP.exe`，stdio 会话结束后进程自动退出；用户不需要手动启动或停止它。

GUI 在“高级设置 → Agent 接口配置”中可复制 Codex、Claude Code 和通用 JSON 配置。安装版通用结构如下，实际绝对路径由 GUI 自动生成：

```json
{
  "mcpServers": {
    "agent-mail-bridge": {
      "command": "C:\\Users\\<用户>\\AppData\\Local\\Programs\\AgentMailBridge\\AgentMailBridgeMCP.exe",
      "args": []
    }
  }
}
```

MCP 只提供 `submit_result`，收件人固定为 `OWNER_GMAIL`，文件必须位于 `DATA_ROOT` 或 `ALLOWED_SEND_ROOTS`。Agent 不能读取邮箱凭据、OAuth 文件、任意扫描目录、修改邮箱设置或指定收件人。

典型使用方式：安装并配置一次后，直接告诉 Agent“任务完成后，把报告发到我的邮箱”。Agent 生成结果、按需启动内部 MCP、调用 `submit_result`，发送完成后 MCP 自动退出。

### 开机启动、升级和卸载

“高级设置”可选择 `AgentMailBridge.exe --background` 开机启动，默认关闭。MCP 从不开机常驻。

覆盖安装同一 AppId 即为升级。用户配置、OAuth、Credential Manager 凭据、DATA_ROOT、SQLite、received/send/sent 和 backups 均位于安装目录外。卸载默认不删除这些内容，重新安装后可继续识别。

## 开发者

需要 Python 3.11 或更高版本。

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python -m agent_mail_bridge --version
python -m agent_mail_bridge.gui
python -m agent_mail_bridge.mcp_server
```

源码模式默认读取仓库根目录 `.env`，相对路径基于该配置文件目录。测试通过 `AGENT_MAIL_BRIDGE_DISABLE_DOTENV=1`、临时 OAuth 路径和临时 DATA_ROOT 隔离真实用户环境。

显式迁移和 OAuth 导入：

```powershell
python -m agent_mail_bridge import-config --from "D:\旧目录\.env"
python -m agent_mail_bridge import-oauth --from "D:\下载\credentials.json"
python -m agent_mail_bridge migrate-credentials
```

### Windows 构建

安装构建依赖和 Inno Setup 6 后执行：

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

流程会清理旧 build/dist/release，运行测试，以 PyInstaller onedir 构建 GUI 和 MCP，执行 packaged smoke 与秘密排除扫描，生成 portable ZIP、Inno Setup 安装器及 SHA-256。

主要产物：

- `release\AgentMailBridge-0.9.0-Setup.exe`
- `release\AgentMailBridge-0.9.0-Windows-x64.zip`
- `release\checksums.sha256`

### 测试分层

- 普通回归：`python -m pytest -q`
- 10,000 条稳定性基准：`python -m agent_mail_bridge stability-benchmark --records 10000 --cycles 50 --output <文件>`
- 构建验证：`scripts\verify_build.ps1`
- packaged MCP mock-free 安全 smoke：`scripts\packaged_smoke.py`
- 真实发信 E2E：`scripts\packaged_real_send_e2e.py`，必须显式传入 `--confirm-real-send`
- 安装、升级、卸载和 Defender 属于发布验收，不放入普通 pytest。

当前自动化测试为 268 项。packaged MCP 已验证 initialize、notifications/initialized、ping、tools/list、submit_result、fixed recipient、path_not_allowed、request_id、duplicate、审计、stdio 纯净和 EOF 自动退出；真实 QQ SMTP 发送已验证 SMTP 接受、本地 sent 归档及 SHA-256 一致，但未宣称 Gmail 最终收到。

## 安全和当前限制

- Gmail API scope 必须且只能是 `gmail.readonly`。
- `.env`、credentials.json、token.json、Credential Manager 秘密、SQLite、日志、邮件和附件不会进入构建产物。
- 当前 EXE 和安装器未签名，可能触发 SmartScreen；不会绕过系统提示。
- 当前机器完成了安装版 Credential Manager、Gmail API 和 QQ SMTP 诊断，并完成 Windows 原生 Qt 后端 DPR 1.00/1.25/1.50 GUI 截图矩阵；尚缺真正独立无 Python Windows 环境、跨版本旧候选升级和托盘人工矩阵。
- LICENSE 为 MIT；第三方依赖说明见 `THIRD_PARTY_NOTICES.md`，公开发布前仍需作者确认最终依赖许可清单。

详细资料见 `docs/Windows安装与升级说明.md`、`docs/MCP使用说明.md`、`docs/安全与诊断说明.md` 和最终收口报告。
