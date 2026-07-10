# Agent Mail Bridge

面向 AI Agent 工作流的本地邮箱桥接工具。

本仓库已完成：
- **第一批次收口**：统一应用服务、结构化结果、跨后端去重和最小 GUI 接入骨架。
- **第二批次正式 GUI**：使用 PySide6 实现三栏桌面界面，继续复用 ApplicationService，不改写邮件与数据核心。
- **第二阶段**：Gmail IMAP 网络适配层（direct / socks5 / auto）+ 连接诊断命令，解决国内复杂网络环境下的 Gmail IMAP 连接问题。

---

## 一、项目用途

把“邮箱”变成 AI Agent 工作流中稳定可靠的数据通道：

1. **收**：自动收取 Gmail 中“用户自己发给自己的邮件”，把正文和附件按日期归档到本地。
2. **发**：以 QQ 邮箱作为发件身份，把用户指定的本地文件发送回用户 Gmail。
3. **留档**：所有收到和发出的文件都在本地留档，用 SQLite 记录 hash、路径与状态，避免重复收取/重复发送，并能在文件被改名/删除/修改时发现异常。

典型用法：在 ChatGPT 里让 ChatGPT 把方案 / Prompt / 任务说明发到你自己的 Gmail → 本工具收取归档 → Agent 读取本地文件处理 → 通过本工具把结果发回 Gmail。

---

## 二、为什么不直接让 Agent 登录邮箱

- **安全**：不把邮箱应用密码 / 授权码交给 Agent，Agent 不直接接触邮箱密钥。
- **可控**：所有文件落盘留档，可审计、可复核。
- **稳定**：收件去重、发送留档，避免 Agent 重复处理或丢失结果。
- **解耦**：Agent 只读写本地文件，邮箱连接由本工具专门负责。

---

## 三、环境要求

- Python 3.11+（已在 3.12 验证）
- Gmail API 后端需 Google 客户端库，正式桌面界面需 PySide6

安装依赖：

```bash
pip install -r requirements.txt
```

依赖包含：
- `python-dotenv` -- 读取 `.env`
- `PySocks` -- Gmail IMAP SOCKS5 网络适配层（remote DNS）
- `google-api-python-client` / `google-auth-httplib2` / `google-auth-oauthlib` -- Gmail API 收件后端（HTTPS 443）
- `PySide6` -- 正式桌面界面、现代控件、表格和高分屏支持

---

## 四、配置 `.env`

复制 `.env.example` 为 `.env`，填入真实值：

```bash
cp .env.example .env
```

### 1. Gmail 应用专用密码

`GMAIL_APP_PASSWORD` 是 **16 位应用专用密码**，不是 Gmail 登录密码。

获取方式：

1. 访问 https://myaccount.google.com/apppasswords
2. 需要先为账号开启两步验证（2FA）。
3. 生成一个“邮件”用途的应用专用密码，得到 16 位字符串。
4. 填入 `.env` 的 `GMAIL_APP_PASSWORD`。

### 2. QQ 邮箱授权码

`QQ_AUTH_CODE` 是 **QQ 邮箱授权码**，不是 QQ 登录密码。

获取方式：

1. 登录 QQ 邮箱网页版。
2. 进入「设置」→「账户」。
3. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」，开启 IMAP/SMTP 服务。
4. 按提示用手机发短信验证，生成授权码（16 位）。
5. 填入 `.env` 的 `QQ_AUTH_CODE`。

> ⚠️ `.env` 已被 `.gitignore` 忽略，**绝不会**进入版本库。代码中也不包含任何真实密码。

### 3. 关键字段说明

