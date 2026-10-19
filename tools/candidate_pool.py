"""Inspect candidate stock, optionally replenish without posting or notifying."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as workflow
from mediaflow.bilibili import BilibiliClient
from mediaflow.candidate_pool import inventory, maintain_candidate_pool
from mediaflow.config import Settings
from mediaflow.database import Database
from mediaflow.notify import FeishuNotifier


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--refill', action='store_true')
    args = parser.parse_args()
    settings = Settings.load()
    with ExitStack() as stack:
        for cls, method in ((BilibiliClient, 'publish_comment'), (BilibiliClient, 'watch_and_like'),
                            (FeishuNotifier, 'send_text')):
            stack.enter_context(patch.object(cls, method, side_effect=AssertionError('Blocked during pool maintenance')))
        if args.refill:
            db = Database(settings.database_path)
            stack.callback(db.close)
            workflow.selected_account(db, args.account, 'promotion')
            result = maintain_candidate_pool(db, args.account,
                lambda policy, target, report: workflow.replenish_promotion_candidates(db, args.account, policy, target, report),
                manual=True)
        else:
            connection = sqlite3.connect(settings.database_path.resolve().as_uri()+'?mode=ro', uri=True)
            stack.callback(connection.close)
            connection.row_factory = sqlite3.Row
            workflow.selected_account(SimpleNamespace(connection=connection), args.account, 'promotion')
            result = dict(status='read_only', stock=inventory(SimpleNamespace(connection=connection)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1 if result['status'] == 'failed' else 0)
