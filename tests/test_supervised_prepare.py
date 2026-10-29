import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from tools import supervised_prepare as supervisor


def isolated_preparation_tree(connection, exit_early):
    """A local driver stand-in; never starts Chrome or connects to a platform."""
    os.setsid()
    source = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
              "print('ready',flush=True); time.sleep(60)")
    child = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'ready'
    connection.send(dict(group_id=os.getpgrp(), child_pid=child.pid))
    connection.close()
    if not exit_early:
        time.sleep(60)


def posix_api(group_id=70001, parent_group=70000):
    api = Mock(wraps=os)
    api.name = 'posix'
    api.getpid = Mock(return_value=group_id)
    api.getpgrp = Mock(return_value=parent_group)
    api.getpgid = Mock(return_value=group_id)
    api.getsid = Mock(return_value=group_id)
    api.setsid = Mock()
    return api


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
            path = Path(temp)/'data/scheduled-runs/quark-bilibili-100000001-20261017-1040.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(account='bilibili-100000001',stage='preflight',execution_id='ours')))
            path.with_suffix('.lock').write_text('ours')
            result = supervisor.finish_timeout('2026-10-17T10:40:00+08:00','bilibili-100000001','ours')
            self.assertEqual(result['stage'],'stopped')
            self.assertEqual(json.loads(path.read_text())['comments_submitted'],0)
            self.assertFalse(path.with_suffix('.lock').exists())
            supervisor.finish_timeout('2026-10-17T10:40:00+08:00','bilibili-100000001','ours')
            alert.assert_called_once()

    def test_timeout_never_overwrites_publishing_or_other_attempt(self):
        for fields in [dict(stage='publishing',execution_id='ours',record_baseline=10), dict(stage='preflight',execution_id='other')]:
            with tempfile.TemporaryDirectory() as temp, patch.object(supervisor,'ROOT',Path(temp)), patch.object(supervisor,'alert_scheduled_stop') as alert:
                path=Path(temp)/'data/scheduled-runs/quark-bilibili-100000001-20261017-1040.json'
                path.parent.mkdir(parents=True)
                original=json.dumps(fields)
                path.write_text(original)
                result=supervisor.finish_timeout('2026-10-17T10:40:00+08:00','bilibili-100000001','ours')
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

    def test_worker_waits_for_parent_acknowledgement_before_preparing(self):
        connection = Mock()
        connection.recv.return_value = {'start': 'attempt'}
        api = posix_api(parent_group=70001)
        with patch.object(supervisor, 'os', api), \
                patch('tools.scheduled_promotion.run', return_value={'stage': 'ready'}) as prepare:
            supervisor._worker(connection, 'slot', 'account', False, 'attempt')
        api.setsid.assert_called_once()
        self.assertEqual(connection.send.call_args_list[0],
                         call(dict(kind='prepare_worker_ready', pid=70001, group_id=70001)))
        connection.recv.assert_called_once()
        prepare.assert_called_once_with('slot', 'prepare', resume=False, account='account', execution_id='attempt')
        self.assertEqual(connection.send.call_args_list[-1], call({'stage': 'ready'}))

    def test_wrong_parent_acknowledgement_never_starts_business_work(self):
        connection = Mock()
        connection.recv.return_value = {'start': 'other-attempt'}
        with patch.object(supervisor, 'os', posix_api(parent_group=70001)), \
                patch('tools.scheduled_promotion.run') as prepare:
            supervisor._worker(connection, 'slot', 'account', False, 'attempt')
        prepare.assert_not_called()
        self.assertEqual(connection.send.call_args_list[-1].args[0]['stage'], 'failed')

    def test_parent_handshake_does_not_restart_total_timeout(self):
        context = Mock()
        receiver, sender, process = Mock(), Mock(), Mock()
        context.Pipe.return_value = (receiver, sender)
        context.Process.return_value = process
        process.pid = 70001
        process.is_alive.return_value = False
        receiver.poll.side_effect = [True, False]
        receiver.recv.return_value = dict(kind='prepare_worker_ready', pid=70001, group_id=None)
        with patch.object(supervisor.multiprocessing, 'get_context', return_value=context), \
                patch.object(supervisor, '_confirm_worker_group'), \
                patch.object(supervisor, 'terminate_worker') as stop, \
                patch.object(supervisor, 'finish_timeout', return_value={'stage': 'stopped'}) as finish, \
                patch.object(supervisor.time, 'monotonic', side_effect=[100, 100, 100.6, 100.9]):
            result = supervisor.supervise_prepare('slot', 'account', timeout=1)
        self.assertEqual(result['stage'], 'stopped')
        context.Pipe.assert_called_once_with(duplex=True)
        self.assertAlmostEqual(receiver.poll.call_args_list[0].args[0], 1)
        self.assertAlmostEqual(receiver.poll.call_args_list[1].args[0], 0.1)
        execution_id = context.Process.call_args.kwargs['args'][-1]
        receiver.send.assert_called_once_with({'start': execution_id})
        stop.assert_called_once_with(process)
        finish.assert_called_once_with('slot', 'account', execution_id)

    def test_parent_cleans_acknowledged_group_even_after_worker_exits(self):
        context = Mock()
        receiver, sender, process = Mock(), Mock(), Mock()
        context.Pipe.return_value = (receiver, sender)
        context.Process.return_value = process
        process.pid = 70001
        process.is_alive.return_value = False
        receiver.poll.return_value = True
        receiver.recv.side_effect = [dict(kind='prepare_worker_ready', pid=70001, group_id=None),
                                     {'stage': 'ready'}]
        with patch.object(supervisor.multiprocessing, 'get_context', return_value=context), \
                patch.object(supervisor, '_confirm_worker_group'), \
                patch.object(supervisor, 'terminate_worker') as stop, \
                patch.object(supervisor, 'finish_timeout') as finish:
            self.assertEqual(supervisor.supervise_prepare('slot', 'account'), {'stage': 'ready'})
        stop.assert_called_once_with(process)
        finish.assert_not_called()

    def test_group_cleanup_escalates_and_never_signals_the_supervisor_group(self):
        process = Mock()
        process.pid = 70001
        process._mediaflow_group_id = 70001
        process.is_alive.return_value = False
        signals = SimpleNamespace(SIGTERM=15, SIGKILL=9)
        with patch.object(supervisor, 'os', posix_api()), patch.object(supervisor, 'signal', signals), \
                patch.object(supervisor, '_signal_group') as send, \
                patch.object(supervisor, '_wait_group', side_effect=[False, True]):
            supervisor.terminate_worker(process)
        self.assertEqual(send.call_args_list, [call(70001, 15), call(70001, 9)])
        process.terminate.assert_not_called()
        self.assertIsNone(process._mediaflow_group_id)
        process._mediaflow_group_id = 70000
        with patch.object(supervisor, 'os', posix_api()), \
                patch.object(supervisor, '_signal_group') as send:
            with self.assertRaisesRegex(RuntimeError, 'unverified'):
                supervisor.terminate_worker(process)
        send.assert_not_called()

    def test_unconfirmed_group_cleanup_cannot_be_reported_as_success(self):
        process = Mock()
        process.pid = process._mediaflow_group_id = 70001
        with patch.object(supervisor, 'os', posix_api()), \
                patch.object(supervisor, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9)), \
                patch.object(supervisor, '_signal_group'), \
                patch.object(supervisor, '_wait_group', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'group termination'):
                supervisor.terminate_worker(process)
        self.assertEqual(process._mediaflow_group_id, 70001)

    def test_timeout_before_setsid_stops_only_the_worker(self):
        process = Mock()
        process.pid = 70001
        process._mediaflow_group_id = None
        process.is_alive.side_effect = [True, True, False]
        api = posix_api()
        api.getpgid.return_value = api.getsid.return_value = 70000
        with patch.object(supervisor, 'os', api), \
                patch.object(supervisor, '_signal_group') as send:
            supervisor.terminate_worker(process)
        process.terminate.assert_called_once()
        send.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Requires Linux process groups and /proc')
    def test_linux_cleans_descendants_and_preserves_an_external_browser_standin(self):
        self.exercise_linux_tree(exit_early=False)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Requires Linux process groups and /proc')
    def test_linux_cleans_a_driver_left_after_its_worker_exits(self):
        self.exercise_linux_tree(exit_early=True)

    def exercise_linux_tree(self, *, exit_early):
        context = supervisor.multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        worker = context.Process(target=isolated_preparation_tree, args=(sender, exit_early))
        external = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                    start_new_session=True)
        worker.start()
        sender.close()
        group_id = None
        try:
            self.assertTrue(receiver.poll(10), 'Isolated preparation did not start')
            identity = receiver.recv()
            group_id = identity['group_id']
            self.assertEqual(group_id, worker.pid)
            worker._mediaflow_group_id = group_id
            self.assertTrue(supervisor._group_running(group_id))
            if exit_early:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
            supervisor.terminate_worker(worker, grace_seconds=0.2, kill_seconds=5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(supervisor._group_running(group_id))
            self.assertIsNone(external.poll(), 'Unrelated browser stand-in must remain running')
        finally:
            receiver.close()
            if group_id is not None:
                supervisor._signal_group(group_id, signal.SIGKILL)
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=10)
            external.kill()
            external.wait(timeout=10)
