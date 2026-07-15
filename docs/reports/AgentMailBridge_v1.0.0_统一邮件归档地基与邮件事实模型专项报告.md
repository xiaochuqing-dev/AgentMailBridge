# AgentMailBridge v1.0.0 统一邮件归档地基与邮件事实模型专项报告

## 1. 开始前 master

开始前本地 `master`、远端跟踪分支和 `origin/master` 一致，基线 commit 为 `38ed223482247e57eb70432656c5ed7feadd0204`，工作树干净。基线完整测试为 325 项通过。

## 2. received_messages / received_files 审计

真实用户库开始时有 `received_messages=10`、`received_files=14`。业务历史、正文文件和附件以旧兼容表及日期平铺目录保存，旧数据没有完整 RFC822 原文。迁移前已创建并校验 SQLite 在线备份。

## 3. 文件平铺根因

旧模型把正文和每个附件分别作为文件事实展示，只有 Message-ID 基础关联，没有一封邮件级的权威对象、资源图、raw、manifest 和统一生命周期，因此正文、附件、图片和链接无法作为同一邮件整体核验。

## 4. 新邮件对象模型

新增 `mail_packages` 和 `mail_resources`。每封邮件一个 package，正文层、CID 图片、附件、链接和下载资源全部通过 `package_id` 归属；兼容表继续服务当前 UI。

## 5. package_id

由规范化 `account_ref + Message-ID` 计算稳定摘要。同一邮件重复扫描、重试或 Gmail API/IMAP 后端切换不会创建第二个 package。自动化和真实重复扫描均验证幂等。

## 6. account_ref / mailbox_ref

当前账号生成稳定 Gmail account_ref；邮箱范围分别记录 Gmail/IMAP mailbox_ref。历史未知账号使用明确 legacy fallback，不把单账号现状固化为永久模型。

## 7. thread_ref

Gmail 使用 provider thread id。IMAP 只在 References/In-Reply-To 可靠时建立会话，否则安全退化，不按主题猜测合并。

## 8. Package directory

正式目录为 `received/mail/YYYY/MM/DD/<package_id>_<subject-slug>`，每封邮件独立。主题、文件名冲突、Unicode、非法字符和 Windows 长路径均受控；显示名与原始名仍保存在数据库和 manifest。

## 9. raw.eml

新邮件必须保存实际取得的原始字节并记录 SHA-256。5 封真实 E2E 邮件的本地 `raw.eml` 均与 Gmail API raw 解码字节逐字节一致。

## 10. Gmail raw 获取

Gmail API 统一收件改为 `messages.get(format=raw)`，base64url 解码后不改变字节写入 package，再由共享 MIME 解析器提取事实。OAuth scope 仍严格为 `gmail.readonly`。

## 11. IMAP raw 获取

IMAP 继续使用 `BODY.PEEK[]`，不改变 Seen 状态；共享解析器直接处理取得的 RFC822 字节。测试覆盖原始字节、编码和重试路径。

## 12. Body plain / html / readable

正文分别保存 plain、HTML 和 readable 层。readable 转换离线执行并忽略 script/style/head/noscript/template；plain+html、HTML only、空正文、异常 charset 和编码正文已有测试。

## 13. Inline image

HTML 中引用的 CID 图片按 Content-ID 归为 inline image，普通图片附件仍归 attachment。真实 D 场景验证 1 个 CID 与 1 个普通图片附件分离且同属一个 package。

## 14. Attachments

附件保存在当前 package 的 `attachments`。真实 B 场景 1 个附件，C 场景 7 个附件，D 场景 1 个普通图片附件；7 个附件没有拆成 7 个邮件对象。重复名、Unicode、长名、0 字节、失败后重试均有自动化覆盖。

## 15. Link detection

plain URL、HTML href 和非 CID img src 离线识别为网页、文件样式链接、云文档或外部图片。真实 E 场景识别 4 个链接事实，没有发出下载请求。

## 16. Trusted domain

可信域保存在 SQLite，默认列表为空；精确域与显式子域规则分开。真实 E2E 保持空列表，下载数为 0。

## 17. Download security

可信下载仅 HTTPS；校验全部 DNS 地址、固定已校验 IP 的 TLS 连接、证书主机名和每次重定向；拒绝 localhost、私网、回环、链路本地、组播、保留、未指定和元数据地址；限制超时、重定向、声明/实际大小、MIME 和文件名，只原子保存到当前 package `downloads`，不执行、不解压。

当前没有合适的真实 HTTPS 测试域，因此真实外部 trusted download 未执行；空白名单无网络访问已在真实 E2E 验证，可信下载安全路径由完整单元/集成测试验证，未伪造外部下载结果。

## 18. Manifest schema

