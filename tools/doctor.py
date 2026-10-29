"""Offline deployment checks. Never opens a browser, database, or notification."""
from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values


def inspect_environment(root: Path = ROOT) -> dict:
    root = Path(root).resolve()
    checks = []

    def check(name, passed, **fields):
        checks.append(dict(check=name, ok=bool(passed), **fields))

    check('python', sys.version_info >= (3, 12), version=platform.python_version())
    check('linux', sys.platform.startswith('linux'), platform=sys.platform)
    versions = {}
    for name in ('playwright', 'websocket-client', 'python-dotenv', 'requests', 'typesafe-sdk'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    check('dependencies', all(versions.values()), versions=versions)
    configured = {}
    path = root/'data/schedules.toml'
    try:
        from mediaflow.promotion_schedule import load_schedule, schedule_path
        path = schedule_path()
        # Test callers may inspect a fixture root without using the real project.
        if root != ROOT and not os.getenv('MEDIAFLOW_SCHEDULE_FILE'):
            path = root/'data/schedules.toml'
        _, configured = load_schedule(path)
        check('private_schedule', True, accounts=len(configured))
    except (OSError, ValueError) as exc:
        check('private_schedule', False, error_type=type(exc).__name__)
    try:
        with (root/'data/account.csv').open(encoding='utf-8-sig', newline='') as handle:
            rows = {row.get('账号'): row for row in csv.DictReader(handle)}
        enabled = {name for name, row in rows.items()
                   if row.get('角色') == '推广' and row.get('启用状态') == '启用'}
        check('account_csv', bool(configured) and set(configured).issubset(enabled),
              configured_accounts=len(configured), enabled_accounts=len(enabled))
    except (OSError, ValueError, csv.Error) as exc:
        check('account_csv', False, error_type=type(exc).__name__)
    env_path = root/'.env'
    values = dotenv_values(env_path) if env_path.is_file() else {}
    check('environment_file', env_path.is_file())
    # Only configuration presence is reported, never actual values or filenames.
    check('typesafe_configured', bool(os.environ.get('TYPESAFE_API_KEY') or values.get('TYPESAFE_API_KEY')))
    check('feishu_configured', bool(os.environ.get('FEISHU_WEBHOOK') or values.get('FEISHU_WEBHOOK')))
    data_path = root/'data'
    check('data_directory_writable', data_path.is_dir() and os.access(data_path, os.W_OK))
    check('runtime_database_present', (data_path/'mediaflow.db').is_file())
    if os.name == 'posix':
        private_paths = [env_path, data_path/'account.csv', path]
        check('private_config_permissions', all(not path.exists() or path.stat().st_mode & 0o077 == 0
                                               for path in private_paths))
    return dict(ok=all(check['ok'] for check in checks), checks=checks,
                scope='offline_configuration_only', browser_checked=False, publication_ready=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Output is always structured JSON')
    parser.parse_args()
    report = inspect_environment()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
