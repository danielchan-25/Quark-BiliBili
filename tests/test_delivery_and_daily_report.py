import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediaflow.database import Database
from mediaflow.daily_report import daily_report
from mediaflow.delivery import DeliveryUncertain, deliver_once
from mediaflow.notify import NotificationRejected
from mediaflow.scheduled_notifications import alert_scheduled_stop


class DeliveryTests(unittest.TestCase):
    def test_success_receipt_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'receipt.json'
            notify=Mock(webhook='configured')
            self.assertTrue(deliver_once(path,notify,'test',kind='test'))
            self.assertFalse(deliver_once(path,notify,'test',kind='test'))
            notify.send_text.assert_called_once()
            self.assertEqual(json.loads(path.read_text())['status'],'sent')

    def test_unknown_send_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'receipt.json'
            notify=Mock(webhook='configured')
            notify.send_text.side_effect=TimeoutError()
            with self.assertRaises(TimeoutError):deliver_once(path,notify,'test',kind='test')
            with self.assertRaises(DeliveryUncertain):deliver_once(path,notify,'test',kind='test')
            notify.send_text.assert_called_once()
            self.assertEqual(json.loads(path.read_text())['status'],'unknown')

    def test_rejection_is_audited_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'receipt.json'
            notify=Mock(webhook='configured')
            notify.send_text.side_effect=NotificationRejected('reject')
            with self.assertRaises(NotificationRejected):deliver_once(path,notify,'test',kind='test')
            self.assertEqual(json.loads(path.read_text())['status'],'failed')

    def test_each_explicit_resume_failure_has_own_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json'
            state={'account':'a','slot':'2026-10-07T10:40:00+08:00','stage':'stopped','reason':'local_cdp_unavailable','comments_submitted':0,'attempt_id':'one'}
            path.write_text(json.dumps(state))
            notify=Mock(webhook='configured')
            self.assertTrue(alert_scheduled_stop(path,notify,Path(tmp)/'log.jsonl'))
            self.assertFalse(alert_scheduled_stop(path,notify,Path(tmp)/'log.jsonl'))
            state=json.loads(path.read_text());state['attempt_id']='two';path.write_text(json.dumps(state))
            self.assertTrue(alert_scheduled_stop(path,notify,Path(tmp)/'log.jsonl'))
            self.assertEqual(notify.send_text.call_count,2)


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.settings=SimpleNamespace(database_path=self.root/'db')
        self.db=Database(self.settings.database_path);self.addCleanup(self.db.close)
        self.db.connection.executescript("""INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(1,'a','bilibili','promotion','mock','1');
            INSERT INTO promotion_records(account_id,platform,video_url,bvid,text,content_hash,publish_status,verification_status,created_at)
            VALUES(1,'bilibili','video1','BVone','text','hash','published','verified','2026-10-06 00:41:00');
            INSERT INTO task_runs(account_id,task_type,status,started_at,detail) VALUES(1,'promotion','SUCCESS','2026-10-05 16:41:00','');
            INSERT INTO task_runs(account_id,task_type,status,started_at,detail) VALUES(1,'promotion','FAILED','2026-10-05 16:40:00','reason=dry-run');""")
        folder=self.root/'data/scheduled-runs';folder.mkdir(parents=True)
        (folder/'quark-bilibili-a.json').write_text(json.dumps({'account':'a','slot':'2026-10-06T20:40:00+08:00','stage':'stopped','reason':'local_cdp_unavailable'}))
        self.notify=Mock(webhook='configured')

    def call_report(self):
        return daily_report('2026-10-06','a',settings=self.settings,notifier=self.notify,root=self.root)

    def test_report_sends_without_browser_and_handles_beijing_boundary(self):
        before='\n'.join(self.db.connection.iterdump())
        with patch('mediaflow.bilibili.BilibiliClient.__enter__',side_effect=AssertionError('No browser')):
            result=self.call_report()
        self.assertEqual(result['status'],'sent')
        message=self.notify.send_text.call_args.args[0]
        self.assertTrue(message.startswith('【Quark-BiliBili】\n2026-10-06\n任务状态：成功0,失败5\n'))
        self.assertIn('账户: a',message)
        self.assertIn('- 20:40,任务失败：（浏览器 CDP 连接不可用',message)
        report=json.loads((self.root/'data/daily-reports/a-2026-10-06.json').read_text(encoding='utf-8'))
        self.assertEqual(report['published'],1)
        self.assertEqual(report['task_records_successful'],1)
        self.assertEqual(report['task_records_failed'],0)
        self.assertEqual(before,'\n'.join(self.db.connection.iterdump()))

    def test_daily_report_sent_once(self):
        self.call_report()
        self.assertEqual(self.call_report()['status'],'already_sent')
        self.notify.send_text.assert_called_once()

    def test_daily_report_unknown_is_not_reported_sent(self):
        self.notify.send_text.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.call_report()
        with self.assertRaises(DeliveryUncertain):self.call_report()
        self.notify.send_text.assert_called_once()

    def test_second_account_daily_report_uses_its_schedule_and_records(self):
        account = 'bilibili-1085803742'
        self.db.connection.execute("INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(2,?,'bilibili','promotion','newmock','1085803742')", (account,))
        self.db.connection.execute("""INSERT INTO promotion_records(account_id,platform,video_url,bvid,text,content_hash,publish_status,verification_status,created_at)
            VALUES(2,'bilibili','video2','BVtwo','text2','hash2','published','verified','2026-10-06 01:41:00')""")
        self.db.connection.commit()
        folder = self.root/'data/scheduled-runs'
        (folder/'quark-bilibili-1085803742-20261006-0140.json').write_text(json.dumps(
            {'account':account,'slot':'2026-10-06T01:40:00+08:00','stage':'completed'}))
        result = daily_report('2026-10-06',account,settings=self.settings,notifier=self.notify,root=self.root)
        self.assertEqual(result['published'], 1)
        report = json.loads((self.root/'data/daily-reports'/f'{account}-2026-10-06.json').read_text(encoding='utf-8'))
        self.assertEqual(report['missing_slots'], ['06:40','11:40','16:40','21:40'])
        self.assertEqual(report['scheduled_completed'], 1)
        self.assertEqual([r['bvid'] for r in report['records']], ['BVtwo'])

    def test_new_schedule_does_not_count_slots_before_activation_as_missing(self):
        account = 'bilibili-1085803742'
        folder = self.root/'data/promotion-schedules'
        folder.mkdir()
        (folder/f'{account}.json').write_text(json.dumps(
            {'account':account,'active_from':'2026-10-06T08:37:00+08:00'}))
        daily_report('2026-10-06',account,settings=self.settings,notifier=self.notify,root=self.root)
        report = json.loads((self.root/'data/daily-reports'/f'{account}-2026-10-06.json').read_text(encoding='utf-8'))
        self.assertEqual(report['missing_slots'], ['11:40','16:40','21:40'])
