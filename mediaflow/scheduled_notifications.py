"""Alerts for scheduler failures that occur before main.promotion starts."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mediaflow.audit import write_audit_event, audited
from mediaflow.config import ROOT
from mediaflow.delivery import deliver_once

BEIJING = timezone(timedelta(hours=8))
REASONS = {
    'browser_busy': '浏览器正被另一个项目任务占用，本轮停止以避免并发操作',
    'preparation_hard_timeout': '准备进程超过总时间预算，已终止且未进入发布',
    'comment_section_repeated_timeout': '3个视频累计9次评论区加载超时，需根据日志排查持续性故障',
    'browser_connection_failed': '连接浏览器失败，请检查CDP连接日志',
    'browser_identity_check_failed': '浏览器登录身份读取失败',
    'video_navigation_failed': '视频页面导航或加载失败（非候选或文案库存不足）',
    'browser_dom_access_failed': '页面自动化读取超时，尚未完成评论区检测，请检查浏览器读取通道及主机负载',
    'comment_section_load_failed': '视频评论区加载失败（非候选或文案库存不足）',
    'comment_editor_readiness_failed': '主评论输入框未就绪或评论权限受限',
    'comment_editor_resolve_failed': '主评论输入框定位失败',
    'comment_editor_focus_failed': '主评论输入框点击或焦点检查失败',
    'local_cdp_unavailable': '浏览器 CDP 连接不可用，请启动对应浏览器',
    'browser_not_logged_in_as_expected_uid': '浏览器未登录或登录账号不匹配',
    'browser_identity_changed': '浏览器登录账号发生变化',
    'registered_identity_or_browser_conflict': '登记账号或浏览器与配置不一致',
    'csv_account_not_enabled_for_promotion': '配置中的推广账号未启用',
    'enabled_resources_missing_or_not_matching_account_configuration': '推广资源缺失或与账号配置不一致',
    'no_approved_visible_unpublished_candidate': '没有通过审核且可访问的未发布候选',
    'candidate_pool_refill_failed': '候选池补充失败且没有可发布候选，请检查审核与搜索日志',
    'candidate_preparation_failed': '候选或文案准备失败，请检查任务日志',
    'account_not_enabled_in_database': '数据库中的推广账号未启用或不存在',
    'notification_path_test': '告警链路验证：模拟失败，无实际发布',
}


def stop_reason(state: dict) -> str:
    reason = state.get('reason') or ''
    if reason.startswith('daily comment limit'):
        return '已达到每日发布上限'
    if reason.startswith('minimum 15-minute interval'):
        return '尚未满足最少 15 分钟发布间隔'
    description = REASONS.get(reason, '任务执行异常，请检查本地记录')
    if state.get('underlying_error_type') == 'TimeoutError':
        description += '；等待超时'
    return description


@audited('scheduled_state_read', 'scheduled-alerts.jsonl')
def scheduled_states(day, directory: Path | None = None, *, account: str | None = None) -> list[dict]:
    directory = directory or ROOT / 'data/scheduled-runs'
    result = {}
    for path in sorted(directory.glob('quark-bilibili-*.json')):
        file_account = account if not account or account.startswith('bilibili-') else 'bilibili-'+account
        if account and not (path.name.startswith(f'quark-{file_account}-') or path.name == f'quark-{file_account}.json'):
            continue
        state = json.loads(path.read_text(encoding='utf-8'))
        if account and state['account'] != account:
            continue
        slot = datetime.fromisoformat(state['slot'])
        if slot.tzinfo is None:
            slot = slot.replace(tzinfo=BEIJING)
        slot = slot.astimezone(BEIJING)
        if slot.date() == day:
            result[(state['account'], slot.isoformat())] = state
    return list(result.values())


@audited('scheduled_alert', 'scheduled-alerts.jsonl')
def alert_scheduled_stop(state_path: Path, notify, log_path: Path | None = None) -> bool:
    """Send once per scheduled round. Ambiguous sends require manual review."""
    log_path = log_path or ROOT / 'logs/scheduled-alerts.jsonl'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    if state.get('stage') not in ('stopped', 'failed', 'requires_readonly_recheck'):
        write_audit_event(log_path, 'alert_skipped', reason='round_not_stopped')
        return False
    attempt = state.get('attempt_id', 'initial')
    alert_attempt = state.get('feishu_alert_attempt_id', 'initial')
    if attempt == alert_attempt and state.get('feishu_alert_status') in ('sent', 'sending', 'unknown'):
        write_audit_event(log_path, 'alert_skipped', reason='already_sent_or_uncertain')
        return False
    if not notify.webhook:
        write_audit_event(log_path, 'alert_failed', account=state['account'], slot=state['slot'], reason='FEISHU_WEBHOOK not configured')
        raise RuntimeError('FEISHU_WEBHOOK not configured')
    clock = datetime.fromisoformat(state['slot']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M')
    state['feishu_alert_status'] = 'sending'
    state['feishu_alert_attempt_id'] = attempt
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    try:
        import hashlib
        key = hashlib.sha256(f"{state['account']}|{state['slot']}|{attempt}".encode()).hexdigest()
        deliver_once(state_path.parent/'notification-receipts'/f'{key}.json', notify,
                     f"【Quark-BiliBili】\n\n- {clock}: 账户={state['account']}，定时任务停止，原因={stop_reason(state)}",
                     kind='scheduled_stop', account=state['account'], slot=state['slot'], attempt_id=attempt)
    except Exception as exc:
        state['feishu_alert_status'] = 'unknown'
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        write_audit_event(log_path, 'alert_failed', account=state['account'], slot=state['slot'], error_type=type(exc).__name__)
        raise
    state['feishu_alert_status'] = 'sent'
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    write_audit_event(log_path, 'alert_sent', account=state['account'], slot=state['slot'])
    return True
