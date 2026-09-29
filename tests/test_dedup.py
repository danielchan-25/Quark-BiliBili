import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from main import available_candidate, available_visible_candidate, phrase
from mediaflow.bilibili import Candidate
from mediaflow.database import Database
from mediaflow.dedup import reserve, text_key


class DedupTests(unittest.TestCase):
    def test_global_reservations_and_phrase_exhaustion(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / 'test.db')
            db.connection.execute("INSERT INTO promotion_resources(id,category,url,keywords) VALUES(1,'AI','https://example.com','AI')")
            db.connection.execute("INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,status,ai_status,ai_score,ai_confidence) VALUES(1,'bilibili','https://video/1','BV1','available','approved',3,1)")
            db.connection.commit()
            candidate = available_candidate(db, 1, 1)
            self.assertIsNotNone(candidate)
            text, _ = phrase(db, 'AI', 'https://example.com')
            reserve(db, candidate, text, 1)
            self.assertIsNone(available_candidate(db, 2, 1))
            with self.assertRaises(ValueError):
                phrase(db, 'AI', 'https://different.com')
            other = Database(Path(folder) / 'test.db')
            with self.assertRaises(sqlite3.IntegrityError):
                reserve(other, candidate, 'different text', 2)
            with self.assertRaises(ValueError):
                reserve(other, Candidate('BV2','https://video/2','',''), text, 2)
            other.close()
            db.close()

    def test_normalization(self):
        self.assertEqual(text_key('你好 \nhttps://one.com'), text_key('你 好 https://two.com'))

    def test_invisible_video_is_skipped_before_reservation(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / 'test.db')
            db.connection.execute("INSERT INTO promotion_resources(id,category,url,keywords) VALUES(1,'AI','https://example.com','AI')")
            db.connection.executemany(
                "INSERT INTO promotion_candidates(resource_id,platform,video_url,bvid,status,ai_status,ai_score) VALUES(1,'bilibili',?,?,'available','approved',?)",
                [('https://video/hidden', 'BVhidden', 3), ('https://video/visible', 'BVvisible', 2)],
            )
            client = Mock()
            client.video_visible.side_effect = [False, True]
            candidate = available_visible_candidate(db, client, 1, 1)
            self.assertEqual(candidate.bvid, 'BVvisible')
            self.assertEqual(client.video_visible.call_count, 2)
            self.assertEqual(db.connection.execute("SELECT status FROM promotion_candidates WHERE bvid='BVhidden'").fetchone()[0], 'unavailable')
            db.close()
