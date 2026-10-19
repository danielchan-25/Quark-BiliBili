import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediaflow.database import Database
from tools import validate_comment_preparation as validation


class CommentPreparationValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict('os.environ', MEDIAFLOW_LOG_DIR=str(self.root/'logs')))
        self.db = Database(self.root/'test.db')
        self.addCleanup(self.db.close)
        for index, account in enumerate(validation.ACCOUNTS, 1):
            self.db.connection.execute('''INSERT INTO accounts(id,name,platform,role,cdp_url,uid)
                VALUES(?,?,'bilibili','promotion',?,?)''', (index,account,'mock'+str(index),account.split('-')[1]))
        self.db.connection.execute("INSERT INTO promotion_resources(category,url,keywords) VALUES('test','private','test')")
        self.db.connection.commit()
        self.stack.enter_context(patch.object(validation,'ROOT',self.root))
        self.stack.enter_context(patch.object(validation.Settings,'load',return_value=SimpleNamespace(database_path=self.root/'test.db')))
        self.stack.enter_context(patch.object(validation.BilibiliClient,'__enter__',lambda client:client))
        self.stack.enter_context(patch.object(validation.BilibiliClient,'__exit__',lambda *args:None))
        self.stack.enter_context(patch.object(validation.BilibiliClient,'uid',lambda client: validation.ACCOUNTS[int(client.cdp_url[-1])-1].split('-')[1]))
        self.page = Mock()
        self.stack.enter_context(patch.object(validation.BilibiliClient,'take_prepared_comment',return_value=self.page))
        self.stack.enter_context(patch.object(validation,'_recheck_prepared_comment_editor'))
        self.stack.enter_context(patch.object(validation,'_comment_page_state',return_value=dict(editor_visible=True)))
        self.candidate = SimpleNamespace(bvid='BVtest',url='video')
        self.selector = self.stack.enter_context(patch.object(validation.workflow,'available_visible_candidate',return_value=self.candidate))

    def test_readonly_entry_checks_both_accounts_without_business_changes(self):
        before = '\n'.join(self.db.connection.iterdump())
        path, report = validation.validate(list(validation.ACCOUNTS))
        self.assertTrue(report['passed'])
        self.assertEqual(report['blocked_write_calls'],0)
        self.assertEqual([row['status'] for row in report['accounts']],['passed','passed'])
        self.assertEqual(before,'\n'.join(self.db.connection.iterdump()))
        self.assertTrue(json.loads(path.read_text())['business_data_unchanged'])
        self.assertEqual(self.page.close.call_count,2)
        for call in self.selector.call_args_list:
            self.assertFalse(call.kwargs['mutate'])
            self.assertTrue(call.kwargs['retain_page'])

    def test_readonly_database_rejects_unintended_write_and_other_account_continues(self):
        def selection(db,*args,**kwargs):
            if args[1] == 1:
                db.connection.execute("UPDATE promotion_resources SET keywords='changed'")
            return self.candidate
        self.selector.side_effect = selection
        _, report = validation.validate(list(validation.ACCOUNTS))
        self.assertFalse(report['passed'])
        self.assertEqual(report['accounts'][0]['error_type'],'OperationalError')
        self.assertEqual(report['accounts'][1]['status'],'passed')
        self.assertTrue(report['business_data_unchanged'])

    def test_readonly_entry_blocks_accidental_publication(self):
        def selection(db,client,*args,**kwargs):
            client.publish_comment('video','private')
        self.selector.side_effect = selection
        _, report = validation.validate([validation.ACCOUNTS[0]])
        self.assertFalse(report['passed'])
        self.assertEqual(report['blocked_write_calls'],1)
        self.assertEqual(report['accounts'][0]['error_type'],'AssertionError')
        self.assertTrue(report['business_data_unchanged'])