| 字段 | 含义 |
|------|------|
| `GMAIL_ADDRESS` | 你的 Gmail 地址（收件邮箱） |
| `GMAIL_APP_PASSWORD` | Gmail 应用专用密码（16 位，仅 IMAP 后端需要） |
| `GMAIL_RECEIVE_BACKEND` | 收件后端：`imap` / `gmail_api` / `auto` |
| `GMAIL_API_CREDENTIALS_PATH` | Gmail API OAuth credentials.json 路径 |
| `GMAIL_API_TOKEN_PATH` | Gmail API OAuth token.json 路径（授权后自动生成） |
| `GMAIL_API_SCOPES` | Gmail API 权限 scope，默认只读 |
| `GMAIL_API_MAX_RESULTS` | Gmail API 单次最多抓取数，默认 20 |
| `GMAIL_API_QUERY` | Gmail API 查询条件，默认 `in:inbox` |
| `QQ_EMAIL` | 你的 QQ 邮箱（发件身份） |
| `QQ_AUTH_CODE` | QQ 邮箱授权码（16 位） |
| `OWNER_GMAIL` | 发件时固定的收件人（应为你的 Gmail） |
| `DATA_ROOT` | 本地数据根目录，默认 `./AgentMailBridgeData` |
| `MAX_FETCH_LIMIT` | 单次最多抓取邮件数，默认 30（IMAP 后端） |
| `MAX_ATTACHMENT_MB` | 附件大小上限，默认 25MB |

---

## 五、初始化

```bash
python -m agent_mail_bridge init
```

创建目录与数据库：

```
AgentMailBridgeData/
├── received/      收到的邮件归档
├── send/          准备发送的文件副本
├── sent/          发送成功后的副本
├── logs/app.log   日志
└── agent_mail_bridge.db   SQLite 数据库
```

---

## 六、手动收取邮件

```bash
python -m agent_mail_bridge receive
```

可选参数：

```bash
python -m agent_mail_bridge receive --limit 30      # 最多抓取数量
python -m agent_mail_bridge receive --unseen-only   # 只收未读
python -m agent_mail_bridge receive --mark-seen     # 收取后标记已读
```

收件规则（本阶段核心）：

- 只扫描 **Inbox**（不扫垃圾箱 / 已发送 / 全部邮件）。
- 只收取 `from == 用户Gmail` 且 `to 包含用户Gmail` 的自发自收邮件。
- 按 `message_id` 去重，重复执行不会重复保存。
- 邮件正文保存为 `.md`（顶部附加来源元信息），附件保存到 `attachments/`。

---

## 七、发送本地文件到 Gmail

```bash
python -m agent_mail_bridge send --file "./result.md"
```

可选主题：

```bash
python -m agent_mail_bridge send --file "./result.md" --subject "ProjectFlow 执行结果"
```

流程：

1. 校验文件存在、大小合理、扩展名非危险类型。
2. 复制到 `send/YYYY-MM-DD/`。
3. 用 QQ 邮箱发送到 `OWNER_GMAIL`（收件人固定，不可任意指定）。
4. 发送成功后复制到 `sent/YYYY-MM-DD/`。
5. 写入 `sent_files` 表与日志。

> 发送失败不会崩溃，会输出明确错误（如 SMTP 认证失败会提示检查授权码）。

---

## 八、查询与状态扫描

### 列出某天收到的文件

```bash
python -m agent_mail_bridge list-received --date today
python -m agent_mail_bridge list-received --date 2026-07-09
python -m agent_mail_bridge list-received --date yesterday
```

### 列出某天发送的文件

```bash
python -m agent_mail_bridge list-sent --date today
```

### 扫描文件状态

```bash
python -m agent_mail_bridge scan-status
```

检测本地文件是否被删除 / 修改 / 改名：

- 文件不存在 → `missing`
- 文件 hash 改变 → `modified`
- 同目录下找到 hash 相同但文件名不同的文件 → `renamed`（并更新路径）

> 不会自动删除数据库记录，不会覆盖用户改过的文件。

### 查看当前配置（脱敏）

```bash
python -m agent_mail_bridge show-config
```

---

## 九、本地目录结构

```
AgentMailBridgeData/
├── received/
│   └── 2026-07-09/
│       ├── 01-31-22_Agent Mail Bridge 测试邮件.md
│       └── attachments/
│           ├── 01-31-22_界面截图.png
│           └── 01-31-22_接口文档.pdf
├── send/
│   └── 2026-07-09/
│       └── 22-30-15_Agent执行结果.md
├── sent/
│   └── 2026-07-09/
│       └── 22-30-15_Agent执行结果.md
├── logs/
│   └── app.log
└── agent_mail_bridge.db
```

规则：

