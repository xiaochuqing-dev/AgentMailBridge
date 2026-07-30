"""邮件运行状态页的无状态文本呈现。"""

from __future__ import annotations

from typing import Any


RECOVERY_STATUS_NAMES = {
    "delivery_unknown": "结果不确定，绝不会自动重发",
    "sent_archive_failed": "SMTP 已接受，等待恢复本地归档",
    "recovery_required": "执行中断，需要恢复或对账",
    "smtp_accepted": "SMTP 已接受，等待本地归档",
    "sent_archive_pending": "等待本地归档",
}


def recovery_status_name(value: Any) -> str:
    status = str(value or "未知")
    return RECOVERY_STATUS_NAMES.get(status, status)


def format_mail_health(details: dict[str, Any], format_size) -> str:
    send = details.get("send", {})
    facts = details.get("facts", {})
    storage = details.get("storage", {})
    mailboxes = details.get("mailboxes", [])
    history_imports = details.get("history_imports", [])
    failed_mailboxes = sum(
        1
        for row in mailboxes
        if int(row.get("consecutive_failures") or 0) > 0
        or row.get("reconciliation_required")
    )
    lines = [
        "邮件运行状态",
        f"邮箱目录：{len(mailboxes)} 个，需处理 {failed_mailboxes} 个",
        "发件："
        f"待确认 {send.get('pending_confirmation', 0)}，"
        f"执行中 {send.get('sending', 0)}，"
        f"等待 Sent {send.get('waiting_sent', 0)}，"
        f"结果不确定 {send.get('delivery_unknown', 0)}，"
        f"归档待恢复 {send.get('archive_recovery', 0)}，"
        f"恢复待处理 {send.get('recovery_required', 0)}，"
        f"已完成 {send.get('completed', 0)}，"
        f"已取消或过期 {send.get('cancelled_or_expired', 0)}",
        "事实："
        f"未匹配 {facts.get('unmatched_sent', 0)}，"
        f"模糊候选 {facts.get('ambiguous_candidates', 0)}，"
        f"重复候选 {facts.get('duplicate_candidates', 0)}，"
        f"多目录归属 {facts.get('multi_membership', 0)}，"
        f"服务器未发现 {facts.get('server_absent', 0)}",
        "存储："
        f"永久资源 {format_size(storage.get('permanent_resources_bytes', 0))}，"
        f"发件快照 {format_size(storage.get('send_snapshots_bytes', 0))}，"
        f"工作副本 {format_size(storage.get('work_copies_bytes', 0))}，"
        f"备份 {format_size(storage.get('backups_bytes', 0))}，"
        f"可安全清理 {format_size(storage.get('safe_cleanup_bytes', 0))} "
        f"({storage.get('safe_cleanup_count', 0)} 项)",
    ]
    lines.append("邮箱目录明细")
    if mailboxes:
        for row in mailboxes:
            state = _sync_state(row.get("last_result"))
            attempt = row.get("current_attempt") or {}
            stage = str(attempt.get("stage") or "")
            if stage and str(row.get("last_result") or "") == "running":
                state = f"同步中 ({stage})"
            flags = []
            failures = int(row.get("consecutive_failures") or 0)
            if failures:
                flags.append(f"连续失败 {failures} 次")
            if row.get("reconciliation_required"):
                flags.append("需要重新对账")
            if row.get("uidvalidity_changed_at"):
                flags.append(
                    f"UIDVALIDITY 已变化 {row.get('uidvalidity_changed_at')}"
                )
            lines.append(
                f"{row.get('account_name') or row.get('account_id')} / "
                f"{row.get('mailbox_name') or row.get('mailbox_id')}：{state}；"
                f"上次成功 {row.get('last_success_at') or '尚无'}；"
                f"上次尝试 {row.get('last_attempt_at') or '尚无'}"
                + (f"；{'，'.join(flags)}" if flags else "")
            )
    else:
        lines.append("尚未发现已启用的邮箱目录")
    lines.append("历史导入进度")
    if history_imports:
        for row in history_imports:
            total = int(row.get("total_segments") or 0)
            segment = int(row.get("segment_index") or 0)
            progress = f"第 {segment}/{total} 段" if total else "进度待更新"
            lines.append(
                f"{row.get('account_name') or row.get('account_id')}："
                f"{_history_state(row.get('status'))}，{progress}，"
                f"扫描 {row.get('scanned', 0)}，保存 {row.get('saved', 0)}，"
                f"失败 {row.get('failed', 0)}，更新 {row.get('updated_at') or '尚无'}"
            )
    else:
        lines.append("当前没有进行中或待继续的历史导入")
    issues = details.get("issues", [])
    if issues:
        lines.append(f"待处理问题：{details.get('issue_count', len(issues))} 项")
        for item in issues[:100]:
            lines.append(
                f"{_severity(item.get('severity'))} "
                f"{_issue_name(item.get('issue_code'))} "
                f"({item.get('entity_id', '')})"
            )
    else:
        lines.append("待处理问题：当前扫描结果中没有未解决问题")
    return "\n".join(lines)


def _sync_state(value: Any) -> str:
    return {
        "not_started": "尚未同步",
        "running": "同步中",
        "success": "正常",
        "no_changes": "正常，无变化",
        "partial": "部分完成",
        "failed": "失败",
    }.get(str(value or "not_started"), str(value or "尚未同步"))


def _history_state(value: Any) -> str:
    return {
        "running": "正在导入",
        "partial": "部分完成，可继续",
        "failed": "失败，可重试",
        "cancelled": "已取消，可继续",
    }.get(str(value or ""), str(value or "未知"))


def format_recovery_requests(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "当前没有结果不确定或归档待恢复的发件请求"
    lines = [f"待恢复发件：{len(rows)} 项"]
    lines.extend(
        f"{str(row.get('send_request_id') or '')}："
        f"{recovery_status_name(row.get('status'))}"
        for row in rows[:30]
    )
    return "\n".join(lines)


def _severity(value: Any) -> str:
    return {
        "critical": "严重",
        "error": "错误",
        "warning": "警告",
    }.get(str(value), "提示")


def _issue_name(value: Any) -> str:
    return {
        "delivery_unknown": "发件结果不确定",
        "sent_archive_failed": "发件归档待恢复",
        "stale_send_lease": "发件租约已过期",
        "duplicate_fact_candidate": "存在重复事实候选",
        "direction_conflict": "邮件方向证据冲突",
        "checkpoint_requires_attention": "邮箱目录需要重新对账",
        "reconciliation_unresolved": "Sent 对账尚未解决",
        "snapshot_missing": "发件快照缺失",
        "snapshot_hash_mismatch": "发件快照校验失败",
        "missing": "归档文件缺失",
        "hash_mismatch": "归档文件校验失败",
        "manifest_identity_mismatch": "邮件清单身份不一致",
        "manifest_raw_mismatch": "原始邮件清单不一致",
        "manifest_resource_mismatch": "邮件资源清单不一致",
        "manifest_resource_ownership_mismatch": "邮件资源归属不一致",
        "orphan_work_copy": "存在未知邮件工作副本",
        "send_request_package_invalid": "发件请求与邮件事实不一致",
        "send_request_outbound_invalid": "发件请求与发件记录不一致",
        "outbound_fact_link_invalid": "发件事实关联不一致",
        "resource_ownership_invalid": "邮件资源归属无效",
    }.get(str(value), str(value or "待处理问题"))
