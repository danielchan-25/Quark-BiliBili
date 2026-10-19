import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mediaflow.bilibili import Candidate
from mediaflow.database import Database
from mediaflow.dedup import text_key
from mediaflow.notify import FeishuNotifier
from tools.validate_workflow import preview_block_reason, validate


class FakeBrowser:
    evidence = {'status': 'verified', 'method': 'root_detail',
                'reason': 'identity_and_full_text_match', 'api_code': 0,
                'source': 'official_comment_api', 'scope': {'root_comments_checked': 1}}

    def __init__(self, cdp_url):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def uid(self):
        return '1'

    def search(self, *args, **kwargs):
        return [Candidate('BVfuture', 'https://example.invalid/video', '学习资料', 'test')]

    def video_visible(self, *args):
        return True

    def check_comment_editor(self, *args, **kwargs):
        return {'editor_visible': True}

    def verify_comment_details(self, *args):
        return self.evidence

    def publish_comment(self, *args):
        raise RuntimeError('Unblocked publication')

    def watch_and_like(self, *args):
        raise RuntimeError('Unblocked like')


class ValidationResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        audit = patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(self.root/'logs'))
        audit.start()
        self.addCleanup(audit.stop)
        self.db = Database(self.root/'test.db')
        self.addCleanup(self.db.close)
        self.db.connection.executescript("""
            INSERT INTO accounts(id,name,platform,role,cdp_url,uid)
                VALUES(1,'bilibili-1','bilibili','promotion','mock','1');
            INSERT INTO promotion_resources(id,category,url,keywords)
                VALUES(1,'资料','https://example.invalid/private','test');
            INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,status,ai_status,ai_score)
                VALUES(1,'bilibili','https://example.invalid/video','BVfuture','available','approved',3);
            INSERT INTO phrases(category,text,source)
                VALUES('资料','合适的学习资料可按需参考，链接在这里：{url}','test');
            INSERT INTO promotion_records(account_id,resource_id,platform,video_url,bvid,text,
                content_hash,publish_status,remote_comment_id,created_at)
                VALUES(1,1,'bilibili','historical-video','BVpast','historical-private-text',
                    'test','published','123','2026-01-01 00:00:00');
        """)
        self.db.connection.commit()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('tools.validate_workflow.ROOT', self.root))
        self.stack.enter_context(patch('main.ROOT', self.root))
        self.stack.enter_context(patch('mediaflow.audit.LOG_ROOT', self.root/'logs'))
        self.stack.enter_context(patch('tools.validate_workflow.Settings.load', return_value=SimpleNamespace(
            database_path=self.root/'test.db', typesafe_api_key='mock', feishu_webhook='mock')))
        self.stack.enter_context(patch('tools.validate_workflow.promotion_keywords', return_value=['test']))
        self.stack.enter_context(patch('main.promotion_keywords', return_value=['test']))
        self.stack.enter_context(patch('tools.validate_workflow.BilibiliClient', FakeBrowser))
        self.stack.enter_context(patch('main.BilibiliClient', FakeBrowser))
        self.stack.enter_context(patch('tools.validate_workflow.screen_candidate', return_value=SimpleNamespace(status='approved')))
        self.stack.enter_context(patch.object(FakeBrowser, 'evidence', dict(FakeBrowser.evidence)))

    def run_validation(self):
        before = list(self.db.connection.iterdump())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report, folder = validate('bilibili-1')
        self.assertEqual(before, list(self.db.connection.iterdump()))
        serialized = json.dumps(report)
        self.assertNotIn('historical-private-text', serialized)
        self.assertNotIn('https://example.invalid/private', serialized)
        self.assertNotIn('DRY-RUN', output.getvalue())
        self.assertEqual(report, json.loads((folder/'report.json').read_text(encoding='utf-8')))
        return report

    def test_verified_history_and_ready_preview_pass_without_side_effects(self):
        report = self.run_validation()
        self.assertTrue(report['passed'])
        self.assertTrue(report['functional_checks_passed'])
        self.assertTrue(report['publication_ready'])
        self.assertTrue(report['historical_comment_verified'])
        self.assertEqual(report['warnings'], [])
        self.assertEqual(report['blocked_write_calls'], 0)

    def test_unconfirmed_history_is_scoped_warning_not_core_failure(self):
        FakeBrowser.evidence = {'status': 'unconfirmed', 'method': 'listing',
            'reason': 'remote_comment_unavailable', 'api_code': 12006,
            'source': 'official_comment_api', 'scope': {'listing_pages': 2, 'sorts_checked': [0, 2]},
            'response_body': 'private exception or comment body'}
        report = self.run_validation()
        self.assertTrue(report['functional_checks_passed'])
        self.assertTrue(report['publication_ready'])
        self.assertTrue(report['passed'])
        self.assertFalse(report['historical_comment_verified'])
        warning = report['warnings'][0]
        self.assertEqual(warning['reason'], 'remote_comment_unavailable')
        self.assertEqual(warning['api_code'], 12006)
        self.assertEqual(warning['scope']['listing_pages'], 2)
        self.assertNotIn('private exception', json.dumps(report))
        self.assertNotIn('deleted', json.dumps(report))
        self.assertNotIn('hidden', json.dumps(report))

    def test_history_technical_error_remains_failed_with_safe_evidence(self):
        FakeBrowser.evidence = {'status': 'error', 'method': 'metadata',
            'reason': 'video_metadata_api_error', 'api_code': -412,
            'error_type': 'CommentApiError', 'source': 'official_comment_api', 'scope': {},
            'exception_message': 'https://example.invalid/private sensitive text'}
        report = self.run_validation()
        history = next(item for item in report['steps'] if item['step'] == 'existing_comment')
        self.assertEqual(history['status'], 'failed')
        self.assertEqual(history['detail']['reason'], 'video_metadata_api_error')
        self.assertTrue(report['functional_checks_passed'])
        self.assertFalse(report['passed'])
        self.assertNotIn('exception_message', history['detail'])

    def test_phrase_exhaustion_blocks_readiness_without_core_failure(self):
        phrase = self.db.connection.execute('SELECT text FROM phrases').fetchone()[0]
        fallback = '整理了一份 资料 相关资料，有需要可以看看：\nhttps://example.invalid/private'
        for index, text in enumerate((phrase.replace('{url}', 'https://example.invalid/private'), fallback)):
            self.db.connection.execute('INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES(?,?,1)',
                                       (f'BVreserved{index}', text_key(text)))
        self.db.connection.commit()
        report = self.run_validation()
        preview = next(item for item in report['steps'] if item['step'] == 'preview')
        self.assertEqual(preview['status'], 'blocked')
        self.assertEqual(preview['detail']['reason'], 'phrase_exhausted')
        self.assertEqual(preview['detail']['business_writes'], 0)
        self.assertTrue(report['functional_checks_passed'])
        self.assertFalse(report['publication_ready'])
        self.assertFalse(report['passed'])

    def test_preview_reasons_ignore_stale_and_sensitive_audit_details(self):
        path = self.root/'audit.jsonl'
        path.write_text(json.dumps({'event': 'phrase_exhausted'}) + '\n', encoding='utf-8')
        offset = path.stat().st_size
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'event': 'frequency_checked', 'ready': False,
                                     'reason': 'private text https://example.invalid/private'}) + '\n')
        self.assertEqual(preview_block_reason(path, offset), 'frequency_limit')
        path.write_text(json.dumps({'event': 'promotion_result', 'reason': 'private text'}) + '\n', encoding='utf-8')
        self.assertEqual(preview_block_reason(path), 'read_only_preview_not_ready')

    def test_publish_like_and_notification_guards_block_attempts(self):
        actions = (lambda: FakeBrowser('mock').publish_comment('video', 'text'),
                   lambda: FakeBrowser('mock').watch_and_like('video'),
                   lambda: FeishuNotifier('mock', None).send_text('text'))
        for action in actions:
            with self.subTest(action=action), patch('tools.validate_workflow.workflow.promotion', side_effect=lambda *args: action()):
                report = self.run_validation()
                self.assertEqual(report['blocked_write_calls'], 1)
                self.assertFalse(report['functional_checks_passed'])
                self.assertFalse(report['passed'])

    def test_read_only_database_blocks_business_writes(self):
        def attempt_write(db, notifier, args):
            self.assertTrue(args.dry_run)
            self.assertIsNone(notifier)
            db.connection.execute("INSERT INTO phrases(category,text,source) VALUES('资料','blocked-write','test')")
        with patch('tools.validate_workflow.workflow.promotion', side_effect=attempt_write):
            report = self.run_validation()
        preview = next(item for item in report['steps'] if item['step'] == 'preview')
        self.assertEqual(preview['status'], 'failed')
        self.assertEqual(preview['error_type'], 'OperationalError')
        self.assertFalse(report['passed'])


if __name__ == '__main__':
    unittest.main()
