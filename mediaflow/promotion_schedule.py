"""Local, private Beijing schedules; public defaults are fictional examples."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from mediaflow.config import ROOT

_EXAMPLE_HOURS = {
    'bilibili-100000001': (0, 5, 10, 15, 20),
    'bilibili-100000002': (1, 6, 11, 16, 21),
}


def schedule_path() -> Path:
    """An exported override is relative to the project, never the caller's cwd."""
    value = Path(os.environ.get('MEDIAFLOW_SCHEDULE_FILE') or 'data/schedules.toml').expanduser()
    return value if value.is_absolute() else ROOT / value


def load_schedule(path: Path | None = None) -> tuple[str, dict[str, tuple[int, ...]]]:
    path = Path(path) if path is not None else schedule_path()
    with path.open('rb') as handle:
        config = tomllib.load(handle)
    if config.get('timezone') != 'Asia/Shanghai':
        raise ValueError('Schedule timezone must be Asia/Shanghai')
    if config.get('minute', 40) != 40:
        raise ValueError('The supported promotion minute is 40')
    rows = config.get('accounts')
    if not isinstance(rows, dict) or not rows:
        raise ValueError('Schedule must contain nonempty accounts')
    hours = {}
    for account, row in rows.items():
        if not re.fullmatch(r'bilibili-[1-9][0-9]*', account) or not isinstance(row, dict):
            raise ValueError('Invalid scheduled Bilibili account')
        value = row.get('hours')
        if (not isinstance(value, list) or not value
                or any(type(hour) is not int or not 0 <= hour <= 23 for hour in value)
                or len(value) != len(set(value))):
            raise ValueError('Schedule hours must be distinct integers from 0 to 23')
        hours[account] = tuple(sorted(value))
    default = config.get('default_account')
    if not isinstance(default, str) or default not in hours:
        raise ValueError('Default account must be present in accounts')
    return default, hours


def require_local_schedule() -> tuple[str, dict[str, tuple[int, ...]]]:
    """Production automation must call this, rather than use example defaults."""
    if not schedule_path().is_file():
        raise ValueError('Configure data/schedules.toml before enabling scheduled work')
    return load_schedule()


# Production installation creates the private file; scheduled commands require it.
if schedule_path().is_file() or os.environ.get('MEDIAFLOW_SCHEDULE_FILE'):
    DEFAULT_ACCOUNT, ACCOUNT_HOURS = load_schedule()
else:
    DEFAULT_ACCOUNT, ACCOUNT_HOURS = next(iter(_EXAMPLE_HOURS)), dict(_EXAMPLE_HOURS)
