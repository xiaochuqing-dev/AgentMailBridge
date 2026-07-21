# AgentMailBridge v1.3.0 前置复杂邮件 MCP 验收基线

## 验收结论

验收时间：2026-07-21

开始前 HEAD：`fd0a5c720ea23f97d930d7466e2382ffecb49656`

基线版本：1.2.1

总体结果：PASS。目标复杂邮件存在于本机 1.2.1 安装版的正式归档中，真实 stdio MCP 完成搜索、正文和资源读取、受控资源准备、完整性校验及生命周期验收。源码模式 DATA_ROOT 中没有该邮件，但安装版正式 DATA_ROOT 命中；本报告以用户实际安装版数据为准，没有扫描或绕过 MCP 读取正式邮件目录。

## 环境与账号事实

- PASS：本地 master 与 origin/master 一致。
- PASS：当前 Gmail 地址经脱敏后为 `q***@gmail.com`，与目标邮件真实收件账号一致。
- PASS：实际收件后端为 Gmail API。
- PASS：OAuth 状态为可刷新 Token，scope 保持 `gmail.readonly`。
- PASS：安装版 GUI 与 MCP 均为 1.2.1。
- PASS：持久化 MCP 邮件读取开关保持关闭。本次仅对独立验收子进程临时设置读取权限，进程退出即失效，没有改写用户配置。

## 自动化基线

- PASS：`py -3.12 -m pytest -q`
- 结果：478 passed，1 skipped。
- 耗时：1028.65 秒。
- 失败：0。

## stdio MCP 与目标邮件

- PASS：initialize 协议版本 `2025-06-18`。
- PASS：serverInfo 版本 1.2.1。
- PASS：tools/list 返回 7 个工具，名称与既有契约完全一致。
- PASS：get_mail_sync_status 成功。
- PASS：search_mails 使用 `time_scope=all` 和完整测试标识，命中 1 封，不依赖 GUI 筛选。
- PASS：稳定 package 为 `pkg_5663fc0c188414e33c5bf4f0`，没有第二个重复归档。
- PASS：附件 9、邮件中的图片 1、链接 5、下载文件 0、总资源 18。
- PASS：stdout 只有 JSON-RPC 协议输出。
- PASS：stdin EOF 后退出码为 0。

## 正文与资源读取

- PASS：get_mail 正文包含 `AMB_SEARCH_BODY_MARKER_2026`。
- PASS：正文包含多语言、Emoji、Python、JSON、CSV 示例及 5 个 HTTPS URL。
- PASS：Markdown 附件包含 `AMB_SEARCH_ATTACHMENT_MARKER_2026`。
- PASS：TXT 附件包含 `TXT_RESOURCE_READ_OK`。
- PASS：JSON expected_manifest 的 test_id、expected_inline_images=1、expected_regular_attachments=9、expected_links=5 均正确。
- PASS：CSV preview 返回 6 列、12 行，中文、quoted comma、quoted newline、空字段均正确。
- PASS：CID PNG 为 640×240，普通 PNG 为 960×540；两者资源类型和 SHA-256 不同。
- PASS：raw.eml 通过 MCP raw 模式有界读取，From、To、Subject、Content-Type 和 multipart boundary 结构存在，长内容保持分页。

## 受控资源准备

使用 prepare_mail_resources 准备 CID PNG、普通 PNG、PDF、DOCX、XLSX 和 0 字节 DAT。首次传入不属于契约的 overwrite_policy 值时被稳定拒绝；改用契约允许的 `overwrite` 后结果如下，该输入拒绝不属于产品失败。

- PASS：6 个资源全部由 MCP 准备，失败 0。
- PASS：每项 source size、prepared size 和实际文件大小一致。
- PASS：每项 source SHA-256、MCP prepared SHA-256 和准备后实际 SHA-256 一致。
- PASS：0 字节 DAT 准备后仍为 0 字节，SHA-256 为标准空内容 Hash。
- PASS：未执行或解压任何二进制资源。

准备文件大小：

- CID PNG：19561 bytes
- 普通 PNG：37851 bytes
- PDF：38119 bytes
- DOCX：37298 bytes
- XLSX：6120 bytes
- DAT：0 bytes

## 基线发现

- PARTIAL：安装版 MCP 返回的部分中文资源显示名存在乱码，正文与资源内容读取正常。本轮联系人和展示整改将保留原始 raw 事实，并统一改善人类可读解码。
- PASS：链接识别数量正确；现有显示名仍包含信息量不足的 `view` 和 `report`，与本轮待整改问题一致。
- PASS：未发现资源数量、Hash、0 字节附件、MCP 工具数量、stdout purity 或 EOF 生命周期回退。

## 最终基线判定

PASS。真实复杂邮件 MCP 能力可作为 v1.3.0 整改前基线。中文联系人/资源显示和链接标题属于已记录的待整改项，不影响本次复杂邮件事实、资源读取与 Hash 闭环成立。