- 每天一个文件夹。
- 收到的邮件只在当天 `received/YYYY-MM-DD/`。
- 不会生成 `latest.md`。
- 工具不自动删除旧日期文件夹，删除权归用户。

---

## 十、安全注意事项

1. ✅ Gmail 应用密码、QQ 授权码**不写入代码**，仅通过 `.env` 读取。
2. ✅ `.env` 被 `.gitignore` 忽略。
3. ✅ 不自动执行任何收到的附件。
4. ✅ 不自动解压 zip 附件。
5. ✅ 发件收件人固定为 `OWNER_GMAIL`，不可任意指定。
6. ✅ 不删除用户文件，不自动删除历史日期文件夹。
7. ✅ 日志中不打印完整密码 / 授权码（`show-config` 输出已脱敏）。
8. ✅ 配置缺失时给出明确错误提示。
9. ✅ IMAP / SMTP 登录失败时，提示检查应用专用密码 / 授权码，而非普通密码。
10. ✅ 危险扩展名（`.exe`/`.bat`/`.cmd`/`.ps1`/`.sh`/`.msi` 等）仅记录 warning，不执行。

---

## 十一、收件后端说明（IMAP / Gmail API）

本工具支持两种 Gmail 收件后端，通过 `GMAIL_RECEIVE_BACKEND` 切换：

| 后端 | 协议/端口 | 认证方式 | 适用场景 |
|------|-----------|----------|----------|
| `imap` | IMAP/993 | Gmail 应用专用密码 | 网络支持 993 端口、国外用户、代理放行邮件端口 |
| `gmail_api` | HTTPS/443 | OAuth（credentials.json + token.json） | Gmail 网页能开但 993/465/587 不通 |
| `auto` | -- | -- | 优先 `gmail_api`（已配置 credentials.json），否则回退 `imap` |

> 当前用户网络已确认：HTTPS 443 可用，IMAP 993 / SMTP 465/587 不可用。
> 因此推荐使用 `gmail_api` 模式（已设为默认）。

### Gmail API 模式（推荐当前用户）

适合：
- Gmail 网页能打开，但 IMAP 993 / SMTP 465/587 不通的用户；
- 只想通过 HTTPS 443 读取 Gmail 的用户。

需要：
- Google Cloud 项目；
- 启用 Gmail API；
- OAuth Desktop App Client；
- `credentials.json`（从 Google Cloud Console 下载）；
- 首次运行浏览器授权生成 `token.json`。

```env
GMAIL_RECEIVE_BACKEND=gmail_api
GMAIL_API_CREDENTIALS_PATH=secrets/credentials.json
GMAIL_API_TOKEN_PATH=secrets/token.json
GMAIL_API_SCOPES=https://www.googleapis.com/auth/gmail.readonly
GMAIL_API_MAX_RESULTS=20
GMAIL_API_QUERY=in:inbox
```

首次授权：

```bash
python -m agent_mail_bridge gmail-api-auth
```

会打开浏览器，登录 Google 并授权。授权完成后生成 `secrets/token.json`，后续自动刷新，无需重复授权。

诊断：

```bash
python -m agent_mail_bridge diagnose-gmail-api
```

### IMAP 模式

适合：
- 国外用户；
- 网络支持 `imap.gmail.com:993` 的用户；
- 支持邮件端口的代理用户。

需要：
- Gmail 地址；
- Gmail 16 位应用专用密码；
- Gmail IMAP 开启；
- 网络能访问 IMAP 993。

```env
GMAIL_RECEIVE_BACKEND=imap
GMAIL_APP_PASSWORD=你的16位应用专用密码
```

IMAP 模式的网络适配层（direct / socks5 / auto）见下文。

### 安全说明

- Gmail API 不使用 Gmail 密码，也不使用 16 位应用专用密码。
- `token.json` 是授权令牌，仍是敏感文件，不能提交 GitHub。
- `credentials.json` 和 `token.json` 都不能提交 GitHub（`.gitignore` 已覆盖）。
- 当前推荐只用 `gmail.readonly` 权限：只读权限不能发邮件、不能删邮件、不能标记已读。
- QQ 发件仍然使用 QQ SMTP 授权码，不受 Gmail 后端切换影响。

