import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from mediaflow.audit import audit_step, register_secrets
from mediaflow.bilibili import SubmissionUncertain, CommentPreparationError
from mediaflow.database import Database


class WorkflowSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(self.root/'logs')))
        self.stack.enter_context(patch('main.ROOT', self.root))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.db = Database(self.root/'test.db')
        self.addCleanup(self.db.close)
        self.db.connection.executescript("""
            INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(1,'a','bilibili','promotion','mock','1');
            INSERT INTO promotion_resources(id,category,url,keywords) VALUES(1,'资料','https://example.invalid/private','test');
            INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,title,status,ai_status,ai_score)
              VALUES(1,'bilibili','https://www.bilibili.com/video/BVtest1','BVtest1','test','available','approved',90);
            INSERT INTO phrases(category,text,source) VALUES('资料','相关资料可按需参考：{url}','test');
        """)
        self.client = Mock()
        self.client.uid.return_value = '1'
        self.client.video_visible.return_value = True
        self.client.publish_comment.return_value = 'network_confirmed'
        self.client.verify_comment.return_value = '123'
        factory = self.stack.enter_context(patch('main.BilibiliClient'))
        factory.return_value.__enter__.return_value = self.client
        self.stack.enter_context(patch('main.promotion_keywords', return_value=['test']))
        self.notify = Mock(webhook='mock')

    def run_flow(self, dry_run=False):
        return main.promotion(self.db, self.notify, argparse.Namespace(account='a', max_comments=1, dry_run=dry_run, slot=None))

    def dump(self):
        return '\n'.join(self.db.connection.iterdump())

    def test_preview_is_pure(self):
        before = self.dump()
        self.assertEqual(self.run_flow(True), 0)
        self.assertEqual(before, self.dump())
        self.client.publish_comment.assert_not_called()
        self.client.verify_comment.assert_not_called()
        self.notify.send_text.assert_not_called()

    def test_prepared_publication_reuses_connection_and_candidate_checks(self):
        candidate=main.available_candidate(self.db,1,1)
        with patch('main.BilibiliClient',side_effect=AssertionError('Unexpected reconnect')) as factory:
            with main.prepared_publication(self.db,self.client,1,1,candidate):
                self.assertEqual(self.run_flow(),0)
            factory.assert_not_called()
        self.client.uid.assert_called_once()
        self.client.check_comment_editor.assert_not_called()
        self.client.publish_comment.assert_called_once()
        self.assertIsNone(main._prepared_publication.get())

    def test_prepared_publication_rejects_wrong_account_without_submission(self):
        candidate=main.available_candidate(self.db,1,1)
        with main.prepared_publication(self.db,self.client,2,1,candidate):
            with self.assertRaisesRegex(RuntimeError,'mismatch'):
                self.run_flow(True)
        self.client.publish_comment.assert_not_called()

    def test_external_history_blocks_both_accounts_even_if_candidate_is_refreshed(self):
        from mediaflow.candidate_pool import eligible_count
        for uid in ('35199508', '1085803742'):
            with self.subTest(uid=uid):
                self.db.connection.execute('DELETE FROM historical_comment_videos')
                self.db.connection.execute('INSERT INTO historical_comment_videos(bvid,account_uid,source) VALUES(?,?,?)',
                                          ('BVtest1',uid,'user_confirmed'))
                self.db.connection.execute("UPDATE promotion_candidates SET status='available',ai_status='approved'")
                self.db.connection.commit()
                self.assertIsNone(main.available_candidate(self.db,1,1))
                self.assertIsNone(main.available_candidate(self.db,2,1))
                self.assertEqual(eligible_count(self.db,1),0)
                self.assertEqual(self.run_flow(True),1)
                self.client.publish_comment.assert_not_called()
                self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_records').fetchone()[0],0)

    def test_hidden_preview_candidate_does_not_mutate(self):
        self.client.video_visible.return_value = False
        before = self.dump()
        self.assertEqual(self.run_flow(True), 1)
        self.assertEqual(before, self.dump())
        self.client.publish_comment.assert_not_called()

    def test_accepted_then_verification_error_stays_published(self):
        self.client.verify_comment.side_effect = TimeoutError('mock')
        self.assertEqual(self.run_flow(), 0)
        record = self.db.connection.execute('SELECT * FROM promotion_records').fetchone()
        self.assertEqual(record['publish_status'], 'published')
        self.assertEqual(record['verification_status'], 'accepted_by_api_recheck_error')
        self.assertFalse(main.rate_ready(self.db, 1)[0])
        self.client.publish_comment.assert_called_once()
        self.notify.send_text.assert_called_once()

    def test_accepted_id_is_saved_before_verification_and_retained_on_miss(self):
        self.client.last_submitted_comment_id = '987654321'
        def verify(bvid, uid, text, url, remote):
            record = self.db.connection.execute('SELECT * FROM promotion_records').fetchone()
            self.assertEqual(record['publish_status'], 'published')
            self.assertEqual(record['remote_comment_id'], '987654321')
            self.assertEqual(remote, '987654321')
            return None
        self.client.verify_comment.side_effect = verify
        self.assertEqual(self.run_flow(), 0)
        record = self.db.connection.execute('SELECT * FROM promotion_records').fetchone()
        self.assertEqual(record['remote_comment_id'], '987654321')
        self.assertEqual(record['verification_status'], 'accepted_by_api')
        self.client.publish_comment.assert_called_once()
        self.notify.send_text.assert_not_called()

    def test_accepted_id_survives_verification_error_and_alert(self):
        self.client.last_submitted_comment_id = '987654321'
        self.client.verify_comment.side_effect = TimeoutError('mock')
        self.assertEqual(self.run_flow(), 0)
        record = self.db.connection.execute('SELECT * FROM promotion_records').fetchone()
        self.assertEqual(record['publish_status'], 'published')
        self.assertEqual(record['remote_comment_id'], '987654321')
        self.assertEqual(record['verification_status'], 'accepted_by_api_recheck_error')
        self.assertEqual(self.client.verify_comment.call_args.args[-1], '987654321')
        self.client.publish_comment.assert_called_once()
        self.notify.send_text.assert_called_once()

    def test_invalid_accepted_id_cannot_be_persisted_or_used_for_lookup(self):
        self.client.last_submitted_comment_id = '987?invalid'
        self.client.verify_comment.return_value = None
        self.assertEqual(self.run_flow(), 0)
        record = self.db.connection.execute('SELECT * FROM promotion_records').fetchone()
        self.assertIsNone(record['remote_comment_id'])
        self.assertIsNone(self.client.verify_comment.call_args.args[-1])

    def test_uncertain_submit_stays_pending_and_blocks_next_run(self):
        self.client.publish_comment.side_effect = SubmissionUncertain('mock')
        self.client.verify_comment.return_value = None
        self.assertEqual(self.run_flow(), 1)
        self.assertEqual(self.db.connection.execute('SELECT status FROM task_runs').fetchone()[0], 'PENDING')
        self.assertEqual(self.run_flow(), 1)
        self.client.publish_comment.assert_called_once()
        self.assertFalse(main.rate_ready(self.db, 1)[0])

    def test_uncertain_submit_can_be_confirmed_readonly(self):
        self.client.publish_comment.side_effect = SubmissionUncertain('mock')
        self.assertEqual(self.run_flow(), 0)
        self.client.publish_comment.assert_called_once()
        self.assertEqual(self.db.connection.execute('SELECT verification_status FROM promotion_records').fetchone()[0], 'verified')

    def test_rejection_keeps_reservation(self):
        self.client.publish_comment.side_effect = RuntimeError('mock rejection')
        self.assertEqual(self.run_flow(), 1)
        self.assertEqual(self.db.connection.execute('SELECT publish_status FROM promotion_records').fetchone()[0], 'failed')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_reservations').fetchone()[0], 1)
        self.client.verify_comment.assert_not_called()

    def test_preparation_failure_records_stage_and_alerts_once(self):
        self.client.publish_comment.side_effect = CommentPreparationError('editor_click', 2, 'TimeoutError')
        self.assertEqual(self.run_flow(), 1)
        record = self.db.connection.execute('SELECT * FROM promotion_records').fetchone()
        self.assertEqual(record['publish_status'], 'failed')
        self.assertIn('stage=editor_click', record['detail'])
        self.assertIn('attempts=2', record['detail'])
        self.notify.send_text.assert_called_once()
        self.assertIn('stage=editor_click', self.notify.send_text.call_args.args[0])
        self.client.verify_comment.assert_not_called()

    def test_interrupt_retains_durable_intent(self):
        self.client.publish_comment.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_flow()
        self.assertEqual(self.db.connection.execute('SELECT publish_status FROM promotion_records').fetchone()[0], 'submitted')
        self.assertFalse(main.rate_ready(self.db, 1)[0])

    def test_identity_mismatch_never_submits(self):
        self.client.uid.return_value = '2'
        with self.assertRaises(RuntimeError):
            self.run_flow()
        self.client.publish_comment.assert_not_called()

    def test_unapproved_candidate_never_submits(self):
        self.db.connection.execute("UPDATE promotion_candidates SET ai_status='rejected'")
        self.db.connection.commit()
        self.assertEqual(self.run_flow(), 1)
        self.client.publish_comment.assert_not_called()

    def test_disabled_comments_skip_candidate_without_reserving_or_using_phrase(self):
        self.client.check_comment_editor.side_effect = CommentPreparationError('comment_permission', 1, 'CommentUnavailable', 'comments_disabled_by_author')
        self.assertEqual(self.run_flow(), 1)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_reservations').fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT use_count FROM phrases').fetchone()[0], 0)
        self.client.publish_comment.assert_not_called()
        self.notify.send_text.assert_called_once()

    def test_disabled_candidate_can_be_skipped_for_a_commentable_candidate(self):
        self.db.connection.execute("""INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,title,status,ai_status,ai_score)
            VALUES(1,'bilibili','https://www.bilibili.com/video/BVtest2','BVtest2','test2','available','approved',80)""")
        self.db.connection.commit()
        self.client.check_comment_editor.side_effect = [CommentPreparationError('comment_permission', 1, 'CommentUnavailable', 'comments_disabled_by_author'), {'editor': True}]
        self.assertEqual(self.run_flow(), 0)
        self.client.publish_comment.assert_called_once()
        self.assertEqual(self.client.publish_comment.call_args.args[0], 'https://www.bilibili.com/video/BVtest2')

    def test_three_permission_denials_do_not_hide_fourth_usable_video(self):
        self.add_fallback_candidates(4)
        denied = CommentPreparationError('comment_permission', 1, 'CommentUnavailable', 'comments_disabled_by_author')
        self.client.check_comment_editor.side_effect = [denied, denied, denied, {'editor': True}]
        result = main.available_visible_candidate(self.db, self.client, 1, 1, mutate=False)
        self.assertEqual(result.bvid, 'BVtest4')
        self.assertEqual(self.client.check_comment_editor.call_count, 4)
        self.client.publish_comment.assert_not_called()

    def test_permission_scan_has_independent_upper_bound(self):
        self.add_fallback_candidates(12)
        self.client.check_comment_editor.side_effect = CommentPreparationError('comment_permission', 1, 'CommentUnavailable', 'comments_disabled_by_author')
        result = main.available_visible_candidate(self.db, self.client, 1, 1, mutate=False)
        self.assertIsNone(result)
        self.assertEqual(self.client.check_comment_editor.call_count, 9)

    def test_readiness_priority_is_account_specific_and_does_not_bypass_dedup(self):
        self.add_fallback_candidates()
        self.db.connection.execute("INSERT INTO comment_candidate_readiness(account_id,bvid,expires_at) VALUES(1,'BVtest3',datetime('now','+1 hour'))")
        self.db.connection.commit()
        self.assertEqual(main.available_candidate(self.db,1,1).bvid,'BVtest3')
        self.assertEqual(main.available_candidate(self.db,2,1).bvid,'BVtest1')
        self.db.connection.execute("INSERT INTO promotion_reservations(bvid,account_id,text_key) VALUES('BVtest3',1,'reserved-text')")
        self.db.connection.commit()
        self.assertNotEqual(main.available_candidate(self.db,1,1).bvid,'BVtest3')

    def test_multiple_comments_rejected_before_task_creation(self):
        with self.assertRaises(ValueError):
            main.promotion(self.db, self.notify, argparse.Namespace(account='a', max_comments=2))
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0], 0)

    def add_fallback_candidates(self, count=3):
        for number in range(2, count+2):
            self.db.connection.execute('''INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,title,status,ai_status,ai_score)
                VALUES(1,'bilibili',?,?,?,'available','approved',?)''',
                (f'https://www.bilibili.com/video/BVtest{number}', f'BVtest{number}', 'test', 90-number))
        self.db.connection.commit()

    def test_transient_candidate_timeout_switches_before_single_reservation_and_submit(self):
        self.add_fallback_candidates()
        self.client.check_comment_editor.side_effect = [CommentPreparationError('comment_permission',2,'TimeoutError'), {'editor':True}]
        self.assertEqual(self.run_flow(), 0)
        self.assertEqual(self.client.publish_comment.call_args.args[0], 'https://www.bilibili.com/video/BVtest2')
        self.client.publish_comment.assert_called_once()
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_reservations').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT use_count FROM phrases').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT bvid FROM comment_candidate_cooldowns').fetchone()[0], 'BVtest1')
        self.assertTrue(self.client.check_comment_editor.call_args.kwargs['retain'])
        self.notify.send_text.assert_not_called()

    def test_cooldown_is_account_specific_expires_and_keeps_video_globally_available(self):
        self.client.check_comment_editor.side_effect = CommentPreparationError('comment_permission',2,'TimeoutError')
        with self.assertRaises(CommentPreparationError):
            main.available_visible_candidate(self.db,self.client,1,1)
        self.assertIsNone(main.available_candidate(self.db,1,1))
        self.assertEqual(main.available_candidate(self.db,2,1).bvid,'BVtest1')
        self.assertEqual(self.db.connection.execute('SELECT status FROM promotion_candidates').fetchone()[0], 'available')
        self.db.connection.execute("UPDATE comment_candidate_cooldowns SET expires_at=datetime('now','-1 second')")
        self.assertEqual(main.available_candidate(self.db,1,1).bvid,'BVtest1')

    def test_nine_section_timeouts_require_diagnosis_without_submission(self):
        self.add_fallback_candidates()
        errors = []
        for _ in range(3):
            error = CommentPreparationError('comment_section', 3, 'TimeoutError')
            error.failures = [dict(stage='comment_section', attempt=i, error_type='TimeoutError') for i in (1,2,3)]
            errors.append(error)
        self.client.check_comment_editor.side_effect = errors
        with self.assertRaises(CommentPreparationError) as caught:
            main.available_visible_candidate(self.db,self.client,1,1)
        self.assertEqual(caught.exception.reason, 'comment_section_repeated_timeout')
        self.assertEqual(len(caught.exception.timeout_videos), 3)
        self.assertEqual(self.client.check_comment_editor.call_count, 3)
        self.client.publish_comment.assert_not_called()
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_reservations').fetchone()[0], 0)

    def test_three_bad_candidates_stop_and_alert_once_without_intent(self):
        self.add_fallback_candidates()
        self.client.check_comment_editor.side_effect = CommentPreparationError('comment_permission',2,'TimeoutError')
        with self.assertRaises(CommentPreparationError):
            self.run_flow()
        self.assertEqual(self.client.check_comment_editor.call_count, 3)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_records').fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_reservations').fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT use_count FROM phrases').fetchone()[0], 0)
        self.client.publish_comment.assert_not_called()
        self.notify.send_text.assert_called_once()

    def test_dry_run_fallback_never_writes_cooldowns_or_business_data(self):
        self.add_fallback_candidates()
        self.client.check_comment_editor.side_effect = [CommentPreparationError('comment_permission',2,'TimeoutError'), {'editor':True}]
        before = self.dump()
        self.assertEqual(self.run_flow(True), 0)
        self.assertEqual(self.dump(), before)
        self.assertFalse(self.client.check_comment_editor.call_args.kwargs['retain'])
        self.client.publish_comment.assert_not_called()

    def test_nontransient_preparation_failure_does_not_switch_or_cool_candidate(self):
        self.add_fallback_candidates()
        self.client.check_comment_editor.side_effect = CommentPreparationError('navigation',1,'Error')
        with self.assertRaises(CommentPreparationError):
            self.run_flow()
        self.client.check_comment_editor.assert_called_once()
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM comment_candidate_cooldowns').fetchone()[0],0)
        self.client.publish_comment.assert_not_called()

    def test_candidate_budget_prevents_another_browser_attempt(self):
        self.add_fallback_candidates()
        with patch('main.task_clock.monotonic', return_value=0) as clock:
            def fail(*args, **kwargs):
                clock.return_value = 331
                raise CommentPreparationError('comment_permission',2,'TimeoutError')
            self.client.check_comment_editor.side_effect = fail
            with self.assertRaises(CommentPreparationError):
                main.available_visible_candidate(self.db,self.client,1,1)
        self.client.check_comment_editor.assert_called_once()
        self.client.publish_comment.assert_not_called()

    def test_visibility_budget_exhaustion_does_not_cool_an_unchecked_editor(self):
        with patch('main.task_clock.monotonic', return_value=0) as clock:
            def visible(*args):
                clock.return_value = 331
                return True
            self.client.video_visible.side_effect = visible
            with self.assertRaises(CommentPreparationError):
                main.available_visible_candidate(self.db,self.client,1,1)
        self.client.check_comment_editor.assert_not_called()
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM comment_candidate_cooldowns').fetchone()[0],0)

    def test_disabled_account_never_connects(self):
        self.db.connection.execute('UPDATE accounts SET enabled=0')
        with self.assertRaises(RuntimeError):
            self.run_flow(True)
        self.client.uid.assert_not_called()

    def test_daily_limit_stops_before_browser(self):
        for number in range(5):
            self.db.connection.execute("""INSERT INTO promotion_records(account_id,platform,video_url,bvid,text,content_hash,publish_status,created_at)
                VALUES(1,'bilibili',?,?,?,'hash','published',?)""", (str(number), str(number), str(number), main.datetime.now().strftime('%Y-%m-%d 00:00:00')))
        self.db.connection.commit()
        self.assertEqual(self.run_flow(True), 1)
        self.client.uid.assert_not_called()

    def test_refresh_approved_and_rejected(self):
        from mediaflow.bilibili import Candidate
        from mediaflow.candidate_screening import ScreeningResult
        from types import SimpleNamespace
        self.client.search.return_value = [Candidate('BVnew1', 'video1', 'title1', 'author'), Candidate('BVnew2', 'video2', 'title2', 'author')]
        settings = SimpleNamespace(log_dir=self.root/'logs', typesafe_api_key='mock')
        with patch('main.Settings.load', return_value=settings), patch('main.screen_candidate', side_effect=[ScreeningResult(3,.9,.1,'approved'), ScreeningResult(0,.9,.8,'rejected')]):
            self.assertEqual(main.refresh_promotion_candidates(self.db, argparse.Namespace(account='a')), 0)
        statuses = [r[0] for r in self.db.connection.execute("SELECT ai_status FROM promotion_candidates WHERE bvid LIKE 'BVnew%' ORDER BY bvid")]
        self.assertEqual(statuses, ['approved', 'rejected'])
        self.client.publish_comment.assert_not_called()

    def test_refresh_failure_rolls_back(self):
        from mediaflow.bilibili import Candidate
        from mediaflow.candidate_screening import ScreeningResult
        from types import SimpleNamespace
        self.client.search.return_value = [Candidate('BVnew1', 'video1', 'title1', 'author'), Candidate('BVnew2', 'video2', 'title2', 'author')]
        before = self.dump()
        with patch('main.Settings.load', return_value=SimpleNamespace(log_dir=self.root/'logs', typesafe_api_key='mock')), patch('main.screen_candidate', side_effect=[ScreeningResult(3,.9,.1,'approved'), RuntimeError('mock')]):
            with self.assertRaises(RuntimeError):
                main.refresh_promotion_candidates(self.db, argparse.Namespace(account='a'))
        self.assertEqual(before, self.dump())

    def test_phrase_inventory_exhaustion_skips_submission(self):
        from mediaflow.dedup import text_key
        for text in ('相关资料可按需参考：https://example.invalid/private', '整理了一份 资料 相关资料，有需要可以看看：\nhttps://example.invalid/private'):
            self.db.connection.execute('INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES(?,?,1)', (text_key(text), text_key(text)))
        self.db.connection.commit()
        self.assertEqual(self.run_flow(True), 1)
        self.client.publish_comment.assert_not_called()

    def test_account_registration_and_verification(self):
        self.client.uid.return_value = '99'
        self.assertEqual(main.account(self.db, argparse.Namespace(account_command='add', cdp_url='mock99', name=None, role='promotion', profile_label=None)), 0)
        self.assertEqual(main.check_account(self.db, argparse.Namespace(name='bilibili-99')), 0)
        self.assertEqual(self.db.connection.execute("SELECT uid FROM accounts WHERE name='bilibili-99'").fetchone()[0], '99')

    def test_resource_and_toutiao_record(self):
        self.assertEqual(main.resource(self.db, argparse.Namespace(resource_command='add', category='test', title=None, url='mock', keywords='test', custom_text=None)), 0)
        self.assertEqual(main.toutiao(self.db, argparse.Namespace(toutiao_command='account-add', uid='88', cdp_url='mock88', profile_label=None)), 0)
        args = argparse.Namespace(toutiao_command='record', account='toutiao-88', article_url='https://www.toutiao.com/article/1234567890123456789/', text='test', status='published', verification='verified', detail=None, published_at=None)
        self.assertEqual(main.toutiao(self.db, args), 0)
        with self.assertRaises(RuntimeError):
            main.toutiao(self.db, args)

    def test_nurture_records_success_failure_and_dedup_without_real_likes(self):
        self.db.connection.execute("UPDATE accounts SET role='nurture'")
        self.db.connection.commit()
        page = self.client.page.return_value
        page.locator.return_value.evaluate_all.return_value = ['https://www.bilibili.com/video/BVwatch1', 'https://www.bilibili.com/video/BVwatch2']
        self.client.watch_and_like.side_effect = [(60,60,True), RuntimeError('mock stalled')]
        args = argparse.Namespace(account='a', max_videos=5)
        self.assertEqual(main.nurture(self.db, self.notify, args), 0)
        self.assertEqual(main.nurture(self.db, self.notify, args), 0)
        self.assertEqual(self.client.watch_and_like.call_count, 2)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 1)
        rows = [json.loads(line) for line in (self.root/'logs/workflow.jsonl').read_text(encoding='utf-8').splitlines()]
        names = {row['event'] for row in rows}
        self.assertTrue({'nurture_video_completed', 'nurture_video_failed', 'nurture_video_skipped'} <= names)

    def test_trace_and_redaction(self):
        register_secrets('test-private-secret')
        with audit_step('outer', 'test.jsonl', note='test-private-secret https://example.invalid/private'):
            with self.assertRaises(ValueError):
                with audit_step('inner', 'test.jsonl', token='hidden'):
                    raise ValueError('never record exception body')
        rows = [json.loads(line) for line in (self.root/'logs/test.jsonl').read_text().splitlines()]
        self.assertEqual(len({row['trace_id'] for row in rows}), 1)
        self.assertEqual(rows[1]['parent_span_id'], rows[0]['span_id'])
        self.assertEqual(rows[2]['event'], 'inner_failed')
        self.assertIn('elapsed_ms', rows[2])
        raw = json.dumps(rows)
        for secret in ('test-private-secret', 'example.invalid', 'hidden', 'exception body'):
            self.assertNotIn(secret, raw)
        with audit_step('next', 'test.jsonl'):
            pass
        last = json.loads((self.root/'logs/test.jsonl').read_text().splitlines()[-1])
        self.assertNotEqual(last['trace_id'], rows[0]['trace_id'])


if __name__ == '__main__':
    unittest.main()
