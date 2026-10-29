import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediaflow import promotion_schedule as schedule


class ScheduleConfigurationTests(unittest.TestCase):
    def write_config(self, folder):
        path = Path(folder) / 'schedules.toml'
        path.write_text('timezone="Asia/Shanghai"\nminute=40\ndefault_account="bilibili-42"\n'
                        '[accounts.bilibili-42]\nhours=[20,0,5]\n', encoding='utf-8')
        return path

    def test_custom_account_and_sorted_hours(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(schedule.load_schedule(self.write_config(folder)),
                             ('bilibili-42', {'bilibili-42': (0, 5, 20)}))

    def test_relative_override_is_project_relative(self):
        with patch.dict(os.environ, MEDIAFLOW_SCHEDULE_FILE='private/schedule.toml'):
            self.assertEqual(schedule.schedule_path(), schedule.ROOT/'private/schedule.toml')

    def test_missing_production_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(schedule, 'ROOT', Path(folder)), patch.dict(os.environ, MEDIAFLOW_SCHEDULE_FILE='absent.toml'):
            with self.assertRaises(ValueError):
                schedule.require_local_schedule()

    def test_bad_timezone_hour_duplicates_and_default_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder)
            source = path.read_text(encoding='utf-8')
            for invalid in (source.replace('Asia/Shanghai', 'UTC'),
                            source.replace('[20,0,5]', '[0,0]'),
                            source.replace('[20,0,5]', '[24]'),
                            source.replace('[20,0,5]', '[true]'),
                            source.replace('default_account="bilibili-42"', 'default_account=["bilibili-42"]'),
                            source.replace('default_account="bilibili-42"', 'default_account="bilibili-43"')):
                path.write_text(invalid, encoding='utf-8')
                with self.assertRaises(ValueError):
                    schedule.load_schedule(path)
