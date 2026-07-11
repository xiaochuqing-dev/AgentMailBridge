# AgentMailBridge 第二部分：Windows 打包与发布前收口报告

## 1. 执行摘要与最终结论

最终 Gate：CONDITIONALLY PASS。

AgentMailBridge 已从源码启动项目推进为可安装的 Windows 0.9.0 候选产物：用户可见主程序、内部按需 MCP、当前用户安装器、portable ZIP、运行时路径、凭据安全、OAuth 受控导入、升级/卸载保留策略、可重复构建和哈希均已完成。当前机器的源码与 packaged 验收通过，但没有真正独立无 Python 的 Windows Sandbox/VM，也没有旧候选版本用于跨版本升级，因此不能写为完整 PASS，不建议立即创建公开 GitHub Release。

## 2. 开始前真实状态与问题

开始分支为 master，工作区干净，`git pull --ff-only origin master` 显示已是最新，基线提交为 `cd5561c`。基线 241 项自动化测试通过。

实际确认的问题包括：可写路径依赖 PROJECT_ROOT；安装版路径未定义；首次向导会把 QQ_AUTH_CODE 交给普通配置；版本在 0.1.0、0.4.0 和 GUI 1.0.0 间漂移；StartupManager 不支持 frozen；没有 PyInstaller、安装器、升级/卸载和发布产物流程；GUI MCP 页面只生成源码命令；根目录缺 LICENSE 和 AGENTS.md。

## 3. Runtime Paths、配置、OAuth 与凭据

新增 `runtime_paths.py`，明确 source_root、install_root、resource_root、user_config_root、oauth_root、data_root、cache_root 和 temp_root。源码模式保留仓库 `.env`；frozen 模式默认使用 `%LOCALAPPDATA%\AgentMailBridge`，不依赖当前工作目录。

普通配置原子保存、UTF-8，并拒绝非空 GMAIL_APP_PASSWORD/QQ_AUTH_CODE。首次向导不回显旧授权码，秘密通过 CredentialService 写入和回读验证，失败恢复旧值。旧 `.env` 只能由用户明确选择，事务式迁移失败会回滚新配置和凭据。OAuth credentials.json 经结构验证后复制到受控用户目录，token 独立持久化，Gmail scope 继续强制为 gmail.readonly。

阶段结论：PASS。

## 4. 版本统一

单一来源为 `agent_mail_bridge/version.py`，最终版本 0.9.0。pyproject 动态读取该版本；GUI 标题/关于页、CLI `--version`、MCP serverInfo、两个 EXE 的 FileVersion/ProductVersion 和安装器均为 0.9.0。主 EXE 和 MCP EXE 的 OriginalFilename 已分别设置正确。

阶段结论：PASS。

## 5. Windows frozen 构建

采用 PyInstaller 6.21 onedir。用户可见入口为无控制台 `AgentMailBridge.exe`；内部接口为 console 子系统的 `AgentMailBridgeMCP.exe`，以确保 Windows stdio 稳定。安装目录只包含程序、DLL、Qt、资源和许可文件。Logo、ICO、Qt platform plugin、Google API/OAuth、CA 依赖及 EXE 元数据已打包。

构建脚本执行清理、测试、双入口构建、packaged self-test、MCP smoke、秘密排除、portable 压缩、Inno Setup 和 SHA-256。GUI packaged self-test同时验证 frozen 路径、品牌资源和 Windows Credential Manager 临时写读删。

阶段结论：PASS。

## 6. MCP 成品化

MCP 保持本机 stdio，不引入 HTTP、端口、Socket、Named Pipe 或常驻服务。安装版 GUI 自动生成内部 EXE 的 Codex、Claude Code 和通用 JSON 配置，正确处理空格和中文路径。内部 EXE 无快捷方式、无托盘、不开机启动，stdin EOF 后退出。

packaged 验证覆盖 initialize、notifications/initialized、ping、tools/list、tools/call submit_result、stdout 纯协议、重复独立启动/退出、path_not_allowed、fixed recipient、request_id、duplicate、频率限制、审计和秘密脱敏。

真实 packaged MCP 使用固定收件人发送了一份无隐私验收文本：SMTP 接受成功，第二次相同 request_id 返回 duplicate，sent 归档 SHA-256 与源文件一致，为 `4e1395645e019dc73475181af8c64e34ad925d5bdb2f655dbcd4ce7f870d7011`。本报告不据此宣称 Gmail 最终收到。

阶段结论：PASS。

## 7. 安装器、开机启动、升级与卸载

