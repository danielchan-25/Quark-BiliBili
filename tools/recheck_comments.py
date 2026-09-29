"""Read-only browser recheck of historical submissions; preserve original records."""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediaflow.config import ROOT
from mediaflow.bilibili import BilibiliClient
from mediaflow.audit import write_audit_event


if __name__ == '__main__':
    start, end = sys.argv[1:3]
    log = ROOT / 'logs' / ('comment-recheck-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.jsonl')
    db = sqlite3.connect('file:' + (ROOT / 'data/mediaflow.db').as_posix() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT p.*,a.name,a.uid,a.cdp_url,r.url AS resource_url FROM promotion_records p
        JOIN accounts a ON a.id=p.account_id LEFT JOIN promotion_resources r ON r.id=p.resource_id
        WHERE p.platform='bilibili' AND p.created_at>=? AND p.created_at<=? ORDER BY a.id,p.id""", (start,end)).fetchall()
    print(str(log), flush=True)
    write_audit_event(log,'recheck_started',start_beijing=start,end_beijing=end,records=len(rows))
    for row in rows:
        outcome = dict(record_id=row['id'],account=row['name'],video_url=row['video_url'],original_status=row['publish_status'],original_comment_id=row['remote_comment_id'])
        try:
            with BilibiliClient(row['cdp_url']) as client:
                if client.uid() != row['uid']:
                    raise RuntimeError('CDP UID mismatch')
                remote = client.verify_comment(row['bvid'],row['uid'],row['text'],row['resource_url'] or '',row['remote_comment_id'])
                outcome.update(result='verified_now' if remote else 'not_found_in_checked_pages',comment_id=remote)
        except Exception as exc:
            outcome.update(result='verification_error',error=str(exc)[:1200])
        write_audit_event(log,'comment_rechecked',**outcome)
        print(json.dumps(outcome,ensure_ascii=False),flush=True)
    write_audit_event(log,'recheck_completed',records=len(rows))
    db.close()
