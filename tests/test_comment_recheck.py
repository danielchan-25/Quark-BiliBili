import unittest
from unittest.mock import Mock

from tools.recheck_comments import recheck_record


class CommentRecheckTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.uid.return_value = '123'
        self.row = dict(uid='123', bvid='BVtest', text='existing body',
                        resource_url='https://example.invalid/configured',
                        remote_comment_id='456', publish_status='published')

    def test_unavailable_comment_is_observation_not_deleted_history(self):
        self.client.verify_comment_details.return_value = dict(
            status='unconfirmed', method='root_detail', reason='remote_comment_unavailable',
            api_code=12006, scope={'pages_checked': 0}, comment_id=None)
        outcome = recheck_record(self.client, self.row)
        self.assertEqual(outcome['result'], 'unconfirmed')
        self.assertEqual(outcome['reason'], 'remote_comment_unavailable')
        self.assertEqual(outcome['api_code'], 12006)
        self.assertEqual(self.row['publish_status'], 'published')
        self.client.publish_comment.assert_not_called()
        self.client.watch_and_like.assert_not_called()

    def test_exact_comment_id_is_passed_and_actual_match_is_preserved(self):
        self.client.verify_comment_details.return_value = dict(
            status='verified', method='root_detail', comment_id='456', reason='exact_match')
        outcome = recheck_record(self.client, self.row)
        self.assertEqual(outcome['result'], 'verified_now')
        self.assertEqual(outcome['comment_id'], '456')
        self.client.verify_comment_details.assert_called_once_with(
            'BVtest', '123', self.row['text'], self.row['resource_url'], '456')
        self.assertNotIn('text', outcome)
        self.assertNotIn('resource_url', outcome)

    def test_uid_mismatch_stops_before_remote_lookup(self):
        self.client.uid.return_value = '1234'
        outcome = recheck_record(self.client, self.row)
        self.assertEqual(outcome['reason'], 'uid_mismatch')
        self.assertEqual(outcome['result'], 'verification_error')
        self.client.verify_comment_details.assert_not_called()

    def test_api_failure_retains_technical_diagnosis(self):
        self.client.verify_comment_details.return_value = dict(
            status='error', method='listing', reason='reply_api_rejected',
            api_code=-352, error_type='ReplyAPIError')
        outcome = recheck_record(self.client, self.row)
        self.assertEqual(outcome['result'], 'verification_error')
        self.assertEqual(outcome['api_code'], -352)
        self.assertEqual(outcome['error_type'], 'ReplyAPIError')


if __name__ == '__main__':
    unittest.main()
