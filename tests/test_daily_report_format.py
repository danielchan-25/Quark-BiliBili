import contextlib
import io
import json
import os
import runpy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediaflow.database import Database
from mediaflow.daily_report import build_daily_report, daily_report, render_daily_report
from mediaflow.delivery import DeliveryUncertain
from mediaflow.notify import NotificationRejected
from mediaflow.promotion_schedule import ACCOUNT_HOURS, DEFAULT_ACCOUNT
from mediaflow.scheduled_notifications import BEIJING


class GroupedDailyReportTests(unittest.TestCase):
    day = '2026-10-06'
    accounts = (DEFAULT_ACCOUNT, 'bilibili-100000002')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(self.root/'logs')))
        for method in ('__enter__', 'publish_comment', 'watch_and_like'):
            self.stack.enter_context(patch('mediaflow.bilibili.BilibiliClient.'+method,
                side_effect=AssertionError('Daily report must not touch a browser')))
        self.settings = SimpleNamespace(database_path=self.root/'db')
        self.db = Database(self.settings.database_path)
        self.addCleanup(self.db.close)
        for index, account in enumerate(self.accounts, 1):
            self.db.connection.execute("INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(?,?,'bilibili','promotion',?,?)",
                                       (index, account, f'mock{index}', account.removeprefix('bilibili-')))
        self.db.connection.commit()
        self.folder = self.root/'data/scheduled-runs'
        self.folder.mkdir(parents=True)
        self.notify = Mock(webhook='configured')
        self.now = datetime(2026,10,7,0,1,tzinfo=BEIJING)

    def state(self, account, hour, stage='completed', **extra):
        data = dict(account=account, slot=f'{self.day}T{hour:02}:40:00+08:00', stage=stage,
                    results=[dict(id=100+hour, bvid=f'BVvideo{hour}', publish_status='published', verification_status='verified')]
                    if stage == 'completed' else [])
        data.update(extra)
        path = self.folder/f'quark-{account}-20261006-{hour:02}40.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def sample(self):
        first, second = self.accounts
        self.state(first,0,'stopped',reason='local_cdp_unavailable',comments_submitted=0)
        self.state(first,5,'stopped',reason='candidate_preparation_failed',error_type='TimeoutError',comments_submitted=0)
        self.state(first,10,history=[dict(stage='stopped', reason='candidate_preparation_failed')], error_type='TimeoutError')
        self.state(first,20)
        for hour in ACCOUNT_HOURS[second]:
            self.state(second,hour)

    def call(self, **kwargs):
        return daily_report(self.day, accounts=self.accounts, settings=self.settings,
                            notifier=self.notify, root=self.root, now=self.now, **kwargs)

    def receipt(self, account):
        return self.root/'data/daily-reports'/f'{account}-{self.day}-receipt.json'

    def test_sample_is_one_message_with_consistent_totals_and_sorted_times(self):
        self.sample()
        before = '\n'.join(self.db.connection.iterdump())
        result = self.call()
        self.assertEqual(result['status'], 'sent')
        self.notify.send_text.assert_called_once()
        message = self.notify.send_text.call_args.args[0]
        self.assertTrue(message.startswith('【Quark-BiliBili】\n2026-10-06\n任务状态：成功7,失败3\n\n账户: 100000001\n'))
        self.assertEqual(message.count(',任务成功：'),7)
        self.assertEqual(message.count(',任务失败：'),3)
        self.assertIn('- 00:40,任务失败：（浏览器 CDP 连接不可用',message)
        self.assertIn('- 05:40,任务失败：（候选或文案准备阶段请求超时，TimeoutError）',message)
        self.assertIn('- 10:40,任务成功：https://www.bilibili.com/video/BVvideo10（恢复成功）',message)
        self.assertIn('- 15:40,任务失败：（未找到到期任务的执行记录）',message)
        self.assertIn('\n\n账户: 100000002\n- 01:40,任务成功：',message)
        first_part = message.split('账户: 100000002')[0]
        self.assertEqual([line[2:7] for line in first_part.splitlines() if line.startswith('- ')],
                         ['00:40','05:40','10:40','15:40','20:40'])
        self.assertEqual((result['task_success'], result['task_failed']), (7,3))
        self.assertEqual(before,'\n'.join(self.db.connection.iterdump()))
        for account in self.accounts:
            self.assertEqual(json.loads(self.receipt(account).read_text())['status'],'sent')

    def test_group_and_single_account_calls_share_delivery_deduplication(self):
        self.sample()
        self.call()
        self.assertEqual(self.call()['status'],'already_sent')
        result = daily_report(self.day, self.accounts[0], settings=self.settings,
                              notifier=self.notify, root=self.root, now=self.now)
        self.assertEqual(result['status'],'already_sent')
        self.notify.send_text.assert_called_once()

    def test_previously_sent_account_is_not_in_a_new_group_message(self):
        self.sample()
        daily_report(self.day,self.accounts[0],settings=self.settings,notifier=self.notify,root=self.root,now=self.now)
        first_receipt = self.receipt(self.accounts[0]).read_bytes()
        result = self.call()
        self.assertEqual(result['sent_accounts'],[self.accounts[1]])
        message = self.notify.send_text.call_args.args[0]
        self.assertNotIn('账户: 100000001',message)
        self.assertIn('任务状态：成功5,失败0',message)
        self.assertEqual(first_receipt,self.receipt(self.accounts[0]).read_bytes())
        self.assertEqual(self.notify.send_text.call_count,2)

    def test_unknown_delivery_is_preserved_for_both_accounts_without_retry(self):
        self.sample()
        self.notify.send_text.side_effect = TimeoutError('private body https://private.invalid/resource')
        with self.assertRaises(TimeoutError):
            self.call()
        for account in self.accounts:
            receipt = json.loads(self.receipt(account).read_text())
            self.assertEqual(receipt['status'],'unknown')
            self.assertNotIn('private body',json.dumps(receipt))
        with self.assertRaises(DeliveryUncertain):
            self.call()
        self.notify.send_text.assert_called_once()

    def test_known_rejection_marks_group_and_account_receipts_failed(self):
        self.sample()
        self.notify.send_text.side_effect = NotificationRejected('reject')
        with self.assertRaises(NotificationRejected):
            self.call()
        for account in self.accounts:
            self.assertEqual(json.loads(self.receipt(account).read_text())['status'],'failed')

    def test_uncertain_account_does_not_block_the_other_accounts_report(self):
        self.sample()
        path = self.receipt(self.accounts[0]);path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(status='unknown',account=self.accounts[0],day=self.day)))
        result = self.call()
        self.assertEqual(result['sent_accounts'],[self.accounts[1]])
        self.assertEqual(result['uncertain_accounts'],[dict(account=self.accounts[0],status='unknown')])
        self.assertEqual(json.loads(path.read_text())['status'],'unknown')
        self.assertNotIn('账户: 100000001',self.notify.send_text.call_args.args[0])
        self.notify.send_text.assert_called_once()

    def test_preview_does_not_send_construct_a_notifier_or_overwrite_history(self):
        self.sample()
        self.call()
        snapshots = {path:path.read_bytes() for path in (self.root/'data/daily-reports').glob('*')}
        self.notify.reset_mock()
        with patch('mediaflow.daily_report.FeishuNotifier',side_effect=AssertionError('No preview notifications')):
            result = daily_report(self.day,accounts=self.accounts,preview=True,
                                  settings=self.settings,root=self.root,now=self.now)
        self.assertEqual(result['status'],'preview')
        self.assertIn('任务状态：成功7,失败3',result['message'])
        self.notify.send_text.assert_not_called()
        self.assertTrue(all(path.read_bytes() == saved for path,saved in snapshots.items()))

    def test_accepted_unconfirmed_comment_is_pending_and_never_success(self):
        self.sample()
        self.state(self.accounts[1],21,'requires_readonly_recheck',comments_submitted=1,
                   results=[dict(id=321,bvid='BVpending',publish_status='submitted',verification_status='unconfirmed')])
        result = self.call(preview=True)
        self.assertIn('任务状态：成功6,失败3,待确认1',result['message'])
        self.assertIn('- 21:40,任务待确认：（提交或执行结果待确认',result['message'])
        self.assertNotIn('- 21:40,任务成功',result['message'])
        self.notify.send_text.assert_not_called()

    def test_preparation_failed_before_submission_counts_as_failure(self):
        self.sample()
        self.db.connection.execute("""INSERT INTO task_runs(id,account_id,task_type,status,started_at,detail)
            VALUES(56,1,'promotion','FAILED','2026-10-06 02:44:00',?)""",
            ('reason=CommentPreparationError: 评论准备失败 (stage=comment_section, attempts=2, error=TimeoutError)',))
        self.db.connection.commit()
        self.state(self.accounts[0],10,'requires_readonly_recheck',comments_submitted=0,
            error_type='CommentPreparationError',task_runs=[dict(id=56,status='FAILED')])
        result = self.call(preview=True)
        self.assertEqual((result['task_success'],result['task_failed'],result['task_pending']),(6,4,0))
        self.assertIn('- 10:40,任务失败：（评论区加载超时，TimeoutError）',result['message'])
        self.notify.send_text.assert_not_called()

    def test_zero_records_without_preparation_evidence_remains_uncertain(self):
        self.sample()
        self.state(self.accounts[0],10,'requires_readonly_recheck',comments_submitted=0,
            error_type='RuntimeError',task_runs=[dict(id=56,status='FAILED')])
        result = self.call(preview=True)
        self.assertEqual((result['task_success'],result['task_failed'],result['task_pending']),(6,3,1))
        self.assertIn('- 10:40,任务待确认：',result['message'])
        self.notify.send_text.assert_not_called()

    def test_activation_excludes_earlier_slots_and_future_preparation_is_not_failed(self):
        account = self.accounts[1]
        folder = self.root/'data/promotion-schedules';folder.mkdir(parents=True)
        (folder/f'{account}.json').write_text(json.dumps(dict(account=account,active_from=self.day+'T08:00:00+08:00')))
        self.state(account,16,'ready')
        report = build_daily_report(self.day,(account,),settings=self.settings,root=self.root,
            now=datetime(2026,10,6,11,39,tzinfo=BEIJING))
        self.assertEqual(report['accounts'][0]['entries'],[])
        self.assertEqual(report['task_failed'],0)
        report = build_daily_report(self.day,(account,),settings=self.settings,root=self.root,
            now=datetime(2026,10,6,11,41,tzinfo=BEIJING))
        self.assertEqual(report['accounts'][0]['missing_slots'],['11:40'])
        self.assertNotIn('16:40',render_daily_report(report))

    def test_corrupt_account_state_does_not_block_other_accounts_statistics(self):
        self.sample()
        (self.folder/f'quark-{self.accounts[0]}-20261006-0040.json').write_text('{invalid json')
        result = self.call()
        self.assertEqual(result['account_errors'],[dict(account=self.accounts[0],error_type='JSONDecodeError')])
        message = self.notify.send_text.call_args.args[0]
        self.assertIn('任务状态：成功5,失败0,统计异常1个账户',message)
        self.assertIn('日报统计失败：',message)
        self.assertIn('- 21:40,任务成功：',message)
        self.notify.send_text.assert_called_once()

    def test_private_reason_is_not_copied_into_notification(self):
        self.sample()
        self.state(self.accounts[0],0,'failed',reason='private https://private.invalid/resource Cookie=private',
                   error_type='RuntimeError')
        result = self.call(preview=True)
        self.assertNotIn('private.invalid',result['message'])
        self.assertNotIn('Cookie=',result['message'])
        self.assertIn('任务执行异常，请检查本地记录，RuntimeError',result['message'])

    def test_original_trigger_date_remains_when_run_after_midnight(self):
        self.sample()
        result = self.call(preview=True)
        self.assertTrue(result['message'].startswith('【Quark-BiliBili】\n2026-10-06\n'))
        self.assertEqual(result['task_failed'],3)

    def test_duplicate_accounts_are_rejected_before_any_notification(self):
        with self.assertRaises(ValueError):
            daily_report(self.day,accounts=(self.accounts[0],self.accounts[0]),settings=self.settings,
                         notifier=self.notify,root=self.root,now=self.now)
        self.notify.send_text.assert_not_called()

    def run_cli(self, args):
        class Output(io.StringIO):
            def reconfigure(self, **kwargs):
                pass
        output = Output()
        with patch('sys.argv',['tools/daily_report.py',*args]), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(Path(__file__).resolve().parents[1]/'tools/daily_report.py'),run_name='__main__')
        self.assertEqual(stopped.exception.code,1)
        return json.loads(output.getvalue())

    def test_cli_preview_failure_does_not_send_an_alert(self):
        with patch('mediaflow.daily_report.daily_report',side_effect=ValueError('private')), \
             patch('mediaflow.notify.FeishuNotifier',side_effect=AssertionError('No preview alert')) as factory:
            result = self.run_cli(['--preview','--date',self.day])
        self.assertEqual(result,dict(status='failed',error_type='ValueError'))
        factory.assert_not_called()

    def test_partial_report_cli_sends_an_independent_failure_alert(self):
        summary = dict(status='sent',date=self.day,account_errors=[dict(account=self.accounts[0],error_type='JSONDecodeError')])
        settings = SimpleNamespace(feishu_webhook='configured',feishu_secret=None)
        with patch('mediaflow.daily_report.daily_report',return_value=summary), \
             patch('mediaflow.promotion_schedule.require_local_schedule',return_value=(self.accounts[0],{})), \
             patch('mediaflow.config.Settings.load',return_value=settings), \
             patch('mediaflow.config.ROOT',self.root), \
             patch('mediaflow.notify.FeishuNotifier',return_value=self.notify):
            result = self.run_cli(['--accounts',*self.accounts,'--date',self.day])
        self.assertTrue(result['failure_alert_sent'])
        self.notify.send_text.assert_called_once()
        self.assertIn(self.accounts[0],self.notify.send_text.call_args.args[0])
        self.assertNotIn(self.accounts[1],self.notify.send_text.call_args.args[0])

    def test_cli_missing_schedule_never_sends_a_report_or_failure_alert(self):
        with patch('mediaflow.promotion_schedule.require_local_schedule',side_effect=ValueError('missing')), \
             patch('mediaflow.daily_report.daily_report') as report, \
             patch('mediaflow.notify.FeishuNotifier') as notify:
            result = self.run_cli(['--date',self.day])
        self.assertEqual(result['reason'],'schedule_configuration_required')
        report.assert_not_called()
        notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
