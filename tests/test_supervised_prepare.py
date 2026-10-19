import json
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import Mock, patch
from tools import supervised_prepare as supervisor


class SupervisedPrepareTests(unittest.TestCase):
    def test_termination_of_real_preparation_only_child(self):
        context = supervisor.multiprocessing.get_context('spawn')
        child = context.Process(target=time.sleep, args=(60,))
        child.start()
        try:
            supervisor.terminate_worker(child)
            self.assertFalse(child.is_alive())
        finally:
            if child.is_alive():
                child.kill()
                child.join()

    def test_timeout_finalizes_only_matching_prepublication_attempt_once(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(supervisor, 'ROOT', Path(temp)), patch.object(supervisor, 'Settings') as settings, patch.object(supervisor, 'FeishuNotifier'), patch.object(supervisor, 'alert_scheduled_stop', return_value=True) as alert:
            path = Path(temp)/'data/scheduled-runs/quark-bilibili-35199508-20261017-1040.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(account='bilibili-35199508',stage='preflight',execution_id='ours')))
            path.with_suffix('.lock').write_text('ours')
            result = supervisor.finish_timeout('2026-10-17T10:40:00+08:00','bilibili-35199508','ours')
            self.assertEqual(result['stage'],'stopped')
            self.assertEqual(json.loads(path.read_text())['comments_submitted'],0)
            self.assertFalse(path.with_suffix('.lock').exists())
            supervisor.finish_timeout('2026-10-17T10:40:00+08:00','bilibili-35199508','ours')
            alert.assert_called_once()

    def test_timeout_never_overwrites_publishing_or_other_attempt(self):
        for fields in [dict(stage='publishing',execution_id='ours',record_baseline=10), dict(stage='preflight',execution_id='other')]:
            with tempfile.TemporaryDirectory() as temp, patch.object(supervisor,'ROOT',Path(temp)), patch.object(supervisor,'alert_scheduled_stop') as alert:
                path=Path(temp)/'data/scheduled-runs/quark-bilibili-35199508-20261017-1040.json'
                path.parent.mkdir(parents=True)
                original=json.dumps(fields)
                path.write_text(original)
                result=supervisor.finish_timeout('2026-10-17T10:40:00+08:00','bilibili-35199508','ours')
                self.assertEqual(result['stage'],'failed')
                self.assertEqual(path.read_text(),original)
                alert.assert_not_called()

    def test_supervisor_only_starts_prepare_and_terminates_before_finalizing(self):
        context=Mock(); receiver=Mock(); sender=Mock(); process=Mock()
        context.Pipe.return_value=(receiver,sender);context.Process.return_value=process
        process.is_alive.return_value=False
        receiver.poll.return_value=False
        sequence=[]
        with patch.object(supervisor.multiprocessing,'get_context',return_value=context), patch.object(supervisor,'terminate_worker',side_effect=lambda p:sequence.append('terminate')), patch.object(supervisor,'finish_timeout',side_effect=lambda *a:sequence.append('finalize') or {'stage':'stopped'}):
            supervisor.supervise_prepare('slot','account',timeout=1)
        self.assertEqual(sequence,['terminate','finalize'])
        self.assertIs(context.Process.call_args.kwargs['target'],supervisor._worker)
