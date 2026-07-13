# AgentMailBridge 全局文件、凭据安全、数据维护与稳定性专项报告

## 1. 执行摘要

阶段 A、B、C、D 均已完成。GUI 已支持用户手动选择全局文件；MCP 和 CLI 目录边界未放宽；两项旧 `.env` 凭据已迁移到 Windows Credential Manager；数据维护中心可用；10,000 条历史规模和 50 周期稳定性基准通过。

## 2. 开始前项目真实状态

开始时分支为 codex/email-ui-fixes，工作树干净，比远端领先 4 个提交。阶段 3 至 5 闭环、阶段 5.5 GUI 整改、Gmail API/IMAP、QQ SMTP、MCP、SQLite WAL、托盘、自动收件、单实例和开机启动均已存在，基线测试 213 项通过。GUI 手动发件仍受目录白名单限制，凭据仍从 `.env` 读取，缺少维护中心和大数据量实测。

## 3. README、TODO、Issue 和最近改动检查

已检查 README、CHANGELOG、docs、两份阶段总报告、实现报告、测试、最近 Git 提交和 TODO/FIXME 搜索。仓库没有独立 Issue 文件；README 后续计划中的系统密钥环事项已完成，ProjectFlow 和 QQ SMTP SOCKS5 仍不在本轮范围。

## 4. 本轮执行计划

按 A 全局文件、B 凭据、C 维护、D 稳定性的顺序实施。每阶段先审计、再修改、补测试、跑完整回归并提交检查点，最后统一更新文档和本报告。

## 5. 阶段 A：全局手动文件选择

GUI 文件选择器起始于用户目录，可浏览桌面、下载目录、其他磁盘和普通目录。选择状态记录文件名、大小、修改时间和 SHA-256，支持中文、长文件名、空文件、复制路径、打开目录和安全预览。

## 6. 用户手动入口与 MCP 边界

新增 ApplicationService.send_user_selected_file，仅由 GUI 调用。原 send_file 与 submit_result 保持 DATA_ROOT / ALLOWED_SEND_ROOTS 校验。MCP 没有手动来源参数、凭据接口或全局读取能力，桌面和下载目录等越界路径继续返回 path_not_allowed。

## 7. 暂存、快照与 Hash 校验

确认后在 DATA_ROOT/send/staging/request_id 下创建稳定快照，复制后核对大小和 SHA-256。SMTP 和 sent 归档基于快照，附件名保持用户确认的原文件名。相同 request_id 的快照内容不一致时拒绝覆盖，避免静默变化和无限重复副本。

## 8. 全局文件发送验收

自动化模拟外部目录、下载目录等价路径、中文长文件名、空文件、危险类型、文件变化、快照 Hash、邮件附件内容和 MCP 越界。危险脚本拒绝发送且不执行；历史显示原文件名、大小、来源和 request_id。未触发真实 SMTP 发信。

## 9. 阶段 A Git 检查点

34ecc8f feat: 完成全局手动文件受控快照发送

## 10. 阶段 B：Windows 凭据安全存储

使用 Windows Credential Manager 通用凭据和标准库 ctypes 实现统一读、写、更新、删除、状态查询。未引入第三方运行依赖。GUI 不回显旧秘密值，只显示已配置状态；删除必须确认。

## 11. 旧 `.env` 迁移结果

真实本机迁移成功 2 项、失败 0 项。GMAIL_APP_PASSWORD 和 QQ_AUTH_CODE 已写入并回读验证，`.env` 对应值已清空，非敏感配置保留。迁移逻辑逐项处理，失败项不会清空旧值，可重复运行。

## 12. GUI、CLI、ApplicationService 和 MCP 凭据边界

load_config 优先使用安全存储，旧环境变量仅作兼容回退。GUI、CLI 和 ApplicationService 共用 CredentialService；credential-status 只显示状态，migrate-credentials 执行迁移。MCP 不暴露凭据工具。日志、错误、诊断和维护报告均不输出秘密值。

## 13. 阶段 B Git 检查点

caf8200 feat: 迁移邮箱凭据到 Windows 安全存储

## 14. 阶段 C：数据维护中心

GUI 新增“数据维护”页，显示 SQLite 完整性、数据库大小、表记录数、received/send/sent/logs 容量、备份列表，并提供创建、验证、恢复、扫描、打开备份目录和导出维护报告。

## 15. 备份与恢复结果

SQLite 使用在线 backup API，备份后执行 integrity_check，生成 SHA-256 JSON 清单。损坏或 Hash 不一致的备份被拒绝。恢复必须明确确认，运行任务时禁止；恢复前自动备份当前库，失败时从安全备份回滚，成功后重新初始化连接。恢复不覆盖 received 或 sent 文件。

本轮新备份：`<DATA_ROOT>\backups\agent_mail_bridge_20260711_133711_932229_manual.db`，90,112 字节。

既有备份：`<DATA_ROOT>\backups\agent_mail_bridge_AMB-20260710-222514_before_stage3.db`，57,344 字节。

