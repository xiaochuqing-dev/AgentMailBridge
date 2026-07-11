# Windows 安装、升级与卸载

## 安装

运行 `AgentMailBridge-0.9.0-Setup.exe`。默认当前用户安装目录为 `%LOCALAPPDATA%\Programs\AgentMailBridge`，无需管理员权限。可选桌面快捷方式，开始菜单只显示主程序。

安装完成后启动主程序并完成首次配置。内部 `AgentMailBridgeMCP.exe` 仅供 Agent 客户端按需调用，不要手动创建快捷方式或开机启动项。

## 升级

关闭主窗口和托盘后运行新版安装器覆盖安装。安装器使用稳定 AppId，程序文件被替换；配置、OAuth、Credential Manager、DATA_ROOT、SQLite、received/send/sent 和 backups 位于安装目录外并保留。

本轮已完成同版本覆盖/修复安装和隔离数据保留验证。由于没有保留真实旧候选安装器，0.8.x 到 0.9.0 的跨版本升级仍是发布前限制。

## 卸载

从 Windows“已安装的应用”卸载。卸载会删除程序、Qt、快捷方式、安装记录及指向已卸载 EXE 的开机启动值。默认保留配置、OAuth、Credential Manager 凭据和用户数据，重新安装后可继续使用。

## 当前验收范围

已在 Windows 11 当前用户环境验证：中文和空格安装路径、无源码路径依赖、主 EXE packaged self-test、Credential Manager 写读删、Gmail API 只读诊断、QQ SMTP 认证诊断、MCP stdio、覆盖安装、卸载和数据保留。

通过缩减 PATH 启动验证了 frozen 运行不调用系统 Python，但本机仍安装了 Python，因此不能等同于真正干净 Windows Sandbox/VM。正式公开发布前必须补做独立无 Python 环境安装、托盘、单实例、DPI 和卸载复核。
