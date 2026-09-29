import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, call
from mediaflow.database import Database
from mediaflow.daily_review import daily_review


class ReviewTests(unittest.TestCase):
    def test_fresh_results_saved_before_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(Path(tmp)/'db')
            db.connection.execute("INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(1,'a','bilibili','promotion','test','1')")
            for n in range(3):
                db.connection.execute("INSERT INTO promotion_records(account_id,platform,video_url,bvid,text,content_hash,publish_status,created_at) VALUES(1,'bilibili',?,?,?,'hash','published','2026-09-23 01:00:00')",(str(n),str(n),'text'))
            db.connection.commit()
            notify=Mock(webhook='configured')
            with patch('mediaflow.daily_review.BilibiliClient') as browser, patch('mediaflow.daily_review.write_audit_event') as audit, patch('mediaflow.daily_review.time.sleep') as sleep, patch('mediaflow.daily_review.random.uniform', side_effect=[12,18]):
                client=browser.return_value.__enter__.return_value
                client.uid.return_value='1'
                client.verify_comment.side_effect=['rpid',None,RuntimeError('CDP error')]
                result=daily_review(db,notify,argparse.Namespace(date='2026-09-23'))
                self.assertEqual(sleep.call_args_list, [call(12), call(18)])
                self.assertEqual(sum(c.args[1] == 'review_wait' for c in audit.call_args_list), 2)
            self.assertEqual(result,1)
            self.assertEqual([r[0] for r in db.connection.execute('SELECT status FROM comment_rechecks ORDER BY id')],['SUCCESS','FAILED','FAILED'])
            self.assertEqual([r[0] for r in db.connection.execute('SELECT verification_status FROM promotion_records ORDER BY id')],['recheck_verified','recheck_not_found','recheck_error'])
            message=notify.send_text.call_args.args[0]
            self.assertTrue(message.startswith('【Quark-BiliBili】\n\n- 01:00: 平台=BiliBili, 账户=a, 成功'))
            self.assertIn('- 01:00: 平台=BiliBili, 账户=a, 失败，原因=',message)
            self.assertEqual(message.count('平台=BiliBili'),3)
            self.assertEqual(db.connection.execute("SELECT count(*) FROM promotion_records WHERE publish_status='published'").fetchone()[0],3)
            db.close()
