"""Check tracked publication candidates without printing secret contents."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DATA = {'data/.gitkeep', 'data/account.example.csv'}
SECRET_PATTERNS = (
    re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{40,}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
)


def inspect_paths(root: Path, paths: list[str]) -> list[dict]:
    errors = []
    for relative in paths:
        parts = PurePosixPath(relative).parts
        path = root/relative
        private = (not parts or '..' in parts or relative.startswith('/')
                   or parts[0] in ('logs', 'outputs', 'work', 'backups', '.venv', 'venv')
                   or (parts[0] == 'data' and relative not in ALLOWED_DATA)
                   or (path.name.startswith('.env') and path.name != '.env.example')
                   or relative == 'config/schedules.toml'
                   or path.suffix in ('.db', '.sqlite', '.sqlite3', '.zip', '.tar', '.gz'))
        if private or path.is_symlink():
            errors.append(dict(path=relative, reason='private_or_unsafe_file'))
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            errors.append(dict(path=relative, reason='unexpected_non_text_file'))
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            errors.append(dict(path=relative, reason='credential_pattern'))
        if relative == '.env.example':
            if any(line.strip() and not line.lstrip().startswith('#') and '=' in line
                   and line.split('=', 1)[1].strip()
                   and line.split('=', 1)[0].strip() not in {'REVIEW_WAIT_MIN_SECONDS', 'REVIEW_WAIT_MAX_SECONDS'}
                   for line in text.splitlines()):
                errors.append(dict(path=relative, reason='configured_environment_example'))
    return errors


def main() -> int:
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True)
    paths = [path for path in result.stdout.decode('utf-8').split('\0') if path]
    errors = inspect_paths(ROOT, paths)
    print(json.dumps(dict(ok=bool(paths) and not errors, checked_files=len(paths), violations=errors), ensure_ascii=False))
    return 0 if paths and not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
