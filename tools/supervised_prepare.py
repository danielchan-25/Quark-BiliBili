"""Hard deadline for PREPARE only. Never runs or restarts publication."""
import contextlib
import io
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

from mediaflow.config import ROOT, Settings
from mediaflow.browser_lease import BrowserLease, BrowserBusy
from mediaflow.notify import FeishuNotifier
from mediaflow.scheduled_notifications import BEIJING, alert_scheduled_stop
from mediaflow.audit import write_audit_event


def _worker(connection, slot, account, resume, execution_id):
    try:
        # This session contains only this preparation and drivers it starts.
        # CDP attaches to externally managed Chrome; it never launches Chrome.
        group_id = None
        if os.name == 'posix':
            os.setsid()
            group_id = os.getpgrp()
        connection.send(dict(kind='prepare_worker_ready', pid=os.getpid(), group_id=group_id))
        # Do not start business work until the parent knows the group's identity.
        # If the parent disappears here, EOF leaves no Playwright descendants.
        if connection.recv() != {'start': execution_id}:
            raise RuntimeError('Preparation startup acknowledgement mismatch')
        from tools.scheduled_promotion import run
        with contextlib.redirect_stdout(io.StringIO()):
            result = run(slot, 'prepare', resume=resume, account=account, execution_id=execution_id)
        connection.send(result)
    except BaseException as exc:
        connection.send(dict(stage='failed', reason='prepare_worker_failed', error_type=type(exc).__name__))
    finally:
        connection.close()


def _owned_group(process):
    if os.name != 'posix':
        return None
    group_id = getattr(process, '_mediaflow_group_id', None)
    if group_id is not None:
        if type(group_id) is not int or group_id != process.pid or group_id <= 1 or group_id == os.getpgrp():
            raise RuntimeError('Refusing to signal an unverified preparation group')
        return group_id
    # Startup may time out before the handshake. A live setsid leader is safe
    # to stop, but a child still in the supervisor's group must be stopped alone.
    if process.is_alive():
        try:
            if os.getpgid(process.pid) == process.pid and os.getsid(process.pid) == process.pid:
                return process.pid
        except ProcessLookupError:
            pass
    return None


def _group_running(group_id):
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    if not sys.platform.startswith('linux'):
        return True
    # A dead orphan can await reaping by PID 1. Such a zombie cannot execute
    # work and should not be mistaken for a surviving Playwright driver.
    unreadable = False
    try:
        entries = os.scandir('/proc')
    except OSError:
        return True
    with entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                with open(entry.path + '/stat', encoding='utf-8') as handle:
                    fields = handle.read().rsplit(')', 1)[1].split()
            except PermissionError:
                unreadable = True
                continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, IndexError):
                unreadable = True
                continue
            if len(fields) >= 4 and fields[2] == str(group_id) and fields[0] not in ('Z', 'X'):
                return True
    return unreadable


def _wait_group(process, group_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        process.join(timeout=0)
        if not _group_running(group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0, deadline-time.monotonic())))


def _signal_group(group_id, sig):
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        pass


def terminate_worker(process, *, grace_seconds=5, kill_seconds=5):
    """Stop only this preparation's verified session, including its drivers."""
    group_id = _owned_group(process)
    if group_id is not None:
        _signal_group(group_id, signal.SIGTERM)
        if not _wait_group(process, group_id, grace_seconds):
            _signal_group(group_id, signal.SIGKILL)
            if not _wait_group(process, group_id, kill_seconds):
                raise RuntimeError('Preparation process group termination could not be confirmed')
    elif os.name == 'nt' and process.is_alive():
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    elif process.is_alive():
        process.terminate()
    process.join(timeout=10)
    if process.is_alive():
        raise RuntimeError('Preparation worker termination could not be confirmed')
    process._mediaflow_group_id = None


def _confirm_worker_group(process, message):
    if message.get('pid') != process.pid:
        raise RuntimeError('Preparation worker PID mismatch')
    group_id = message.get('group_id')
    if os.name == 'posix':
        if (type(group_id) is not int or group_id <= 1 or group_id != process.pid
                or group_id == os.getpgrp() or os.getpgid(process.pid) != group_id
                or os.getsid(process.pid) != group_id):
            raise RuntimeError('Preparation worker did not enter its own session')
        process._mediaflow_group_id = group_id
    elif group_id is not None:
        raise RuntimeError('Unexpected preparation process group')


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
    receiver, sender = context.Pipe(duplex=True)
    process = context.Process(target=_worker, args=(sender, slot, account, resume, execution_id))
    process._mediaflow_group_id = None
    deadline = time.monotonic() + timeout
    process.start()
    sender.close()
    started = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                terminate_worker(process)
                return finish_timeout(slot, account, execution_id)
            try:
                result = receiver.recv()
            except EOFError:
                result = dict(stage='failed', reason='prepare_worker_exited_without_result')
            if isinstance(result, dict) and result.get('kind') == 'prepare_worker_ready':
                try:
                    if started:
                        raise RuntimeError('Duplicate preparation startup')
                    _confirm_worker_group(process, result)
                except (RuntimeError, OSError):
                    terminate_worker(process)
                    return dict(stage='failed', reason='prepare_worker_startup_invalid')
                if time.monotonic() >= deadline:
                    terminate_worker(process)
                    return finish_timeout(slot, account, execution_id)
                try:
                    receiver.send({'start': execution_id})
                except (OSError, EOFError):
                    terminate_worker(process)
                    return dict(stage='failed', reason='prepare_worker_exited_before_start')
                started = True
                continue
            if not isinstance(result, dict) or (not started and result.get('stage') != 'failed'):
                result = dict(stage='failed', reason='prepare_worker_invalid_result')
            process.join(timeout=10)
            # A worker can exit before a stuck driver. Keep the acknowledged
            # group identity until all remaining preparation descendants stop.
            terminate_worker(process)
            return result
    finally:
        receiver.close()
        if process.is_alive() or type(getattr(process, '_mediaflow_group_id', None)) is int:
            terminate_worker(process)
