import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from mediaflow.bilibili import BilibiliClient, Candidate
from mediaflow.candidate_pool import PoolPolicy, eligible_count, inventory, maintain_candidate_pool
from mediaflow.candidate_screening import ScreeningResult
from mediaflow.database import Database


class CandidatePoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(self.root/'logs')))
        self.db = Database(self.root/'db')
        self.addCleanup(self.db.close)
        self.db.connection.executescript('''
            INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(1,'a','bilibili','promotion','mock','1');
            INSERT INTO promotion_resources(id,category,url,keywords) VALUES(1,'资料','private-url','configured');
        ''')
        self.db.connection.commit()
        self.policy = PoolPolicy(low_watermark=1, target=2, pages_per_keyword=2, max_reviews=5, cooldown_seconds=0)
        self.client = Mock()
        self.client.uid.return_value = '1'
        self.client.video_visible.return_value = True
        self.pages = {}
        self.client.search.side_effect = lambda keyword, limit, page_number: self.pages.get(page_number, [])
        factory = self.stack.enter_context(patch('main.BilibiliClient'))
        factory.return_value.__enter__.return_value = self.client
        self.stack.enter_context(patch('main.ROOT', self.root))
        self.stack.enter_context(patch('main.Settings.load', return_value=SimpleNamespace(typesafe_api_key='mock')))
        self.stack.enter_context(patch('main.promotion_keywords', return_value=['configured']))
        self.screen = self.stack.enter_context(patch('main.screen_candidate', return_value=ScreeningResult(3,.9,.1,'approved')))

    def add_candidate(self, bvid, *, status='available', ai='approved', url=None):
        self.db.connection.execute('''INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,title,author,
            relevance,status,ai_status) VALUES(1,'bilibili',?,?,?,'author','configured',?,?)''',
            (url or bvid, bvid, bvid, status, ai))
        self.db.connection.commit()

    def candidate(self, bvid):
        return Candidate(bvid, bvid, bvid, 'author')

    def callback(self, policy, target, report):
        main.replenish_promotion_candidates(self.db, 'a', policy, target, report)

    def maintain(self, **kwargs):
        result = maintain_candidate_pool(self.db, 'a', self.callback, state_dir=self.root/'pool',
                                         policy=self.policy, **kwargs)
        self.client.publish_comment.assert_not_called()
        self.client.watch_and_like.assert_not_called()
        return result

    def test_counts_exclude_used_reserved_unavailable_and_rejected_globally(self):
        for bvid in ('BVfresh', 'BVused', 'BVreserved'):
            self.add_candidate(bvid)
        self.add_candidate('BVfresh', url='alternate-url')
        self.add_candidate('BVgone', status='unavailable')
        self.add_candidate('BVrejected', ai='rejected')
        self.db.connection.execute("INSERT INTO promotion_records(platform,video_url,bvid,text,content_hash,publish_status) VALUES('bilibili','BVused','BVused','used','hash','failed')")
        self.db.connection.execute("INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES('BVreserved','hash',99)")
        self.db.connection.commit()
        self.assertEqual(inventory(self.db)[0]['total'], 6)
        self.assertEqual(eligible_count(self.db, 1), 1)
        self.assertEqual(main.available_candidate(self.db, 1, 1).bvid, 'BVfresh')

    def test_healthy_stock_does_not_connect_search_or_screen(self):
        self.add_candidate('BVfresh')
        result = self.maintain()
        self.assertEqual(result['status'], 'healthy')
        self.client.uid.assert_not_called()
        self.screen.assert_not_called()

    def test_low_stock_refills_from_later_page_and_skips_used_and_reserved(self):
        self.add_candidate('BVused')
        self.db.connection.execute("INSERT INTO promotion_records(platform,video_url,bvid,text,content_hash,publish_status) VALUES('bilibili','BVused','BVused','used','hash','published')")
        self.db.connection.execute("INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES('BVreserved','hash',1)")
        self.db.connection.commit()
        self.pages = {1:[self.candidate('BVused'),self.candidate('BVreserved')],
                      2:[self.candidate('BVnew1'),self.candidate('BVnew2')]}
        result = self.maintain()
        self.assertEqual(result['status'], 'refilled')
        self.assertEqual(result['after'][0]['eligible'], 2)
        self.assertEqual(result['refresh']['skipped_used'], 2)
        self.assertEqual(self.screen.call_count, 2)
        self.assertEqual([call.kwargs['page_number'] for call in self.client.search.call_args_list], [1,2])
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM promotion_records').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0], 0)

    def test_screening_failure_keeps_earlier_approved_candidate_and_no_unreviewed_new_row(self):
        self.pages = {1:[self.candidate('BVnew1'),self.candidate('BVnew2')]}
        self.screen.side_effect = [ScreeningResult(3,.9,.1,'approved'),TimeoutError('private body and URL')]
        result = self.maintain()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['after'][0]['eligible'], 1)
        self.assertEqual(result['error_type'], 'TimeoutError')
        self.assertNotIn('private body', json.dumps(result))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM promotion_candidates WHERE bvid='BVnew2'").fetchone()[0], 0)
        with contextlib.closing(__import__('sqlite3').connect(self.root/'db')) as observer:
            self.assertEqual(observer.execute("SELECT ai_status FROM promotion_candidates WHERE bvid='BVnew1'").fetchone()[0], 'approved')

    def test_rejections_and_duplicate_search_hits_do_not_inflate_usable_stock(self):
        self.pages = {1:[self.candidate('BVreject'),self.candidate('BVreject')],2:[self.candidate('BVgood')]}
        self.screen.side_effect = [ScreeningResult(0,.9,.5,'rejected'),ScreeningResult(3,.9,.1,'approved')]
        result = self.maintain()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['after'][0]['eligible'], 1)
        self.assertEqual(self.screen.call_count, 2)
        self.assertEqual(result['refresh']['rejected'], 1)

    def test_review_budget_bounds_api_calls(self):
        self.policy = PoolPolicy(low_watermark=1,target=5,max_reviews=1,cooldown_seconds=0)
        self.pages = {1:[self.candidate('BVnew1'),self.candidate('BVnew2')]}
        result = self.maintain()
        self.assertEqual(self.screen.call_count, 1)
        self.assertEqual(result['refresh']['reason'], 'review_budget_reached')
        self.assertEqual(result['after'][0]['eligible'], 1)

    def test_unavailable_video_is_not_screened_or_added(self):
        self.pages = {1:[self.candidate('BVgone')]}
        self.client.video_visible.return_value = False
        result = self.maintain()
        self.assertEqual(result['after'][0]['eligible'], 0)
        self.screen.assert_not_called()

    def test_lock_prevents_concurrent_replenishment(self):
        folder = self.root/'pool';folder.mkdir()
        (folder/'maintenance.lock').write_text('running')
        result = self.maintain()
        self.assertEqual(result['status'], 'already_running')
        self.client.uid.assert_not_called()

    def test_expired_bounded_maintenance_lease_does_not_block_future_refills(self):
        folder = self.root/'pool';folder.mkdir()
        (folder/'maintenance.lock').write_text(json.dumps({'started':time.time()-10000}))
        self.pages = {1:[self.candidate('BVnew1'),self.candidate('BVnew2')]}
        result = self.maintain()
        self.assertEqual(result['status'], 'refilled')
        self.assertFalse((folder/'maintenance.lock').exists())

    def test_cooldown_avoids_repeated_empty_searches(self):
        self.policy = PoolPolicy(low_watermark=1,target=2,cooldown_seconds=900)
        self.assertEqual(self.maintain()['status'], 'partial')
        self.client.search.reset_mock()
        self.assertEqual(self.maintain()['status'], 'cooldown')
        self.client.search.assert_not_called()

    def test_known_stock_can_force_extra_search_for_account_permissions(self):
        self.add_candidate('BVknown')
        self.pages = {1:[self.candidate('BVknown'),self.candidate('BVnew')]}
        result = self.maintain(force=True)
        self.assertEqual(result['status'], 'refilled')
        self.assertEqual(result['refresh']['skipped_known'], 1)
        self.assertEqual(self.screen.call_count, 1)

    def test_explicit_manual_refill_targets_full_stock_without_automatic_cooldown(self):
        self.policy = PoolPolicy(low_watermark=1,target=3,cooldown_seconds=900)
        self.pages = {1:[self.candidate('BVnew1')]}
        self.assertEqual(self.maintain()['status'], 'partial')
        self.pages = {1:[self.candidate('BVnew1'),self.candidate('BVnew2'),self.candidate('BVnew3')]}
        result = self.maintain(manual=True)
        self.assertEqual(result['status'], 'refilled')
        self.assertEqual(result['after'][0]['eligible'], 3)

    def test_uid_mismatch_never_searches_or_screens(self):
        self.client.uid.return_value = '2'
        result = self.maintain()
        self.assertEqual(result['status'], 'failed')
        self.client.search.assert_not_called()
        self.screen.assert_not_called()


class SearchPaginationTests(unittest.TestCase):
    def test_paging_uses_distinct_search_url_and_remains_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, MEDIAFLOW_LOG_DIR=tmp):
            client = BilibiliClient('mock')
            page = Mock()
            client.page = Mock(return_value=page)
            page.locator.return_value.evaluate_all.return_value = [dict(url='https://www.bilibili.com/video/BVexample',title='title',author='author')]
            self.assertEqual(len(client.search('configured',limit=20,page_number=2)), 1)
            self.assertIn('&page=2', page.goto.call_args.args[0])
            page.keyboard.type.assert_not_called()
            page.expect_response.assert_not_called()
            page.close.assert_called_once()
            with self.assertRaises(ValueError):
                client.search('configured',page_number=0)
            self.assertEqual(client.page.call_count, 1)


if __name__ == '__main__':
    unittest.main()
