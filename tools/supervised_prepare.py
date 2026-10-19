"""Hard deadline for PREPARE only. Never runs or restarts publication."""
import contextlib
import io
import json
import multiprocessing
import os
import subprocess
from datetime import datetime
from uuid import uuid4

from mediaflow.config import ROOT, Settings
from mediaflow.browser_lease import BrowserLease, BrowserBusy
from mediaflow.notify import FeishuNotifier
from mediaflow.scheduled_notifications import BEIJING, alert_scheduled_stop
from mediaflow.audit import write_audit_event


def _worker(connection, slot, account, resume, execution_id):
    try:
        from tools.scheduled_promotion import run
        with contextlib.redirect_stdout(io.StringIO()):
            result = run(slot, 'prepare', resume=resume, account=account, execution_id=execution_id)
        connection.send(result)
    except BaseException as exc:
        connection.send(dict(stage='failed', reason='prepare_worker_failed', error_type=type(exc).__name__))
    finally:
        connection.close()


def terminate_worker(process):
    # Playwright's driver is a child; the externally managed CDP Chrome is not.
    if os.name == 'nt' and process.is_alive():
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    elif process.is_alive():
        process.terminate()
    process.join(timeout=10)
    if process.is_alive():
        raise RuntimeError('Preparation worker termination could not be confirmed')


def finish_timeout(slot, account, execution_id):
    try:
        with BrowserLease('round:' + account + ':' + slot, ROOT/'data/round-locks'):
            return _finish_timeout_locked(slot, account, execution_id)
    except BrowserBusy:
        return dict(stage='failed', reason='prepare_timeout_round_busy_requires_review')


def _finish_timeout_locked(slot, account, execution_id):
    stamp = datetime.fromisoformat(slot).astimezone(BEIJING)
    path = ROOT/'data/scheduled-runs'/f'quark-{account}-{stamp:%Y%m%d-%H%M}.json'
    lock = path.with_suffix('.lock')
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    # Never overwrite a different attempt or anything that entered publication.
    if (state.get('execution_id') != execution_id or 'record_baseline' in state
            or state.get('stage') not in ('preflight', 'ready')):
        return dict(stage='failed', reason='prepare_timeout_state_requires_review')
    if lock.exists() and lock.read_text(encoding='utf-8') != execution_id:
        return dict(stage='failed', reason='prepare_timeout_lock_requires_review')
    state.update(stage='stopped', reason='preparation_hard_timeout', comments_submitted=0,
                 ended_at=datetime.now(BEIJING).isoformat())
    path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    write_audit_event(ROOT/'logs/scheduled-promotion.jsonl', 'round_state', **state)
    lock.unlink(missing_ok=True)
    settings = Settings.load()
    notifier = FeishuNotifier(settings.feishu_webhook, settings.feishu_secret)
    try:
        sent = alert_scheduled_stop(path, notifier)
        return dict(stage='stopped', reason=state['reason'], alert_sent=sent, comments_submitted=0)
    except Exception as exc:
        return dict(stage='stopped', reason=state['reason'], alert_error=type(exc).__name__, comments_submitted=0)


def supervise_prepare(slot, account, resume=False, *, timeout=1200):
    if not 0 < timeout <= 1200:
        raise ValueError('Invalid preparation deadline')
    execution_id = uuid4().hex
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, slot, account, resume, execution_id))
    process.start()
    sender.close()
    try:
        if receiver.poll(timeout):
            try:
                result = receiver.recv()
            except EOFError:
                result = dict(stage='failed', reason='prepare_worker_exited_without_result')
            process.join(timeout=10)
            if process.is_alive():
                terminate_worker(process)
            return result
        terminate_worker(process)
        return finish_timeout(slot, account, execution_id)
    finally:
        receiver.close()
        if process.is_alive():
            terminate_worker(process)
