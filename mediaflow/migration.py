"""Offline migration of PRIVATE local state; never connects to a platform.

A bundle is a private directory, not a ZIP file. Its manifest contains only
relative allowlisted paths and integrity metadata. Keep it outside repositories.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = frozenset({'.env', 'data/account.csv', 'data/mediaflow.db'})
LOCAL_CONFIG = frozenset({'data/schedules.toml'})
STATE_DIRS = ('data/scheduled-runs', 'data/notification-receipts',
              'data/daily-reports', 'data/promotion-schedules')
OPTIONAL = frozenset({'data/typesafe-cache.db', 'data/candidate-pool/maintenance.json'})
MAX_FILES = 100_000
MAX_BYTES = 4 * 1024 ** 3
MANIFEST_LIMIT = 10 * 1024 ** 2
WINDOWS = os.name == 'nt'


class MigrationError(RuntimeError):
    """A safe error code. Never attach file contents, URLs or SQLite errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require_stopped(confirmed: bool) -> None:
    if confirmed is not True:
        raise MigrationError('stopped_confirmation_required')


def _path(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if '..' in candidate.parts:
        raise MigrationError('path_traversal_rejected')
    candidate = Path(os.path.abspath(candidate))
    _reject_links(candidate)
    return candidate


def _reject_links(path: Path) -> None:
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0) & 0x400):
            raise MigrationError('symbolic_link_rejected')


def _regular(path: Path) -> None:
    _reject_links(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise MigrationError('required_file_missing') from None
    if not stat.S_ISREG(info.st_mode):
        raise MigrationError('non_regular_file_rejected')


@lru_cache(maxsize=1)
def _current_sid() -> str:
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
                            capture_output=True, check=True, timeout=10)
    match = re.search(r'S-1-\d+(?:-\d+)+', result.stdout.decode('ascii', errors='ignore'))
    if not match:
        raise MigrationError('private_permissions_unavailable')
    return match.group()