---

## 十二、Gmail IMAP 网络模式说明（IMAP 适配层）

> 本节仅适用于 `GMAIL_RECEIVE_BACKEND=imap`（或 auto 回退到 imap）的情况。
> Gmail API 后端走 HTTPS 443，不涉及 IMAP 端口，无需以下配置。

Gmail IMAP 在不同网络环境下连接方式不同。本工具通过 `GMAIL_NETWORK_MODE` 支持三种模式，**不提供代理、不绕过地区限制**，只支持连接用户本机已有的代理。

### 1. `direct` 模式

适合：
- 国外用户；
- 系统 VPN / TUN 已经能正确接管所有流量的用户；
- 用户的网络可以直接访问 Gmail IMAP。

```env
GMAIL_NETWORK_MODE=direct
```

### 2. `socks5` 模式

适合国内用户，使用 v2rayN / Clash Verge / NekoRay / sing-box / Clash for Windows 等提供本地 SOCKS5 端口的代理客户端。

```env
GMAIL_NETWORK_MODE=socks5
GMAIL_SOCKS5_HOST=127.0.0.1
GMAIL_SOCKS5_PORT=10808
GMAIL_SOCKS5_REMOTE_DNS=true
```

说明：
- 不同代理软件端口不同，**必须看自己的客户端设置**。
- v2rayN 常见 SOCKS5 端口：`10808`。
- Clash Verge 可能是 `mixed-port` 或 `socks-port`，不一定是 10808。
- NekoRay / sing-box 也需查看本地入站端口。
- `GMAIL_SOCKS5_REMOTE_DNS=true` 建议开启，把 `imap.gmail.com` 交给代理端解析，避免本机 DNS 污染/泄露。

### 3. `auto` 模式（默认，推荐新手）

```env
GMAIL_NETWORK_MODE=auto
```

逻辑：先尝试 direct；direct 失败且配置了 SOCKS5 则尝试 socks5；输出每一步失败原因。auto **不会吞掉错误**，保留 direct 与 socks5 两次失败原因。

### QQ SMTP

QQ 邮箱是国内服务，默认保持 direct，不与 Gmail 绑死：

```env
QQ_SMTP_NETWORK_MODE=direct
```

本阶段仅实现 QQ direct；`QQ_SMTP_SOCKS5_*` 配置键已预留但暂未实现连接。

---

## 十三、诊断命令

### Gmail API 授权

首次使用 Gmail API 后端前，需完成 OAuth 授权：

```bash
python -m agent_mail_bridge gmail-api-auth
```

- 首次运行打开浏览器，登录 Google 并授权；
- 授权成功生成 `secrets/token.json`，后续自动刷新；
- 已授权时提示已授权，token 过期时自动刷新；
- **不输出 token / credentials 内容**。

### 诊断 Gmail API 收件后端

```bash
python -m agent_mail_bridge diagnose-gmail-api
```

分步输出 6 项检查：

```text
[1] credentials.json：存在且合法
[2] token.json：有效 / 过期可刷新 / 不存在 / 无效
[3] Gmail API service 创建
[4] Gmail profile 获取（授权账号、总邮件数）
[5] messages.list 测试
[6] messages.get 测试（若有邮件）
```

诊断命令**不修改邮件、不删除邮件、不标记已读**，**不输出 token 内容**。

### 诊断 Gmail IMAP 连接（分步骤输出）

> 仅 `GMAIL_RECEIVE_BACKEND=imap`（或 auto 回退 imap）时相关。

```bash
python -m agent_mail_bridge diagnose-gmail
```

按配置模式分步输出：配置读取 -> 端口/direct -> TLS -> 登录 -> 结论。失败时给出可能原因与建议，**不输出真实密码**。

### 诊断整体网络环境

```bash
python -m agent_mail_bridge diagnose-network
```

输出 6 项检查：

```text
[1] Python 环境
[2] PySocks 是否安装
[3] Direct 连接 imap.gmail.com:993
[4] SOCKS5 端口 127.0.0.1:10808
[5] SOCKS5 remote DNS 连接 imap.gmail.com:993
[6] QQ SMTP direct 连接 smtp.qq.com:465
```

