# MCP 邮件读取工具说明

## search_mails

输入支持 query、time_scope、recent_days、date_from/date_to、subject、sender、recipient、has_attachments、status、sort、limit、offset、ensure_fresh、allow_cached、account_ref 和 mailbox_ref。time_scope 取 latest、today、yesterday、recent_days、date_range 或 all；limit 为 1 至 100。输出包含唯一邮件摘要、result_count、分页、cached、sync_triggered、sync_error 和同步状态。

## get_mail

输入 mail_id 或 package_id，以及 offset 和 max_chars。输出邮件元数据、线程身份、archive 状态、有界正文、资源清单、分类计数和 raw.eml 描述。正文返回 character_count、offset、next_offset 和 has_more；不会一次返回无界正文。

## read_mail_resource

输入 mail_id/package_id、resource_id、mode、字符分页或 CSV 行分页。mode 为 text、preview、csv_preview 或 raw。text 用于可读文本；csv_preview 返回 delimiter、columns、row_count 和 rows；preview 返回图片或二进制安全描述；raw 只能读取当前邮件真实 raw.eml。资源不可用、不是当前邮件、Hash 不符或类型不支持时返回稳定错误。

能力值包括 directly_readable、structured_preview、visual_file、document_file、binary_file、link 和 unavailable。能力只说明桥接器允许的安全处理方式，不表示会执行附件。

## prepare_mail_resources

输入 mail_id/package_id、1 至 100 个 resource_ids，可选 target_workspace、target_subdir 和 overwrite_policy。target_workspace 接受 `list_agent_workspaces` 返回的 ID 或完整授权路径。输出目标目录、说明文件、每个资源的 prepared_path、size_bytes、sha256、失败项和 success/partial/failed 状态。

## list_agent_workspaces

无输入。返回 workspace_id、完整 display_path、available 和 default。没有唯一可用工作区时，prepare 返回 workspace_required，不自行猜测目录。

## get_mail_sync_status

无输入且不受邮件读取开关阻断。返回 enabled、background_status、is_syncing、freshness、阈值、数据年龄、最近本地邮件时间、上次检查/成功/结果、下次检查和重试计数。freshness 为 fresh、stale 或 unknown。

## submit_result

输入与既有版本相同：file_path 必填，title 与稳定 request_id 可选。读取开关关闭不影响发送。成功、duplicate 和 SMTP 已接受但归档失败不会被客户端误判为协议错误；路径、类型、大小、配置、速率和 Hash 失败返回稳定业务状态。

## 通用协议约定

七个工具均拒绝额外未知字段。tools/list 返回 inputSchema、outputSchema 和 MCP 标准 annotations：读信类声明 readOnlyHint，准备和发送声明写入边界，所有工具声明非破坏性，只有发送会接触固定外部收件人。业务结果位于 structuredContent，同时提供简短 text；stdout 不包含日志。
