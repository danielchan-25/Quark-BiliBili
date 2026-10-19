"""Observe promotion selection; default to a non-publishing preview."""
import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import available_candidate
from mediaflow.audit import audited, write_audit_event


@audited('promotion_observation')
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', action='append', required=True)
    parser.add_argument('--publish', action='store_true', help='Explicitly submit one comment per account; default is preview only')
    args = parser.parse_args()
    folder = ROOT / 'logs' / ('promotion-observation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    folder.mkdir(parents=True)
    db = sqlite3.connect((ROOT / 'data/mediaflow.db').resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        for name in args.account:
            account = db.execute('SELECT id FROM accounts WHERE name=? AND enabled=1', (name,)).fetchone()
            if not account:
                raise RuntimeError('Requested account is not registered and enabled')
            snapshot = {'account': name, 'dry_run': not args.publish, 'resources': []}
            for resource in db.execute('SELECT id FROM promotion_resources WHERE enabled=1 ORDER BY id'):
                candidate = available_candidate(SimpleNamespace(connection=db), account['id'], resource['id'])
                snapshot['resources'].append({'resource_id': resource['id'], 'bvid': candidate.bvid if candidate else None})
            command = [sys.executable, '-u', str(ROOT / 'main.py'), 'promotion', '--account', name, '--max-comments', '1']
            if not args.publish:
                command.append('--dry-run')
            # Discard captured previews: they contain private configured links.
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
            snapshot.update(exit_code=result.returncode, ended_at=datetime.now(timezone.utc).isoformat())
            (folder / f"account-{account['id']}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
            write_audit_event(ROOT/'logs/workflow.jsonl', 'observation_result', **snapshot)
            print(json.dumps(snapshot, ensure_ascii=False))
    finally:
        db.close()


if __name__ == '__main__':
    main()