## 16. 一致性扫描结果

真实数据只读扫描结果：缺失 0、孤立 1、Hash 异常 0、越界 0、暂存残留 0、无法访问 0。孤立文件未自动删除或修改，留给用户审查。

## 17. 维护报告与安全边界

维护报告仅包含 integrity_check、记录数量、容量、异常计数、备份数量和建议，不包含邮件正文、附件内容、凭据、token 或完整私人路径。一致性扫描默认只报告，不执行删除、合并或重建。

## 18. 阶段 C Git 检查点

ce71284 feat: 增加数据备份恢复与一致性维护中心

## 19. 阶段 D：大数据量与长期稳定性专项

新增 stability-benchmark 隔离入口，生成临时 DATA_ROOT、数据库、MCP 审计、日志和代表性文件，退出时释放 SQLite 与日志句柄。自动化覆盖参数边界、多周期刷新、索引计划、日志轮转和 SMTP 失败后显式恢复与幂等。

## 20. 测试数据规模

收件 10,000 条、发件 5,000 条、MCP 10,000 条、事件日志 10,000 条、代表性文件 200 个，连续组合刷新 50 周期。隔离数据库大小 5,718,016 字节。

## 21. 页面和查询性能

应用服务启动最大 12.302 ms；历史查询平均 2.451 ms、最大 3.528 ms；组合刷新平均 5.167 ms、最大 10.259 ms。日志、MCP 和今日文件查询均为毫秒级。GUI 刷新继续在单线程 QThreadPool 后台执行，不阻塞主线程。

## 22. 内存、线程、句柄和数据库结果

工作集 50,155,520 → 51,539,968 字节，增加约 1.38 MB；tracemalloc 峰值低于 0.5 MB。线程 1 → 1，句柄 212 → 208。SQLite integrity_check 为 ok，无锁死；日期查询使用 idx_received_messages_saved_date。

## 23. 故障与恢复模拟

覆盖 Gmail/OAuth/IMAP/SMTP 既有错误映射、SMTP 连接失败后显式重试、成功后同 request_id 拒绝再次发送、数据库损坏备份拒绝、恢复回滚、文件变化和安全存储不可用。未执行真实断网和真实 SMTP 发信。

## 24. 完成的性能优化

保留最近 100 条的有界加载、后台刷新、现有日期索引和 rowid 逆序查询。测量未发现需要分页重写的瓶颈，因此未增加无收益索引或重写数据库。基准显式关闭日志 handler 和 SQLite 连接，解决 Windows 临时目录句柄占用。

## 25. 阶段 D Git 检查点

69eeecc perf: 增加大数据量与资源稳定性基准

## 26. 修改文件清单

核心新增 credentials.py、maintenance.py、performance.py；修改 application_service.py、config.py、database.py、mail_send.py、cli.py、ui/main_window.py。同步 README、CHANGELOG、GUI/MCP/安全说明、.env.example、requirements.txt、pyproject.toml 和 .gitignore。

## 27. 新增和修改的测试

新增 test_stage_a_global_send.py、test_stage_b_credentials.py、test_stage_c_maintenance.py、test_stage_d_stability.py；调整 GUI 和测试隔离夹具。测试不访问真实邮箱、OAuth、用户数据库、用户文件或真实 Windows 凭据条目。

## 28. 最终测试总数和结果

234 项通过，0 失败，最终回归耗时 28.38 秒。pip check：No broken requirements found。git diff --check 无空白错误。

## 29. 文档与问题清单更新

README、CHANGELOG、GUI 使用说明、MCP 使用说明、安全与诊断说明、.env.example、依赖声明和忽略规则已更新。README 的系统密钥环待办已关闭；无独立 Issue 文件需要修改。

## 30. 仍存在的限制

仍为本地单用户、固定收件人、Python 启动方式；未制作 Windows 安装包；未执行真实 SMTP 验收邮件、真实断网、系统级多显示器 125%/150% 切换、休眠唤醒或连续数日运行。完整数据快照未打包附件，当前采用数据库备份和一致性扫描。

## 31. 需要用户后续观察的事项

日常观察休眠唤醒、网络恢复、真实 QQ SMTP 链路和连续数日托盘运行。数据维护页当前报告 1 个孤立文件，应由用户确认归属后再决定处理，本工具不会自动删除。

## 32. 是否具备进入 Windows 打包阶段的条件

建议进入 Windows 打包阶段。核心功能、安全迁移、维护能力和 10,000 条规模基线已具备；打包前应补一次明确测试文件的真实 SMTP 收发复核，并在打包产物中验证 Credential Manager、OAuth 文件路径和高 DPI。

## 33. 下一步建议

下一轮只做 Windows 打包与安装后验收：PyInstaller 或等价方案、签名与安装路径、首次配置、Credential Manager、OAuth 回调、托盘、开机启动、升级前备份和卸载保留数据。不要扩展多用户、任意收件人或通用 Gmail MCP。
