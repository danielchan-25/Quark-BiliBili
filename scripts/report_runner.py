"""Optional report-only runner. The default previews; --send sends once.

This runner never prepares candidates, generates copy, or publishes comments.
For delayed execution, the default date is the last due Beijing 23:55 trigger.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BEIJING = timezone(timedelta(hours=8))


def report_date(now: datetime | None = None) -> str:
    current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    day = current.date()
    if current.time().replace(tzinfo=None) < time(23, 55):
        day -= timedelta(days=1)
    return day.isoformat()


def report_command(*, send: bool, day: str | None = None, root: Path = ROOT) -> list[str]:
    """Require an actual local schedule; demonstration accounts are never used."""
    root = root.resolve()
    configured = Path(os.getenv('MEDIAFLOW_SCHEDULE_FILE') or root / 'data/schedules.toml')
    if not configured.is_absolute():
        configured = root / configured
    if not configured.is_file():
        raise ValueError('A local schedules.toml is required; configure accounts before reports')
    from mediaflow.promotion_schedule import require_local_schedule
    _, hours = require_local_schedule()
    accounts = tuple(hours)
    if not accounts:
        raise ValueError('At least one configured report account is required')
    selected_day = day or report_date()
    parsed = datetime.strptime(selected_day, '%Y-%m-%d').date()
    if parsed.isoformat() != selected_day:
        raise ValueError('Report date must be YYYY-MM-DD')
    command = [sys.executable, str(root / 'tools/daily_report.py'), '--accounts', *accounts,
               '--date', selected_day]
    if not send:
        command.append('--preview')
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--send', action='store_true', help='Explicitly send the report once')
    mode.add_argument('--preview', action='store_true', help='Preview only (the default)')
    parser.add_argument('--date', help='YYYY-MM-DD; default is the most recent due Beijing 23:55')
    args = parser.parse_args(argv)
    try:
        command = report_command(send=args.send, day=args.date)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    # Unknown notification outcomes are retained by daily_report; never retry here.
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
