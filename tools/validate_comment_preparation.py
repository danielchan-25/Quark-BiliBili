"""Live, read-only checks of candidate fallback and prepared-page reuse."""
import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as workflow
from mediaflow.bilibili import BilibiliClient, _recheck_prepared_comment_editor, _comment_page_state
from mediaflow.config import Settings
from mediaflow.notify import FeishuNotifier
from playwright.sync_api import Keyboard

ACCOUNTS = ('bilibili-35199508', 'bilibili-1085803742')
BEIJING = timezone(timedelta(hours=8))


def snapshot(connection):
    """Compare business rows without exposing their values in a report."""
    tables = ('accounts', 'promotion_resources', 'promotion_candidates', 'phrases',
              'promotion_records', 'promotion_reservations', 'task_runs', 'interactions')
    rows = {}
    for table in tables:
        values = [list(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
        rows[table] = dict(count=len(values), digest=hashlib.sha256(
            json.dumps(values, ensure_ascii=False, default=str).encode()).hexdigest())
    return rows


def validate(accounts):
    folder = ROOT/'logs'/('comment-preparation-validation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    folder.mkdir(parents=True, exist_ok=True)
    report = dict(mode='read_only', started_at=datetime.now(BEIJING).isoformat(), accounts=[])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(folder)))
        guards = [stack.enter_context(patch.object(cls, method, side_effect=AssertionError('Blocked during validation')))
                  for cls, method in ((BilibiliClient, 'publish_comment'), (BilibiliClient, 'watch_and_like'),
                                      (FeishuNotifier, 'send_text'), (Keyboard, 'type'))]
        settings = Settings.load()
        connection = sqlite3.connect(settings.database_path.resolve().as_uri()+'?mode=ro', uri=True)
        stack.callback(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        db = SimpleNamespace(connection=connection)
        before = snapshot(connection)
        resource = connection.execute('SELECT id FROM promotion_resources WHERE enabled=1 ORDER BY id LIMIT 1').fetchone()
        for account in accounts:
            result = dict(account=account, status='failed', steps=[])
            stage = 'account'
            started = time.monotonic()
            page = None
            try:
                acc = workflow.selected_account(db, account, 'promotion')
                if not resource:
                    raise ValueError('No enabled resource')
                stage = 'browser_identity'
                with BilibiliClient(acc['cdp_url']) as client:
                    actual_uid = client.uid()
                    if actual_uid != acc['uid'] or account != 'bilibili-' + actual_uid:
                        raise ValueError('Browser identity mismatch')
                    result['steps'].append(dict(step=stage, status='passed'))
                    stage = 'candidate_selection'
                    candidate = workflow.available_visible_candidate(db, client, acc['id'], resource['id'],
                                                                      mutate=False, retain_page=True)
                    if candidate is None:
                        raise ValueError('No usable candidate')
                    result['bvid'] = candidate.bvid
                    result['steps'].append(dict(step=stage, status='passed'))
                    stage = 'prepared_page_reuse'
                    page = client.take_prepared_comment(candidate.url)
                    if page is None:
                        raise ValueError('Prepared page could not be reused')
                    try:
                        _recheck_prepared_comment_editor(page)
                        result['editor_state'] = _comment_page_state(page)
                        result['steps'].append(dict(step=stage, status='passed'))
                    finally:
                        page.close()
                        page = None
                    result['status'] = 'passed'
            except Exception as exc:
                result.update(error_step=stage, error_type=type(exc).__name__)
                if hasattr(exc, 'stage'):
                    result.update(error_stage=exc.stage, reason=exc.reason, attempts=exc.attempts)
            result['elapsed_ms'] = round((time.monotonic()-started)*1000)
            report['accounts'].append(result)
        after = snapshot(connection)
        report['business_data_unchanged'] = before == after
        report['blocked_write_calls'] = sum(guard.call_count for guard in guards)
    report['passed'] = (report['business_data_unchanged'] and report['blocked_write_calls'] == 0
                        and all(row['status'] == 'passed' for row in report['accounts']))
    report['ended_at'] = datetime.now(BEIJING).isoformat()
    path = folder/'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return path, report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accounts', choices=ACCOUNTS, nargs='+', default=list(ACCOUNTS))
    args = parser.parse_args()
    path, report = validate(list(dict.fromkeys(args.accounts)))
    print(json.dumps(dict(report_path=str(path), **report), ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
