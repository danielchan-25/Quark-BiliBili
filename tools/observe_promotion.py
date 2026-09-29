"""Run one promotion per configured test account and retain selection evidence."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / 'logs' / ('promotion-observation-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(parents=True)
    db = sqlite3.connect(ROOT / 'data' / 'mediaflow.db')
    db.row_factory = sqlite3.Row
    for account in ('bilibili-35199508', 'bilibili-1085803742'):
        account_id = db.execute('SELECT id FROM accounts WHERE name=?', (account,)).fetchone()['id']
        before = db.execute('SELECT COALESCE(MAX(id),0) FROM promotion_records').fetchone()[0]
        snapshot = {'account': account, 'started_at': datetime.now(timezone.utc).isoformat(), 'resources': []}
        for resource in db.execute('SELECT id FROM promotion_resources WHERE enabled=1'):
            rows = [dict(r) for r in db.execute('''SELECT c.id,c.bvid,c.title,c.video_url,c.ai_score,c.ai_confidence,c.ai_risk,c.ai_status,c.created_at
                FROM promotion_candidates c WHERE c.resource_id=? AND c.platform='bilibili' AND c.status='available'
                AND NOT EXISTS(SELECT 1 FROM promotion_records p WHERE p.account_id=? AND p.video_url=c.video_url)
                ORDER BY c.created_at DESC,c.id DESC''', (resource['id'], account_id))]
            approved = sorted((r for r in rows if r['ai_status']=='approved'), key=lambda r: (r['ai_score'], r['ai_confidence'], r['created_at'], r['id']), reverse=True)
            snapshot['resources'].append({'resource_id': resource['id'], 'eligible_before_ai':len(rows), 'approved':len(approved), 'previous_selection':rows[0] if rows else None, 'ai_selection':approved[0] if approved else None})
        target = folder / (account + '.json')
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        with (folder / (account + '.stdout.log')).open('w', encoding='utf-8') as out, (folder / (account + '.stderr.log')).open('w', encoding='utf-8') as err:
            result = subprocess.run([sys.executable, '-u', str(ROOT / 'main.py'), 'promotion', '--account', account, '--max-comments', '1'], cwd=ROOT, stdout=out, stderr=err)
        snapshot.update(exit_code=result.returncode, ended_at=datetime.now(timezone.utc).isoformat(), records=[dict(r) for r in db.execute('SELECT id,video_url,publish_status,verification_status,remote_comment_id,created_at FROM promotion_records WHERE id>? AND account_id=?', (before,account_id))])
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
    db.close()


if __name__ == '__main__':
    main()
