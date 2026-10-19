"""Prepare account-specific commentable inventory, without typing or publishing."""
import argparse
import contextlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as workflow
from mediaflow.bilibili import BilibiliClient
from mediaflow.config import Settings
from mediaflow.database import Database
from mediaflow.notify import FeishuNotifier
from mediaflow.promotion_schedule import ACCOUNT_HOURS
from playwright.sync_api import Keyboard


def prepare_stock(account, target=3, budget=330):
    if account not in ACCOUNT_HOURS or not 1 <= target <= 5 or not 0 < budget <= 330:
        raise ValueError('Invalid stock preparation arguments')
    ready = []
    started = time.monotonic()
    report = dict(account=account, mode='no_publish', ready=ready, target=target)
    with contextlib.ExitStack() as stack:
        guards = [stack.enter_context(patch.object(cls, method, side_effect=AssertionError('Publication blocked during stock preparation')))
                  for cls, method in ((BilibiliClient, 'publish_comment'), (BilibiliClient, 'watch_and_like'),
                                      (FeishuNotifier, 'send_text'), (Keyboard, 'type'))]
        db = Database(Settings.load().database_path)
        stack.callback(db.close)
        stage = 'account_configuration'
        try:
            account_row = workflow.selected_account(db, account, 'promotion')
            resource = db.connection.execute('SELECT id FROM promotion_resources WHERE enabled=1 ORDER BY id LIMIT 1').fetchone()
            if not resource:
                raise ValueError('No enabled resource')
            stage = 'browser_connection'
            with BilibiliClient(account_row['cdp_url']) as client:
                stage = 'browser_identity'
                if client.uid() != account.removeprefix('bilibili-'):
                    raise ValueError('Browser identity mismatch')
                stage = 'candidate_selection'
                while len(ready) < target:
                    remaining = budget - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    candidate = workflow.available_visible_candidate(db, client, account_row['id'], resource['id'],
                        initial_excluded=tuple(ready), budget_seconds=remaining)
                    if candidate is None:
                        break
                    ready.append(candidate.bvid)
            report['status'] = 'ready' if len(ready) == target else 'partial'
        except Exception as exc:
            report.update(status='partial' if ready else 'failed', error_type=type(exc).__name__, error_step=stage)
            if hasattr(exc, 'stage'):
                report.update(error_stage=exc.stage, reason=exc.reason)
        report['blocked_write_calls'] = sum(g.call_count for g in guards)
    report['elapsed_seconds'] = round(time.monotonic()-started, 2)
    folder = ROOT/'logs/comment-stock'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/f'{account}-{time.time_ns()}.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['report_path'] = str(path)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, choices=tuple(ACCOUNT_HOURS))
    parser.add_argument('--target', type=int, default=3)
    args = parser.parse_args()
    result = prepare_stock(args.account, args.target)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'ready' else 1)
