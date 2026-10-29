import contextlib
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediaflow import migration
from mediaflow.database import SCHEMA
from tools.migrate_state import main as cli


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.bundle = self.root / 'private-bundle'
        for folder in (self.source, self.target):
            (folder / 'data').mkdir(parents=True)
        (self.source / '.env').write_text('TEST_SECRET=private-test-value\n', encoding='utf-8')
        self.account = 'bilibili-0000000000'
        self.old_endpoint = 'http://127.0.0.1:9222'
        self.new_endpoint = 'http://127.0.0.1:9333'
        self.fields = ['平台', '账号', '角色', 'CDP地址', 'CDP端口', '推广链接', '搜索关键词', '启用状态', '备注']
        self.rows = [dict(zip(self.fields, ['哔哩哔哩', self.account, '推广', self.old_endpoint, '9222',
                                          'https://example.invalid/private-share', '教程', '启用', 'test']))]
        self.write_csv(self.rows)
        self.connection = sqlite3.connect(self.source / 'data/mediaflow.db')
        self.addCleanup(self.connection.close)
        self.connection.executescript(SCHEMA)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('INSERT INTO accounts(id,name,platform,role,cdp_url,uid) VALUES(1,?,\'bilibili\',\'promotion\',?,\'0000000000\')',
                                (self.account, self.old_endpoint))
        self.connection.execute("INSERT INTO promotion_resources(id,category,url,keywords) VALUES(1,'资料','https://example.invalid/private-share','教程')")
        self.connection.execute("INSERT INTO phrases(id,category,text,source,use_count) VALUES(1,'资料','private comment {url}','test',1)")
        self.connection.execute("INSERT INTO historical_comment_videos(bvid,account_uid,source) VALUES('BVold','0000000000','test')")
        self.connection.execute("INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES('BVpending','reserved-body-key',1)")
        self.connection.execute("INSERT INTO promotion_records(id,account_id,resource_id,platform,video_url,bvid,text,content_hash,publish_status,verification_status,remote_comment_id) VALUES(1,1,1,'bilibili','https://example.invalid/private-video','BVpending','private submitted body','hash','submitted','pending_recheck_error',NULL)")
        self.connection.execute("INSERT INTO task_runs(id,account_id,task_type,status,detail) VALUES(1,1,'promotion','PENDING','private diagnostic')")
        self.connection.commit()
        self.states = {
            'data/scheduled-runs/round.json': {'stage': 'requires_readonly_recheck', 'comments_submitted': 1},
            'data/scheduled-runs/notification-receipts/notice.json': {'status': 'unknown'},
            'data/notification-receipts/notice.json': {'status': 'sending'},
            'data/daily-reports/combined-receipt.json': {'status': 'sent'},
            'data/promotion-schedules/account.json': {'active_from': '2026-10-01T00:00:00+08:00'},
            'data/candidate-pool/maintenance.json': {'started_epoch': 1234},
        }
        for relative, content in self.states.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content), encoding='utf-8')
        for relative in ('data/scheduled-runs/stale.lock', 'data/scheduled-runs/state.tmp',
                         'data/browser-locks/lease.lock', 'data/round-locks/lease.lock',
                         '.venv/ignored', 'profile/Cookies'):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('do not migrate', encoding='utf-8')
        (self.source / 'logs').mkdir()
        (self.source / 'logs/workflow.jsonl').write_text('{"event":"test"}\n', encoding='utf-8')
        cache = sqlite3.connect(self.source / 'data/typesafe-cache.db')
        cache.execute('CREATE TABLE scores(key TEXT PRIMARY KEY, result TEXT)')
        cache.execute("INSERT INTO scores VALUES('k','private cache')")
        cache.commit()
        cache.close()
        (self.target / 'data/account.example.csv').write_text('public example', encoding='utf-8')

    def write_csv(self, rows):
        with (self.source / 'data/account.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fields)
            writer.writeheader()
            writer.writerows(rows)

    def make_bundle(self, **kwargs):
        return migration.backup(self.source, self.bundle, confirmed_stopped=True, **kwargs)

    def rewrite_manifest(self, change):
        path = self.bundle / 'manifest.json'
        manifest = json.loads(path.read_bytes())
        change(manifest)
        raw = json.dumps(manifest).encode('utf-8')
        path.write_bytes(raw)
        (self.bundle / 'manifest.sha256').write_text(hashlib.sha256(raw).hexdigest() + '\n', encoding='ascii')

    def values(self, root):
        connection = sqlite3.connect(root / 'data/mediaflow.db')
        try:
            return dict(endpoint=connection.execute('SELECT cdp_url,uid FROM accounts WHERE id=1').fetchone(),
                        record=connection.execute('SELECT publish_status,verification_status,remote_comment_id,text FROM promotion_records WHERE id=1').fetchone(),
                        reservation=connection.execute('SELECT bvid,text_key,account_id FROM promotion_reservations').fetchall(),
                        historical=connection.execute('SELECT * FROM historical_comment_videos').fetchall(),
                        phrase=connection.execute('SELECT text,use_count FROM phrases').fetchall())
        finally:
            connection.close()

    def test_explicit_quiescence_required_before_mutation(self):
        for action in (lambda: migration.backup(self.source, self.bundle),
                       lambda: migration.restore(self.bundle, self.target),
                       lambda: migration.update_account_endpoint(self.source, self.account, self.new_endpoint)):
            with self.assertRaisesRegex(migration.MigrationError, 'stopped_confirmation_required'):
                action()
        self.assertFalse(self.bundle.exists())
        self.assertEqual(self.values(self.source)['endpoint'][0], self.old_endpoint)

    def test_wal_snapshot_and_roundtrip_preserve_unknown_and_dedup_state(self):
        before = self.values(self.source)
        result = self.make_bundle(include_cache=True, include_logs=True)
        self.assertEqual(migration.verify(self.bundle, expected_manifest_hash=result['manifest_sha256'])['status'], 'verified')
        self.assertFalse(list(self.bundle.rglob('*-wal')))
        self.assertFalse(list(self.bundle.rglob('*-shm')))
        result = migration.restore(self.bundle, self.target, confirmed_stopped=True)
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(self.values(self.target), before)
        self.assertEqual((self.target / '.env').read_bytes(), (self.source / '.env').read_bytes())
        for relative in self.states:
            self.assertEqual(json.loads((self.target / relative).read_text()), self.states[relative])
        self.assertTrue((self.target / 'data/typesafe-cache.db').exists())
        self.assertTrue((self.target / 'logs/workflow.jsonl').exists())
        self.assertEqual((self.target / 'data/account.example.csv').read_text(), 'public example')
        self.assertFalse(list(self.target.rglob('*.lock')))
        self.assertFalse(list(self.target.rglob('*.tmp')))
        self.assertFalse((self.target / '.venv').exists())
        self.assertFalse((self.target / 'profile').exists())

    def test_optional_cache_and_logs_are_not_copied_by_default(self):
        self.make_bundle()
        paths = {entry['path'] for entry in json.loads((self.bundle / 'manifest.json').read_bytes())['files']}
        self.assertNotIn('data/typesafe-cache.db', paths)
        self.assertNotIn('data/candidate-pool/maintenance.json', paths)
        self.assertNotIn('logs/workflow.jsonl', paths)
        self.assertTrue(migration.REQUIRED.issubset(paths))

    def test_local_schedule_config_is_optional_but_preserved_when_present(self):
        config = self.source / 'data/schedules.toml'
        config.write_text('enabled = false\n', encoding='utf-8')
        self.make_bundle()
        migration.restore(self.bundle, self.target, confirmed_stopped=True)
        self.assertEqual((self.target / 'data/schedules.toml').read_bytes(), config.read_bytes())

    def test_backup_refuses_destination_in_source_or_any_git_checkout(self):
        with self.assertRaisesRegex(migration.MigrationError, 'outside_repository'):
            migration.backup(self.source, self.source / 'bundle', confirmed_stopped=True)
        checkout = self.root / 'other-checkout'
        (checkout / '.git').mkdir(parents=True)
        with self.assertRaisesRegex(migration.MigrationError, 'outside_repository'):
            migration.backup(self.source, checkout / 'bundle', confirmed_stopped=True)

    def test_existing_bundle_is_not_overwritten(self):
        self.make_bundle()
        before = (self.bundle / 'manifest.json').read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, 'destination_already_exists'):
            self.make_bundle()
        self.assertEqual((self.bundle / 'manifest.json').read_bytes(), before)

    def test_backup_failure_removes_incomplete_bundle(self):
        with patch.object(migration, '_sqlite_snapshot', side_effect=OSError('private diagnostic')):
            with self.assertRaises(OSError):
                self.make_bundle()
        self.assertFalse(self.bundle.exists())

    def test_hash_tampering_is_rejected_before_restore_creates_files(self):
        self.make_bundle()
        (self.bundle / 'files/.env').write_text('tampered secret', encoding='utf-8')
        with self.assertRaisesRegex(migration.MigrationError, 'file_hash_mismatch'):
            migration.restore(self.bundle, self.target, confirmed_stopped=True)
        self.assertEqual(sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob('*') if p.is_file()),
                         ['data/account.example.csv'])

    def test_out_of_band_manifest_digest_detects_replaced_manifest(self):
        expected = self.make_bundle()['manifest_sha256']
        self.rewrite_manifest(lambda m: m.update(created_at='changed'))
        with self.assertRaisesRegex(migration.MigrationError, 'manifest_hash_mismatch'):
            migration.verify(self.bundle, expected_manifest_hash=expected)

    def test_traversal_absolute_backslash_and_unlisted_manifest_paths_rejected(self):
        self.make_bundle()
        original = (self.bundle / 'manifest.json').read_bytes()
        for value in ('../escape', '/tmp/escape', 'data/../escape.json', 'data\\escape.json',
                      'C:/escape', 'profile/Cookies', 'data/scheduled-runs/unsafe.lock'):
            with self.subTest(value=value):
                (self.bundle / 'manifest.json').write_bytes(original)
                self.rewrite_manifest(lambda m: m['files'][0].update(path=value))
                with self.assertRaises(migration.MigrationError):
                    migration.verify(self.bundle)

    def test_duplicate_missing_and_unexpected_members_rejected(self):
        self.make_bundle()
        original = (self.bundle / 'manifest.json').read_bytes()
        self.rewrite_manifest(lambda m: m['files'].append(dict(m['files'][0])))
        with self.assertRaisesRegex(migration.MigrationError, 'invalid_manifest'):
            migration.verify(self.bundle)
        (self.bundle / 'manifest.json').write_bytes(original)
        self.rewrite_manifest(lambda m: m.update(files=[e for e in m['files'] if e['path'] != '.env']))
        with self.assertRaisesRegex(migration.MigrationError, 'required_file_missing'):
            migration.verify(self.bundle)
        (self.bundle / 'manifest.json').write_bytes(original)
        (self.bundle / 'manifest.sha256').write_text(hashlib.sha256(original).hexdigest(), encoding='ascii')
        (self.bundle / 'files/unlisted.txt').write_text('unexpected', encoding='utf-8')
        with self.assertRaisesRegex(migration.MigrationError, 'unexpected_bundle_file'):
            migration.verify(self.bundle)

    def test_database_table_count_tampering_rejected(self):
        self.make_bundle()
        self.rewrite_manifest(lambda m: next(e for e in m['files'] if e['path'] == 'data/mediaflow.db')['table_counts'].update(accounts=99))
        with self.assertRaisesRegex(migration.MigrationError, 'sqlite_metadata_mismatch'):
            migration.verify(self.bundle)

    def test_restore_refuses_any_existing_business_state(self):
        self.make_bundle()
        (self.target / '.env').write_text('existing private config', encoding='utf-8')
        with self.assertRaisesRegex(migration.MigrationError, 'target_business_state_exists'):
            migration.restore(self.bundle, self.target, confirmed_stopped=True)
        self.assertEqual((self.target / '.env').read_text(), 'existing private config')
        self.assertFalse((self.target / 'data/mediaflow.db').exists())

    def test_restore_mid_install_failure_rolls_back_all_installed_state(self):
        self.make_bundle()
        install = migration._install
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated install failure')
            install(source, destination)
        with patch.object(migration, '_install', side_effect=fail_second):
            with self.assertRaises(OSError):
                migration.restore(self.bundle, self.target, confirmed_stopped=True)
        self.assertFalse((self.target / '.env').exists())
        self.assertFalse((self.target / 'data/account.csv').exists())
        self.assertFalse(list(self.target.glob('.migration-*')))
        self.assertEqual((self.target / 'data/account.example.csv').read_text(), 'public example')

    def test_bundle_or_source_symbolic_link_rejected(self):
        link = self.source / 'data/scheduled-runs/link.json'
        try:
            link.symlink_to(self.source / '.env')
        except OSError:
            self.skipTest('Creating symlinks is not permitted on this host')
        with self.assertRaisesRegex(migration.MigrationError, 'symbolic_link_rejected'):
            self.make_bundle()
        link.unlink()
        self.make_bundle()
        path = self.bundle / 'files/.env'
        path.unlink()
        path.symlink_to(self.source / '.env')
        with self.assertRaisesRegex(migration.MigrationError, 'symbolic_link_rejected'):
            migration.verify(self.bundle)

    @unittest.skipIf(migration.WINDOWS, 'POSIX permission bits')
    def test_bundle_permissions_are_restricted_and_broad_permissions_rejected(self):
        self.make_bundle()
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.bundle / 'files/.env').stat().st_mode & 0o777, 0o600)
        self.bundle.chmod(0o755)
        with self.assertRaisesRegex(migration.MigrationError, 'private_permissions_required'):
            migration.verify(self.bundle)

    @unittest.skipUnless(migration.WINDOWS, 'Windows DACL permissions')
    def test_windows_bundle_rejects_an_everyone_read_grant(self):
        self.make_bundle()
        subprocess.run(['icacls', str(self.bundle), '/grant', '*S-1-1-0:(OI)(CI)RX'],
                       capture_output=True, check=True, timeout=10)
        with self.assertRaisesRegex(migration.MigrationError, 'private_permissions_required'):
            migration.verify(self.bundle)

    def test_endpoint_update_changes_both_sources_and_preserves_uid_and_state(self):
        before = self.values(self.source)
        result = migration.update_account_endpoint(self.source, self.account, self.new_endpoint,
                                                   confirmed_stopped=True)
        after = self.values(self.source)
        self.assertEqual(result['status'], 'endpoint_updated')
        self.assertEqual(after.pop('endpoint'), (self.new_endpoint, '0000000000'))
        before.pop('endpoint')
        self.assertEqual(after, before)
        with (self.source / 'data/account.csv').open(encoding='utf-8-sig', newline='') as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row['CDP地址'], self.new_endpoint)
        self.assertEqual(row['CDP端口'], '9333')
        self.assertEqual(row['推广链接'], self.rows[0]['推广链接'])

    def test_endpoint_commit_failure_rolls_back_csv_and_database(self):
        before = self.values(self.source)
        csv_before = (self.source / 'data/account.csv').read_bytes()
        with patch.object(migration, '_commit_endpoint', side_effect=sqlite3.OperationalError('private DB error')):
            with self.assertRaises(sqlite3.OperationalError):
                migration.update_account_endpoint(self.source, self.account, self.new_endpoint,
                                                  confirmed_stopped=True)
        self.assertEqual(self.values(self.source), before)
        self.assertEqual((self.source / 'data/account.csv').read_bytes(), csv_before)
        self.assertFalse(list((self.source / 'data').glob('.migration-*')))

    def test_endpoint_exception_after_successful_commit_is_compensated(self):
        before = self.values(self.source)
        csv_before = (self.source / 'data/account.csv').read_bytes()
        def committed_then_failed(connection):
            connection.commit()
            raise OSError('simulated late failure')
        with patch.object(migration, '_commit_endpoint', side_effect=committed_then_failed):
            with self.assertRaises(OSError):
                migration.update_account_endpoint(self.source, self.account, self.new_endpoint,
                                                  confirmed_stopped=True)
        self.assertEqual(self.values(self.source), before)
        self.assertEqual((self.source / 'data/account.csv').read_bytes(), csv_before)

    def test_endpoint_csv_replace_failure_rolls_back_database(self):
        before = self.values(self.source)
        csv_before = (self.source / 'data/account.csv').read_bytes()
        with patch.object(migration.os, 'replace', side_effect=OSError('simulated replace error')):
            with self.assertRaises(OSError):
                migration.update_account_endpoint(self.source, self.account, self.new_endpoint,
                                                  confirmed_stopped=True)
        self.assertEqual(self.values(self.source), before)
        self.assertEqual((self.source / 'data/account.csv').read_bytes(), csv_before)

    def test_unusable_database_connection_still_restores_csv_and_keeps_recovery(self):
        before = self.values(self.source)
        csv_before = (self.source / 'data/account.csv').read_bytes()
        def close_then_fail(connection):
            connection.close()
            raise OSError('simulated database connection loss')
        with patch.object(migration, '_commit_endpoint', side_effect=close_then_fail):
            with self.assertRaisesRegex(migration.MigrationError, 'endpoint_recovery_required'):
                migration.update_account_endpoint(self.source, self.account, self.new_endpoint,
                                                  confirmed_stopped=True)
        self.assertEqual(self.values(self.source), before)
        self.assertEqual((self.source / 'data/account.csv').read_bytes(), csv_before)
        stages = list((self.source / 'data').glob('.migration-endpoint-*'))
        self.assertEqual(len(stages), 1)
        self.assertEqual((stages[0] / 'original.csv').read_bytes(), csv_before)
        recovery = json.loads((stages[0] / 'recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(recovery['status'], 'recovery_required')
        self.assertTrue(recovery['csv_restored'])
        self.assertFalse(recovery['database_restored'])

    def test_failed_csv_compensation_preserves_private_original_for_recovery(self):
        csv_before = (self.source / 'data/account.csv').read_bytes()
        copy = migration._copy
        def fail_restore(source, target):
            if source.name == 'original.csv':
                raise OSError('simulated CSV compensation failure')
            copy(source, target)
        with patch.object(migration, '_commit_endpoint', side_effect=sqlite3.OperationalError('commit failed')), \
                patch.object(migration, '_copy', side_effect=fail_restore):
            with self.assertRaisesRegex(migration.MigrationError, 'endpoint_recovery_required'):
                migration.update_account_endpoint(self.source, self.account, self.new_endpoint,
                                                  confirmed_stopped=True)
        self.assertEqual(self.values(self.source)['endpoint'][0], self.old_endpoint)
        stages = list((self.source / 'data').glob('.migration-endpoint-*'))
        self.assertEqual(len(stages), 1)
        self.assertEqual((stages[0] / 'original.csv').read_bytes(), csv_before)
        recovery = json.loads((stages[0] / 'recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(recovery['status'], 'recovery_required')
        self.assertFalse(recovery['csv_restored'])

    def test_duplicate_endpoint_constraint_leaves_both_sources_untouched(self):
        self.connection.execute("INSERT INTO accounts(name,platform,role,cdp_url,uid) VALUES('other','bilibili','promotion',?,'9')", (self.new_endpoint,))
        self.connection.commit()
        before = self.values(self.source)
        csv_before = (self.source / 'data/account.csv').read_bytes()
        with self.assertRaises(sqlite3.IntegrityError):
            migration.update_account_endpoint(self.source, self.account, self.new_endpoint, confirmed_stopped=True)
        self.assertEqual(self.values(self.source), before)
        self.assertEqual((self.source / 'data/account.csv').read_bytes(), csv_before)

    def test_endpoint_rejects_nonloopback_credentials_queries_and_missing_ports(self):
        for value in ('http://example.invalid:9222', 'ws://127.0.0.1:9222/devtools/browser/x',
                      'http://user:secret@127.0.0.1:9222', 'http://127.0.0.1:9222/?secret=private',
                      'http://127.0.0.1', 'http://127.0.0.1:99999', 'http://127.0.0.1:9222/path'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(migration.MigrationError, 'invalid_loopback_endpoint'):
                    migration.update_account_endpoint(self.source, self.account, value, confirmed_stopped=True)

    def test_missing_or_duplicate_csv_account_is_not_updated(self):
        self.write_csv(self.rows + self.rows)
        with self.assertRaisesRegex(migration.MigrationError, 'account_csv_not_unique'):
            migration.update_account_endpoint(self.source, self.account, self.new_endpoint, confirmed_stopped=True)
        self.assertEqual(self.values(self.source)['endpoint'][0], self.old_endpoint)

    def test_cli_defaults_to_help_and_does_not_expose_private_content_on_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli([]), 0)
            self.assertEqual(cli(['backup', '--source-root', str(self.source), '--destination', str(self.bundle)]), 1)
            self.assertEqual(cli(['account-endpoint', '--root', str(self.source), '--account', self.account,
                                  '--cdp-url', 'http://user:private-secret@127.0.0.1:9222', '--confirm-stopped']), 1)
        text = output.getvalue()
        self.assertIn('stopped_confirmation_required', text)
        self.assertNotIn('private-secret', text)
        self.assertNotIn('private-test-value', text)
        self.assertNotIn('https://example.invalid/private-share', text)
        self.assertNotIn('private submitted body', text)
        self.assertFalse(self.bundle.exists())


if __name__ == '__main__':
    unittest.main()
