import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from mediaflow.bilibili import BilibiliClient


class CommentVerificationTests(unittest.TestCase):
    """Exercise real Python identity/hash checks against mocked HTTP payloads."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        environment = patch.dict(os.environ, MEDIAFLOW_LOG_DIR=self.folder.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.client = BilibiliClient('mock')
        self.page = MagicMock()
        self.client.page = Mock(return_value=self.page)
        self.page.evaluate.side_effect = self.api_response
        self.message = 'Private complete comment https://private.example/resource'
        self.root = None
        self.root_code = 12006
        self.listed = []
        self.count = 0

    def reply(self, *, uid='42', rpid='9001', message=None):
        return {'rpid_str': rpid, 'member': {'mid': uid},
                'content': {'message': self.message if message is None else message}}

    @staticmethod
    def response(data, *, code=0, http_status=200):
        return {'http_status': http_status, 'payload': {'code': code, 'data': data}}

    def api_response(self, script, args):
        endpoint = urlparse(args['url'])
        self.assertEqual(endpoint.hostname, 'api.bilibili.com')
        self.assertIn('AbortSignal.timeout(timeout_ms)', script)
        self.assertGreater(args['timeout_ms'], 0)
        self.assertLessEqual(args['timeout_ms'], 10_000)
        if endpoint.path == '/x/web-interface/view':
            self.assertEqual(parse_qs(endpoint.query)['bvid'], ['BVtest'])
            return self.response({'aid': 12345})
        if endpoint.path == '/x/v2/reply/detail':
            query = parse_qs(endpoint.query)
            self.assertEqual(query['root'], ['9001'])
            self.assertEqual(query['oid'], ['12345'])
            return self.response({'root': self.root}, code=self.root_code)
        self.assertEqual(endpoint.path, '/x/v2/reply')
        return self.response({'replies': self.listed, 'page': {'count': self.count}})

    def details(self, remote='9001'):
        result = self.client.verify_comment_details('BVtest', '42', self.message,
                                                     'https://private.example/resource', remote)
        self.assert_read_only()
        return result

    def assert_read_only(self):
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()
        self.page.locator.assert_not_called()
        for call in self.page.evaluate.call_args_list:
            endpoint = urlparse(call.args[1]['url'])
            self.assertNotIn('/reply/add', endpoint.path)
            self.assertNotIn(self.message, call.args[0])
        self.page.close.assert_called_once()

    def test_exact_root_identity_and_full_text_are_verified(self):
        self.root_code, self.root = 0, self.reply()
        result = self.details()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['comment_id'], '9001')
        self.assertEqual(result['method'], 'root_detail')
        self.assertEqual(result['source'], 'official_comment_api')
        self.assertEqual(result['scope']['root_comments_checked'], 1)
        self.assertEqual(result['scope']['listing_pages'], 0)

    def test_wrong_uid_is_never_confirmed(self):
        self.root_code = 0
        self.root = self.reply(uid='420')
        self.listed, self.count = [self.root], 1
        self.assertEqual(self.details()['status'], 'unconfirmed')

    def test_wrong_full_text_is_never_confirmed(self):
        self.root_code = 0
        self.root = self.reply(message=self.message + ' changed')
        self.listed, self.count = [self.root], 1
        self.assertEqual(self.details()['status'], 'unconfirmed')

    def test_wrong_rpid_is_never_confirmed_even_with_same_uid_and_text(self):
        self.root_code = 0
        self.root = self.reply(rpid='9002')
        self.listed, self.count = [self.root], 1
        result = self.details()
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertIsNone(result['comment_id'])

    def test_conflicting_member_and_direct_uid_is_not_confirmed(self):
        self.root_code, self.root = 0, self.reply()
        self.root['mid'] = '420'
        self.listed, self.count = [self.root], 1
        self.assertEqual(self.details()['status'], 'unconfirmed')

    def test_no_rpid_search_can_find_exact_match_in_listing(self):
        self.listed, self.count = [self.reply()], 1
        result = self.details(remote=None)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['method'], 'listing')
        self.assertEqual(result['comment_id'], '9001')
        self.assertFalse(result['scope']['root_requested'])

    def test_root_unavailable_is_scoped_uncertainty_not_deletion(self):
        result = self.details()
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertEqual(result['reason'], 'remote_comment_unavailable')
        self.assertEqual(result['api_code'], 12006)
        self.assertEqual(result['scope']['listing_pages'], 2)
        self.assertIsNone(result['comment_id'])

    def test_listing_search_is_limited_and_never_claims_exhaustive_absence(self):
        self.listed, self.count = [self.reply(uid='420')], 10000
        result = self.details(remote=None)
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertEqual(result['reason'], 'not_found_in_checked_scope')
        self.assertEqual(result['scope']['listing_pages'], 10)
        self.assertEqual(result['scope']['listing_comments_checked'], 10)
        pages = [parse_qs(urlparse(c.args[1]['url']).query)['pn'][0]
                 for c in self.page.evaluate.call_args_list
                 if urlparse(c.args[1]['url']).path == '/x/v2/reply']
        self.assertEqual(pages, ['1', '2', '3', '4', '5'] * 2)

    def test_missing_aid_is_metadata_error(self):
        self.page.evaluate.return_value = self.response({})
        self.page.evaluate.side_effect = None
        result = self.details()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'video_metadata_unavailable')
        self.assertEqual(result['error_type'], 'ValueError')
        self.assertFalse(result['scope']['aid_available'])
        self.assertEqual(self.page.evaluate.call_count, 1)

    def test_missing_listing_metadata_is_error(self):
        self.page.evaluate.side_effect = [self.response({'aid': 12345}),
                                          self.response({})]
        result = self.details(remote=None)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'comment_listing_metadata_unavailable')

    def test_metadata_request_exception_is_not_reported_as_missing_comment(self):
        self.page.evaluate.side_effect = TimeoutError('private request details')
        result = self.details()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'video_metadata_api_error')
        self.assertEqual(result['error_type'], 'TimeoutError')
        self.assertNotIn('private request details', json.dumps(result))

    def test_root_api_error_remains_error_after_empty_listing(self):
        self.root_code = -403
        result = self.details()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'root_detail_api_error')
        self.assertEqual(result['api_code'], -403)

    def test_listing_http_failure_is_not_reported_as_deletion(self):
        self.page.evaluate.side_effect = [self.response({'aid': 12345}),
                                          self.response({'replies': []}, http_status=503)]
        result = self.details(remote=None)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'comment_listing_api_error')
        self.assertEqual(result['api_results'][-1]['http_status'], 503)

    def test_false_api_code_is_invalid(self):
        self.page.evaluate.side_effect = [self.response({'aid': 12345}, code=False)]
        result = self.details()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'ValueError')

    def test_overall_time_budget_prevents_more_requests(self):
        with patch('mediaflow.bilibili.time.monotonic', side_effect=[0, 0, 0, 46, 46, 46]):
            result = self.details()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'verification_time_budget_exhausted')
        self.assertEqual(self.page.evaluate.call_count, 1)

    def test_cleanup_failure_does_not_override_positive_evidence(self):
        self.root_code, self.root = 0, self.reply()
        self.page.close.side_effect = RuntimeError('cleanup private details')
        result = self.details()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['cleanup_error_type'], 'RuntimeError')
        self.assertNotIn('cleanup private details', json.dumps(result))

    def test_compatibility_wrapper_returns_id_for_verified(self):
        self.root_code, self.root = 0, self.reply()
        self.assertEqual(self.client.verify_comment('BVtest', '42', self.message, '', '9001'), '9001')
        self.assert_read_only()

    def test_compatibility_wrapper_returns_none_for_unconfirmed(self):
        self.assertIsNone(self.client.verify_comment('BVtest', '42', self.message, '', '9001'))
        self.assert_read_only()

    def test_compatibility_wrapper_raises_for_api_error(self):
        self.page.evaluate.side_effect = TimeoutError('private response')
        with self.assertRaisesRegex(RuntimeError, 'video_metadata_api_error'):
            self.client.verify_comment('BVtest', '42', self.message, '', '9001')
        self.assert_read_only()

    def test_audit_log_does_not_contain_full_text_or_resource_url(self):
        self.root_code, self.root = 0, self.reply()
        self.details()
        raw = (Path(self.folder.name) / 'browser-flow.jsonl').read_text(encoding='utf-8')
        self.assertNotIn(self.message, raw)
        self.assertNotIn('private.example', raw)
        self.assertIn('root_detail', raw)

    def test_successful_requests_have_stage_timing_and_safe_receipts(self):
        self.root_code, self.root = 0, self.reply()
        result = self.details()
        rows = [json.loads(line) for line in
                (Path(self.folder.name) / 'browser-flow.jsonl').read_text(encoding='utf-8').splitlines()]
        for stage in ('metadata', 'root_detail'):
            completed = next(row for row in rows if row['event'] == 'comment_verification_' + stage + '_completed')
            self.assertGreaterEqual(completed['elapsed_ms'], 0)
            self.assertEqual(completed['http_status'], 200)
            self.assertEqual(completed['api_code'], 0)
            self.assertLessEqual(completed['timeout_ms'], 10_000)
        self.assertTrue(all(receipt['elapsed_ms'] >= 0 for receipt in result['api_results']))

    def test_request_failure_has_stage_timing_without_private_exception_body(self):
        self.page.evaluate.side_effect = TimeoutError('private request details https://private.example/cookie')
        result = self.details()
        raw = (Path(self.folder.name) / 'browser-flow.jsonl').read_text(encoding='utf-8')
        rows = [json.loads(line) for line in raw.splitlines()]
        failed = next(row for row in rows if row['event'] == 'comment_verification_metadata_failed')
        self.assertEqual(failed['error_type'], 'TimeoutError')
        self.assertGreaterEqual(failed['elapsed_ms'], 0)
        self.assertEqual(failed['timeout_ms'], 10_000)
        self.assertEqual(result['api_results'][0]['error_type'], 'TimeoutError')
        self.assertGreaterEqual(result['api_results'][0]['elapsed_ms'], 0)
        self.assertNotIn('private request details', raw)
        self.assertNotIn('private.example', raw)


if __name__ == '__main__':
    unittest.main()
