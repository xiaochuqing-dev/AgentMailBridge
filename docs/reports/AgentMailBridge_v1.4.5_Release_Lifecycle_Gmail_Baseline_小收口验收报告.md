# AgentMailBridge v1.4.5 Release Lifecycle & Gmail Baseline 小收口验收报告

验收日期：2026-07-26

## 1. 基线 HEAD

本阶段从 `8d78c7bd78e27c52b9d0c3a16df1851bb49063c4` 开始，分支为 `master`，远端为 `origin/master`。

## 2. 本阶段目标

关闭 1.4.4 到 1.4.5 的安装、覆盖升级、卸载保留和重装恢复；恢复 Gmail 收件基线；对 QQ、163、Gmail 真实本地归档执行最终 MCP EXE 读取验收。未实现 Gmail Send，未扩大 OAuth scope。

## 3. v1.4.4 完成状态

QQ 与 163 的真实双向收发、富 MIME、目录、增量、归档、Hash、调度隔离和错误路径已经闭环并保持正式支持。Generic 仍为 implementation ready / E2E required。

## 4. 当前遗留债

阶段开始时的缺口是隔离安装生命周期未闭环、本机 Gmail Token 已失效、真实 MCP 读取因默认 opt-in 关闭未执行，以及公开分发制品未签名。

## 5. Research Gate 使用情况

本阶段没有改变安装数据策略、OAuth 协议、scope、回调、安全边界或 MCP 权限模型，因此未触发外部方案研究。Gmail 修复只把诊断入口接回项目既有 Account Runtime Router 和账号专属 OAuth 路径，属于现有设计内的定向回归修复。

## 6. 测试环境

Windows 11 家庭中文版，系统版本 10.0.26200；Python 3.11.15；PyInstaller 6.21.0；Inno Setup 6.7.3。测试使用随机 AppId、随机测试账号元数据、隔离程序目录和隔离用户目录。

## 7. 安装生命周期测试设计

用同一个随机 AppId 构建 1.4.4 和 1.4.5 隔离安装器。唯一变化是测试 AppId、显示名和输出文件名；安装脚本、最终 1.4.5 dist payload、升级与卸载逻辑均与候选制品一致。测试前先写入纯测试配置，避免继承生产配置。

## 8. 旧版安装

1.4.4 隔离安装 PASS。GUI 版本、GUI packaged self-test、MCP initialize、七工具列表、同步状态、stdout purity 和 EOF 共 7 项全部通过。

## 9. 覆盖升级

在同一隔离安装目录直接安装 1.4.5，覆盖升级 PASS。升级后 1.4.5 的 7 项 GUI/MCP probe 全部通过。

## 10. DB migration

升级前后 SQLite `PRAGMA integrity_check` 为 `ok`，multi-account schema version 为 3。迁移保持幂等，没有重建或丢失业务事实。

## 11. account_id 保留

升级、卸载和重装前后的 5 个账号记录及其稳定 account_id 集合完全一致，其中包括兼容占位记录；四个正式测试账号分别覆盖 Gmail、QQ、163 和 Generic。

## 12. Credential 保留

随机生成的独立测试 Credential 在升级后、卸载后和重装后均存在，最终由验收脚本显式删除。未读取、覆盖或输出真实邮箱凭据。

## 13. OAuth Token 保留

隔离账号的 credentials 与 Token 两个 JSON 文件在升级、卸载和重装前后均存在，SHA-256 映射完全一致。证据不包含文件内容、Client Secret、Token、state、code 或授权 URL。

## 14. Mail Package 保留

4 个正式 Mail Package 在升级、卸载和重装后数量与归属保持一致。

## 15. raw.eml 保留

4 个实际生成的 `raw.eml` 全部保留，未改写、未伪造、未重新计算内容。

## 16. attachments 保留

4 个测试附件全部保留，升级、卸载和重装前后数量一致。

## 17. Hash 前后对比

