import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from mediaflow.scheduled_notifications import alert_scheduled_stop, scheduled_states


class ScheduledNotificationTests(unittest.TestCase):
    def test_offline_browser_alert_sent_once_per_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=root/'state.json'
            path.write_text(json.dumps({'account':'a','slot':'2026-10-03T20:40:00+08:00','stage':'stopped','reason':'local_cdp_unavailable'}),encoding='utf-8')
            notify=Mock(webhook='configured')
            self.assertTrue(alert_scheduled_stop(path,notify,root/'alerts.jsonl'))
            self.assertFalse(alert_scheduled_stop(path,notify,root/'alerts.jsonl'))
            notify.send_text.assert_called_once()
            self.assertIn('2026-10-03 20:40',notify.send_text.call_args.args[0])
            self.assertIn('浏览器 CDP 连接不可用',notify.send_text.call_args.args[0])

    def test_uncertain_delivery_does_not_blindly_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=root/'state.json'
            path.write_text(json.dumps({'account':'a','slot':'2026-10-03T20:40:00+08:00','stage':'failed'}),encoding='utf-8')
            notify=Mock(webhook='configured')
            notify.send_text.side_effect=RuntimeError('timeout')
            with self.assertRaises(RuntimeError):
                alert_scheduled_stop(path,notify,root/'alerts.jsonl')
            self.assertEqual(json.loads(path.read_text())['feishu_alert_status'],'unknown')
            self.assertFalse(alert_scheduled_stop(path,notify,root/'alerts.jsonl'))
            notify.send_text.assert_called_once()

    def test_schedule_date_uses_beijing_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,slot in [('a','2026-10-02T16:40:00+00:00'),('b','2026-10-03T16:40:00+00:00')]:
                (root/f'quark-bilibili-{name}.json').write_text(json.dumps({'account':'a','slot':slot,'stage':'stopped'}))
            self.assertEqual(len(scheduled_states(date(2026,10,3),root)),1)
