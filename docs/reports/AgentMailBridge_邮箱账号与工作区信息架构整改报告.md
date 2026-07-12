# AgentMailBridge 邮箱账号与工作区信息架构整改报告

## 1. 执行摘要

本轮按照“账号配置归账号、收发工作归收发、应用设置归设置”完成专项整改。结论：PASS。

主界面顶部已收口为“收件 / 发件 / 高级设置”，默认进入收件。账号新增与编辑统一从左上角添加入口或 Gmail、QQ 账号卡片进入。Gmail API、Gmail IMAP、QQ SMTP 使用独立配置页，首次向导复用同一套账号组件和业务逻辑。

## 2. 开始前真实状态

- 分支：master。
- 基线提交：e4d1c6d。
- 与 origin/master 一致，开始时工作区干净。
- 基线完整测试：251 passed。
- 已确认底层具备 Runtime Paths、Credential Manager、OAuth 受控导入、Gmail API、IMAP、QQ SMTP、维护、诊断和 MCP 配置生成能力，主要缺口位于前端归属和重复路由。

## 3. 发现的问题

- Gmail API 与 IMAP 共用基础配置表单，API 模式仍显示 IMAP 密码。
- 账号配置与日常收件混在基础配置页。
- QQ 地址、授权码、删除和测试散落在高级设置及侧栏重复入口。
- OAuth GUI 导入位于高级设置，首次配置流程不完整。
- 凭据主要依赖 placeholder 表示已配置，状态不够明确。
- 仪表盘、基础配置、收件、历史、日志等一级路由重叠。
- 首次向导维护独立字段和保存逻辑。

## 4. 最终信息架构

- 左侧账号区：添加邮箱账号、Gmail（收件）卡片、QQ（发件）卡片。
- 顶部一级页：收件、发件、高级设置。
- 高级设置子入口：数据维护、Agent 接口配置。
- 不再存在基础配置、设置发件身份、侧栏仪表盘/日志/历史等重复一级入口。

## 5. Gmail API 专属配置

只显示 Gmail 地址、OAuth 客户端状态、credentials.json 选择/替换、授权状态、显式 OAuth 授权、API 连接测试和保存。页面不显示 IMAP secret、删除 IMAP 凭据或 IMAP 诊断。

## 6. Gmail IMAP 专属配置

只显示 Gmail 地址、连接方式、Google 生成的 Gmail IMAP 应用专用密码、固定掩码状态、修改、删除、IMAP 测试和保存。页面不显示 OAuth JSON、token 或 Gmail API 授权。

## 7. QQ 发件账号配置

QQ 邮箱地址、QQ SMTP 授权码、配置状态、修改、删除、连接测试和保存已统一到 QQ 账号专属页。发件页和高级设置不再维护 QQ 账号字段。

## 8. OAuth 与路径体验

用户只需选择 credentials.json。ApplicationService 调用受控导入能力完成 JSON 校验和原子复制。已有文件替换需确认，token 不会被静默删除，并提示不兼容时重新授权。普通流程不要求用户输入或理解 OAuth 目标路径。

## 9. 凭据视觉状态与安全

- 已配置状态使用固定 16 位视觉掩码和“✓ 已配置”。
- 掩码与真实长度无关。
- 旧 secret 不进入输入框、不回显。
- 仅在修改状态输入新值。
- 配置写入失败时恢复旧凭据。
- 删除必须确认，取消有明确反馈。
- Gmail IMAP 与 QQ SMTP 凭据保持独立。

## 10. 收件页

收件成为默认工作首页。顶部显示当前账号、连接方式和状态，提供账号管理快捷入口、自动收取、检查间隔、当前连接测试、立即收取、刷新和收件偏好。今日收到文件为主体，最近日志为次级区域，继续支持安全预览、打开目录、双击预览和刷新。

## 11. 发件页

只保留文件选择、文件信息、固定收件人、主题、确认、进度、结果和最近发送记录。QQ 地址与授权码不在发件页出现，MCP/CLI 路径白名单未改变。

## 12. 高级设置

已按运行设置、网络模式、本地数据与路径、全局诊断、配置与迁移、维护和 Agent 接口重新分组。路径以状态和打开目录为主；账号级 OAuth、IMAP、QQ secret 和单项测试全部移出。

## 13. 后端能力到前端入口映射