def _windows_private(path: Path) -> None:
    """Read the DACL as SDDL so localized account names are irrelevant."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    get_security = advapi.GetNamedSecurityInfoW
    get_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                            ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    get_security.restype = wintypes.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [pointer, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    descriptor = pointer()
    dacl = pointer()
    text = wintypes.LPWSTR()
    try:
        if get_security(str(path), 1, 5, None, None, ctypes.byref(dacl), None,
                        ctypes.byref(descriptor)) or not dacl.value:
            raise MigrationError('private_permissions_required')
        if not convert(descriptor, 1, 5, ctypes.byref(text), None):
            raise MigrationError('private_permissions_required')
        allowed = {_current_sid(), 'SY', 'BA', 'S-1-5-18', 'S-1-5-32-544'}
        owner = text.value.split('D:', 1)[0]
        if not owner.startswith('O:') or owner[2:] not in allowed:
            raise MigrationError('private_permissions_required')
        # Python 3.13+ creates mode-0700 directories with an OWNER RIGHTS ACE.
        allowed.update({'OW', 'S-1-3-4'})
        aces = [ace.split(';') for ace in re.findall(r'\(([^()]*)\)', text.value)]
        grants = [ace for ace in aces if ace and ace[0] in ('A', 'OA')]
        if not grants or any(len(ace) != 6 or ace[-1] not in allowed for ace in grants):
            raise MigrationError('private_permissions_required')
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, pointer))
        if descriptor:
            kernel.LocalFree(descriptor)


def _assert_private(path: Path) -> None:
    _reject_links(path)
    if WINDOWS:
        _windows_private(path)
    else:
        info = path.stat()
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise MigrationError('private_permissions_required')


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    try:
        if WINDOWS:
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                            '*' + _current_sid() + ':(OI)(CI)F'],
                           capture_output=True, check=True, timeout=10)
        else:
            path.chmod(0o700)
        _assert_private(path)
    except BaseException:
        path.rmdir()
        raise


def _outside_repository(path: Path, source: Path | None = None) -> None:
    for root in (ROOT, source):
        if root is not None and path.is_relative_to(root):
            raise MigrationError('bundle_must_be_outside_repository')
    if any((ancestor / '.git').exists() for ancestor in (path.parent, *path.parents)):
        raise MigrationError('bundle_must_be_outside_repository')


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise MigrationError('manifest_path_rejected')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in ('', '.', '..') for p in value.split('/')):
        raise MigrationError('manifest_path_rejected')
    allowed = value in REQUIRED or value in OPTIONAL or value in LOCAL_CONFIG
    allowed |= any(value.startswith(folder + '/') and value.endswith('.json') for folder in STATE_DIRS)
    allowed |= value.startswith('logs/') and value.endswith('.jsonl')
    if not allowed:
        raise MigrationError('manifest_path_not_allowlisted')
    return value


def _walk(path: Path):
    _reject_links(path)
    if not path.exists():
        return
    if not path.is_dir():
        raise MigrationError('state_directory_required')
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            _reject_links(Path(base) / name)
        for name in files:
            candidate = Path(base) / name
            _regular(candidate)
            yield candidate


def _selected(root: Path, include_cache: bool, include_logs: bool, *, require: bool):
    files = set(REQUIRED)
    for value in LOCAL_CONFIG:
        _reject_links(root / value)
        if (root / value).exists():
            files.add(value)
    for folder in STATE_DIRS:
        files.update(p.relative_to(root).as_posix() for p in _walk(root / folder)
                     if p.suffix == '.json')
    if include_cache:
        for value in OPTIONAL:
            _reject_links(root / value)
            if (root / value).exists():
                files.add(value)
    if include_logs:
        files.update(p.relative_to(root).as_posix() for p in _walk(root / 'logs')
                     if p.suffix == '.jsonl')
    result = []
    for relative in sorted(files):
        path = root / _relative(relative)
        _reject_links(path)
        if not path.exists():
            if require and relative in REQUIRED:
                raise MigrationError('required_file_missing')
            continue
        _regular(path)
        result.append(relative)
    return result


def _digest(path: Path) -> tuple[int, str]:
    _regular(path)
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MigrationError('non_regular_file_rejected')
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            size += len(block)
            if size > MAX_BYTES:
                raise MigrationError('bundle_size_limit')
            digest.update(block)
    return size, digest.hexdigest()


def _copy(source: Path, target: Path) -> None:
    _regular(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(source, flags), 'rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    target.chmod(0o600)


def _sqlite_summary(path: Path) -> dict[str, int]:
    _regular(path)
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise MigrationError('sqlite_integrity_failed')
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: connection.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for name in names}
    finally:
        connection.close()


def _sqlite_snapshot(source: Path, target: Path) -> None:
    _regular(source)
    for suffix in ('-wal', '-shm'):
        _reject_links(Path(str(source) + suffix))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        # The transferred snapshot must be standalone, with no WAL sidecars.
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()
    target.chmod(0o600)


def _safe_remove(path: Path, parent: Path) -> None:
    if path.parent != parent or not path.is_relative_to(parent):
        raise MigrationError('cleanup_path_rejected')
    _reject_links(path)
    shutil.rmtree(path)


def backup(source_root: str | Path, destination: str | Path, *, confirmed_stopped=False,
           include_cache=False, include_logs=False) -> dict:
    """Create a new PRIVATE bundle outside repositories after explicit quiescence."""
    _require_stopped(confirmed_stopped)
    source, destination = _path(source_root), _path(destination)
    if not source.is_dir() or not destination.parent.is_dir():
        raise MigrationError('existing_source_and_destination_parent_required')
    _outside_repository(destination, source)
    if destination.exists():
        raise MigrationError('destination_already_exists')
    selected = _selected(source, include_cache, include_logs, require=True)
    if len(selected) > MAX_FILES:
        raise MigrationError('bundle_file_limit')
    _private_directory(destination)
    try:
        entries, total = [], 0
        for relative in selected:
            target = destination / 'files' / relative
            database = relative.endswith('.db')
            if database:
                _sqlite_snapshot(source / relative, target)
            else:
                _copy(source / relative, target)
            size, digest = _digest(target)
            total += size
            if total > MAX_BYTES:
                raise MigrationError('bundle_size_limit')
            entry = dict(path=relative, size=size, sha256=digest, kind='sqlite' if database else 'file')
            if database:
                entry['table_counts'] = _sqlite_summary(target)
            entries.append(entry)
        manifest = dict(format='mediaflow-private-state', version=1, private=True,
                        created_at=datetime.now(timezone.utc).isoformat(), files=entries)
        encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
        (destination / 'manifest.json').write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        (destination / 'manifest.sha256').write_text(digest + '\n', encoding='ascii')
        for name in ('manifest.json', 'manifest.sha256'):
            (destination / name).chmod(0o600)
        verify(destination, expected_manifest_hash=digest)
        return dict(status='backed_up', private=True, files=len(entries), bytes=total,
                    manifest_sha256=digest)
    except BaseException:
        _safe_remove(destination, destination.parent)
        raise


def _verified_manifest(bundle: Path, expected_manifest_hash: str | None = None) -> tuple[dict, str]:
    _assert_private(bundle)
    manifest_path, checksum_path = bundle / 'manifest.json', bundle / 'manifest.sha256'
    _regular(manifest_path)
    _regular(checksum_path)
    if manifest_path.stat().st_size > MANIFEST_LIMIT or checksum_path.stat().st_size > 100:
        raise MigrationError('invalid_manifest')
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if checksum_path.read_text(encoding='ascii').strip() != digest or (
            expected_manifest_hash is not None and expected_manifest_hash != digest):
        raise MigrationError('manifest_hash_mismatch')
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise MigrationError('invalid_manifest') from None
    if (not isinstance(manifest, dict) or manifest.get('format') != 'mediaflow-private-state'
            or type(manifest.get('version')) is not int or manifest['version'] != 1
            or manifest.get('private') is not True or not isinstance(manifest.get('files'), list)
            or not 3 <= len(manifest['files']) <= MAX_FILES):
        raise MigrationError('invalid_manifest')
    paths, total = set(), 0
    for entry in manifest['files']:
        if not isinstance(entry, dict):
            raise MigrationError('invalid_manifest')
        relative = _relative(entry.get('path'))
        if (relative in paths or type(entry.get('size')) is not int or entry['size'] < 0
                or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                or entry.get('kind') != ('sqlite' if relative.endswith('.db') else 'file')):
            raise MigrationError('invalid_manifest')
        paths.add(relative)
        total += entry['size']
        if total > MAX_BYTES:
            raise MigrationError('bundle_size_limit')
        path = bundle / 'files' / relative
        if _digest(path) != (entry['size'], entry['sha256']):
            raise MigrationError('file_hash_mismatch')
        if relative.endswith('.db') and _sqlite_summary(path) != entry.get('table_counts'):
            raise MigrationError('sqlite_metadata_mismatch')
    if not REQUIRED.issubset(paths):
        raise MigrationError('required_file_missing')
    actual = {p.relative_to(bundle).as_posix() for p in _walk(bundle)}
    expected = {'manifest.json', 'manifest.sha256'} | {'files/' + path for path in paths}
    if actual != expected:
        raise MigrationError('unexpected_bundle_file')
    return manifest, digest


def verify(bundle: str | Path, *, expected_manifest_hash: str | None = None) -> dict:
    """Read and verify all manifest members before any restore mutation."""
    bundle = _path(bundle)
    if not bundle.is_dir():
        raise MigrationError('bundle_directory_required')
    _outside_repository(bundle)
    manifest, digest = _verified_manifest(bundle, expected_manifest_hash)
    return dict(status='verified', private=True, files=len(manifest['files']),
                bytes=sum(item['size'] for item in manifest['files']), manifest_sha256=digest)


def _install(source: Path, destination: Path) -> None:
    """An exclusive hard link installs a complete file without overwriting."""
    os.link(source, destination)


def restore(bundle: str | Path, target_root: str | Path, *, confirmed_stopped=False,
            expected_manifest_hash: str | None = None) -> dict:
    _require_stopped(confirmed_stopped)
    bundle, target = _path(bundle), _path(target_root)
    verify(bundle, expected_manifest_hash=expected_manifest_hash)
    manifest, digest = _verified_manifest(bundle, expected_manifest_hash)
    if not target.is_dir():
        raise MigrationError('existing_target_root_required')
    if _selected(target, True, True, require=False):
        raise MigrationError('target_business_state_exists')
    entries = manifest['files']
    for entry in entries:
        destination = target / entry['path']
        _reject_links(destination)
        if destination.exists():
            raise MigrationError('target_business_state_exists')
    stage = Path(tempfile.mkdtemp(prefix='.migration-restore-', dir=target))
    installed, created_dirs = [], []
    try:
        # mkdtemp is private on POSIX; restrict its inherited Windows DACL.
        if WINDOWS:
            subprocess.run(['icacls', str(stage), '/inheritance:r', '/grant:r',
                            '*' + _current_sid() + ':(OI)(CI)F'],
                           capture_output=True, check=True, timeout=10)
        _assert_private(stage)
        for entry in entries:
            path = stage / entry['path']
            _copy(bundle / 'files' / entry['path'], path)
            if _digest(path) != (entry['size'], entry['sha256']):
                raise MigrationError('file_changed_during_restore')
        for entry in entries:
            destination = target / entry['path']
            _reject_links(destination)
            missing = []
            parent = destination.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir(mode=0o700)
                created_dirs.append(parent)
            _install(stage / entry['path'], destination)
            installed.append(destination)
        return dict(status='restored', private=True, files=len(entries), manifest_sha256=digest)
    except BaseException:
        for path in reversed(installed):
            path.unlink()
        for path in reversed(created_dirs):
            path.rmdir()
        raise
    finally:
        _safe_remove(stage, target)


def _endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise MigrationError('invalid_loopback_endpoint') from None
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or port is None or not 1 <= port <= 65535):
        raise MigrationError('invalid_loopback_endpoint')
    host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
    return f'http://{host}:{port}', port


def _commit_endpoint(connection) -> None:
    connection.commit()


def update_account_endpoint(root: str | Path, account: str, endpoint: str, *, confirmed_stopped=False) -> dict:
    """Synchronize CSV and DB with rollback; never changes UID or publishes."""
    _require_stopped(confirmed_stopped)
    root = _path(root)
    endpoint, port = _endpoint(endpoint)
    csv_path, database_path = root / 'data/account.csv', root / 'data/mediaflow.db'
    _regular(csv_path)
    _regular(database_path)
    for suffix in ('-wal', '-shm'):
        _reject_links(Path(str(database_path) + suffix))
    original = csv_path.read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(original.decode('utf-8-sig'), newline=''))
        rows, fields = list(reader), reader.fieldnames
    except (ValueError, UnicodeError, csv.Error):
        raise MigrationError('invalid_account_csv') from None
    if not fields or not {'账号', 'CDP地址'}.issubset(fields) or len(set(fields)) != len(fields):
        raise MigrationError('invalid_account_csv')
    matched = [row for row in rows if row.get('账号') == account]
    if len(matched) != 1 or any(None in row or None in row.values() for row in rows):
        raise MigrationError('account_csv_not_unique_or_invalid')
    matched[0]['CDP地址'] = endpoint
    if 'CDP端口' in fields:
        matched[0]['CDP端口'] = str(port)
    rendered = io.StringIO(newline='')
    writer = csv.DictWriter(rendered, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    stage = Path(tempfile.mkdtemp(prefix='.migration-endpoint-', dir=csv_path.parent))
    connection = None
    replaced = False
    previous = None
    retain_recovery = False
    try:
        if WINDOWS:
            subprocess.run(['icacls', str(stage), '/inheritance:r', '/grant:r',
                            '*' + _current_sid() + ':(OI)(CI)F'],
                           capture_output=True, check=True, timeout=10)
        _assert_private(stage)
        replacement, rollback_file = stage / 'replacement.csv', stage / 'original.csv'
        replacement.write_bytes(rendered.getvalue().encode('utf-8-sig'))
        rollback_file.write_bytes(original)
        replacement.chmod(0o600)
        rollback_file.chmod(0o600)
        connection = sqlite3.connect(database_path, timeout=15)
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT id,cdp_url,uid,updated_at FROM accounts WHERE name=?', (account,)).fetchone()
        if previous is None:
            raise MigrationError('database_account_not_found')
        # This private journal is retained only if automatic compensation fails.
        (stage / 'recovery.json').write_text(json.dumps(dict(
            status='prepared', account_id=previous[0], original_endpoint=previous[1],
            original_uid=previous[2], original_updated_at=previous[3]),
            ensure_ascii=False), encoding='utf-8')
        (stage / 'recovery.json').chmod(0o600)
        connection.execute('UPDATE accounts SET cdp_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                           (endpoint, previous[0]))
        if csv_path.read_bytes() != original:
            raise MigrationError('account_csv_changed_during_update')
        os.replace(replacement, csv_path)
        replaced = True
        _commit_endpoint(connection)
        return dict(status='endpoint_updated', accounts_updated=1, uid_preserved=True)
    except BaseException:
        database_restored = csv_restored = True
        try:
            if connection is not None:
                connection.rollback()
                # Compensate a commit that succeeded before an exceptional return.
                if previous is not None:
                    current = connection.execute('SELECT cdp_url,uid,updated_at FROM accounts WHERE id=?', (previous[0],)).fetchone()
                    if current != previous[1:]:
                        connection.execute('UPDATE accounts SET cdp_url=?,uid=?,updated_at=? WHERE id=?',
                                           (*previous[1:], previous[0]))
                        connection.commit()
        except BaseException:
            database_restored = False
        if replaced:
            try:
                # Keep original.csv intact until compensation has fully succeeded.
                _copy(stage / 'original.csv', stage / 'restore.csv')
                os.replace(stage / 'restore.csv', csv_path)
            except BaseException:
                csv_restored = False
        if not database_restored or not csv_restored:
            retain_recovery = True
            journal = stage / 'recovery.json'
            if journal.exists():
                recovery = json.loads(journal.read_text(encoding='utf-8'))
                recovery.update(status='recovery_required', database_restored=database_restored,
                                csv_restored=csv_restored)
                journal.write_text(json.dumps(recovery, ensure_ascii=False), encoding='utf-8')
            raise MigrationError('endpoint_recovery_required') from None
        raise
    finally:
        if connection is not None:
            connection.close()
        if not retain_recovery:
            _safe_remove(stage, csv_path.parent)
