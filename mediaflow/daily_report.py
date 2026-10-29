"""Beijing daily task summaries, built from local records without a browser."""
import json
import re
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path

from mediaflow.audit import audited, write_audit_event
from mediaflow.config import ROOT, Settings
from mediaflow.delivery import DeliveryUncertain, deliver_once
from mediaflow.notify import FeishuNotifier
from mediaflow.promotion_schedule import DEFAULT_ACCOUNT, ACCOUNT_HOURS
from mediaflow.scheduled_notifications import BEIJING, scheduled_states, stop_reason

VERIFIED = {'verified', 'manual_verified', 'recheck_verified'}
TASK_REASONS = {
    'publish_failed': '评论发布失败',
    'publish_unconfirmed': '评论已提交，发布结果待确认',
    'verification_failed': '评论核验未通过',
    'phrase_exhausted': '没有未使用的合适文案',
    'no_phrase': '没有未使用的合适文案',
    'comments_disabled_by_author': '作者关闭了评论',
    'comments_disabled': '当前账号无法在该视频评论',
    'comment_editor_not_found': '未找到可用的评论输入框',
}


def _account_names(accounts):
    names = tuple(accounts)
    if not names or len(names) != len(set(names)):
        raise ValueError('Report accounts must be nonempty and distinct')
    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in names):
        raise ValueError('Invalid report account name')
    return names


def _failure_reason(state, tasks):
    reason = state.get('reason') or ''
    preparation_stage = None
    preparation_timeout = False
    if not reason:
        ids = {task.get('id') for task in state.get('task_runs', [])}
        for task in tasks:
            if task['id'] in ids and task['status'] != 'SUCCESS':
                match = re.search(r'(?:^|;)\s*reason=([^;]+)', task.get('detail') or '')
                if match:
                    reason = match.group(1).strip()
                    if state.get('error_type') == 'CommentPreparationError':
                        stage_match = re.search(r'\bstage=(navigation|comment_section|comment_permission|editor_resolve|editor_click|editor_focus)\b', reason)
                        if stage_match:
                            preparation_stage = stage_match.group(1)
                            preparation_timeout = bool(re.search(r'\berror=TimeoutError\b', reason))
                    break
    description = TASK_REASONS.get(reason) or stop_reason(dict(state, reason=reason))
    error = state.get('error_type') or (state.get('candidate_pool') or {}).get('error_type')
    if preparation_stage:
        label = {'navigation':'视频页面加载', 'comment_section':'评论区加载',
                 'comment_permission':'评论权限及主输入框检查', 'editor_resolve':'主输入框定位',
                 'editor_click':'主输入框点击', 'editor_focus':'主输入框焦点检查'}[preparation_stage]
        description = label+('超时' if preparation_timeout else '失败')
        if preparation_timeout:
            error = 'TimeoutError'
    # Raw exception messages and task details can contain private URLs.
    if isinstance(error, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,79}', error):
        if reason == 'candidate_preparation_failed' and error in ('TimeoutError', 'APITimeoutError'):
            description = '候选或文案准备阶段请求超时'
        description += f'，{error}'
    return description