诊断命令**不发送真实邮件**；Gmail 登录测试只做 login + logout，不读取/修改邮件；QQ SMTP 只测 TCP/TLS 不登录。

### 错误分类

| 错误码 | 含义 |
|--------|------|
| `CONFIG_ERROR` | 网络配置缺失或非法 |
| `PROXY_PORT_UNAVAILABLE` | 本地 SOCKS5 端口不可达 |
| `DIRECT_CONNECT_FAILED` | direct 连接 Gmail 失败 |
| `SOCKS5_CONNECT_FAILED` | 通过 SOCKS5 连接 Gmail 失败 |
| `TLS_HANDSHAKE_FAILED` | TLS 握手失败 |
| `GMAIL_AUTH_FAILED` | Gmail 登录认证失败 |
| `GMAIL_IMAP_DISABLED_OR_REJECTED` | IMAP 被禁用或登录被拒 |
| `TIMEOUT` | 连接超时 |
| `UNKNOWN_NETWORK_ERROR` | 未知网络错误 |

### 各类用户配置示例

**IMAP + v2rayN 用户（国内）：**
```env
GMAIL_RECEIVE_BACKEND=imap
GMAIL_NETWORK_MODE=socks5
GMAIL_SOCKS5_HOST=127.0.0.1
GMAIL_SOCKS5_PORT=10808
GMAIL_SOCKS5_REMOTE_DNS=true
```

**IMAP + Clash Verge 用户（国内）：** 端口需到客户端设置里确认 `mixed-port` / `socks-port`：
```env
GMAIL_RECEIVE_BACKEND=imap
GMAIL_NETWORK_MODE=socks5
GMAIL_SOCKS5_HOST=127.0.0.1
GMAIL_SOCKS5_PORT=7897     # 以实际端口为准
GMAIL_SOCKS5_REMOTE_DNS=true
```

**Gmail API 用户（IMAP 端口不通，推荐当前用户）：**
```env
GMAIL_RECEIVE_BACKEND=gmail_api
GMAIL_API_CREDENTIALS_PATH=secrets/credentials.json
GMAIL_API_TOKEN_PATH=secrets/token.json
GMAIL_API_SCOPES=https://www.googleapis.com/auth/gmail.readonly
```

**国外直连用户：**
```env
GMAIL_RECEIVE_BACKEND=imap
GMAIL_NETWORK_MODE=direct
```

---

## 十四、测试

```bash
pip install pytest
pytest
```

覆盖：文件名清洗、数据库增查去重（含 Gmail API 迁移列）、存储目录与路径生成、
文件状态扫描、网络适配层配置解析、连接工厂（direct/socks5/auto）、诊断命令（含脱敏校验）、
Gmail API 配置解析（backend 切换 / 非法值 / 默认值）、OAuth 授权（mock：token 有效/过期刷新/首次授权/credentials 缺失）、
Gmail API 收件（mock：list 空/多封、text/plain、multipart、附件、RFC Message-ID 去重、gmail_api:id 去重、自发自收过滤）。

---

## 十五、模块结构

```
agent_mail_bridge/
├── __init__.py
├── __main__.py        python -m 入口
├── cli.py             命令行命令（含 gmail-api-auth / diagnose-gmail-api）
├── config.py          配置加载与校验（含收件后端切换 / 网络模式）
├── network.py         IMAP 网络适配层（direct/socks5/auto + 诊断原语）
├── diagnose.py        diagnose-gmail / diagnose-gmail-api / diagnose-network 逻辑
├── gmail_api_auth.py  Gmail API OAuth 授权 / token 加载刷新 / service 创建
├── gmail_api_receive.py Gmail API 收件（正文/附件/去重/保存/写库）
├── database.py        SQLite 4 表 + 增查改 + 向后兼容迁移
├── mail_receive.py    收件协调入口（按 backend 分发 IMAP / Gmail API）
├── mail_send.py       QQ SMTP 发件
├── storage.py         日期归档 / 文件复制 / 路径生成
├── file_index.py      hash / scan_file_status
├── logging_setup.py   日志配置
├── security.py        危险扩展名 / 路径越权 / 大小校验
├── gui.py             PySide6 正式界面启动入口
├── ui/                正式界面主题、组件、主窗口和配置保存
└── utils.py           文件名清洗 / 时间 / sha256 / 邮件头解码
```

