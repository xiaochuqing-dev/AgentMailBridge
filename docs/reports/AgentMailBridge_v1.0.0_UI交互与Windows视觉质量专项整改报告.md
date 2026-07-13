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
- Windows 字体与 DPI：集中定义 11 类 Typography Token；注册 Microsoft YaHei UI Regular/Bold，字重收敛为 400/700，Qt 6 保持 PassThrough 高 DPI 策略；旧窗口几何限制到当前屏幕可用区。收件页取消会截获滚轮的嵌套外层滚动，压缩非核心间距，使今日文件与最近日志在紧凑高度同屏显示并各自内部滚动；右侧面板保留独立滚动。

## 3. UI QA

实际运行源码 GUI 成功，主窗口标题正确。使用真实 Qt 渲染分别检查 100%、125%、150%、150% 紧凑窗口和深色主题，并单独检查文件操作列、收件偏好、连接健康和主题按钮。结果均通过：中文无缺字、字重稳定、无重叠和省略号生成，表格可横向滚动至完整路径与操作列；在 150%、1240×680 窗口下，今日文件与最近日志标题及表格均直接可见，无中央外层滚动。截图保存于 gitignored 的 artifacts/gui-qa-20260713，不进入 Git。

## 4. 自动化测试与文档

完整测试结果：295 passed in 210.75s。新增 10 项专项回归，覆盖四类收件状态、唯一刷新、完整表格值、打开与复制、缺失文件保护、双击预览、偏好保存、五项健康状态、字体/主题图标，以及高 DPI 屏幕边界、收件页无外层嵌套滚动和最近日志可见性。

已同步 README.md、CHANGELOG.md、AGENTS.md、docs/GUI使用说明.md 和 docs/安全与诊断说明.md。

## 5. 敏感信息扫描

已执行 git status、git diff、git diff --cached、git diff --check、git ls-files、敏感文件名、秘密模式、本机绝对路径和项目 secret_scan。项目扫描结果：864 files，0 configured secret markers，PASS。QA 截图、.env、OAuth、数据库、日志、邮件、附件和 build/dist/release 均未被加入跟踪。

## 6. Git、Windows 构建与安装

功能提交 8ee3f27 和高 DPI 修复提交 710ef41 已正常 push 至 origin/master，未 force push、未改写历史、未创建 GitHub Release。

最终源码执行 clean build，GUI packaged self-test、内部 MCP smoke、双 EXE 构建验证、安装器编译和构建产物秘密扫描均通过。最终产物为：

- 安装器：release/AgentMailBridge-1.0.0-Setup.exe
- 安装器 SHA-256：0c97412177dbecbcc3ffd39274d804e8995a3fe6615bdd8549c670817bef7302
- Portable ZIP：release/AgentMailBridge-1.0.0-Windows-x64.zip
- ZIP SHA-256：47b503bf0158c1fb3bf5d9f406efeb4a5354cb6b5519de0af43d8ca9416105fb

Windows Defender 自定义扫描未发现威胁。安装器、GUI EXE 和 MCP EXE 的 Authenticode 状态均为 NotSigned，未伪造签名。

最终安装器静默覆盖安装成功，安装前后用户数据文件数量和总字节数一致；已安装 GUI/MCP 文件 SHA-256 与最终 dist 完全一致，版本均为 1.0.0。桌面快捷方式指向正式安装目录的 AgentMailBridge.exe，实际启动进程路径一致。144 DPI 下窗口为 1240×680，标题、刷新、搜索、完整表格、编辑偏好和连接健康均为新界面；第二次启动仍只有一个进程，关闭主窗口后托盘进程保持运行。

## 7. 剩余风险与阶段结论

剩余 P0：无。剩余 P1：无。P2：正式公开发布前可选购代码签名证书并签署安装器与双 EXE。

最终结论：PASS。具体 UI、状态语义、测试、秘密扫描、正常 push、Windows clean build、本机覆盖安装和桌面快捷方式验收均已完成。