def _round_entry(state, records, tasks):
    slot = datetime.fromisoformat(state['slot'])
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=BEIJING)
    slot = slot.astimezone(BEIJING)
    results = [dict(row) for row in state.get('results', [])]
    by_id = {record['id']: record for record in records}
    results = [dict(row, **by_id[row['id']]) if row.get('id') in by_id else row for row in results]
    stage = state.get('stage')
    task_ids = {task.get('id') for task in state.get('task_runs', [])}
    # A preparation error is raised before typing or submission. Require the
    # failed task record as well; zero rows alone cannot prove non-submission.
    preparation_failed = (stage == 'requires_readonly_recheck' and not results
        and state.get('comments_submitted') == 0 and state.get('error_type') == 'CommentPreparationError'
        and any(task['id'] in task_ids and task['status'] == 'FAILED' for task in tasks))
    entry = dict(slot=slot.isoformat(), time=slot.strftime('%H:%M'), source='scheduled',
                 recovered=bool(state.get('history') or state.get('previous_attempt')),
                 videos=sorted({row['bvid'] for row in results
                                if re.fullmatch(r'BV[A-Za-z0-9]+', row.get('bvid') or '')}),
                 verification_pending=any(row.get('publish_status') == 'published'
                                          and row.get('verification_status') not in VERIFIED for row in results))
    if preparation_failed:
        entry.update(status='failed', reason=_failure_reason(state, tasks))
    elif any(row.get('publish_status') == 'submitted' for row in results) or stage == 'requires_readonly_recheck':
        entry.update(status='pending', reason='提交或执行结果待确认，请只读复核，勿重复发布')
    elif stage in ('stopped', 'failed') or any(row.get('publish_status') == 'failed' for row in results):
        entry.update(status='failed', reason=_failure_reason(state, tasks))
    elif stage == 'completed':
        entry.update(status='success', reason='')
    else:
        label = {'preflight': '预检', 'ready': '准备', 'publishing': '发布'}.get(stage, '执行')
        entry.update(status='running', reason=f'仍停留在{label}阶段，结果未完成')
    return entry


