# AgentMailBridge v1.0.0 UI 交互与 Windows 视觉质量专项整改报告

## 1. 开始前状态

master 与 origin/master 同步，起点为 c800e9c，工作区干净。版本、本机安装和桌面入口均为 1.0.0，快捷方式指向当前用户正式安装目录。基线完整测试因最初 124 秒执行时限未取得最终结果；整改后以更长时限重新完成全套测试。

## 2. 问题与整改结果

- 收件标题与刷新：主刷新已移到“收件”标题右侧，统一刷新当前页数据；最近日志重复刷新已删除，保留“管理日志”。
- 搜索框：扩大宽度，使用正式线性搜索图标和“搜索收到的文件”完整占位符，支持清空，并真实过滤文件名、完整路径和关联邮件主题。
- 文件表格：列固定为文件名、大小、保存路径、收取时间、操作；关闭文本省略，按内容确定列宽，支持横向滚动和用户拖动列宽。
- 文件操作：每行提供真实“打开”“复制路径”紧凑按钮；打开前验证 DATA_ROOT 和文件存在性，复制后短暂显示“已复制”；双击行仍为安全预览。
- 收件偏好：页面显示当前摘要并提供“编辑偏好”按钮；对话框支持“仅本人邮件”和“当前收件范围内的全部邮件”，保存到现有配置，API/IMAP 与手动/自动收件共用。
- 状态语义：统一 success、no_changes、partial、failed。无新邮件为中性成功检查，不写 ERROR、不增加失败统计；partial 使用 WARNING，failed 仅用于真实主流程故障。
- 按钮与图标：当前收件页文字操作均改为真实按钮；Primary 使用克制紫蓝渐变，Secondary/Compact 有边界和交互态；月亮字符、刷新、搜索、文件、打开、复制和健康图标统一为单色线性图标。
- 连接健康：重构为 Gmail 收件、QQ SMTP、Agent/MCP、凭据/OAuth、SQLite/数据目录五项面板，独立展示状态、说明、最近检查时间和定向“去处理”。
- 右侧布局：顺序保持服务状态、今日统计、连接健康、快捷提示；健康面板承担主要空间，提示缩减为两条有效内容。
- Windows 字体与 DPI：集中定义 11 类 Typography Token；注册 Microsoft YaHei UI Regular/Bold，字重收敛为 400/700，Qt 6 保持 PassThrough 高 DPI 策略。

## 3. UI QA

实际运行源码 GUI 成功，主窗口标题正确。使用真实 Qt 渲染分别检查 100%、125%、150% 和深色主题，并单独检查文件操作列、收件偏好、连接健康和主题按钮。结果均通过：中文无缺字、字重稳定、无重叠和省略号生成，表格可横向滚动至完整路径与操作列。截图保存于 gitignored 的 artifacts/gui-qa-20260713，不进入 Git。

## 4. 自动化测试与文档

完整测试结果：294 passed in 194.67s。新增 9 项专项回归，覆盖四类收件状态、唯一刷新、完整表格值、打开与复制、缺失文件保护、双击预览、偏好保存、五项健康状态和字体/主题图标。

已同步 README.md、CHANGELOG.md、AGENTS.md、docs/GUI使用说明.md 和 docs/安全与诊断说明.md。

## 5. 敏感信息扫描

已执行 git status、git diff、git diff --cached、git diff --check、git ls-files、敏感文件名、秘密模式、本机绝对路径和项目 secret_scan。项目扫描结果：864 files，0 configured secret markers，PASS。QA 截图、.env、OAuth、数据库、日志、邮件、附件和 build/dist/release 均未被加入跟踪。

## 6. Git、Windows 构建与安装

本报告初稿生成时，提交、正常 push、Windows clean build、packaged smoke、安装器、SHA-256、本机覆盖安装和桌面快捷方式最终验收仍待按顺序执行；完成后将在本报告更新真实结果，不提前宣称通过。

## 7. 剩余风险与阶段结论

源码、UI、测试、文档和秘密扫描无剩余 P0/P1；P2 为正式发布前可选的 Authenticode 签名。当前阶段结论为 CONDITIONALLY PASS，条件是完成 Git push、Windows 构建、本机安装和桌面快捷方式验收。
