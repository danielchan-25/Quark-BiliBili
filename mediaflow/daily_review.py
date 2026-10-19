"""Report historical publications, scheduler stops and fresh comment checks separately."""
from datetime import datetime, timedelta
import math
import os
import random
import time
from mediaflow.bilibili import BilibiliClient
from mediaflow.audit import write_audit_event, audited
from mediaflow.config import ROOT
from mediaflow.scheduled_notifications import scheduled_states, stop_reason, BEIJING


@audited('daily_review', 'daily-review.jsonl')
def daily_review(db, notify, args):
    wait_min = float(os.getenv('REVIEW_WAIT_MIN_SECONDS', '10'))
    wait_max = float(os.getenv('REVIEW_WAIT_MAX_SECONDS', '20'))
    if not (math.isfinite(wait_min) and math.isfinite(wait_max) and 1 <= wait_min <= wait_max <= 60):
        raise ValueError('Review wait range must satisfy 1 <= min <= max <= 60 seconds')
    day = datetime.strptime(args.date, '%Y-%m-%d') if args.date else datetime.now().replace(hour=0,minute=0,second=0,microsecond=0)
    # promotion_records timestamps are local Beijing time (task_runs are UTC).
    rows = db.connection.execute('''SELECT p.*,a.name,a.uid,a.cdp_url,r.url AS resource_url
        FROM promotion_records p JOIN accounts a ON a.id=p.account_id
        LEFT JOIN promotion_resources r ON r.id=p.resource_id
        WHERE p.platform='bilibili' AND p.created_at>=? AND p.created_at<?
        ORDER BY p.created_at,p.id''', (day.strftime('%Y-%m-%d %H:%M:%S'),(day+timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'))).fetchall()
    db.connection.execute('''CREATE TABLE IF NOT EXISTS comment_rechecks (
        id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL, checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status TEXT NOT NULL, reason TEXT NOT NULL, remote_comment_id TEXT)''')
    db.connection.commit()
    rounds = scheduled_states(day.date())
    stopped = [state for state in rounds
               if state.get('stage') in ('stopped', 'failed', 'requires_readonly_recheck')
               and not state.get('publication_ids')]
    published = sum(row['publish_status'] == 'published' for row in rows)
    pending = sum(row['publish_status'] == 'submitted' for row in rows)
    failed = sum(row['publish_status'] == 'failed' for row in rows)
    lines = ['【Quark-BiliBili】', '', f"{day:%Y-%m-%d} 日报（北京时间）",
             f'当日发布记录：已发布 {published}，提交待确认 {pending}，发布失败 {failed}。',
             f'定时任务停止或异常：{len(stopped)}。', '']
    success = 0
    unavailable = 0
    write_audit_event(ROOT/'logs/daily-review.jsonl', 'review_plan', day=f'{day:%Y-%m-%d}', records=len(rows), stopped_rounds=len(stopped))
    for index, row in enumerate(rows):
        if index:
            delay = random.uniform(wait_min, wait_max)
            write_audit_event(ROOT/'logs/daily-review.jsonl', 'review_wait',
                next_record_id=row['id'], account=row['name'], wait_seconds=round(delay, 3))
            time.sleep(delay)
        remote = None
        write_audit_event(ROOT/'logs/daily-review.jsonl', 'comment_recheck_started', record_id=row['id'], account=row['name'])
        try:
            with BilibiliClient(row['cdp_url']) as client:
                if client.uid() != row['uid']:
                    raise RuntimeError('浏览器登录账号不匹配')
                remote = client.verify_comment(row['bvid'],row['uid'],row['text'],row['resource_url'] or '',row['remote_comment_id'])
            reason = '当前找到匹配评论' if remote else '页面定位及所检查评论分页均未找到匹配留言；不能据此断言已删除'
            verification = 'recheck_verified' if remote else 'recheck_not_found'
        except Exception as exc:
            reason = ('浏览器 CDP 连接不可用' if '本地 CDP 服务不可用' in str(exc)
                      else f'页面核验异常：{type(exc).__name__}')
            verification = 'recheck_error'
            unavailable += 1
        status = 'SUCCESS' if remote else 'FAILED'
        with db.connection:
            db.connection.execute('INSERT INTO comment_rechecks(record_id,status,reason,remote_comment_id) VALUES(?,?,?,?)', (row['id'],status,reason,remote))
            db.connection.execute('UPDATE promotion_records SET verification_status=? WHERE id=?',(verification,row['id']))
        write_audit_event(ROOT/'logs/daily-review.jsonl','comment_rechecked',record_id=row['id'],account=row['name'],status=status,reason=reason,remote_comment_id=remote)
        success += bool(remote)
        clock = row['created_at'][11:16]
        original = {'published': '已发布', 'submitted': '提交待确认', 'failed': '发布失败'}.get(row['publish_status'], row['publish_status'])
        review = '通过' if remote else ('无法核验' if verification == 'recheck_error' else '暂未找到')
        line = f"- {clock}: 平台=BiliBili, 账户={row['name']}, 发布记录={original}，本次复核={review}"
        if not remote:
            line += f"，原因={reason.replace(chr(13), ' ').replace(chr(10), ' ')}"
        lines.append(line)
    if not rows:
        lines.append('- 当日无留言记录可核验')
    for state in stopped:
        slot = datetime.fromisoformat(state['slot']).astimezone(BEIJING)
        lines.append(f"- {slot:%H:%M} 定时任务：账户={state['account']}，停止或异常，原因={stop_reason(state)}")
    lines.extend(['', f'本次复核：通过 {success}，暂未找到 {len(rows)-success-unavailable}，无法核验 {unavailable}。',
                  '本次复核结果不改变历史发布结果；未找到或无法核验不能据此断言评论已删除。'])
    if not notify.webhook:
        write_audit_event(ROOT/'logs/daily-review.jsonl','notification_failed',reason='FEISHU_WEBHOOK not configured')
        raise RuntimeError('FEISHU_WEBHOOK not configured; recheck results saved but notification not sent')
    try:
        notify.send_text('\n'.join(lines))
    except Exception as exc:
        write_audit_event(ROOT/'logs/daily-review.jsonl','notification_failed',day=f'{day:%Y-%m-%d}',error_type=type(exc).__name__)
        raise
    write_audit_event(ROOT/'logs/daily-review.jsonl','daily_review_notified',day=f'{day:%Y-%m-%d}',success=success,failed=len(rows)-success,published=published,scheduled_stopped=len(stopped),unavailable=unavailable)
    print(f'daily review: success={success} failed={len(rows)-success}; notification sent')
    return 0 if success == len(rows) else 1
