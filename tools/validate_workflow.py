"""Read-only live validation. Publishing, likes and notifications are blocked."""
import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as workflow
from mediaflow.account_csv import promotion_keywords
from mediaflow.audit import audit_step
from mediaflow.bilibili import BilibiliClient
from mediaflow.candidate_screening import screen_candidate
from mediaflow.config import Settings
from mediaflow.notify import FeishuNotifier


def preview_block_reason(log_path, start_position=0):
    """Read only new audit events and return a controlled, non-sensitive reason."""
    events = []
    if log_path.exists():
        with log_path.open('rb') as handle:
            handle.seek(start_position)
            for line in handle:
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(item, dict):
                    events.append(item)
    if any(item.get('event') == 'phrase_exhausted' for item in events):
        return 'phrase_exhausted'
    if any(item.get('event') == 'frequency_checked' and item.get('ready') is False for item in events):
        return 'frequency_limit'
    if any(item.get('event') == 'resource_skipped' and item.get('reason') == 'no_candidate' for item in events):
        return 'no_eligible_candidate'
    for item in reversed(events):
        if item.get('event') == 'promotion_result':
            if item.get('reason') == '没有未使用的留言文案':
                return 'phrase_exhausted'
            if item.get('reason') == '没有可发布的候选或文案':
                return 'no_eligible_candidate_or_phrase'
    return 'read_only_preview_not_ready'


def verification_detail(result):
    """Keep bounded API evidence, never a response body or exception message."""
    if not isinstance(result, dict) or result.get('status') not in ('verified', 'unconfirmed', 'error'):
        return {'status': 'error', 'reason': 'invalid_verification_result', 'error_type': 'ValueError'}
    return {key: result.get(key) for key in ('status', 'method', 'reason', 'api_code', 'error_type', 'source', 'scope')}


def finish_report(report, blocked_write_calls):
    by_name = {item['step']: item for item in report['steps']}
    required = {'configuration', 'identity', 'search', 'visibility', 'comment_editor', 'typesafe', 'preview'}
    report['blocked_write_calls'] = blocked_write_calls
    report['functional_checks_passed'] = (
        required.issubset(by_name)
        and not any(item['status'] == 'failed' for item in report['steps'] if item['step'] != 'existing_comment')
        and blocked_write_calls == 0
    )
    preview = by_name.get('preview', {}).get('detail', {})
    report['publication_ready'] = report['functional_checks_passed'] and preview.get('ready') is True
    historical = by_name.get('existing_comment')
    report['historical_comment_verified'] = (
        historical.get('detail', {}).get('status') == 'verified' if historical else None
    )
    report['warnings'] = [dict(step=item['step'], **item.get('detail', {}))
                          for item in report['steps'] if item['status'] == 'warning']
    report['passed'] = (report['publication_ready']
                        and not any(item['status'] == 'failed' for item in report['steps']))


def validate(account):
    folder = ROOT/'logs'/('validation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    folder.mkdir(parents=True)
    report = {'account': account, 'mode': 'read_only', 'steps': []}

    def step(name, action):
        try:
            with audit_step('validation_' + name):
                detail = action()
            status = 'passed'
            if name == 'preview' and not detail['ready']:
                status = 'blocked'
            elif name == 'existing_comment':
                status = {'verified': 'passed', 'unconfirmed': 'warning', 'error': 'failed'}[detail['status']]
            report['steps'].append(dict(step=name, status=status, detail=detail))
            return detail
        except Exception as exc:
            detail = dict(step=name, status='failed', error_type=type(exc).__name__)
            if hasattr(exc, 'stage'):
                detail.update(error_stage=exc.stage, reason=exc.reason)
            report['steps'].append(detail)
            return None

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, MEDIAFLOW_LOG_DIR=str(folder)))
        guards = [stack.enter_context(patch.object(cls, method, side_effect=AssertionError('Blocked during validation')))
                  for cls, method in ((BilibiliClient, 'publish_comment'), (BilibiliClient, 'watch_and_like'),
                                      (FeishuNotifier, 'send_text'))]
        try:
            settings = Settings.load()
            connection = sqlite3.connect(settings.database_path.resolve().as_uri()+'?mode=ro', uri=True)
            stack.callback(connection.close)
            connection.row_factory = sqlite3.Row
            db = SimpleNamespace(connection=connection)
            acc = workflow.selected_account(db, account, 'promotion')
            keywords = promotion_keywords(ROOT/'data/account.csv', account)
            step('configuration', lambda: dict(database_enabled=bool(acc['enabled']), csv_enabled=True,
                keywords=len(keywords), typesafe_configured=bool(settings.typesafe_api_key),
                feishu_configured=bool(settings.feishu_webhook)))
            with BilibiliClient(acc['cdp_url']) as client:
                def identity():
                    if client.uid() != acc['uid']:
                        raise RuntimeError('UID mismatch or not logged in')
                    return {'uid_matches': True}
                if step('identity', identity) is None:
                    return report, folder
                found = []
                def search():
                    found.extend(client.search(keywords[0], limit=1))
                    if not found:
                        raise RuntimeError('No search results')
                    return {'count': len(found)}
                step('search', search)
                if found:
                    def visibility():
                        visible = client.video_visible(found[0].bvid)
                        if not visible:
                            raise RuntimeError('Video unavailable')
                        return {'visible': True, 'bvid': found[0].bvid}
                    step('visibility', visibility)
                    step('comment_editor', lambda: client.check_comment_editor(found[0].url))
                    resource = connection.execute('SELECT category FROM promotion_resources WHERE enabled=1 ORDER BY id LIMIT 1').fetchone()
                    if resource:
                        with tempfile.TemporaryDirectory() as tmp:
                            def screening():
                                result = screen_candidate(api_key=settings.typesafe_api_key, keyword=keywords[0],
                                    resource_category=resource['category'], title=found[0].title, author=found[0].author,
                                    cache_path=Path(tmp)/'screen.db', log_path=folder/'typesafe-usage.jsonl')
                                return {'decision': result.status}
                            step('typesafe', screening)
                record = connection.execute('''SELECT p.*,r.url AS resource_url FROM promotion_records p
                    JOIN promotion_resources r ON r.id=p.resource_id
                    WHERE p.account_id=? AND p.publish_status='published' ORDER BY p.id DESC LIMIT 1''', (acc['id'],)).fetchone()
                if record:
                    def recheck():
                        result = client.verify_comment_details(record['bvid'], acc['uid'], record['text'], record['resource_url'], record['remote_comment_id'])
                        return verification_detail(result)
                    step('existing_comment', recheck)
            def preview():
                log_path = folder/'promotion-flow.jsonl'
                start_position = log_path.stat().st_size if log_path.exists() else 0
                with contextlib.redirect_stdout(io.StringIO()):
                    code = workflow.promotion(db, None, argparse.Namespace(account=account, max_comments=1, dry_run=True, slot=None))
                detail = {'exit_code': code, 'ready': code == 0, 'business_writes': connection.total_changes}
                if code != 0:
                    detail['reason'] = preview_block_reason(log_path, start_position)
                return detail
            step('preview', preview)
        except Exception as exc:
            report['steps'].append(dict(step='setup_or_browser', status='failed', error_type=type(exc).__name__))
        finally:
            finish_report(report, sum(mock.call_count for mock in guards))
            (folder/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report, folder


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    report, folder = validate(args.account)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'Report: {folder / "report.json"}')
    raise SystemExit(0 if report['passed'] else 1)