def _account_snapshot(connection, account, day, now, states, root):
    start = datetime.combine(day, time.min, BEIJING)
    end = start + timedelta(days=1)
    records = [dict(row) for row in connection.execute('''SELECT p.id,p.created_at,p.bvid,
        p.publish_status,p.verification_status,p.remote_comment_id
        FROM promotion_records p JOIN accounts a ON a.id=p.account_id
        WHERE a.name=? AND p.platform='bilibili' AND p.created_at>=? AND p.created_at<?
        ORDER BY p.created_at,p.id''',
        (account, start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))]
    tasks = [dict(row) for row in connection.execute('''SELECT t.id,t.status,t.detail
        FROM task_runs t JOIN accounts a ON a.id=t.account_id
        WHERE a.name=? AND t.task_type='promotion' AND t.started_at>=? AND t.started_at<?
        AND COALESCE(t.detail,'') NOT LIKE '%reason=dry-run%' ORDER BY t.id''',
        (account, (start-timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S'),
         (end-timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')))]
    activated_at = None
    activation_path = root/'data/promotion-schedules'/f'{account}.json'
    if activation_path.exists():
        activation = json.loads(activation_path.read_text(encoding='utf-8'))
        if activation.get('account') != account:
            raise ValueError('Schedule activation account mismatch')
        activated_at = datetime.fromisoformat(activation['active_from'])
        if activated_at.tzinfo is None:
            raise ValueError('Schedule activation must include timezone')
    due = [start.replace(hour=hour, minute=40) for hour in ACCOUNT_HOURS.get(account, ())
           if start.replace(hour=hour, minute=40) <= now
           and (activated_at is None or start.replace(hour=hour, minute=40) >= activated_at)]
    entries, rounds = {}, []
    for state in states:
        entry = _round_entry(state, records, tasks)
        if datetime.fromisoformat(entry['slot']) <= now:
            rounds.append(state)
            entries[entry['slot']] = entry
    missing = []
    for slot in due:
        if slot.isoformat() not in entries:
            missing.append(slot.strftime('%H:%M'))
            entries[slot.isoformat()] = dict(slot=slot.isoformat(), time=slot.strftime('%H:%M'),
                source='missing', status='failed', reason='未找到到期任务的执行记录', videos=[],
                recovered=False, verification_pending=False)
    entries = sorted(entries.values(), key=lambda entry: entry['slot'])
    return dict(account=account, date=str(day), generated_at=now.isoformat(),
        published=sum(row['publish_status'] == 'published' for row in records),
        pending=sum(row['publish_status'] == 'submitted' for row in records),
        failed=sum(row['publish_status'] == 'failed' for row in records),
        verified=sum(row['verification_status'] in VERIFIED for row in records),
        scheduled_completed=sum(state.get('stage') == 'completed' for state in rounds),
        scheduled_stopped=sum(state.get('stage') in ('stopped', 'failed', 'requires_readonly_recheck') for state in rounds),
        recovered_rounds=sum(entry['status'] == 'success' and entry['recovered'] for entry in entries),
        missing_slots=missing, records=records, entries=entries,
        task_records_successful=sum(task['status'] == 'SUCCESS' for task in tasks),
        task_records_failed=sum(task['status'] != 'SUCCESS' for task in tasks))


@audited('daily_report_build', 'daily-report.jsonl')
def build_daily_report(day=None, accounts=None, *, settings=None, root=None, now=None):
    """Read each account independently; a malformed account does not hide the other."""
    root = Path(root or ROOT)
    settings = settings or Settings.load()
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    day = datetime.strptime(day, '%Y-%m-%d').date() if day else now.date()
    accounts = _account_names(accounts if accounts is not None else ACCOUNT_HOURS)
    snapshots, errors = [], []
    connection = sqlite3.connect(settings.database_path.resolve().as_uri()+'?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for account in accounts:
            try:
                states = scheduled_states(day, root/'data/scheduled-runs', account=account)
                snapshots.append(_account_snapshot(connection, account, day, now, states, root))
            except Exception as exc:
                errors.append(dict(account=account, error_type=type(exc).__name__))
                snapshots.append(dict(account=account, date=str(day), entries=[], error_type=type(exc).__name__))
    finally:
        connection.close()
    counts = {status: sum(entry['status'] == status for snapshot in snapshots for entry in snapshot['entries'])
              for status in ('success', 'failed', 'pending', 'running')}
    return dict(date=str(day), generated_at=now.isoformat(), accounts=snapshots, account_errors=errors,
                **{f'task_{key}': value for key, value in counts.items()})


def render_daily_report(report):
    status = f"任务状态：成功{report['task_success']},失败{report['task_failed']}"
    if report['task_pending']:
        status += f",待确认{report['task_pending']}"
    if report['task_running']:
        status += f",未完成{report['task_running']}"
    if report['account_errors']:
        status += f",统计异常{len(report['account_errors'])}个账户"
    lines = ['【Quark-BiliBili】', report['date'], status]
    labels = {'success': '任务成功', 'failed': '任务失败', 'pending': '任务待确认', 'running': '任务未完成'}
    for account in report['accounts']:
        lines.extend(['', f"账户: {account['account'].removeprefix('bilibili-')}"])
        if account.get('error_type'):
            lines.append(f"- 日报统计失败：（读取该账户记录异常，{account['error_type']}）")
        for entry in account['entries']:
            line = f"- {entry['time']},{labels[entry['status']]}："
            if entry['status'] == 'success':
                line += ' '.join('https://www.bilibili.com/video/'+bvid for bvid in entry['videos']) or '执行已完成'
                if entry['recovered']:
                    line += '（恢复成功）'
                if entry['verification_pending']:
                    line += '（历史核验未确认）'
            else:
                line += f"（{entry['reason']}）"
            lines.append(line)
        if not account['entries'] and not account.get('error_type'):
            lines.append('- 当日暂无到期任务')
    return '\n'.join(lines)


def _summary(report, status, receipts=None, **extra):
    result = dict(date=report['date'], status=status,
        **{key: report[key] for key in ('task_success', 'task_failed', 'task_pending', 'task_running', 'account_errors')},
        notification_receipts=receipts or [], **extra)
    if len(report['accounts']) == 1:
        account = report['accounts'][0]
        result.update({key: account.get(key, 0) for key in ('published', 'scheduled_completed', 'scheduled_stopped')})
    return result


@audited('daily_report', 'daily-report.jsonl')
def daily_report(day=None, account=DEFAULT_ACCOUNT, *, accounts=None, settings=None,
                 notifier=None, root=None, preview=False, now=None):
    """Send one grouped message and keep authoritative receipts for each account."""
    root = Path(root or ROOT)
    settings = settings or Settings.load()
    combined = accounts is not None
    requested = _account_names(accounts if combined else (account,))
    report = build_daily_report(day, requested, settings=settings, root=root, now=now)
    if preview:
        return _summary(report, 'preview', message=render_daily_report(report))
    if len(report['account_errors']) == len(requested):
        raise RuntimeError('No account daily report could be read')
    folder = root/'data/daily-reports'
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder/f"report-{report['date']}.lock"
    try:
        with lock.open('x', encoding='utf-8') as handle:
            json.dump(dict(accounts=requested, started_at=report['generated_at']), handle)
    except FileExistsError:
        return _summary(report, 'already_running')
    try:
        checks, send_accounts = [], []
        for name in requested:
            receipt_path = folder/f"{name}-{report['date']}-receipt.json"
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                checks.append(dict(account=name, status=receipt['status']))
            else:
                checks.append(dict(account=name, status='not_sent'))
                send_accounts.append(name)
        uncertain = [check for check in checks if check['status'] not in ('sent', 'not_sent')]
        if not send_accounts:
            if uncertain:
                raise DeliveryUncertain('Existing daily report requires manual delivery review')
            return _summary(report, 'already_sent', checks)
        sending_report = dict(report, accounts=[item for item in report['accounts'] if item['account'] in send_accounts],
            account_errors=[item for item in report['account_errors'] if item['account'] in send_accounts])
        for status in ('success', 'failed', 'pending', 'running'):
            sending_report['task_'+status] = sum(entry['status'] == status
                for item in sending_report['accounts'] for entry in item['entries'])
        prefix = ('combined-'+'-'.join(sorted(name.removeprefix('bilibili-') for name in send_accounts))) if combined else account
        path = folder/f"{prefix}-{report['date']}.json"
        primary_receipt = path.with_name(path.stem+'-receipt.json')
        saved = dict(sending_report, message=render_daily_report(sending_report), delivery_accounts=send_accounts)
        if not combined:
            saved.update(sending_report['accounts'][0])
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        aliases = []
        if combined:
            for item in sending_report['accounts']:
                name = item['account']
                alias = folder/f"{name}-{report['date']}-receipt.json"
                state = dict(kind='daily_report', account=name, day=report['date'], status='sending',
                             started_at=report['generated_at'], combined_receipt=primary_receipt.name)
                with alias.open('x', encoding='utf-8') as handle:
                    json.dump(state, handle, ensure_ascii=False)
                aliases.append((alias, state))
                snapshot = dict(item, delivery_mode='combined', combined_report=path.name)
                (folder/f"{name}-{report['date']}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        notifier = notifier or FeishuNotifier(settings.feishu_webhook, settings.feishu_secret)
        try:
            sent = deliver_once(primary_receipt, notifier, saved['message'], kind='daily_report',
                                **({'accounts':send_accounts} if combined else {'account':account}), day=report['date'])
        except BaseException:
            primary = json.loads(primary_receipt.read_text(encoding='utf-8')) if primary_receipt.exists() else {'status':'unknown'}
            for alias, state in aliases:
                state.update({key:primary[key] for key in ('status', 'error_type') if key in primary})
                alias.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            raise
        primary = json.loads(primary_receipt.read_text(encoding='utf-8'))
        if primary.get('status') != 'sent':
            raise DeliveryUncertain('Daily report delivery was not confirmed')
        for alias, state in aliases:
            state.update(status='sent', sent_at=primary['sent_at'])
            alias.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        checks = [dict(check, status='sent' if check['account'] in send_accounts else check['status']) for check in checks]
        write_audit_event(root/'logs/daily-report.jsonl', 'daily_report_notified', day=report['date'],
                          accounts=send_accounts, task_success=sending_report['task_success'],
                          task_failed=sending_report['task_failed'], sent=sent)
        return _summary(report, 'sent' if sent else 'already_sent', checks,
                        report_path=str(path), sent_accounts=send_accounts, uncertain_accounts=uncertain)
    finally:
        lock.unlink(missing_ok=True)
