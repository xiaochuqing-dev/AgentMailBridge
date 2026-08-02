# Sent 对账与重复识别

## 目标

Sent 同步把 AgentMailBridge、本机其他客户端、网页端和手机发出的邮件纳入统一 outbound 事实。对账必须优先避免错误合并；缺少确定证据时允许暂时 unmatched 或 ambiguous。

## 证据顺序

本地请求与 Sent 观察首先要求 account_id 一致。自动确认按以下确定性证据处理：当前 request/package/outbound 的精确关联、已保存且账号一致的 Provider mapping、raw MIME SHA-256 精确一致，以及唯一 Message-ID 加完整复合证据。Provider mapping 与 raw Hash 指向不同事实时进入 `ambiguous/manual_review`，不按优先级静默覆盖。

完整复合证据必须同时核对规范化 Message-ID、From、To/Cc 集合、Subject、内容与附件指纹，以及观察事实与候选事实之间绝对不超过七天的时间窗口。缺少指纹、有效时间或关键 Header 时保守降级，不猜测。明确的 request/package/outbound 关联、Provider mapping 或 raw MIME Hash 均可按可审计规则覆盖指向另一个候选的弱 Message-ID，但不会覆盖彼此冲突的其他强证据。

仅 Message-ID 相同、仅内容指纹相同、仅主题相同、仅发件人相同或仅时间接近都不足以合并。多个 Message-ID 候选或强证据冲突进入 `reconciliation_records.status=ambiguous`，不会任选一个。Message-ID 仍可用于查找候选和确定性线程关系，但永远不能单独把 `delivery_unknown` 判为已发送。

## 本地与外部发件

匹配本地请求后，`sent_server_mappings` 指向同一个 package，并推进 request/outbound reconciliation 状态，不新建第二个正式事实。

没有本地 request 的 Sent 邮件形成 external outbound package。它拥有 direction、账号、目录 membership、raw、正文、资源和服务器 mapping，但不会伪造 Client、确认模式或幂等键。

`delivery_unknown` 即使尚无本地 package，也可在账号一致且固定 raw Hash 精确匹配，或唯一 Message-ID 加完整 Header、内容/附件指纹和七天窗口一致时由 Sent 回流恢复。恢复不调用 SMTP，不增加尝试次数；多个请求候选仍保持 ambiguous。

## 重复识别边界

同一账号内 raw Hash 相同会作为高置信重复候选；不同物理邮件即使复用了 Message-ID 也保持独立。对账不使用主题相似度、AI 聚类或无限时间窗口。扫描报告重复候选与 unresolved 记录，但一致性修复不会自动合并 ambiguous，也不会改写 package_id、raw.eml 或历史 Hash。
