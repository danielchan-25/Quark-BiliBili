import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import stat
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReportDeploymentTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'Linux launcher argument test')
    def test_launcher_dispatches_only_whitelisted_main_commands_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'project with spaces'
            (root / 'scripts').mkdir(parents=True)
            (root / '.venv/bin').mkdir(parents=True)
            (root / 'scripts/run.sh').write_bytes((ROOT / 'scripts/run.sh').read_bytes())
            fake = root / '.venv/bin/python'
            fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({"cwd":os.getcwd(),"argv":sys.argv[1:]}))\n', encoding='utf-8')
            fake.chmod(0o700)
            cases = [(['account', 'list'], [str(root / 'main.py'), 'account', 'list']),
                     (['promotion', '--account', 'bilibili-123', '--dry-run'],
                      [str(root / 'main.py'), 'promotion', '--account', 'bilibili-123', '--dry-run']),
                     (['tools/doctor.py', '--json'], ['tools/doctor.py', '--json']),
                     (['-c', 'print("test value")'], ['-c', 'print("test value")']),
                     (['account-other', 'not a command'], ['account-other', 'not a command'])]
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(['bash', str(root / 'scripts/run.sh'), *arguments],
                                            cwd=temp, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = json.loads(result.stdout)
                    self.assertEqual(observed['cwd'], str(root))
                    self.assertEqual(observed['argv'], expected)

    def test_report_date_keeps_original_trigger_day_after_midnight(self):
        runner = load_script('report_runner')
        beijing = timezone(timedelta(hours=8))
        self.assertEqual(runner.report_date(datetime(2026, 10, 28, 0, 5, tzinfo=beijing)), '2026-10-27')
        self.assertEqual(runner.report_date(datetime(2026, 10, 28, 23, 54, tzinfo=beijing)), '2026-10-27')
        self.assertEqual(runner.report_date(datetime(2026, 10, 28, 23, 55, tzinfo=beijing)), '2026-10-28')

    def test_report_refuses_missing_configuration_without_running_a_command(self):
        runner = load_script('report_runner')
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'MEDIAFLOW_SCHEDULE_FILE': str(Path(temp) / 'absent.toml')}):
            with self.assertRaisesRegex(ValueError, 'local schedules.toml'):
                runner.report_command(send=True, root=Path(temp))

    def test_report_preview_and_send_use_configured_accounts(self):
        runner = load_script('report_runner')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'schedule.toml'
            config.write_text('timezone="Asia/Shanghai"\ndefault_account="bilibili-123456"\n[accounts.bilibili-123456]\nhours=[1,6]\n', encoding='utf-8')
            with patch.dict(os.environ, {'MEDIAFLOW_SCHEDULE_FILE': str(config)}):
                command = runner.report_command(send=False, day='2026-10-27', root=root)
                self.assertIn('--preview', command)
                self.assertEqual(command[command.index('--accounts') + 1], 'bilibili-123456')
                self.assertNotIn('--preview', runner.report_command(send=True, day='2026-10-27', root=root))

    def test_unit_installation_never_overwrites_existing_units_or_enables_them(self):
        renderer = load_script('render_report_units')
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'units'
            paths = renderer.install_units(ROOT, destination)
            before = {path: path.read_bytes() for path in paths}
            self.assertEqual(renderer.install_units(ROOT, destination), [])
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            paths[0].write_text(paths[0].read_text(encoding='utf-8') + '# local change\n', encoding='utf-8')
            changed = {path: path.read_bytes() for path in paths}
            with self.assertRaises(FileExistsError):
                renderer.install_units(ROOT, destination)
            self.assertEqual(changed, {path: path.read_bytes() for path in paths})
            service = paths[0].read_text(encoding='utf-8')
            timer = paths[1].read_text(encoding='utf-8')
            self.assertIn('--send', service)
            self.assertNotIn('scheduled_promotion', service)
            self.assertIn('Persistent=false', timer)
            self.assertFalse((destination / 'timers.target.wants').exists())

    def test_unit_renderer_refuses_symlink_without_changing_its_target(self):
        renderer = load_script('render_report_units')
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'units'
            destination.mkdir()
            original = Path(temp) / 'original'
            original.write_text('preserve', encoding='utf-8')
            try:
                (destination / 'quark-bilibili-daily-report.service').symlink_to(original)
            except OSError:
                self.skipTest('Creating symlinks is not allowed on this host')
            with self.assertRaises(FileExistsError):
                renderer.install_units(ROOT, destination)
            self.assertEqual(original.read_text(encoding='utf-8'), 'preserve')
            self.assertFalse((destination / 'quark-bilibili-daily-report.timer').exists())

    def test_unit_path_quoting_handles_spaces_and_percent_expansion(self):
        renderer = load_script('render_report_units')
        self.assertEqual(renderer.systemd_quoted('/home/demo/my project%/scripts/run.sh'), '"/home/demo/my project%%/scripts/run.sh"')
        with self.assertRaises(ValueError):
            renderer.systemd_quoted('/home/demo/bad\npath')

    @unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'geteuid') and os.geteuid() != 0, 'ordinary Linux user installer test')
    def test_installer_from_another_directory_preserves_all_local_config_and_disables_timers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'project with spaces'
            (root / 'scripts').mkdir(parents=True)
            (root / 'config').mkdir()
            (root / 'data').mkdir()
            (root / 'scripts/install-user.sh').write_bytes((ROOT / 'scripts/install-user.sh').read_bytes())
            (root / 'requirements-linux.lock').write_text('example==1.0\n')
            (root / '.env.example').write_text('EXAMPLE=\n')
            (root / 'data/account.example.csv').write_text('example\n')
            (root / 'config/schedules.example.toml').write_text('example=true\n')
            existing = {'.env': b'PRIVATE=preserve\n', 'data/account.csv': b'local-account\n',
                        'data/schedules.toml': b'local-schedule\n'}
            for name, content in existing.items():
                (root / name).write_bytes(content)
            fake = root / 'fake-python'
            fake.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
if sys.argv[1:3] == ['-m', 'venv']:
    destination = pathlib.Path(sys.argv[3]) / 'bin/python'
    destination.parent.mkdir(parents=True)
    destination.write_text(pathlib.Path(__file__).read_text())
    destination.chmod(0o700)
elif sys.argv[1:4] == ['-m', 'pip', 'install']:
    pass
''', encoding='utf-8')
            fake.chmod(0o700)
            config_home = Path(temp) / 'unused-config'
            env = dict(os.environ, PYTHON_BIN=str(fake), XDG_CONFIG_HOME=str(config_home))
            result = subprocess.run(['bash', str(root / 'scripts/install-user.sh')], cwd=temp,
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(existing, {name: (root / name).read_bytes() for name in existing})
            self.assertTrue((root / '.venv/bin/python').is_file())
            self.assertFalse(config_home.exists())
            self.assertIn('no timer was enabled', result.stdout)
            # A repeat remains safe, then a clean local setup receives private modes.
            result = subprocess.run(['bash', str(root / 'scripts/install-user.sh')], cwd=temp,
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(existing, {name: (root / name).read_bytes() for name in existing})
            for name in existing:
                (root / name).unlink()
            result = subprocess.run(['bash', str(root / 'scripts/install-user.sh')], cwd=temp,
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in existing:
                self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / 'logs').stat().st_mode), 0o700)

    @unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'geteuid') and os.geteuid() == 0, 'root-only refusal check')
    def test_installer_refuses_root_before_creating_runtime_files(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/install-user.sh')], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not root', result.stderr)