采用 Inno Setup 6.7.3，稳定 AppId、当前用户安装、无需管理员权限，默认目录 `%LOCALAPPDATA%\Programs\AgentMailBridge`。开始菜单和可选桌面快捷方式只指向主 EXE。StartupManager 在 frozen 下生成带引号的 `AgentMailBridge.exe --background`；MCP 不常驻。

在中文和空格安装路径执行静默安装、同版本覆盖/修复安装和卸载，退出码均为 0。覆盖安装后隔离用户数据仍存在；卸载后主 EXE 已删除、开机启动值已清理、隔离配置和数据仍存在。没有旧候选安装器，因此跨版本升级为未完成项。

阶段结论：安装与卸载 PASS；升级 CONDITIONALLY PASS。

## 8. Packaged E2E

- GUI 主 EXE packaged self-test：PASS。
- `--background` 启动并保持运行：PASS。
- Credential Manager packaged 写入、读取、删除：PASS。
- Gmail API packaged 只读诊断：PASS，复用现有授权，不记录 token。
- QQ SMTP packaged 连接与认证诊断：PASS。
- QQ SMTP packaged 真实发送、duplicate 和归档 Hash：PASS。
- MCP packaged stdio E2E：PASS。
- 数据库、备份、维护、全局文件和安全边界：自动化回归 PASS。
- 托盘恢复、双实例提示和 100%/125%/150% DPI 完整人工矩阵：未执行。

阶段结论：CONDITIONALLY PASS。

## 9. 干净环境、升级与卸载

安装版在去除 Python 目录的 PATH 下从安装目录启动，证明运行时不调用系统 Python；但宿主系统仍安装 Python、源码和开发工具，不能等同于真正干净机器。Windows Sandbox/独立 VM 未执行。

同版本覆盖和卸载数据保留通过；真正旧版本到 0.9.0 的升级未执行。

阶段结论：CONDITIONALLY PASS。

## 10. 安全扫描、Defender、签名与 Hash

dist、release 和 portable ZIP 共扫描 862 个文件，未发现 `.env`、credentials.json、token.json、secrets、SQLite 或用户邮件/附件路径。安装器只收集已验证的 dist 目录。Windows Defender 已启用，对 dist 和 release 自定义扫描未产生新增检测。

主 EXE、MCP EXE 和安装器 Authenticode 均为 NotSigned。没有签名证书，未伪造签名或绕过 SmartScreen。

最终产物：

- `release/AgentMailBridge-0.9.0-Setup.exe`
  SHA-256：`2e2a245cb0b99544fa707743d5169e3032ce1ae7d0b66d603b25250cc6ca1796`
- `release/AgentMailBridge-0.9.0-Windows-x64.zip`
  SHA-256：`6cff9f4d94bddf95738af972cc688ad1c7753efc8ab27806997365b13e27b5d2`

阶段结论：安全排除与 Hash PASS；签名 CONDITIONALLY PASS。

## 11. 测试、性能、修改和文档

最终 pytest：251 passed。新增 Runtime Paths、source/frozen、中文和空格路径、配置秘密拒写、凭据失败回滚、旧配置事务迁移、OAuth 导入、StartupManager、MCP 安装版配置和版本一致性测试。

10,000 条记录、50 个刷新周期稳定性基准通过；SQLite integrity_check 为 ok，refresh bundle 最大 8.86 ms，线程数前后均为 1。

新增或更新 Runtime Paths、配置、凭据、OAuth、GUI、MCP、版本、PyInstaller、Inno Setup、构建/验证/真实 E2E 脚本、LICENSE、第三方说明、README、AGENTS、CHANGELOG 和用户文档。

## 12. Git 检查点、剩余限制和优先级

未 force push、未改写历史、未删除分支、未发布 GitHub Release。最终提交前执行 `git diff --check` 和完整回归。

P0：无。

P1：真正无 Python 的独立 Windows 环境未验收；旧候选到 0.9.0 的跨版本升级未验收。

P2：完整 DPI/托盘/单实例人工矩阵、代码签名、SmartScreen 信誉和最终第三方许可确认。

## 13. Release Candidate 结论

代码、构建、安装器和当前机器 packaged 验收已经达到内部候选产物质量，但严格 Release Candidate Gate 要求真正干净环境和跨版本升级证据，因此当前结论仍为 CONDITIONALLY PASS，不标记完整内部 RC，不建议创建正式 GitHub Release。

补齐两项 P1 后，再执行最终 clean build、Defender、签名状态和 Hash 复核，可重新评估为 PASS。
