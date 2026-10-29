import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from pathlib import Path
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from mediaflow.bilibili import BilibiliClient, SubmissionRejected, SubmissionUncertain, CommentPreparationError, _scroll_to_comment_section as scroll_comment_section


class BrowserSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, MEDIAFLOW_LOG_DIR=self.tmp.name)
        env.start()
        self.addCleanup(env.stop)
        self.client = BilibiliClient('mock')
        self.page = MagicMock()
        self.client.page = Mock(return_value=self.page)
        scroll = patch('mediaflow.bilibili._scroll_to_comment_section')
        scroll.start()
        self.addCleanup(scroll.stop)
        self.button = self.page.locator.return_value.evaluate_handle.return_value.as_element.return_value
        self.page.locator.return_value.evaluate.return_value = {'editor': True}
        self.response = self.page.expect_response.return_value.__enter__.return_value.value
        self.response.json.return_value = {'code': 0}

    def test_lazy_load_wakeup_precedes_comment_wait(self):
        actions = Mock()
        actions.attach_mock(self.page.mouse.wheel, 'wheel')
        actions.attach_mock(self.page.locator.return_value.first.wait_for, 'wait')
        scroll_comment_section(self.page, reload_once=False)
        self.assertEqual(actions.mock_calls[0][0], 'wheel')
        self.assertEqual(actions.mock_calls[1][0], 'wait')
        self.page.reload.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def test_video_visibility_recovers_timeout_without_editor_or_submit(self):
        self.client.context = MagicMock()
        response = Mock(ok=True)
        response.json.return_value = {'code': 0}
        request = self.client.context.request.get
        request.side_effect = [PlaywrightTimeoutError('timeout'), response]
        self.assertTrue(self.client.video_visible('BVtest'))
        self.assertEqual(request.call_count, 2)
        self.client.page.assert_not_called()

    def test_visibility_timeout_is_bounded_and_classified(self):
        self.client.context = MagicMock()
        self.client.context.request.get.side_effect = PlaywrightTimeoutError('timeout')
        with self.assertRaises(CommentPreparationError) as caught:
            self.client.video_visible('BVtest')
        self.assertEqual(caught.exception.stage, 'video_visibility')
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(self.client.context.request.get.call_count, 3)

    def test_visibility_rejection_never_retries(self):
        self.client.context = MagicMock()
        response = Mock(ok=True)
        response.json.return_value = {'code': -412}
        self.client.context.request.get.return_value = response
        with self.assertRaises(RuntimeError):
            self.client.video_visible('BVtest')
        self.client.context.request.get.assert_called_once()

    def test_identity_uses_browser_session_without_homepage_navigation(self):
        self.client.context = MagicMock()
        response = Mock(ok=True)
        response.json.return_value = {'code': 0, 'data': {'isLogin': True, 'mid': 100000002}}
        self.client.context.request.get.side_effect = [PlaywrightTimeoutError('timeout'), response]
        self.assertEqual(self.client.uid(), '100000002')
        self.assertEqual(self.client.context.request.get.call_count, 2)
        self.client.page.assert_not_called()

    def test_identity_rejects_malformed_or_denied_response_without_retry(self):
        for payload in ({'code': -412}, {'code': 0, 'data': {'isLogin': True, 'mid': True}},
                        {'code': 0, 'data': {'isLogin': True, 'mid': 0}}, {'code': 0}):
            with self.subTest(payload=payload):
                self.client.context = MagicMock()
                response = Mock(ok=True)
                response.json.return_value = payload
                self.client.context.request.get.return_value = response
                with self.assertRaises(RuntimeError):
                    self.client.uid()
                self.client.context.request.get.assert_called_once()

    def test_identity_explicit_logout_returns_none(self):
        for payload in ({'code': -101}, {'code': 0, 'data': {'isLogin': False}}):
            self.client.context = MagicMock()
            response = Mock(ok=True)
            response.json.return_value = payload
            self.client.context.request.get.return_value = response
            self.assertIsNone(self.client.uid())

    def test_identity_timeout_has_three_attempt_limit(self):
        self.client.context = MagicMock()
        self.client.context.request.get.side_effect = PlaywrightTimeoutError('timeout')
        self.page.goto.side_effect = PlaywrightTimeoutError('browser network timeout')
        with self.assertRaises(PlaywrightTimeoutError):
            self.client.uid()
        self.assertEqual(self.client.context.request.get.call_count, 3)
        self.client.page.assert_called_once()
        self.page.goto.assert_called_once()
        self.page.close.assert_called_once()

    def test_identity_timeout_falls_back_to_browser_network_with_strict_uid(self):
        self.client.context = MagicMock()
        self.client.context.request.get.side_effect = PlaywrightTimeoutError('request channel timeout')
        self.page.url = 'https://www.bilibili.com/'
        self.page.locator.return_value.evaluate.return_value = {
            'ok': True, 'payload': {'code':0, 'data': {'isLogin':True, 'mid':100000002}}}
        self.assertEqual(self.client.uid(),'100000002')
        self.page.goto.assert_called_once_with('https://www.bilibili.com/',wait_until='commit',timeout=10_000)
        self.page.close.assert_called_once()
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()

    def test_completed_api_diagnostics_keep_only_numeric_code(self):
        from mediaflow.bilibili import _PreparationNetworkDiagnostics
        diagnostics = _PreparationNetworkDiagnostics(self.page)
        request = Mock(url='https://api.bilibili.com/x/v2/reply/wbi/main', method='GET', resource_type='xhr')
        request.response.return_value.json.return_value = {'code': -352, 'message': 'private', 'data': {'replies': ['private']}}
        diagnostics.request_finished(request)
        self.assertEqual(diagnostics.comment_api_codes, {'-352': 1})
        self.assertNotIn('private', repr(diagnostics.events))
        request.method = 'POST'
        diagnostics.request_finished(request)
        self.assertEqual(diagnostics.comment_api_codes, {'-352': 1})

    def test_native_diagnosis_distinguishes_playwright_dom_timeout(self):
        self.client.cdp_url = 'http://127.0.0.1:9600'
        self.page.url = 'https://www.bilibili.com/video/BVtest1'
        response = MagicMock()
        response.__enter__.return_value = response
        self.page.locator.return_value.evaluate.side_effect = PlaywrightTimeoutError('unavailable')
        with patch('mediaflow.bilibili.build_opener') as opener, patch('mediaflow.bilibili.json.load', return_value=[dict(type='page',url=self.page.url,webSocketDebuggerUrl='ws://127.0.0.1/owned')]), patch('mediaflow.bilibili._cdp_command', return_value={'result':{'value':{'html':True,'comment':True}}}):
            opener.return_value.open.return_value = response
            self.client.diagnose_comment_runtime(self.page)
        row = next(r for r in self.log_rows() if r['event']=='comment_runtime_diagnosis')
        self.assertEqual(row['classification'], 'playwright_dom_access_failed_native_responsive')
        self.page.keyboard.type.assert_not_called()

    def test_accepted_survives_cleanup_error(self):
        self.response.json.return_value = {'code': 0, 'data': {'reply': {'rpid': 9001}}}
        self.page.close.side_effect = RuntimeError('cleanup')
        self.assertEqual(self.client.publish_comment('mock', 'mock'), 'network_confirmed')
        self.assertEqual(self.client.last_submitted_comment_id, '9001')
        # Editor click, actionability trial, and exactly one submit click.
        self.assertEqual(self.button.click.call_count, 3)

    def test_submitted_id_starts_empty(self):
        self.assertIsNone(self.client.last_submitted_comment_id)

    def test_accepted_reply_prefers_exact_string_id(self):
        self.response.json.return_value = {'code': 0, 'data': {'reply': {'rpid_str': '9001', 'rpid': 9002}}}
        self.assertEqual(self.client.publish_comment('mock', 'mock'), 'network_confirmed')
        self.assertEqual(self.client.last_submitted_comment_id, '9001')
        accepted = next(row for row in self.log_rows() if row['event'] == 'submission_accepted')
        self.assertTrue(accepted['comment_id_available'])
        for field in ('comment_id', 'rpid', 'payload', 'text', 'url'):
            self.assertNotIn(field, accepted)

    def test_accepted_reply_numeric_id_and_legacy_id_are_retained(self):
        for data in ({'reply': {'rpid': 9001}}, {'rpid': 9001},
                     {'reply': {'rpid_str': 'invalid', 'rpid': 9001}}):
            with self.subTest(data=data):
                self.response.json.return_value = {'code': 0, 'data': data}
                self.assertEqual(self.client.publish_comment('mock', 'mock'), 'network_confirmed')
                self.assertEqual(self.client.last_submitted_comment_id, '9001')

    def test_accepted_receipt_without_id_clears_previous_id(self):
        self.client.last_submitted_comment_id = '9001'
        self.assertEqual(self.client.publish_comment('mock', 'mock'), 'network_confirmed')
        self.assertIsNone(self.client.last_submitted_comment_id)
        accepted = next(row for row in self.log_rows() if row['event'] == 'submission_accepted')
        self.assertFalse(accepted['comment_id_available'])

    def test_invalid_ids_never_revoke_acceptance_or_reuse_previous_id(self):
        for value in (True, False, 0, -1, 1.5, 9_007_199_254_740_992,
                      '', '0', '01', '-1', '1.0', ' 9001', '９００１'):
            with self.subTest(value=value):
                self.client.last_submitted_comment_id = 'old-id'
                self.response.json.return_value = {'code': 0, 'data': {'reply': {'rpid_str': value, 'rpid': value}}}
                self.assertEqual(self.client.publish_comment('mock', 'mock'), 'network_confirmed')
                self.assertIsNone(self.client.last_submitted_comment_id)

    def test_rejected_receipt_cannot_reuse_previous_id(self):
        self.client.last_submitted_comment_id = '9001'
        self.response.json.return_value = {'code': 12002, 'data': {'reply': {'rpid': 9002}}}
        with self.assertRaises(SubmissionRejected):
            self.client.publish_comment('mock', 'mock')
        self.assertIsNone(self.client.last_submitted_comment_id)

    def test_uncertain_receipt_cannot_reuse_previous_id(self):
        self.client.last_submitted_comment_id = '9001'
        self.response.json.side_effect = ValueError('invalid JSON')
        with self.assertRaises(SubmissionUncertain):
            self.client.publish_comment('mock', 'mock')
        self.assertIsNone(self.client.last_submitted_comment_id)

    def test_failure_before_submit_cannot_reuse_previous_id(self):
        self.client.last_submitted_comment_id = '9001'
        self.page.goto.side_effect = RuntimeError('navigation')
        with self.assertRaises(RuntimeError):
            self.client.publish_comment('mock', 'mock')
        self.assertIsNone(self.client.last_submitted_comment_id)
        self.page.expect_response.assert_not_called()

    def test_malformed_response_after_click_is_uncertain(self):
        self.response.json.side_effect = ValueError('invalid JSON')
        with self.assertRaises(SubmissionUncertain):
            self.client.publish_comment('mock', 'mock')
        self.assertEqual(self.button.click.call_count, 3)

    def test_explicit_rejection_is_not_uncertain(self):
        self.response.json.return_value = {'code': 12002}
        with self.assertRaises(SubmissionRejected):
            self.client.publish_comment('mock', 'mock')

    def test_false_receipt_is_not_success(self):
        self.response.json.return_value = {'code': False}
        with self.assertRaises(SubmissionUncertain):
            self.client.publish_comment('mock', 'mock')

    def test_navigation_error_never_clicks(self):
        self.page.goto.side_effect = RuntimeError('navigation')
        with self.assertRaises(RuntimeError):
            self.client.publish_comment('mock', 'mock')
        self.button.click.assert_not_called()

    def test_http_failure_is_not_misclassified_as_comment_timeout(self):
        self.page.goto.return_value.status = 503
        with self.assertRaises(CommentPreparationError) as caught:
            self.client.check_comment_editor('mock')
        self.assertEqual(caught.exception.stage,'navigation')
        self.assertEqual(caught.exception.reason,'document_http_error')
        self.page.wait_for_function.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def test_redirect_never_reaches_comment_controls(self):
        self.page.url = 'https://passport.bilibili.com/login'
        with self.assertRaises(CommentPreparationError) as caught:
            self.client.check_comment_editor('https://www.bilibili.com/video/BVtest1')
        self.assertEqual(caught.exception.reason,'unexpected_page_target')
        self.page.wait_for_function.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def test_editor_click_timeout_recovers_before_typing_and_submits_once(self):
        self.button.click.side_effect = [PlaywrightTimeoutError('blocked https://example.invalid/private'), None, None, None]
        self.assertEqual(self.client.publish_comment('mock', 'private comment'), 'network_confirmed')
        self.assertEqual(self.page.goto.call_count, 2)
        self.page.keyboard.type.assert_called_once()
        self.page.expect_response.assert_called_once()
        rows = self.log_rows()
        retry = next(r for r in rows if r['event'] == 'comment_preparation_retry')
        self.assertEqual(retry['stage'], 'editor_click')
        raw = json.dumps(rows)
        self.assertNotIn('private comment', raw)
        self.assertNotIn('example.invalid', raw)
        self.assertEqual(sum(r['event'] == 'submit_click_started' for r in rows), 1)

    def test_repeated_timeout_is_bounded_and_never_types_or_submits(self):
        self.button.click.side_effect = PlaywrightTimeoutError('blocked')
        with self.assertRaises(CommentPreparationError) as caught:
            self.client.publish_comment('mock', 'private comment')
        self.assertEqual(caught.exception.stage, 'editor_click')
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(self.page.goto.call_count, 3)
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()
        self.page.close.assert_called_once()

    def test_comment_section_timeout_reloads_three_times_with_failure_history(self):
        with patch('mediaflow.bilibili._scroll_to_comment_section', side_effect=PlaywrightTimeoutError('section')):
            with self.assertRaises(CommentPreparationError) as caught:
                self.client.check_comment_editor('mock')
        self.assertEqual(self.page.goto.call_count, 3)
        self.assertEqual(len(caught.exception.failures), 3)
        self.assertTrue(all(f['stage'] == 'comment_section' for f in caught.exception.failures))
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()

    def test_stalled_page_is_replaced_before_third_attempt_and_retained(self):
        fresh = MagicMock()
        self.client.context = fresh.context
        self.client.page.side_effect = [self.page, fresh]
        with patch('mediaflow.bilibili._scroll_to_comment_section', side_effect=[PlaywrightTimeoutError('one'), PlaywrightTimeoutError('two'), None]), patch('mediaflow.bilibili._ready_comment_editor'), patch('mediaflow.bilibili._comment_page_state', return_value={'editor': True}):
            self.client.check_comment_editor('mock', retain=True)
        self.assertEqual(self.page.goto.call_count, 2)
        fresh.goto.assert_called_once()
        self.page.close.assert_called_once()
        self.assertIs(self.client._prepared_comment['page'], fresh)
        fresh.close.assert_not_called()
        fresh.keyboard.type.assert_not_called()
        fresh.expect_response.assert_not_called()
        self.assertTrue(any(r.get('recovery') == 'replace_owned_page_before_typing' for r in self.log_rows()))

    def test_confirmed_runtime_failure_replaces_page_on_first_retry(self):
        fresh = MagicMock()
        self.client.page.side_effect = [self.page, fresh]
        self.client.diagnose_comment_runtime = Mock(return_value={'classification': 'playwright_dom_access_failed_native_responsive'})
        with patch('mediaflow.bilibili._scroll_to_comment_section', side_effect=[PlaywrightTimeoutError('one'), None]), patch('mediaflow.bilibili._ready_comment_editor'), patch('mediaflow.bilibili._comment_page_state', return_value={'editor': True}):
            self.client.check_comment_editor('mock')
        self.page.goto.assert_called_once()
        fresh.goto.assert_called_once()
        fresh.close.assert_called_once()
        fresh.keyboard.type.assert_not_called()

    def test_navigation_timeout_can_recover_once(self):
        self.page.goto.side_effect = [PlaywrightTimeoutError('network'), None]
        self.assertEqual(self.client.publish_comment('mock', 'mock'), 'network_confirmed')
        self.page.expect_response.assert_called_once()

    def test_navigation_timeout_probes_runtime_before_repeating_broken_page(self):
        fresh = MagicMock()
        self.client.page.side_effect = [self.page, fresh]
        self.page.goto.side_effect = PlaywrightTimeoutError('navigation stalled')
        self.client.diagnose_comment_runtime = Mock(return_value={
            'classification': 'playwright_dom_access_failed_native_responsive'})
        self.client.check_comment_editor('mock')
        self.client.diagnose_comment_runtime.assert_called_once()
        self.page.goto.assert_called_once()
        self.page.close.assert_called_once()
        fresh.goto.assert_called_once()
        fresh.keyboard.type.assert_not_called()
        fresh.expect_response.assert_not_called()

    def test_same_target_retry_reloads_without_typing(self):
        self.page.url = 'https://www.bilibili.com/video/BVtest1/'
        self.page.wait_for_function.side_effect = [PlaywrightTimeoutError('slow'), MagicMock()]
        self.client.check_comment_editor('https://www.bilibili.com/video/BVtest1')
        self.page.goto.assert_called_once()
        self.page.reload.assert_called_once()
        self.page.keyboard.type.assert_not_called()
        ready = next(r for r in self.log_rows() if r['event'] == 'comment_preparation_ready')
        self.assertTrue(ready['recovered'])
        self.assertEqual(ready['attempt'], 2)

    def test_missing_header_replaces_page_on_first_retry(self):
        fresh = MagicMock()
        self.client.page.side_effect = [self.page, fresh]
        self.client.diagnose_comment_runtime = Mock(return_value={'playwright_html': True})
        self.page.wait_for_function.side_effect = PlaywrightTimeoutError('header missing')
        with patch('mediaflow.bilibili._comment_page_state', return_value={
                'comment_root': True, 'component_defined': True, 'comment_header': False}):
            self.client.check_comment_editor('mock')
        self.page.goto.assert_called_once()
        fresh.goto.assert_called_once()
        self.page.close.assert_called_once()
        analysis = next(r for r in self.log_rows() if r['event'] == 'comment_failure_analysis')
        self.assertEqual(analysis['classification'], 'comment_header_not_rendered')
        fresh.keyboard.type.assert_not_called()
        fresh.expect_response.assert_not_called()

    def test_fast_dom_probe_recovers_before_waiting_for_comment_component(self):
        fresh = MagicMock()
        self.client.page.side_effect = [self.page, fresh]
        self.page.locator.return_value.evaluate.side_effect = PlaywrightTimeoutError('utility stalled')
        self.client.diagnose_comment_runtime = Mock(return_value={
            'classification': 'playwright_dom_access_failed_native_responsive'})
        self.client.check_comment_editor('mock')
        self.page.wait_for_function.assert_not_called()
        fresh.wait_for_function.assert_called_once()
        probe = self.page.locator.return_value.evaluate.call_args
        self.assertLessEqual(probe.kwargs['timeout'], 5000)
        error = next(r for r in self.log_rows() if r['event'] == 'comment_preparation_error')
        self.assertEqual(error['stage'], 'dom_probe')
        fresh.keyboard.type.assert_not_called()

    def test_editor_readiness_does_not_depend_on_animation_frames(self):
        self.client.check_comment_editor('mock')
        self.assertEqual(self.page.wait_for_function.call_args.kwargs['polling'], 250)

    def test_diagnostic_failure_does_not_abort_safe_retry(self):
        self.client.diagnose_comment_runtime = Mock(side_effect=RuntimeError('probe unavailable'))
        self.page.wait_for_function.side_effect = [PlaywrightTimeoutError('slow'), MagicMock()]
        self.client.check_comment_editor('mock')
        self.assertEqual(self.page.goto.call_count, 2)
        self.page.keyboard.type.assert_not_called()

    def test_classification_does_not_mislabel_author_restriction_as_missing_box(self):
        from mediaflow.bilibili import _comment_failure_classification
        self.assertEqual(_comment_failure_classification({
            'comments_disabled': True, 'comment_root': True, 'comment_box': False}, {}),
            'comments_disabled')
        self.assertEqual(_comment_failure_classification({}, {}), 'undetermined')

    def test_disabled_box_arriving_after_timeout_skips_without_reload(self):
        self.page.wait_for_function.side_effect = PlaywrightTimeoutError('late disabled box')
        with patch('mediaflow.bilibili._comment_page_state', return_value={'comments_disabled': True}):
            with self.assertRaises(CommentPreparationError) as caught:
                self.client.check_comment_editor('mock')
        self.assertEqual(caught.exception.reason, 'comments_disabled')
        self.assertEqual(caught.exception.error_type, 'CommentUnavailable')
        self.page.goto.assert_called_once()
        self.page.reload.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def test_failure_after_typing_does_not_recover_or_submit(self):
        self.button.click.side_effect = [None, PlaywrightTimeoutError('button unavailable')]
        with self.assertRaises(PlaywrightTimeoutError):
            self.client.publish_comment('mock', 'mock')
        self.assertEqual(self.page.goto.call_count, 1)
        self.page.keyboard.type.assert_called_once()
        self.page.expect_response.assert_not_called()

    def test_timeout_after_submit_does_not_retry(self):
        # The trial succeeds; the real submit click times out.
        self.button.click.side_effect = [None, None, PlaywrightTimeoutError('receipt missing')]
        self.page.locator.return_value.inner_text.return_value = ''
        self.assertEqual(self.client.publish_comment('mock', 'mock'), 'submitted_unconfirmed')
        self.assertEqual(self.page.goto.call_count, 1)
        self.page.keyboard.type.assert_called_once()
        self.page.expect_response.assert_called_once()

    def test_editor_check_never_types_or_submits(self):
        self.assertEqual(self.client.check_comment_editor('mock'), {'editor': True})
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()
        self.button.click.assert_called_once_with(timeout=10_000)
        self.page.close.assert_called_once()

    def test_missing_focus_never_types_or_submits(self):
        self.page.locator.return_value.evaluate.return_value = False
        with self.assertRaises(CommentPreparationError) as caught:
            self.client.publish_comment('mock', 'mock')
        self.assertEqual(caught.exception.stage, 'editor_focus')
        self.assertEqual(self.page.goto.call_count, 1)
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()

    def test_disabled_comments_fail_immediately_without_retry_or_click(self):
        self.page.wait_for_function.return_value.json_value.return_value = 'disabled_by_author'
        with self.assertRaises(CommentPreparationError) as caught:
            self.client.publish_comment('mock', 'mock')
        self.assertEqual(caught.exception.reason, 'comments_disabled_by_author')
        self.assertEqual(caught.exception.stage, 'comment_permission')
        self.assertEqual(self.page.goto.call_count, 1)
        self.button.click.assert_not_called()
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()

    def log_rows(self):
        return [json.loads(s) for s in (Path(self.tmp.name)/'browser-flow.jsonl').read_text(encoding='utf-8').splitlines()]

    def retain_editor(self):
        self.client.context = object()
        self.page.context = self.client.context
        self.page.url = 'mock'
        self.page.is_closed.return_value = False
        self.client.check_comment_editor('mock', retain=True)

    def test_fresh_prepared_page_reuses_navigation_and_still_checks_permission(self):
        self.retain_editor()
        self.page.close.assert_not_called()
        self.assertEqual(self.client.publish_comment('mock', 'private'), 'network_confirmed')
        self.client.page.assert_called_once()
        self.page.goto.assert_called_once()
        self.assertEqual(self.page.wait_for_function.call_count, 2)
        self.page.keyboard.type.assert_called_once()
        self.page.expect_response.assert_called_once()
        self.page.close.assert_called_once()
        self.assertIsNone(self.client._prepared_comment)

    def test_tracking_query_and_trailing_slash_keep_same_video_but_other_targets_do_not(self):
        from mediaflow.bilibili import _same_comment_target
        url = 'https://www.bilibili.com/video/BVtest1'
        self.assertTrue(_same_comment_target(url,url+'/?tracking=private#reply'))
        self.assertFalse(_same_comment_target(url,'https://www.bilibili.com/video/BVtest2/'))
        self.assertFalse(_same_comment_target(url,'https://other.invalid/video/BVtest1'))
        self.assertFalse(_same_comment_target(url,url+'/other'))
        self.assertFalse(_same_comment_target(url,url+'suffix'))

    def test_retained_page_permission_change_stops_without_typing(self):
        self.retain_editor()
        self.page.wait_for_function.return_value.json_value.return_value = 'disabled_by_author'
        with self.assertRaises(CommentPreparationError):
            self.client.publish_comment('mock', 'private')
        self.page.goto.assert_called_once()
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()
        self.page.close.assert_called_once()

    def test_retained_page_focus_error_does_not_navigate_or_submit(self):
        self.retain_editor()
        self.page.locator.return_value.evaluate.return_value = False
        with self.assertRaises(CommentPreparationError):
            self.client.publish_comment('mock', 'private')
        self.page.goto.assert_called_once()
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()

    def test_stale_or_changed_prepared_page_is_closed_before_fresh_preparation(self):
        for change in ('expired', 'url', 'context', 'closed'):
            with self.subTest(change=change):
                self.page.reset_mock()
                self.retain_editor()
                if change == 'expired':
                    self.client._prepared_comment['created'] -= 31
                elif change == 'url':
                    self.page.url = 'other'
                    self.page.goto.side_effect = lambda url, **kwargs: setattr(self.page, 'url', url)
                elif change == 'context':
                    self.page.context = object()
                else:
                    self.page.is_closed.return_value = True
                self.assertEqual(self.client.publish_comment('mock', 'private'), 'network_confirmed')
                self.assertEqual(self.page.goto.call_count, 2)
                self.assertEqual(self.page.close.call_count, 2)
                self.page.expect_response.assert_called_once()

    def test_detach_closes_owned_retained_page_but_never_user_browser(self):
        self.retain_editor()
        browser = self.client._browser = Mock()
        pw = self.client._pw = Mock()
        self.client.__exit__()
        self.page.close.assert_called_once()
        browser.close.assert_not_called()
        pw.stop.assert_called_once()

    def test_preparation_budget_stops_retry_when_consumed(self):
        with patch('mediaflow.bilibili.time.monotonic', return_value=0) as clock:
            def timeout(*args, **kwargs):
                self.assertLessEqual(kwargs['timeout'], 30_000)
                clock.return_value = 91
                raise PlaywrightTimeoutError('network')
            self.page.goto.side_effect = timeout
            with self.assertRaises(CommentPreparationError):
                self.client.check_comment_editor('mock')
        self.page.goto.assert_called_once()
        self.page.keyboard.type.assert_not_called()
        self.page.expect_response.assert_not_called()
        self.assertFalse(any(r['event'] == 'comment_preparation_retry' for r in self.log_rows()))

    def test_network_diagnostics_are_bounded_private_and_listeners_removed(self):
        from mediaflow.bilibili import _PreparationNetworkDiagnostics
        request = Mock(url='https://api.bilibili.com/x/v2/reply/main?token=private',
                       resource_type='fetch', failure='net::ERR_CONNECTION_RESET https://private.invalid/key')
        with _PreparationNetworkDiagnostics(self.page) as network:
            for _ in range(20):
                network.request_failed(request)
            network.response(Mock(request=request, status=503))
            network.page_error(RuntimeError('private cookie'))
            network.request_failed(Mock(url='https://private.invalid/file', resource_type='script'))
        self.assertEqual(network.counts, dict(request_failed=20, http_error=1, page_error=1))
        self.assertEqual(len(network.events), 12)
        raw = json.dumps(network.events)
        self.assertIn('net::ERR_CONNECTION_RESET', raw)
        self.assertNotIn('private', raw)
        self.assertNotIn('token', raw)
        self.assertEqual(self.page.remove_listener.call_args_list, self.page.on.call_args_list)

    def test_paused_watch_has_bounded_timeout(self):
        self.page.locator.return_value.first.evaluate.return_value = dict(duration=60, current=0, paused=True, ended=False)
        with patch('mediaflow.bilibili.time.sleep'), self.assertRaisesRegex(RuntimeError, 'stalled'):
            self.client.watch_and_like('mock')
        self.assertEqual(self.page.locator.return_value.first.evaluate.call_count, 10)
        self.page.locator.return_value.first.click.assert_not_called()
