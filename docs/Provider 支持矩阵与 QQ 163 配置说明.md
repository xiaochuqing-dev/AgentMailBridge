# Provider 支持矩阵与 QQ/163 配置说明

## 支持矩阵

| Provider | Auth | Login | Folder | Receive | Incremental | Send | Attachment | Restart | Error | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Gmail | Desktop OAuth `gmail.readonly` 或应用专用密码 | 收件已验证 | Gmail API/IMAP 已实现 | 正式支持 | 已实现并长期回归 | planned | 收件已支持 | 已支持 | 已支持 | receive supported |
| QQ | 授权码 | NOT_TESTED | LIST/SPECIAL-USE 已实现，真实名称 NOT_TESTED | implementation ready | 自动化通过，真实 NOT_TESTED | implementation ready | 自动化通过，真实 NOT_TESTED | 自动化通过，真实 NOT_TESTED | 自动化通过，真实 NOT_TESTED | `implementation_ready_e2e_required` |
| 163 | 授权码 | NOT_TESTED | LIST/SPECIAL-USE 已实现，真实名称 NOT_TESTED | implementation ready | 自动化通过，真实 NOT_TESTED | implementation ready | 自动化通过，真实 NOT_TESTED | 自动化通过，真实 NOT_TESTED | 自动化通过，真实 NOT_TESTED | `implementation_ready_e2e_required` |
| Generic-Test | 账号级 IMAP/SMTP secret | NOT_TESTED | LIST/SPECIAL-USE 已实现 | implementation ready | 自动化通过，真实 NOT_TESTED | implementation ready | 自动化通过，真实 NOT_TESTED | 自动化通过，真实 NOT_TESTED | 自动化通过，真实 NOT_TESTED | `implementation_ready_e2e_required` |
| Outlook/Microsoft | 未来 MSAL/PKCE/OAuth | 未实现 | 未实现 | planned | planned | planned | planned | planned | planned | planned |

QQ、163 与 Generic 共用 Generic IMAP/SMTP Core、统一 Mail Package、Mail Facts、调度、重试、历史补扫和 outbound archive。v1.4.3 把 IMAP 重试身份收紧为 mailbox、UIDVALIDITY、UID，统一解码国际化目录，并把协议错误转换为不含服务端敏感原文的稳定分类。Provider profile 只保存服务器默认值和少量差异，不复制协议代码。

## QQ 配置

1. 在 QQ 邮箱网页设置中启用 IMAP/SMTP 服务并生成授权码。第三方客户端密码填写授权码，不填写 QQ 登录密码。
2. 在“添加邮箱账号”选择 QQ，填写完整 `@qq.com` 地址和授权码。
3. 保存后先执行“测试连接”，再执行“发现目录”。连接测试只认证和读取目录，不收件、不发件。
4. 连接通过后，可在收件页选择该账号立即收取或历史补扫，也可在发件页选择该账号发送一封 GUI 手工邮件。

默认 profile：

- IMAP：`imap.qq.com`，993，SSL/TLS
- SMTP：`smtp.qq.com`，465，SSL/TLS
- 用户名：完整 QQ 邮箱地址
- 收件目录：INBOX

## 163 配置

1. 在 163 邮箱网页设置中确认 IMAP/SMTP 已开启，并为第三方客户端生成授权码。不要把网页登录密码写入应用。
2. 在“添加邮箱账号”选择 163，填写完整 `@163.com` 地址和授权码。
3. 保存后先执行“测试连接”和“发现目录”。只有真实连接通过后，才启用自动收件和正式发件。

默认 profile：

- IMAP：`imap.163.com`，993，SSL/TLS
- SMTP：`smtp.163.com`，465，SSL/TLS
- 用户名：完整 163 邮箱地址
- 收件目录：INBOX

本次环境无法可靠取得 163 个人邮箱官方帮助页，也没有独立 163 测试账号。以上服务器默认值来自 Thunderbird ISPDB；因此 163 真实登录、目录、收信、发信和回收验证保持 NOT_TESTED。

## Generic 配置

Generic 至少配置 IMAP 或 SMTP 之一。端口必须为 1 至 65535，传输只允许 SSL/TLS 或 STARTTLS，plain 会被拒绝。IMAP 与 SMTP 可使用不同 secret，均按 account_id 存入 Windows Credential Manager。

服务器连接通过并不代表所有邮件行为均兼容。启用自动收件前应验证中文主题、HTML、附件、重复同步、目录和历史补扫；正式发件前应验证收件人拒绝、附件大小和 Sent 行为。AgentMailBridge 只保留本地 outbound/sent archive，不会擅自在远端 Sent 目录追加副本。

## 真实验收方法

真实网络验收必须使用独立测试账号。脚本只从现有账号和 Windows Credential Manager 读取凭据，不接受命令行密码，也不输出邮箱地址、正文、目录名或服务端原始错误。无显式网络确认时拒绝运行；无显式真实发件确认时只执行登录、目录和两轮收件：

```powershell
python scripts\provider_validation.py --account-id <account_id> --confirm-network --output evidence.json
python scripts\provider_validation.py --account-id <account_id> --confirm-network --confirm-real-send --output evidence.json
```

只有证据同时覆盖 login、folder、receive、incremental、send、attachment、receive-back、restart/reconnect 和核心错误路径，且没有 P0/P1，才能人工复核并升级正式支持状态。当前仓库没有安全测试账号，QQ、163、Generic-Test 均保持 NOT_TESTED。

## 安全说明

账号凭据不写 SQLite、日志、诊断、报告或 Git。GUI 可选择 QQ、163 或 Generic 发件账号并填写一个明确收件人；MCP `submit_result` 仍使用兼容发件配置并固定发送到 `OWNER_GMAIL`，不接受任意账号或任意收件人参数。

参考：

- QQ 官方授权码说明：https://help.mail.qq.com/detail/106/985
- Thunderbird QQ profile：https://github.com/thunderbird/autoconfig/blob/master/ispdb/qq.com.xml
- Thunderbird 163 profile：https://github.com/thunderbird/autoconfig/blob/master/ispdb/163.com.xml
