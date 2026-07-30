# 同步 Checkpoint 与 UIDVALIDITY 恢复

## 独立进度

每个 account_id 与 mailbox_id 组合独立保存 last successful UID/checkpoint、UIDVALIDITY、UIDNEXT、highestmodseq、最近尝试、最近成功、结果、失败次数、重扫游标和当前尝试阶段。账号级旧状态只作为兼容摘要，不覆盖目录级事实。

同步开始记录 attempt，但只有目录完整成功后才提交新 checkpoint。`no_changes` 是成功结果；`partial` 保留已经归档的邮件，但不把未完成范围伪装成完整快照。连接失败、SQLite busy、损坏邮件或单目录失败均保留上一次成功 checkpoint。

## UIDVALIDITY

UIDVALIDITY 变化只使该目录的旧 UID checkpoint 失效，记录变化时间并进入 reconciliation/full rescan。历史 package、membership 历史、raw.eml 和 Hash 不改变。其他目录继续按自己的 checkpoint 同步。

新代际扫描仍通过账号、Message-ID、raw Hash、Provider id 和内容指纹复用已有事实；不能仅因 UID 改变创建重复 package。

## Membership 快照

成功目录快照可把本次未出现的旧 membership 标记为 server absent，但不会删除 package。失败或部分快照不能标记缺失。Gmail Label 归属使用消息实际 labelIds；明确空列表表示当前没有可见 Label，不回退查询来源。

快照到期使用观察时间，并防御系统时间回拨：数据库中的未来快照时间视为立即需要刷新，避免长期停止 membership 对账。

## 恢复操作

运行健康页展示目录最后成功、最后尝试、连续失败、UIDVALIDITY 变化和需重新对账状态。用户可重新同步受影响目录；系统不会因为一个目录失败而清空整个账号进度或修改服务器邮件。
