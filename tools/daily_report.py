"""Send the Beijing calendar day's report without touching the browser."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediaflow.daily_report import daily_report
from mediaflow.config import ROOT, Settings
from mediaflow.delivery import deliver_once
from mediaflow.notify import FeishuNotifier
from mediaflow.promotion_schedule import ACCOUNT_HOURS, DEFAULT_ACCOUNT, require_local_schedule
from uuid import uuid4


def alert_report_failure(accounts, day, error_type):
    settings = Settings.load()
    notify = FeishuNotifier(settings.feishu_webhook, settings.feishu_secret)
    return deliver_once(
        ROOT/'data/notification-receipts'/f'daily-report-failure-{uuid4().hex}.json', notify,
        f'【Quark-BiliBili】\n\n账户={"、".join(accounts)}，日报生成或发送异常：{error_type}。请检查日报与发送回执，不自动重发状态不明的日报。',
        kind='daily_report_failure', accounts=accounts, day=day)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', help='YYYY-MM-DD; defaults to today in Beijing')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--account', default=DEFAULT_ACCOUNT)
    group.add_argument('--accounts', nargs='+', choices=tuple(ACCOUNT_HOURS), help='Combine these accounts in one report')
    parser.add_argument('--preview', action='store_true', help='Build a local preview without sending notifications')
    parser.add_argument('--output', help='Save preview text to this path (requires --preview)')
    args = parser.parse_args()
    if args.output and not args.preview:
        parser.error('--output requires --preview')
    if not args.preview:
        try:
            require_local_schedule()
        except (ValueError, OSError) as exc:
            print(json.dumps(dict(status='failed', reason='schedule_configuration_required',
                                  error_type=type(exc).__name__)))
            raise SystemExit(1)
    try:
        result = daily_report(args.date, args.account, accounts=args.accounts, preview=args.preview)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(result['message']+'\n', encoding='utf-8')
            result['preview_path'] = str(output.resolve())
        problems = result.get('account_errors', [])+result.get('uncertain_accounts', [])
        if problems and not args.preview:
            try:
                result['failure_alert_sent'] = alert_report_failure(
                    [problem['account'] for problem in problems], result['date'], '部分账户统计异常或发送状态待人工确认')
            except Exception as alert:
                result['alert_error'] = type(alert).__name__
        print(json.dumps(result, ensure_ascii=False))
        if problems:
            raise SystemExit(1)
    except Exception as exc:
        result = {'status':'failed', 'error_type':type(exc).__name__}
        if args.preview:
            print(json.dumps(result))
            raise SystemExit(1)
        try:
            result['failure_alert_sent'] = alert_report_failure(args.accounts or [args.account], args.date, type(exc).__name__)
        except Exception as alert:
            result['alert_error'] = type(alert).__name__
        print(json.dumps(result))
        raise SystemExit(1)
