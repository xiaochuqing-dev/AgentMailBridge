# Sent 对账与重复识别

## 目标

Sent 同步把 AgentMailBridge、本机其他客户端、网页端和手机发出的邮件纳入统一 outbound 事实。对账必须优先避免错误合并；缺少确定证据时允许暂时 unmatched 或 ambiguous。

## 证据顺序

本地请求与 Sent 观察首先要求 account_id 一致。最强证据是固定 raw MIME SHA-256 精确一致。其次是唯一 Message-ID 候选，并同时核对 From、To/Cc、Subject、时间窗口和可用内容/附件指纹。Provider message id、UIDVALIDITY 和 UID 作为服务器 mapping 证据保存。

仅 Message-ID 相同、仅主题相同或仅发件人相同都不足以合并。多个同等级候选进入 `reconciliation_records.status=ambiguous`，不会任选一个。

## 本地与外部发件

匹配本地请求后，`sent_server_mappings` 指向同一个 package，并推进 request/outbound reconciliation 状态，不新建第二个正式事实。

没有本地 request 的 Sent 邮件形成 external outbound package。它拥有 direction、账号、目录 membership、raw、正文、资源和服务器 mapping，但不会伪造 Client、确认模式或幂等键。

`delivery_unknown` 即使尚无本地 package，也可在账号一致且 raw Hash 精确匹配，或唯一 Message-ID 加复合 Header 证据一致时由 Sent 回流恢复。多个请求候选仍保持 ambiguous。

## 重复识别边界

同一账号内 raw Hash 相同会作为高置信重复候选；不同物理邮件即使复用了 Message-ID 也保持独立。对账不使用主题相似度、AI 聚类或无限时间窗口。扫描报告重复候选，但一致性修复不会自动合并。
