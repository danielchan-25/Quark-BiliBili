"""Back up/verify/restore PRIVATE state offline; no publish or notification calls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mediaflow.migration import MigrationError, backup, restore, update_account_endpoint, verify


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ + ' Bundles contain secrets and must stay outside Git repositories.')
    commands = parser.add_subparsers(dest='command')
    command = commands.add_parser('backup', help='Create a new restricted PRIVATE bundle directory')
    command.add_argument('--source-root', type=Path, default=ROOT)
    command.add_argument('--destination', type=Path, required=True)
    command.add_argument('--confirm-stopped', action='store_true', help='I have stopped all source state writers')
    command.add_argument('--include-cache', action='store_true')
    command.add_argument('--include-logs', action='store_true')
    command = commands.add_parser('verify', help='Read-only integrity verification of a PRIVATE bundle')
    command.add_argument('--bundle', type=Path, required=True)
    command.add_argument('--manifest-sha256', help='Manifest digest retained separately during transfer')
    command = commands.add_parser('restore', help='Restore only into a target without existing business state')
    command.add_argument('--bundle', type=Path, required=True)
    command.add_argument('--target-root', type=Path, default=ROOT)
    command.add_argument('--manifest-sha256')
    command.add_argument('--confirm-stopped', action='store_true', help='I have stopped all target state writers')
    command = commands.add_parser('account-endpoint', help='Synchronize one loopback CDP address in CSV and DB')
    command.add_argument('--root', type=Path, default=ROOT)
    command.add_argument('--account', required=True)
    command.add_argument('--cdp-url', required=True)
    command.add_argument('--confirm-stopped', action='store_true', help='I have stopped all state writers')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == 'backup':
            result = backup(args.source_root, args.destination, confirmed_stopped=args.confirm_stopped,
                            include_cache=args.include_cache, include_logs=args.include_logs)
        elif args.command == 'verify':
            result = verify(args.bundle, expected_manifest_hash=args.manifest_sha256)
        elif args.command == 'restore':
            result = restore(args.bundle, args.target_root, confirmed_stopped=args.confirm_stopped,
                             expected_manifest_hash=args.manifest_sha256)
        else:
            result = update_account_endpoint(args.root, args.account, args.cdp_url,
                                             confirmed_stopped=args.confirm_stopped)
    except Exception as exc:
        # Raw parser, SQLite and filesystem exceptions can contain private paths.
        print(json.dumps(dict(status='failed', error=exc.code if isinstance(exc, MigrationError)
                              else 'operation_failed', error_type=type(exc).__name__)))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    raise SystemExit(main())
