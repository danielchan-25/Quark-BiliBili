import unittest
from unittest.mock import Mock, patch
import requests
from mediaflow.notify import FeishuNotifier


class NotifierTests(unittest.TestCase):
    def test_requires_success_acknowledgement(self):
        notify=FeishuNotifier('https://example.invalid/private-hook','test-secret')
        with patch('mediaflow.notify.requests.post') as post:
            for payload in ({'code':0},{'StatusCode':0}):
                post.return_value=Mock()
                post.return_value.json.return_value=payload
                notify.send_text('test')
            for payload in ({},{'code':19021},[]):
                post.return_value.json.return_value=payload
                with self.assertRaises(RuntimeError): notify.send_text('test')

    def test_request_failure_does_not_expose_webhook(self):
        notify=FeishuNotifier('https://example.invalid/private-hook',None)
        with patch('mediaflow.notify.requests.post',side_effect=requests.Timeout('https://example.invalid/private-hook')):
            with self.assertRaises(RuntimeError) as raised: notify.send_text('test')
        self.assertNotIn('private-hook',str(raised.exception))
