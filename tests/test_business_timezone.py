import argparse
import contextlib
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
from mediaflow import time_utils
from mediaflow.daily_review import daily_review
from mediaflow.database import Database


class UtcHostClock(datetime):
    """An instant just after Beijing midnight, on a host whose local time is UTC."""
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 10, 27, 16, 5, tzinfo=timezone.utc)
        return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)


class BusinessTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(self.root/'logs'), TZ='UTC'))
        self.stack.enter_context(patch.object(time_utils, 'datetime', UtcHostClock))
        self.stack.enter_context(patch('main.ROOT', self.root))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.db = Database(self.root/'test.db')
        self.addCleanup(self.db.close)
        self.db.connection.executescript("""
            INSERT INTO accounts(id,name,platform,role,cdp_url,uid)
              VALUES(1,'a','bilibili','promotion','mock','1');
            INSERT INTO promotion_resources(id,category,url,keywords)
              VALUES(1,'资料','https://example.invalid/resource','test');
        """)

    def add_record(self, when, *, status='published', bvid='BVprevious'):
        self.db.connection.execute("""INSERT INTO promotion_records(
            account_id,resource_id,platform,video_url,bvid,text,content_hash,publish_status,created_at)
            VALUES(1,1,'bilibili',?,?,?,'hash',?,?)""",
            (bvid, bvid, 'stored message', status, when))
        self.db.connection.commit()

    def test_business_clock_crosses_midnight_without_using_host_local_time(self):
        self.assertEqual(time_utils.beijing_now().isoformat(), '2026-10-28T00:05:00+08:00')
        self.assertEqual(time_utils.publication_time('2026-10-28 00:01:00').isoformat(),
                         '2026-10-28T00:01:00+08:00')
        self.assertEqual(time_utils.utc_time('2026-10-27 16:01:00').isoformat(),
                         '2026-10-27T16:01:00+00:00')

    def test_frequency_limit_honors_legacy_beijing_times_and_explicit_offsets(self):
        for status in ('published', 'submitted'):
            for when, allowed in (
                    ('2026-10-28 00:00:00', False),
                    ('2026-10-27 23:50:01', False),
                    ('2026-10-27 23:50:00', True),
                    ('2026-10-27T16:00:00+00:00', False),
                    ('2026-10-27T15:50:00Z', True)):
                with self.subTest(status=status, when=when):
                    self.db.connection.execute('DELETE FROM promotion_records')
                    self.add_record(when, status=status)
                    self.assertEqual(main.rate_ready(self.db, 1)[0], allowed)

    def test_new_publication_stays_beijing_and_sqlite_task_time_stays_utc(self):
        self.db.connection.executescript("""
            INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,title,status,ai_status)
              VALUES(1,'bilibili','https://example.invalid/video','BVnew','test','available','approved');
            INSERT INTO phrases(category,text,source) VALUES('资料','相关资料可以按需查阅：{url}','test');
        """)
        client = Mock()
        client.uid.return_value = '1'
        client.video_visible.return_value = True
        client.publish_comment.return_value = 'network_confirmed'
        client.last_submitted_comment_id = '123'
        client.verify_comment.return_value = '123'
        with patch('main.BilibiliClient') as browser, patch('main.promotion_keywords', return_value=['test']):
            browser.return_value.__enter__.return_value = client
            result = main.promotion(self.db, None,
                argparse.Namespace(account='a', max_comments=1, dry_run=False, slot=None))
        self.assertEqual(result, 0)
        self.assertEqual(self.db.connection.execute('SELECT created_at FROM promotion_records').fetchone()[0],
                         '2026-10-28 00:05:00')
        task_time = self.db.connection.execute('SELECT started_at FROM task_runs').fetchone()[0]
        sqlite_now = self.db.connection.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0]
        self.assertLess(abs((datetime.fromisoformat(task_time)-datetime.fromisoformat(sqlite_now)).total_seconds()), 5)
        self.assertLess(abs((time_utils.utc_time(task_time)-datetime.now(timezone.utc)).total_seconds()), 5)

    def test_daily_review_defaults_to_beijing_date_on_utc_host(self):
        self.add_record('2026-10-27 23:59:00', bvid='BVold')
        self.add_record('2026-10-28 00:01:00', bvid='BVtoday')
        notify = Mock(webhook='configured')
        with patch('mediaflow.daily_review.BilibiliClient') as browser, \
                patch('mediaflow.daily_review.scheduled_states', return_value=[]) as states:
            client = browser.return_value.__enter__.return_value
            client.uid.return_value = '1'
            client.verify_comment.return_value = '123'
            self.assertEqual(daily_review(self.db, notify, argparse.Namespace(date=None)), 0)
        states.assert_called_once_with(datetime(2026, 10, 28).date())
        self.assertEqual(client.verify_comment.call_args.args[0], 'BVtoday')
        client.verify_comment.assert_called_once()
        self.assertIn('2026-10-28 日报', notify.send_text.call_args.args[0])
        self.assertEqual(self.db.connection.execute(
            "SELECT verification_status FROM promotion_records WHERE bvid='BVold'").fetchone()[0], 'not_required')

    def test_legacy_summary_uses_beijing_day_but_utc_task_query_bounds(self):
        self.db.connection.executescript("""
            INSERT INTO task_runs(account_id,task_type,status,started_at)
              VALUES(1,'promotion','SUCCESS','2026-10-27 15:59:00');
            INSERT INTO task_runs(account_id,task_type,status,started_at)
              VALUES(1,'promotion','SUCCESS','2026-10-27 16:01:00');
        """)
        notify = Mock()
        with patch('main.promotion_links', return_value=[]):
            main.promotion_summary(self.db, notify, argparse.Namespace(date=None))
        message = notify.send_text.call_args.args[0]
        self.assertIn('成功1, 失败0', message)
        self.assertIn('00:01', message)
        self.assertNotIn('23:59', message)


if __name__ == '__main__':
    unittest.main()