所有 raw.eml、附件和 OAuth JSON 的相对路径到 SHA-256 映射在升级、卸载和重装前后逐项相等。

## 18. scheduler state 保留

Gmail、QQ、163、Generic 共 4 条账号级 scheduler state 全部保留，checkpoint、成功状态和计数未被安装生命周期改写。

## 19. MCP / GUI 升级后状态

升级后 GUI packaged self-test PASS；MCP initialize、七工具、同步状态、UTF-8 stdout 与 EOF 退出 PASS。重装后的相同 7 项 probe 再次全部通过。

## 20. uninstall

隔离卸载 PASS。程序 EXE 与安装目录内容被移除，没有桌面、托盘、启动项、监听器或 MCP 独立入口遗留。

## 21. user data retention

卸载后配置、GUI 设置、SQLite、账号、调度、4 个 Mail Package、4 个 raw.eml、4 个附件、OAuth 文件和测试 Credential 均保留。

## 22. reinstall recovery

卸载后重新安装 1.4.5，所有持久事实恢复可读，7 项 packaged probe 全部通过。最终再次卸载，随机测试 Credential 删除，隔离安装无遗留。

## 23. Gmail OAuth 基线

原账号级 Token 已被 Google 撤销。用户在真实浏览器中手工完成现有 Desktop OAuth 重新授权，未使用 Computer Use 或浏览器自动化。账号级 Token 验证、Gmail API 连接及程序重启后复验均 PASS。

## 24. Gmail receive 基线

真实 Gmail 账号连接测试 PASS，随后连续执行两轮真实收件，结果均为健康的 `no_changes`，失败计数为 0；程序重启后连接复验仍 PASS。诊断误读 legacy Token 的问题已修复为账号级 OAuth 路由。

## 25. gmail.readonly scope 核验

运行配置、自动化断言和真实账号状态均确认 scope 严格且唯一为 `https://www.googleapis.com/auth/gmail.readonly`。没有 Gmail send capability、send API 或 scope 扩张。

## 26. Gmail / QQ / 163 隔离

Gmail 重新授权没有改变 QQ/163 Credential；账号级 OAuth、Credential、scheduler、archive ownership 和失败隔离自动化均通过。Gmail 恢复后 QQ/163 的真实本地归档仍可由最终 MCP EXE 独立读取。

## 27. 真实 MCP 读取

最终 `AgentMailBridgeMCP.exe` 对 QQ、163、Gmail 各选一封真实归档执行 initialize、tools/list、search、account_id filter、get、raw/body/所选资源 read、prepare、Hash、sync status、workspace list、路径拒绝、ownership、UTF-8、stdout、EOF 和审计，三个 Provider 全部 PASS。163 还直接准备了真实附件；报告不含正文、附件内容、地址或 account_id。

## 28. MCP opt-in 恢复

持久 opt-in 测试前为 false、测试后仍为 false。真实读取仅通过 MCP 子进程环境临时开启；默认拒绝会话另行 PASS，临时 workspace 已删除，任意路径未开放。

## 29. GUI

源码版本、最终 EXE 版本和 GUI packaged self-test PASS。本阶段未改视觉，因此没有重复执行 100%/125%/150% 深浅色人工截图，记为 NOT_TESTED。

## 30. targeted tests

Provider、OAuth、MCP、multi-account 与生命周期定向回归 110 passed。生命周期脚本收口后又执行 6 个最小定向测试，6 passed。

## 31. Full Suite Preflight

最终 Preflight 97 passed；版本、Provider 状态、schema、git diff check、compileall 和定向 pytest 全部 PASS。

## 32. final full pytest

最终产品候选代码执行完整 pytest：582 passed、1 skipped、0 failed，耗时 2080.15 秒。其后只修正未打包的验收脚本及对应测试，产品代码与正式 dist 未改变，并以 6 个定向测试和 97 项 Preflight 复核。

## 33. pip check

`python -m pip check` PASS：No broken requirements found。

## 34. clean build