| 后端能力 | 模块/服务 | 最终入口 | 分类 |
| --- | --- | --- | --- |
| OAuth JSON 导入 | oauth_storage / ApplicationService.import_oauth_credentials | Gmail API 账号页 | 普通用户可见 |
| Gmail OAuth 授权与 token 状态 | gmail_api_auth / authorize_gmail_api / get_oauth_status | Gmail API 账号页 | 普通用户可见，token 内容自动隐藏 |
| Gmail API 诊断 | diagnose_gmail_api | Gmail API 账号页；高级设置全局诊断 | 账号主入口 |
| IMAP 凭据与诊断 | CredentialService / diagnose_imap | Gmail IMAP 账号页 | 账号主入口 |
| QQ SMTP 凭据与诊断 | CredentialService / diagnose_qq_smtp | QQ 发件账号页 | 账号主入口 |
| Credential Manager 状态、修改、删除 | credentials / ApplicationService | 对应账号页 | 状态可见，secret 隐藏 |
| Runtime Paths | runtime_paths | 高级设置路径状态与打开目录 | 高级设置 |
| 配置/凭据迁移 | settings_store / migrate_legacy_credentials | 高级设置“配置与迁移”；首次向导兼容入口 | 用户主动执行 |
| 网络模式与全局诊断 | network / diagnose | 高级设置 | 高级设置 |
| 脱敏诊断和最近错误 | export_diagnostic_report / GUI 状态 | 高级设置 | 高级设置/出错时 |
| 数据维护、备份、恢复、一致性扫描 | maintenance / ApplicationService | 高级设置“数据维护与备份” | 高级设置 |
| MCP 配置生成与调用历史 | mcp_client_config / get_mcp_history | 高级设置“Agent 接口配置” | 高级设置 |
| MCP 按需启动与 EOF 退出 | mcp_server / desktop runtime | 自动执行 | 自动/开发者 |
| 开机启动与单实例 | StartupManager / SingleInstanceGuard | 高级设置；程序自动 | 高级设置/自动 |
| 自动收取、手动收取 | receive / GUI timer | 收件页 | 普通用户可见 |
| GUI 全局文件发送 | send_user_selected_file | 发件页 | 普通用户可见 |
| 固定收件人、MCP 白名单 | config / security / mcp_server | 发件状态与 Agent 接口只读展示 | 自动安全边界 |
| 收发历史与日志 | database / ApplicationService | 今日文件、最近日志、最近发送记录 | 工作区 |

审计结论：未发现仍缺少合理入口的高价值普通用户能力。底层 token 内容、内部 stdio 生命周期、单实例锁、自动迁移细节和安全校验继续不直接暴露为按钮。

## 14. 删除的重复入口

- 基础配置一级页。
- 设置发件身份按钮。
- 高级设置中的 QQ 地址、QQ 授权码、删除 QQ 凭据。
- 高级设置中的 OAuth JSON、显式授权和三个分散账号诊断按钮。
- 仪表盘、收件、历史、日志之间的重复一级导航。

## 15. Windows GUI QA

使用 Windows 原生 Qt 图形后端完成 DPR 1.00、1.25、1.50 三档截图。每档覆盖收件、发件、高级设置、添加账号、Gmail API、Gmail IMAP、QQ SMTP和深色收件页，共 24 张截图。自动检查未发现非换行文本截断或横向滚动溢出；人工抽查未发现重叠、按钮拥挤、掩码异常或深色主题不可读。截图位于被 Git 忽略的 artifacts/gui-qa-account-ia/windows。

## 16. 自动化测试

- 基线：251 passed。
- 最终：269 passed。
- 新增覆盖：条件 UI、已有账号编辑模式、切换不删除认证材料、OAuth 有效/无效/取消/替换、固定掩码、不回显、凭据回滚、删除确认与取消、QQ/IMAP 独立、三页导航、账号卡片入口、高级设置去账号化、首次向导复用。

未执行真实邮箱联网授权、收件或发件；本轮没有使用用户真实 secret，也不把离线回归写成真实网络通过。

## 17. 修改文件

- application_service.py：OAuth 导入应用服务入口。
- ui/account_management.py：统一账号控制器、三套专属配置页、固定凭据状态组件。
- ui/main_window.py：三页工作区、收件首页、高级设置分组、账号卡片路由。
- ui/setup_wizard.py：复用账号管理。
- ui/theme.py、ui/widgets.py：账号选择、凭据状态和消息栏视觉。
- tests：专项 UI、安全和导航回归。
- README、AGENTS、CHANGELOG、GUI/安全/MCP 说明及本报告。

## 18. 文档更新

README、AGENTS、CHANGELOG、GUI 使用说明、安全与诊断说明、MCP 使用说明均已按新入口更新。

安装器的主 EXE 启动文案和“快捷方式选项”页也已同步：桌面快捷方式仅指向主程序，内部 MCP EXE 不创建快捷方式。

## 19. Git 检查点

- 邮箱账号管理、专属认证页和三页工作区实现检查点。
- 测试、Windows GUI QA、文档与最终报告检查点。

未 force push、未改写历史、未发布 GitHub Release。

## 20. 剩余问题与优先级

- P2：正式公开发布前仍需在最终安装包上复做无 Python 独立 Windows 环境、安装/升级/卸载、Defender、签名和真实账号在线验收。
- P3：可在后续版本把账号页网络测试也迁移到非阻塞任务执行器，当前已有 running/success/failed 反馈，但连接期间账号对话框保持同步等待。

上述项目不影响本轮信息架构、账号安全和 GUI 离线验收结论。

## 21. 最终结论

PASS。

本轮验收目标全部落地：API/IMAP/QQ 专属配置、OAuth GUI 自动导入、固定凭据状态、统一账号路由、顶部三页、收件主体、高级设置分组和后端能力映射均已完成，现有安全边界未扩大。
