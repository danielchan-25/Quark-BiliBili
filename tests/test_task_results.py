import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from mediaflow.database import Database
from main import promotion, promotion_summary
from tools.import_phrases import import_phrases


class TaskResultTests(unittest.TestCase):
    def test_exception_finishes_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'db')
            db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES('a','bilibili','promotion','test','1')")
            db.connection.commit()
            from unittest.mock import Mock
            notify = Mock(webhook='configured')
            with patch('main._promotion', side_effect=RuntimeError('CDP failed')), patch('main.write_audit_event'):
                with self.assertRaises(RuntimeError):
                    promotion(db, notify, argparse.Namespace(account='a', slot='00:41'))
            notify.send_text.assert_called_once()
            message = notify.send_text.call_args.args[0]
            self.assertTrue(message.startswith('【Quark-BiliBili】\n\n- '))
            self.assertIn('平台=BiliBili, 账户=a, 失败，原因=RuntimeError: CDP failed', message)
            row = db.connection.execute('SELECT * FROM task_runs').fetchone()
            self.assertEqual(row['status'], 'FAILED')
            self.assertIsNotNone(row['ended_at'])
            self.assertIn('CDP failed', row['detail'])
            db.close()

    def test_success_does_not_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'db')
            db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES('a','bilibili','promotion','test','1')")
            db.connection.commit()
            from unittest.mock import Mock
            notify = Mock(webhook='configured')

            def complete(db, notify, args, run_id):
                db.connection.execute("UPDATE task_runs SET status='SUCCESS',detail='slot=00:41; reason=; url=video' WHERE id=?", (run_id,))
                db.connection.commit()
                return 0

            with patch('main._promotion', side_effect=complete):
                self.assertEqual(promotion(db, notify, argparse.Namespace(account='a', slot='00:41')), 0)
            notify.send_text.assert_not_called()
            db.close()

    def test_summary_binary_and_import_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'db')
            db.connection.execute("INSERT INTO accounts(id,name,platform,role,cdp_url) VALUES(1,'a','bilibili','promotion','test')")
            for status in ['SUCCESS','RUNNING','SKIPPED','PENDING','FAILED']:
                db.connection.execute("INSERT INTO task_runs(account_id,task_type,status,started_at,detail) VALUES(1,'promotion',?,'2026-09-22 01:00:00','slot=')", (status,))
            from unittest.mock import Mock
            notify = Mock()
            with patch('main.promotion_links', return_value=[]):
                promotion_summary(db, notify, argparse.Namespace(date='2026-09-22'))
            message = notify.send_text.call_args.args[0]
            self.assertIn('成功1, 失败4', message)
            self.assertIn('09:00', message)
            self.assertNotIn('RUNNING', message)
            db.connection.execute("INSERT INTO promotion_resources(category,url,keywords) VALUES('AI','x','x')")
            rows = [{'category':'AI','text':'这里有一份相关学习资料，按需参考：{url}'}]
            self.assertEqual(import_phrases(db, rows), 1)
            self.assertEqual(import_phrases(db, rows), 0)
            db.close()