`manifest.json` 包含 schema_version、身份、账号/邮箱/会话、元数据、raw、正文、资源、分类计数、状态、错误和时间。5 个真实 manifest 的本地路径全部为 package 相对路径。

## 19. Atomic package lifecycle

新邮件先写 `.staging/<package_id>.tmp`，文件使用临时文件、flush/fsync 和原子替换，完成后原子提升。partial 保留已成功资源并记录错误，重试复用同一 package，只补写缺失或失败项。

## 20. DB migration

schema 增量新增 package/resource/trusted-domain/migration 表及兼容关联列。真实旧库迁移前在线备份 integrity_check 为 ok，迁移后一致性扫描 0 异常。

## 21. Legacy backfill

10 封历史邮件和 14 个历史文件幂等回填到 legacy package，原松散文件不移动、不删除。二次执行不会重复创建对象。

## 22. Authoritative source / compatibility tables

`mail_packages`/`mail_resources` 是完整邮件事实权威来源；`received_messages` 保留业务历史，`received_files` 保留当前文件管理与 GUI 兼容。兼容行通过 package_id/resource_id 回指权威对象。

## 23. Mail facts query

ApplicationService 新增消息、详情、资源、会话、会话详情和搜索只读接口，支持 account、mailbox、时间、发件人、主题、附件与状态过滤；搜索主题、地址、可读正文、附件名和链接。真实 E2E 前缀查询命中 5 封。

## 24. Hermes / Obsidian 边界

未实现 Hermes、Obsidian、知识库写入、摘要生成或 Agent 编排。事实层只读，不执行资源或修改邮箱。

## 25. Route B future compatibility

本轮未进入多邮箱 Route B。account/mailbox/thread 已保留未来兼容身份，不增加多租户、任意账号路由或新 GUI 入口。

## 26. Consistency scan

扫描新增 package/目录、manifest、raw、正文和资源存在性、相对路径越界、大小/Hash、孤立目录和残留 staging 检查。真实迁移和真实 E2E 后均为 0 异常。

## 27. Automated tests

保留全部旧测试并新增 package、raw、body、CID、附件、链接、SSRF、redirect、大小/MIME、manifest、partial/retry、迁移和事实查询测试。最终源码完整 pytest：352 passed。

## 28. Real E2E

通过真实 QQ SMTP 向固定 OWNER_GMAIL 发送 A-E 五封邮件：纯正文、单附件、7 附件、HTML+CID+图片附件、网页/直接文件/云文档链接。不点击“立即收取”，由自动调度发现。普通 inbox 查询自动处理前 4 封；链接邮件被 Gmail 标为 spam，随后使用仅针对该测试主题的进程级临时全邮箱查询由自动调度处理，没有修改永久配置或导入其他垃圾邮件。

结果：5 个 ready package、5 个真实 raw 字节匹配、5 个 manifest、9 个普通附件、1 个 CID、4 个链接、0 个下载、22 个本地资源 Hash 通过、最长路径 178 字符、重试行 0。再次自动扫描仍为 5 个 package，证明真实幂等。

## 29. UI regression screenshots

使用真实数据打开收件、发件、历史记录、文件与数据、设置、关于和 Agent/MCP，均无崩溃、空白、布局变化或发件页错位。浅色全页和深色关键页截图保存在 gitignored `qa-artifacts/mail-archive-phase1`，未进入 Git。

## 30. User UI approval

用户于 2026-07-16 明确回复“gui通过了”，UI 硬门禁已解除。

## 31. Documentation

已同步 README、CHANGELOG、AGENTS、GUI、安全与诊断、Windows 安装升级说明，并新增统一邮件归档设计和邮件事实查询说明。

## 32. Secret scan

提交前扫描通过：26 个变更/新增文件中未发现真实账号、用户绝对路径、OAuth token、非测试 secret 赋值或用户样本；Git 跟踪文件中没有 `.env`、credentials/token、数据库、raw.eml、manifest 用户样本、日志或 QA 图片。现有 dist/release 扫描 864 个文件通过；clean build 后还会对新产物重新扫描。

## 33. Git commit / push

待执行；不得 force push、改写历史或自动创建 GitHub Release。

## 34. Clean build

待执行。

## 35. Install

待执行。

## 36. Shortcut

待执行。

## 37. Installer / ZIP SHA-256

待执行。

## 38. Remaining P0 / P1 / P2

- P0：无。
- P1：发布收口完成前暂不判定。
- P2：真实外部 trusted download 因无合适安全测试域未执行，已按要求由安全单元/集成测试覆盖；邮件级 GUI 与 Route B 属于明确后续范围。

## 39. Gate

当前为 CONDITIONALLY PASS：实现、迁移、测试、真实 E2E 与用户 UI 审批已完成，等待敏感信息扫描、正常 commit/push、clean build、本机安装、快捷方式和打包产物验收后更新最终结论。
