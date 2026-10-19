import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediaflow.database import Database
from tools.scheduled_promotion import run, ACCOUNT


class ScheduledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.audit_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.audit_dir.cleanup)
        audit = patch.dict(os.environ, MEDIAFLOW_LOG_DIR=self.audit_dir.name)
        audit.start()
        self.addCleanup(audit.stop)
        maintenance = patch('tools.scheduled_promotion.maintain_candidate_pool', return_value={'status': 'healthy'})
        self.maintenance = maintenance.start()
        self.addCleanup(maintenance.stop)

    @contextlib.contextmanager
    def second_account(self, browser_uid='1085803742'):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            (root/'data').mkdir()
            name = 'bilibili-1085803742'
            (root/'data/account.csv').write_text(
                '账号,角色,启用状态,推广链接,CDP地址\n'+name+',推广,启用,mock,newmock\n', encoding='utf-8')
            db = Database(root/'db')
            db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES(?,'bilibili','promotion','oldmock','35199508')", (ACCOUNT,))
            db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES(?,'bilibili','promotion','newmock','1085803742')", (name,))
            db.connection.execute("INSERT INTO promotion_resources(category,url,keywords) VALUES('test','mock','test')")
            db.connection.commit()
            db.close()
            settings = SimpleNamespace(database_path=root/'db', feishu_webhook='mock', feishu_secret=None)
            notify = Mock(webhook='mock')
            stack.enter_context(patch('tools.scheduled_promotion.ROOT', root))
            stack.enter_context(patch('tools.scheduled_promotion.Settings.load', return_value=settings))
            stack.enter_context(patch('tools.scheduled_promotion.FeishuNotifier', return_value=notify))
            browser = stack.enter_context(patch('tools.scheduled_promotion.BilibiliClient'))
            browser.return_value.__enter__.return_value.uid.return_value = browser_uid
            stack.enter_context(patch('tools.scheduled_promotion.workflow.available_visible_candidate', return_value=SimpleNamespace(bvid='BVnew', title='test', url='video')))
            stack.enter_context(patch('tools.scheduled_promotion.workflow.choose_phrase', return_value=('text','mock',None)))
            promote = stack.enter_context(patch('tools.scheduled_promotion.workflow.promotion'))
            yield root, name, notify, browser, promote

    def test_second_account_publish_uses_its_identity_and_own_state(self):
        with self.second_account() as (root, name, notify, browser, promote):
            def publish(db, notifier, args):
                self.assertIs(notifier, notify)
                self.assertEqual(args.account, name)
                self.assertEqual(args.max_comments, 1)
                self.assertFalse(args.dry_run)
                self.assertIsNone(args.slot)
                db.connection.execute("INSERT INTO task_runs(account_id,task_type,status) VALUES(2,'promotion','SUCCESS')")
                db.connection.execute("""INSERT INTO promotion_records(account_id,platform,video_url,bvid,text,content_hash,publish_status,verification_status)
                    VALUES(2,'bilibili','video','BVnew','text','hash','published','verified')""")
                db.connection.commit()
                return 0
            promote.side_effect = publish
            slot = '2026-10-09T01:40:00+08:00'
            self.assertEqual(run(slot, account=name)['stage'], 'ready')
            result = run(slot, 'publish', account=name)
            self.assertEqual(result['stage'], 'completed')
            self.assertEqual(result['comments_submitted'], 1)
            self.assertEqual(result['results'][0]['bvid'], 'BVnew')
            self.assertEqual(run(slot, account=name)['stage'], 'already_exists')
            promote.assert_called_once()
            for call in browser.call_args_list:
                self.assertEqual(call.args, ('newmock',))
            states = list((root/'data/scheduled-runs').glob('quark-*.json'))
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].name, 'quark-bilibili-1085803742-20261009-0140.json')
            self.assertEqual(json.loads(states[0].read_text())['account'], name)
            notify.send_text.assert_not_called()

    def test_second_account_rejects_old_account_uid_and_alerts(self):
        with self.second_account(browser_uid='35199508') as (root, name, notify, browser, promote):
            result = run('2026-10-09T06:40:00+08:00', account=name)
            self.assertEqual(result['reason'], 'browser_not_logged_in_as_expected_uid')
            self.assertTrue(result['alert_sent'])
            notify.send_text.assert_called_once()
            self.assertIn(name, notify.send_text.call_args.args[0])
            promote.assert_not_called()

    def test_publish_preflight_can_change_target_without_resuming_or_extra_submit(self):
        with self.second_account() as (root, name, notify, browser, promote):
            slot = '2026-10-09T01:40:00+08:00'
            self.assertEqual(run(slot, account=name)['stage'], 'ready')
            path = root/'data/scheduled-runs/quark-bilibili-1085803742-20261009-0140.json'
            original_attempt = json.loads(path.read_text())['attempt_id']
            promote.return_value = 0
            with patch('tools.scheduled_promotion.workflow.available_visible_candidate',
                       return_value=SimpleNamespace(bvid='BVfallback', title='test', url='video2')):
                self.assertEqual(run(slot,'publish',account=name)['stage'], 'completed')
            state = json.loads(path.read_text())
            self.assertEqual(state['attempt_id'], original_attempt)
            self.assertEqual(state['candidate_changes'], [dict(previous_bvid='BVnew', bvid='BVfallback')])
            self.assertEqual(state['bvid'], 'BVfallback')
            promote.assert_called_once()
            notify.send_text.assert_not_called()

    def test_video_specific_phrase_cannot_follow_a_changed_target(self):
        with self.second_account() as (root, name, notify, browser, promote):
            slot = '2026-10-09T01:40:00+08:00'
            self.assertEqual(run(slot, account=name)['stage'], 'ready')
            with patch('tools.scheduled_promotion.workflow.available_visible_candidate',
                       return_value=SimpleNamespace(bvid='BVfallback', title='test', url='video2')):
                result = run(slot,'publish',phrase_file='unused.json',account=name)
            self.assertEqual(result['stage'],'ready')
            self.assertFalse(result['phrase_ready'])
            self.assertEqual(result['reason'],'candidate_changed_regenerate_phrase')
            promote.assert_not_called()
            notify.send_text.assert_not_called()

    def test_phrase_exhaustion_during_publish_returns_ready_to_replenish(self):
        with self.second_account() as (root,name,notify,browser,promote):
            slot='2026-10-09T01:40:00+08:00'
            self.assertEqual(run(slot,account=name)['stage'],'ready')
            with patch('tools.scheduled_promotion.workflow.choose_phrase',side_effect=ValueError('exhausted')):
                result=run(slot,'publish',account=name)
            self.assertEqual(result['stage'],'ready')
            self.assertFalse(result['phrase_ready'])
            promote.assert_not_called()
            notify.send_text.assert_not_called()

    def test_dom_probe_failure_is_not_reported_as_comment_section_timeout(self):
        from mediaflow.bilibili import CommentPreparationError
        with self.second_account() as (root,name,notify,browser,promote):
            with patch('tools.scheduled_promotion.workflow.available_visible_candidate',
                       side_effect=CommentPreparationError('dom_probe',3,'TimeoutError')):
                result=run('2026-10-09T01:40:00+08:00',account=name)
            self.assertEqual(result['reason'],'browser_dom_access_failed')
            state=json.loads((root/'data/scheduled-runs/quark-bilibili-1085803742-20261009-0140.json').read_text())
            self.assertEqual(state['error_stage'],'dom_probe')
            self.assertEqual(state['comments_submitted'],0)
            self.assertIn('页面自动化读取超时',notify.send_text.call_args.args[0])
            promote.assert_not_called()

    def test_comment_loading_failure_has_specific_alert_and_stage(self):
        from mediaflow.bilibili import CommentPreparationError
        with self.second_account() as (root,name,notify,browser,promote):
            with patch('tools.scheduled_promotion.workflow.available_visible_candidate',
                       side_effect=CommentPreparationError('comment_section',2,'TimeoutError')):
                result=run('2026-10-09T01:40:00+08:00',account=name)
            self.assertEqual(result['reason'],'comment_section_load_failed')
            state=json.loads((root/'data/scheduled-runs/quark-bilibili-1085803742-20261009-0140.json').read_text())
            self.assertEqual(state['error_stage'],'comment_section')
            self.assertEqual(state['underlying_error_type'],'TimeoutError')
            self.assertIn('评论区加载失败',notify.send_text.call_args.args[0])
            promote.assert_not_called()

    def test_optional_refill_failure_keeps_existing_live_candidate_ready(self):
        with self.second_account() as (root, name, notify, browser, promote):
            self.maintenance.return_value = {'status':'failed','reason':'candidate_pool_refill_failed','error_type':'TimeoutError'}
            result = run('2026-10-09T06:40:00+08:00', account=name)
            self.assertEqual(result['stage'], 'ready')
            self.assertEqual(result['candidate_pool']['status'], 'failed')
            self.assertEqual(result['bvid'], 'BVnew')
            notify.send_text.assert_not_called()
            promote.assert_not_called()

    def test_empty_pool_refill_failure_stops_and_alerts_without_submitting(self):
        with self.second_account() as (root, name, notify, browser, promote):
            self.maintenance.return_value = {'status':'failed','reason':'candidate_pool_refill_failed'}
            with patch('tools.scheduled_promotion.workflow.available_visible_candidate', return_value=None):
                result = run('2026-10-09T06:40:00+08:00', account=name)
            self.assertEqual(result['stage'], 'stopped')
            self.assertEqual(result['reason'], 'candidate_pool_refill_failed')
            self.assertTrue(result['alert_sent'])
            state = json.loads((root/'data/scheduled-runs/quark-bilibili-1085803742-20261009-0640.json').read_text())
            self.assertEqual(state['comments_submitted'], 0)
            notify.send_text.assert_called_once()
            promote.assert_not_called()

    def test_stopped_round_is_not_automatically_refilled_or_resumed(self):
        with self.second_account(browser_uid='35199508') as (root, name, notify, browser, promote):
            slot = '2026-10-09T06:40:00+08:00'
            self.assertEqual(run(slot, account=name)['stage'], 'stopped')
            self.assertEqual(run(slot, account=name)['stage'], 'already_exists')
            self.maintenance.assert_not_called()
            notify.send_text.assert_called_once()
            promote.assert_not_called()

    def test_account_specific_slots_reject_swapped_schedule(self):
        with self.second_account() as (root, name, notify, browser, promote):
            for hour in (0, 5, 10, 15, 20):
                with self.assertRaises(ValueError):
                    run(f'2026-10-09T{hour:02}:40:00+08:00', account=name)
            for hour in (1, 6, 11, 16, 21):
                with self.assertRaises(ValueError):
                    run(f'2026-10-09T{hour:02}:40:00+08:00', account=ACCOUNT)
                self.assertEqual(run(f'2026-10-09T{hour:02}:40:00+08:00', account=name)['stage'], 'ready')
            with self.assertRaises(ValueError):
                run('2026-10-09T01:40:00+08:00', account='bilibili-unknown')
            promote.assert_not_called()

    def test_second_account_slot_normalizes_beijing_date(self):
        with self.second_account() as (root, name, notify, browser, promote):
            self.assertEqual(run('2026-10-08T17:40:00+00:00', account=name)['stage'], 'ready')
            state = json.loads((root/'data/scheduled-runs/quark-bilibili-1085803742-20261009-0140.json').read_text())
            self.assertEqual(state['slot'], '2026-10-09T01:40:00+08:00')

    def test_phrase_preparation_failure_alerts_before_main_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data').mkdir()
            (root/'data/account.csv').write_text('账号,角色,启用状态,推广链接\n'+ACCOUNT+',推广,启用,mock\n',encoding='utf-8')
            db=Database(root/'db')
            db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES(?,'bilibili','promotion','mock','35199508')",(ACCOUNT,))
            db.connection.execute("INSERT INTO promotion_resources(category,url,keywords) VALUES('test','mock','test')")
            db.connection.commit();db.close()
            settings=SimpleNamespace(database_path=root/'db',feishu_webhook='mock',feishu_secret=None)
            notify=Mock(webhook='mock')
            candidate=SimpleNamespace(bvid='BVtest',title='test',url='video')
            bad_file=root/'bad.json';bad_file.write_text('invalid JSON')
            with patch('tools.scheduled_promotion.ROOT',root), patch('tools.scheduled_promotion.Settings.load',return_value=settings), patch('tools.scheduled_promotion.FeishuNotifier',return_value=notify), patch('tools.scheduled_promotion.BilibiliClient') as browser, patch('tools.scheduled_promotion.workflow.available_visible_candidate',return_value=candidate), patch('tools.scheduled_promotion.workflow.choose_phrase',return_value=('text','mock',None)), patch('tools.scheduled_promotion.workflow.promotion') as promote:
                browser.return_value.__enter__.return_value.uid.return_value='35199508'
                self.assertEqual(run('2026-10-07T15:40:00+08:00')['stage'],'ready')
                self.assertEqual(run('2026-10-07T15:40:00+08:00','publish',str(bad_file))['stage'],'stopped')
                notify.send_text.assert_called_once()
                promote.assert_not_called()

    def test_database_preflight_failure_is_alerted_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings=SimpleNamespace(database_path=root/'db',feishu_webhook='mock',feishu_secret=None)
            notify=Mock(webhook='mock')
            with patch('tools.scheduled_promotion.ROOT',root), patch('tools.scheduled_promotion.Settings.load',return_value=settings), patch('tools.scheduled_promotion.FeishuNotifier',return_value=notify), patch('tools.scheduled_promotion.Database',side_effect=RuntimeError('db error')):
                result=run('2026-10-07T15:40:00+08:00')
                self.assertEqual(result['stage'],'stopped')
                self.assertTrue(result['alert_sent'])
                again=run('2026-10-07T15:40:00+08:00')
                self.assertEqual(again['stage'],'already_exists')
            notify.send_text.assert_called_once()
            state=json.loads(next((root/'data/scheduled-runs').glob('*.json')).read_text())
            self.assertEqual(state['comments_submitted'],0)

    def test_browser_failure_and_resume_each_alert_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data').mkdir()
            (root/'data/account.csv').write_text('账号,角色,启用状态,推广链接\n'+ACCOUNT+',推广,启用,mock\n',encoding='utf-8')
            db=Database(root/'db')
            db.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES(?,'bilibili','promotion','mock','35199508')",(ACCOUNT,))
            db.connection.execute("INSERT INTO promotion_resources(category,url,keywords) VALUES('test','mock','test')")
            db.connection.commit();db.close()
            settings=SimpleNamespace(database_path=root/'db',feishu_webhook='mock',feishu_secret=None)
            notify=Mock(webhook='mock')
            with patch('tools.scheduled_promotion.ROOT',root), patch('tools.scheduled_promotion.Settings.load',return_value=settings), patch('tools.scheduled_promotion.FeishuNotifier',return_value=notify), patch('tools.scheduled_promotion.BilibiliClient',side_effect=RuntimeError('本地 CDP 服务不可用')), patch('tools.scheduled_promotion.workflow.promotion') as promote:
                self.assertEqual(run('2026-10-07T15:40:00+08:00')['reason'],'local_cdp_unavailable')
                self.assertEqual(run('2026-10-07T15:40:00+08:00',resume=True)['reason'],'local_cdp_unavailable')
                self.assertEqual(notify.send_text.call_count,2)
                promote.assert_not_called()
