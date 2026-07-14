# Windows 安装、升级与卸载

运行 `AgentMailBridge-1.0.0-Setup.exe`。默认安装到 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需管理员权限。开始菜单和可选桌面快捷方式只指向 `AgentMailBridge.exe`；内部 `AgentMailBridgeMCP.exe` 不创建快捷方式或开机启动项。

升级前正常退出主窗口和托盘，再运行新版安装器覆盖安装。稳定 AppId 只替换程序文件；配置、收件规则、OAuth、Credential Manager、DATA_ROOT、SQLite、received、send、sent 和 backups 位于安装目录外并保留。旧 `AUTO_RECEIVE_ONLY_SELF_MAIL` 会由新版自动映射，无需手工编辑配置。

本专项数据库迁移只新增 MCP Hash/staging 字段、`auto_receive_state` 和 `receive_retries`，通过 `CREATE TABLE IF NOT EXISTS` 与缺列检测向前兼容，不重建或清空旧库。首次新版启动后应确认自动收件状态、上次检查和下次检查可见。

从 Windows“已安装的应用”卸载时，程序、Qt、快捷方式、安装记录和失效开机启动值会删除；配置、OAuth、凭据和用户数据默认保留。重新安装后可继续识别。

v1.0.0 发布验收必须覆盖：完整 pytest、clean build、主 EXE packaged self-test、MCP packaged smoke、秘密扫描、哈希、安装覆盖、数据保留、桌面快捷方式目标、快捷方式实际启动、版本、主导航、单实例、托盘、收发入口、历史记录、文件与数据、设置和发件内 MCP。

MCP packaged smoke 还必须以 UTF-8 向 `AgentMailBridgeMCP.exe` 写入 initialize、tools/list、malformed JSON、未知 method 和 EOF，并用允许目录内的中文空格文件名执行 submit_result。不得通过修改控制台 code page 或手工 Copy-Item 规避问题。安装后桌面快捷方式仍只能指向 `AgentMailBridge.exe`，MCP EXE 不创建快捷方式。

安装器和 EXE 未签名时可能触发 SmartScreen。公开发布仍需独立无 Python Windows 环境和最终第三方许可复核；不得因此自动创建 GitHub Release。