执行 Windows clean build 并跳过已完成的重复全量 pytest。双 PyInstaller EXE、build verification、GUI/MCP smoke、Inno installer、ZIP、checksums 和 secret scan 全部成功。

## 35. packaged smoke

最终 GUI EXE packaged self-test PASS；最终 MCP EXE 的协议 smoke、UTF-8、七工具、stdout purity 和 EOF PASS。

## 36. installer

`AgentMailBridge-1.4.5-Setup.exe` 大小 43,979,897 bytes，SHA-256 为 `577ff718fe080f035b1a1d12b968eba41e126147d2753077daa30966e3394a71`。

## 37. ZIP

`AgentMailBridge-1.4.5-Windows-x64.zip` 大小 69,455,625 bytes，SHA-256 为 `72dbb3bf73d1665ec365f53ad40cb095ba44fa85b7b547f99c577147fbdee92e`。

## 38. checksums

`checksums.sha256` 同时记录 installer 与 ZIP，重新计算结果逐项相同。清单自身 SHA-256 为 `6b4d944cb52cad1c819ac0461d9c374c976b1914339db291e530fedb1fad65e2`。

## 39. secret scan

最终 dist、release 与 ZIP 共扫描 319 个文件，0 个配置秘密标记命中。staged diff 另行检查，未包含 `.env`、credentials、Token、数据库、日志、邮件、附件、本机用户名或绝对用户路径。

## 40. Defender

Microsoft Defender 与实时保护均启用，签名版本 1.455.350.0。对 release 和最终 dist 分别执行 CustomScan，命令成功，活动威胁为 0；系统历史检测记录不作为本项目检测。

## 41. Authenticode

GUI EXE、MCP EXE 和 Setup.exe 均为 `NotSigned`。没有伪造签名或把未签名表述为通过。

## 42. P0 / P1 / P2

P0：0。

P1：1。正式公开分发前仍需可信代码签名，这是 Release Gate。

P2：2。Generic 独立第三方真实 E2E 未执行；本阶段未重复人工 DPI/深浅色截图。

## 43. PASS / CONDITIONALLY PASS / FAIL

结论为 CONDITIONALLY PASS。安装生命周期、Gmail receive baseline、真实 MCP 读取、自动化、构建和安全扫描均 PASS；公开发布仍受代码签名 P1 阻断。

## 44. 已知限制

正式固定 AppId 的公开安装器未在当前生产用户上直接运行，以免覆盖现存 1.3.0 安装；实际生命周期使用相同 Inno 脚本和最终 payload，仅随机化测试身份。没有人为篡改真实 Token 过期时间强制刷新，刷新兼容由自动化覆盖，真实基线覆盖重新授权、有效 Token 复验、真实 API 收件和重启复验。

## 45. Gmail Full Account readiness

账号级 OAuth 路由、只读 scope、收件、调度隔离、归档 ownership、升级保留和 MCP 只读消费已经具备稳定基线。Gmail Send 权限模型、增量授权、旧 `gmail.readonly` 用户无损升级和发送 E2E 尚未设计或实现。

## 46. 下一阶段建议

下一阶段单独进行 Gmail Full Account / Gmail Send Permission Model，先完成权限与迁移设计，再决定 Gmail API send 或 SMTP OAuth2；不得在本次 v1.4.5 中提前扩 scope。Generic 真实第三方 E2E 与代码签名可并行作为独立发布门禁。

## 47. commits

`35d03f48b6c3aeba667c3dbfa739c71f5ba76a99`：`fix: close v1.4.5 release lifecycle baseline`，包含产品修复、版本、安装/MCP 验收脚本、测试与用户文档。

专项报告提交与最终状态回写将在后续文档提交中记录。

## 48. push status

`35d03f48b6c3aeba667c3dbfa739c71f5ba76a99` 已普通 push 到 `origin/master`。本报告尚待提交与推送；未 force push，未创建 Tag 或 GitHub Release。