核心函数均为后续 GUI / MCP 调用保留接口，例如：

- `receive_mails(cfg, ...)` -- 收件（自动按 backend 分发 IMAP / Gmail API）
- `receive_gmail_api_messages(cfg, service, limit=...)` -- Gmail API 后端收件
- `get_gmail_api_service(cfg)` -- Gmail API OAuth service 创建
- `send_file_to_owner_gmail(file_path, subject, cfg)` -- 发件
- `list_received_files_for_date(cfg, date_str)` -- 列出今日文件
- `scan_file_status(cfg)` -- 文件状态扫描
- `create_gmail_imap_client(cfg)` -- IMAP 网络适配层连接入口

---

## 十六、后续计划

- [x] Gmail API 收件后端（HTTPS 443，绕过 IMAP 993 端口限制）
- [x] PySide6 GUI 桌面界面：三栏布局、账号配置、今日文件、安全预览、发件、诊断、历史和日志
- [ ] MCP `submit_result` 接口：Agent 通过 MCP 提交结果文件
- [ ] ProjectFlow 集成
- [ ] 系统密钥环（keyring）存储密钥，替代 `.env`
- [ ] QQ SMTP SOCKS5 适配（当前仅预留配置键）

## 十七、第一批次核心收口说明

AgentMailBridge 是本地优先、单用户、Windows 优先的桌面邮箱桥接工具。它不是多用户 SaaS、通用邮箱客户端或 ChatGPT Work 的竞品。邮箱凭据与 OAuth token 只由本地工具管理，Agent 通过受控应用服务处理本地文件，不直接持有邮箱权限。

当前收件后端包括 Gmail IMAP 与 Gmail API。IMAP 适合 993 端口可用且已配置应用专用密码的环境；Gmail API 通过 HTTPS 443 和只读 OAuth 工作，适合 IMAP 端口不可达的环境。Gmail API 模式不要求应用专用密码，IMAP 模式不要求 credentials.json 或 token.json。

CLI 和正式 GUI 均调用 `ApplicationService`。两个收件后端只负责读取并转换邮件，之后共同使用地址判断、Message-ID 归一化、MIME 结果保存、SQLite 去重和结构化结果流程。SQLite 使用 WAL、5 秒 busy timeout、短事务及唯一约束。QQ SMTP 使用 request_id 防止重复发送，并区分 sent、failed、sent_archive_failed 和 duplicate。

第二批次 GUI 使用 PySide6，仅替换界面层。界面包含顶部服务状态、左侧账号和导航、中间配置与文件日志区域、右侧服务状态和今日统计，并提供收邮箱、发邮件、高级设置、历史记录及日志页面。长任务在线程池执行，运行期间禁止重复点击。自动收件只在界面运行期间按分钟调度，默认关闭，不是后台常驻服务。文件预览仅允许安全文本和图片类型，其他类型只在资源管理器中定位。

启动命令：

```bash
python -m agent_mail_bridge init
python -m agent_mail_bridge receive
python -m agent_mail_bridge send --file AgentMailBridgeData/send/result.txt --request-id example-001
python -m agent_mail_bridge diagnose-gmail
python -m agent_mail_bridge diagnose-gmail-api
python -m agent_mail_bridge diagnose-qq-smtp
python -m agent_mail_bridge.gui
python -m pytest -q
```

安全边界：`.env`、`credentials.json`、`token.json`、`secrets/`、日志和用户数据目录均被 Git 忽略。发送文件必须位于 DATA_ROOT 或 ALLOWED_SEND_ROOTS 明确列出的目录。危险附件只保存并标记，不自动执行、打开或解压。自动化测试强制禁用项目 `.env`，OAuth 文件和数据目录均指向 pytest 临时目录。

当前限制：仅支持本地单用户；收件人固定为绑定 Gmail；不支持多租户、MCP、后台常驻收件、托盘、系统通知、开机启动和正式安装包。PySide6 会增加后续安装包体积，当前仍通过 Python 命令启动。
