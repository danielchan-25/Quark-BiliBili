import argparse
import json
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
            self.assertTrue(message.startswith('【Quark-BiliBili】\n\n2026-09-23 日报'))
            self.assertIn('当日发布记录：已发布 3',message)
            self.assertIn('发布记录=已发布，本次复核=无法核验',message)
            self.assertIn('本次复核：通过 1，暂未找到 1，无法核验 1',message)
            self.assertEqual(message.count('平台=BiliBili'),3)
            self.assertEqual(db.connection.execute("SELECT count(*) FROM promotion_records WHERE publish_status='published'").fetchone()[0],3)
            db.close()

    def test_preflight_stop_is_reported_without_a_publication_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            folder=root/'data/scheduled-runs'
            folder.mkdir(parents=True)
            state={'account':'a','slot':'2026-10-03T20:40:00+08:00','stage':'stopped','reason':'local_cdp_unavailable','comments_submitted':0}
            (folder/'quark-bilibili-a.json').write_text(json.dumps(state),encoding='utf-8')
            db=Database(root/'db')
            notify=Mock(webhook='configured')
            with patch('mediaflow.scheduled_notifications.ROOT',root), patch('mediaflow.daily_review.ROOT',root):
                daily_review(db,notify,argparse.Namespace(date='2026-10-03'))
            message=notify.send_text.call_args.args[0]
            self.assertIn('当日发布记录：已发布 0',message)
            self.assertIn('定时任务停止或异常：1',message)
            self.assertIn('20:40 定时任务',message)
            self.assertIn('浏览器 CDP 连接不可用',message)
            db.close()

    def test_delivery_failure_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(Path(tmp)/'db')
            notify=Mock(webhook='configured')
            notify.send_text.side_effect=RuntimeError('Delivery failed')
            with patch('mediaflow.daily_review.scheduled_states',return_value=[]), patch('mediaflow.daily_review.write_audit_event') as audit:
                with self.assertRaises(RuntimeError):
                    daily_review(db,notify,argparse.Namespace(date='2026-10-03'))
                self.assertIn('notification_failed',[call.args[1] for call in audit.call_args_list])
                self.assertNotIn('daily_review_notified',[call.args[1] for call in audit.call_args_list])
            db.close()
