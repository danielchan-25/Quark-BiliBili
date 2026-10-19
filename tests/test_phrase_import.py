import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediaflow.database import Database
from mediaflow.dedup import text_key
from tools.import_phrases import import_phrases


class PhraseImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        audit = patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(self.root / 'logs'))
        audit.start()
        self.addCleanup(audit.stop)
        self.db = Database(self.root / 'test.db')
        self.addCleanup(self.db.close)
        self.db.connection.execute("INSERT INTO promotion_resources(category,url,keywords) VALUES('资料','https://example.invalid/configured','test')")
        self.db.connection.commit()
        self.valid = {'category': '资料', 'text': '相关学习资料可按需参考，链接在这里：{url}'}

    def count(self):
        return self.db.connection.execute('SELECT COUNT(*) FROM phrases').fetchone()[0]

    def observer_count(self):
        with contextlib.closing(sqlite3.connect(self.root / 'test.db')) as observer:
            return observer.execute('SELECT COUNT(*) FROM phrases').fetchone()[0]

    def test_mixed_valid_and_invalid_batch_writes_nothing(self):
        with self.assertRaises(ValueError):
            import_phrases(self.db, [self.valid, {'category': '资料', 'text': '缺少链接占位符的无效文案'}])
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.db.connection.in_transaction)

    def test_malformed_input_is_reported_as_validation_error(self):
        invalid_inputs = (None, {}, 'text', [], [self.valid] * 101,
                          [None], ['text'], [[]], [{}],
                          [{'category': [], 'text': self.valid['text']}],
                          [{'category': '资料', 'text': None}],
                          [{'category': '资料', 'text': 123}])
        for rows in invalid_inputs:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                import_phrases(self.db, rows)
            self.assertEqual(self.count(), 0)

    def test_invalid_rules_reject_entire_batch(self):
        invalid_phrases = (
            {'category': '未启用类别', 'text': self.valid['text']},
            {'category': '资料', 'text': '太短：{url}'},
            {'category': '资料', 'text': '长' * 241 + '{url}'},
            {'category': '资料', 'text': '两个链接都在这里，请按需参考：{url}{url}'},
            {'category': '资料', 'text': self.valid['text'] + ' HTTPS://example.invalid/old'},
        )
        for invalid in invalid_phrases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                import_phrases(self.db, [self.valid, invalid])
            self.assertEqual(self.count(), 0)

    def test_duplicate_body_ignores_whitespace_and_url(self):
        rows = [self.valid, {'category': '资料', 'text': self.valid['text'].replace('相关', '相 关')}]
        self.assertEqual(import_phrases(self.db, rows), 1)
        self.assertEqual(import_phrases(self.db, rows), 0)
        self.assertEqual(self.count(), 1)

    def test_used_body_cannot_be_imported_with_new_url(self):
        published = self.valid['text'].replace('{url}', 'https://example.invalid/old')
        self.db.connection.execute("INSERT INTO promotion_records(platform,video_url,bvid,text,content_hash,publish_status) VALUES('bilibili','video','BVused',?,'test','published')", (published,))
        self.db.connection.commit()
        # The placeholder will render the current configured URL at selection
        # time, but that URL change must not allow reuse of a published body.
        self.assertEqual(import_phrases(self.db, [self.valid]), 0)
        self.assertEqual(self.count(), 0)

    def test_reserved_body_cannot_be_imported(self):
        self.db.connection.execute('INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES(?,?,1)',
                                   ('BVreserved', text_key(self.valid['text'].replace('{url}', 'https://example.invalid/other'))))
        self.db.connection.commit()
        self.assertEqual(import_phrases(self.db, [self.valid]), 0)
        self.assertEqual(self.count(), 0)

    def test_import_does_not_commit_callers_transaction(self):
        self.db.connection.execute("INSERT INTO phrases(category,text,source) VALUES('资料','caller pending phrase','test')")
        self.assertEqual(import_phrases(self.db, [self.valid]), 1)
        self.assertTrue(self.db.connection.in_transaction)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.observer_count(), 0)
        self.db.connection.rollback()
        self.assertEqual(self.count(), 0)

    def test_import_without_outer_transaction_leaves_commit_to_caller(self):
        self.assertFalse(self.db.connection.in_transaction)
        self.assertEqual(import_phrases(self.db, [self.valid]), 1)
        self.assertTrue(self.db.connection.in_transaction)
        self.assertEqual(self.observer_count(), 0)
        self.db.connection.commit()
        self.assertEqual(self.observer_count(), 1)

    def test_validation_failure_preserves_callers_pending_changes(self):
        self.db.connection.execute("INSERT INTO phrases(category,text,source) VALUES('资料','caller pending phrase','test')")
        with self.assertRaises(ValueError):
            import_phrases(self.db, [self.valid, {}])
        self.assertTrue(self.db.connection.in_transaction)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.observer_count(), 0)
        self.db.connection.commit()
        self.assertEqual(self.observer_count(), 1)

    def test_database_failure_rolls_back_only_import_savepoint(self):
        self.db.connection.execute("""CREATE TRIGGER reject_phrase BEFORE INSERT ON phrases
            WHEN NEW.text LIKE '故障%'
            BEGIN SELECT RAISE(ABORT, 'test insertion failure'); END""")
        self.db.connection.commit()
        self.db.connection.execute("INSERT INTO phrases(category,text,source) VALUES('资料','caller pending phrase','test')")
        failed = {'category': '资料', 'text': '故障测试文案，相关资料可按需参考：{url}'}
        with self.assertRaises(sqlite3.IntegrityError):
            import_phrases(self.db, [self.valid, failed])
        self.assertTrue(self.db.connection.in_transaction)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.observer_count(), 0)
        self.db.connection.commit()
        self.assertEqual(self.observer_count(), 1)


if __name__ == '__main__':
    unittest.main()
