import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.doctor import inspect_environment


class OfflineDoctorTests(unittest.TestCase):
    def test_fixture_check_does_not_change_private_files_or_open_database(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            (root/'data').mkdir()
            (root/'.env').write_text('TYPESAFE_API_KEY=private-key\nFEISHU_WEBHOOK=https://private.invalid/webhook', encoding='utf-8')
            (root/'data/account.csv').write_text('账号,角色,启用状态\nbilibili-42,推广,启用\n', encoding='utf-8')
            (root/'data/schedules.toml').write_text('timezone="Asia/Shanghai"\ndefault_account="bilibili-42"\n[accounts.bilibili-42]\nhours=[0,5]\n', encoding='utf-8')
            # A malformed sentinel database proves the doctor does not open it.
            (root/'data/mediaflow.db').write_bytes(b'not-a-database')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            report = inspect_environment(root)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(report['browser_checked'])
            self.assertFalse(report['publication_ready'])
            self.assertNotIn('private-key', str(report))
            self.assertNotIn('private.invalid', str(report))
            checks = {c['check']: c['ok'] for c in report['checks']}
            self.assertTrue(checks['account_csv'])
            self.assertTrue(checks['typesafe_configured'])

    def test_empty_install_is_not_reported_ready(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            report = inspect_environment(Path(temp))
            self.assertFalse(report['ok'])
            self.assertFalse(report['publication_ready'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX file mode check')
    def test_external_schedule_permissions_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root/'external.toml'
            external.write_text('timezone="Asia/Shanghai"\ndefault_account="bilibili-42"\n[accounts.bilibili-42]\nhours=[0]\n', encoding='utf-8')
            external.chmod(0o644)
            with patch.dict(os.environ, MEDIAFLOW_SCHEDULE_FILE=str(external)):
                report = inspect_environment(root)
            checks = {c['check']:c['ok'] for c in report['checks']}
            self.assertFalse(checks['private_config_permissions'])
