import io
import json
import unittest
from unittest.mock import Mock, patch
from websocket import WebSocketTimeoutException
from mediaflow.bilibili import BilibiliClient, _recover_stalled_profile_info_tab


class CDPTests(unittest.TestCase):
    def setUp(self):
        audit = patch('mediaflow.bilibili.write_audit_event')
        self.audit = audit.start()
        self.addCleanup(audit.stop)

    def test_failed_enter_stops_runtime_and_next_account_connects(self):
        with patch('mediaflow.bilibili.sync_playwright') as factory:
            first, second = Mock(), Mock()
            factory.return_value.start.side_effect = [first, second]
            first.chromium.connect_over_cdp.side_effect = RuntimeError('connection failed')
            second.chromium.connect_over_cdp.return_value.contexts = [Mock()]
            client = BilibiliClient('ws://127.0.0.1:9500/devtools/browser/test')
            with self.assertRaisesRegex(RuntimeError, 'connection failed'):
                client.__enter__()
            first.stop.assert_called_once()
            self.assertIsNone(client._pw)
            with BilibiliClient('ws://127.0.0.1:9502/devtools/browser/test'):
                pass
            second.stop.assert_called_once()
            second.chromium.connect_over_cdp.return_value.close.assert_not_called()
            failures = [call.kwargs for call in self.audit.call_args_list if call.args[1] == 'cdp_connection_failed']
            self.assertEqual(failures[0]['phase'], 'connect_over_cdp')
            self.assertEqual(failures[0]['error_type'], 'RuntimeError')

    def test_local_discovery_bypasses_proxy(self):
        with patch('mediaflow.bilibili.build_opener') as opener, patch('mediaflow.bilibili.sync_playwright') as factory, \
                patch('mediaflow.bilibili._recover_stalled_profile_info_tab', return_value=False) as preflight:
            opener.return_value.open.return_value = io.BytesIO(b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9500/devtools/browser/test"}')
            runtime = factory.return_value.start.return_value
            runtime.chromium.connect_over_cdp.return_value.contexts = [Mock()]
            with BilibiliClient('http://127.0.0.1:9500'):
                pass
            self.assertEqual(opener.call_args.args[0].proxies, {})
            preflight.assert_called_once_with('http://127.0.0.1:9500', 'ws://127.0.0.1:9500/devtools/browser/test')
            runtime.chromium.connect_over_cdp.assert_called_once_with('ws://127.0.0.1:9500/devtools/browser/test',timeout=60000)
            events = [call.args[1] for call in self.audit.call_args_list]
            self.assertIn('cdp_connection_ready', events)
            self.assertEqual([call.kwargs['phase'] for call in self.audit.call_args_list
                              if call.args[1] == 'cdp_phase_completed'],
                             ['discovery', 'profile_info_preflight', 'playwright_start', 'connect_over_cdp', 'context_selection'])

    def test_stalled_profile_info_tab_is_replaced_without_touching_business_tabs(self):
        targets = [
            {'id': 'stalled', 'type': 'page', 'title': 'Profile · ChromeManager',
             'url': 'http://127.0.0.1:8765/profiles/example', 'webSocketDebuggerUrl': 'ws://page'},
            {'id': 'business', 'type': 'page', 'title': 'Bilibili',
             'url': 'https://www.bilibili.com/', 'webSocketDebuggerUrl': 'ws://business'},
        ]
        with patch('mediaflow.bilibili.build_opener') as opener, patch('mediaflow.bilibili._cdp_command') as command:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(targets).encode())
            command.side_effect = [WebSocketTimeoutException(), {'targetId': 'replacement'}, {'success': True}]
            self.assertTrue(_recover_stalled_profile_info_tab('http://127.0.0.1:9500', 'ws://browser'))
            self.assertEqual([call.args[1] for call in command.call_args_list],
                             ['Page.getFrameTree', 'Target.createTarget', 'Target.closeTarget'])
            self.assertEqual(command.call_args_list[-1].args[2], {'targetId': 'stalled'})

    def test_responsive_profile_info_tab_is_preserved(self):
        target = [{'id': 'healthy', 'type': 'page', 'title': 'ChromeManager',
                   'url': 'http://127.0.0.1:8765/profiles/example', 'webSocketDebuggerUrl': 'ws://page'}]
        with patch('mediaflow.bilibili.build_opener') as opener, patch('mediaflow.bilibili._cdp_command') as command:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(target).encode())
            command.return_value = {'frameTree': {}}
            self.assertFalse(_recover_stalled_profile_info_tab('http://127.0.0.1:9500', 'ws://browser'))
            command.assert_called_once_with('ws://page', 'Page.getFrameTree')
