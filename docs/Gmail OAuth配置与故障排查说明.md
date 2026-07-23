# Gmail OAuth 配置与故障排查说明

AgentMailBridge 1.4.1 继续只接受 Google Cloud 创建的 Desktop app OAuth Client。Web application JSON 不能用于本地回环授权。每个 Gmail 的 credentials 与 Token 位于独立 `account_id` 目录；导入、授权、重新验证和清除 Token 只作用于所选账号，不把 Token 或 Client Secret 写入账号表。

Gmail scope 必须且只能是 `gmail.readonly`。官方 Gmail API 发件需要 `gmail.send` 或 `gmail.compose`，因此 v1.4.1 没有上线 Gmail send，也不会为了发件静默扩大 scope 或要求现有用户重新授权。

## 正确配置

1. 在 Google Cloud 项目中启用 Gmail API。
2. 创建 OAuth Client ID，应用类型选择 Desktop app。
3. 下载原始 `credentials.json`，不要手工修改 Client Secret 或 redirect URI。
4. 在 Gmail 账号页选择该 JSON。界面应显示 Desktop app、项目 ID（如存在）和 Client ID 脱敏尾号。
5. 点击“开始 Gmail OAuth 授权”，由用户在系统浏览器中选择账号并处理 Google 页面。

OAuth 应用“已发布”不等于“已通过 Google 验证”。AgentMailBridge 不会自动绕过“未经验证”提示，也不会操作密码、验证码或 2FA。

## 授权过程

授权在后台 Worker 中执行，GUI 始终可以移动、最小化、切换页面和重绘。本地回调先绑定 `127.0.0.1` 随机端口，再生成授权链接并打开浏览器。默认等待 5 分钟。

等待期间可执行：

- 取消授权：关闭回环服务器，不保存新 Token。
- 重新打开浏览器：继续使用同一会话、同一 state 和同一授权链接。
- 复制授权链接：只写入当前剪贴板，不进入日志、配置或数据库。

成功回调后依次执行 state 校验、Token 交换、refresh token 检查和 Gmail Profile 验证。授权账号与当前配置 Gmail 不匹配时，不会覆盖旧 Token。

## 已授权但 API 待验证

Gmail API 未启用、临时网络错误、配额限制或 5xx 可能发生在 Token 已取得之后。此时界面显示“已取得授权，API 待重新验证”，有效 Token 会安全保留。启用 Gmail API或恢复网络后点击“重新验证 Gmail API”，无需再次打开浏览器。

## 代理与防火墙

请确保以下本地回环地址不经过代理：

- `127.0.0.1`
- `localhost`
- `::1`

AgentMailBridge 会在当前 OAuth 子流程中合并这些 NO_PROXY 项，不覆盖原有代理配置，也不会关闭系统代理或防火墙。浏览器长时间没有回调时，检查是否仍停留在 Google 页面、浏览器已关闭、本地回环被代理、安全软件拦截或防火墙禁止访问。

## 常见错误码

- `credentials_wrong_type`：选择了 Web application 或混合类型 JSON。
- `credentials_invalid_endpoint`：凭据包含非 Google 官方端点或非法 redirect URI。
- `callback_bind_failed`：无法绑定 127.0.0.1 随机端口。
- `browser_open_failed`：默认浏览器未能自动打开，可复制授权链接。
- `oauth_timeout`：5 分钟内没有收到回调。
- `access_denied`：用户未同意授权或组织策略阻止。
- `oauth_state_mismatch`：回调来自旧会话或错误页面。
- `redirect_uri_mismatch`：通常是使用了 Web application 凭据。
- `refresh_token_missing`：Google 未返回长期授权凭据，需要重新同意。
- `token_client_mismatch`：credentials 已更换，旧 Token 属于另一个 Client ID。
- `gmail_api_disabled`：OAuth 已完成，但项目未启用 Gmail API。
- `insufficient_scope`：Token 不包含唯一允许的 `gmail.readonly`。
- `account_mismatch`：授权账号与当前配置 Gmail 不一致。
- `oauth_lock_busy`：另一 AgentMailBridge 进程正在使用 OAuth 文件。

## 本地安全

credentials 和 Token 位于当前用户 OAuth 目录。文件替换采用同目录临时文件、落盘和原子替换；失败时旧文件保留。日志和诊断不得包含 Client Secret、授权 URL、state、authorization code、access token、refresh token、完整回调 URL或含密码的代理 URL。
