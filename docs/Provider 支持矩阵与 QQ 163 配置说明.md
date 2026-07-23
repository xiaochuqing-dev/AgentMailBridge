# Provider 支持矩阵与 QQ/163 配置说明

## 支持矩阵

| Provider | Receive | Send | 认证 | 目录 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| Gmail | Gmail API 或 IMAP | 不支持 | Desktop OAuth `gmail.readonly` 或 Google 应用专用密码 | Gmail API/IMAP 既有能力 | 收件正式支持；发件 planned |
| QQ | IMAP 993/SSL | SMTP 465/SSL | QQ 邮箱生成的授权码 | LIST、SPECIAL-USE | 实现与自动化通过；真实 E2E NOT_TESTED |
| 163 | IMAP 993/SSL | SMTP 465/SSL | 163 邮箱生成的授权码 | LIST、SPECIAL-USE | 实现与自动化通过；真实 E2E NOT_TESTED |
| Generic | 用户配置 IMAP SSL/STARTTLS | 用户配置 SMTP SSL/STARTTLS | 可分别保存 IMAP/SMTP secret | LIST、SPECIAL-USE | implementation ready；服务端兼容性待账号验证 |
| Outlook/Microsoft | 不支持 | 不支持 | 未来 MSAL/PKCE/OAuth | 未实现 | planned |

QQ、163 与 Generic 共用 Generic IMAP/SMTP Core、统一 Mail Package、Mail Facts、调度、重试、历史补扫和 outbound archive。Provider profile 只保存服务器默认值和少量差异，不复制协议代码。

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

## 安全说明

账号凭据不写 SQLite、日志、诊断、报告或 Git。GUI 可选择 QQ、163 或 Generic 发件账号并填写一个明确收件人；MCP `submit_result` 仍使用兼容发件配置并固定发送到 `OWNER_GMAIL`，不接受任意账号或任意收件人参数。

参考：

- QQ 官方授权码说明：https://help.mail.qq.com/detail/106/985
- Thunderbird QQ profile：https://github.com/thunderbird/autoconfig/blob/master/ispdb/qq.com.xml
- Thunderbird 163 profile：https://github.com/thunderbird/autoconfig/blob/master/ispdb/163.com.xml
