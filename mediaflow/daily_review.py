"""Daily report based on fresh browser evidence, not task_runs status."""
from datetime import datetime, timedelta
import math
import os
import random
import time
from mediaflow.bilibili import BilibiliClient
from mediaflow.audit import write_audit_event
from mediaflow.config import ROOT


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
    lines = ['【Quark-BiliBili】', '']
    success = 0
    for index, row in enumerate(rows):
        if index:
            delay = random.uniform(wait_min, wait_max)
            write_audit_event(ROOT/'logs/daily-review.jsonl', 'review_wait',
                next_record_id=row['id'], account=row['name'], wait_seconds=round(delay, 3))
            time.sleep(delay)
        remote = None
        try:
            with BilibiliClient(row['cdp_url']) as client:
                if client.uid() != row['uid']:
                    raise RuntimeError('浏览器登录账号不匹配')
                remote = client.verify_comment(row['bvid'],row['uid'],row['text'],row['resource_url'] or '',row['remote_comment_id'])
            reason = '当前找到匹配评论' if remote else '页面定位及所检查评论分页均未找到匹配留言；不能据此断言已删除'
            verification = 'recheck_verified' if remote else 'recheck_not_found'
        except Exception as exc:
            reason = f'页面核验异常：{type(exc).__name__}: {str(exc)[:500]}'
            verification = 'recheck_error'
        status = 'SUCCESS' if remote else 'FAILED'
        with db.connection:
            db.connection.execute('INSERT INTO comment_rechecks(record_id,status,reason,remote_comment_id) VALUES(?,?,?,?)', (row['id'],status,reason,remote))
            db.connection.execute('UPDATE promotion_records SET verification_status=? WHERE id=?',(verification,row['id']))
        write_audit_event(ROOT/'logs/daily-review.jsonl','comment_rechecked',record_id=row['id'],account=row['name'],status=status,reason=reason,remote_comment_id=remote)
        success += bool(remote)
        clock = row['created_at'][11:16]
        line = f"- {clock}: 平台=BiliBili, 账户={row['name']}, {'成功' if remote else '失败'}"
        if not remote:
            line += f"，原因={reason.replace(chr(13), ' ').replace(chr(10), ' ')}"
        lines.append(line)
    if not rows:
        lines.append('- 当日无留言记录可核验')
    if not notify.webhook:
        write_audit_event(ROOT/'logs/daily-review.jsonl','notification_failed',reason='FEISHU_WEBHOOK not configured')
        raise RuntimeError('FEISHU_WEBHOOK not configured; recheck results saved but notification not sent')
    notify.send_text('\n'.join(lines))
    write_audit_event(ROOT/'logs/daily-review.jsonl','daily_review_notified',day=f'{day:%Y-%m-%d}',success=success,failed=len(rows)-success)
    print(f'daily review: success={success} failed={len(rows)-success}; notification sent')
    return 0 if success == len(rows) else 1
