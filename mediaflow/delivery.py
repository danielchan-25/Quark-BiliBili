"""Durable delivery receipts. Never retry an ambiguous notification automatically."""
import json
from datetime import datetime, timezone
from pathlib import Path

from mediaflow.audit import write_audit_event
from mediaflow.config import ROOT
from mediaflow.notify import NotificationRejected


class DeliveryUncertain(RuntimeError):
    pass


def deliver_once(path: Path, notify, message: str, *, kind: str, **fields) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {'kind': kind, **fields, 'status': 'sending',
             'started_at': datetime.now(timezone.utc).isoformat()}
    try:
        with path.open('x', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False)
    except FileExistsError:
        prior = json.loads(path.read_text(encoding='utf-8'))
        if prior['status'] == 'sent':
            return False
        # Even a crash between POST and receipt persistence must not resend.
        raise DeliveryUncertain('Existing notification requires manual delivery review')

    def save():
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

    try:
        if not notify or not notify.webhook:
            raise NotificationRejected('FEISHU_WEBHOOK not configured')
        notify.send_text(message)
    except BaseException as exc:
        state.update(status='failed' if isinstance(exc, NotificationRejected) else 'unknown',
                     error_type=type(exc).__name__)
        save()
        write_audit_event(ROOT/'logs/delivery.jsonl', 'delivery_failed', **state)
        raise
    state.update(status='sent', sent_at=datetime.now(timezone.utc).isoformat())
    save()
    write_audit_event(ROOT/'logs/delivery.jsonl', 'delivery_sent', **state)
    return True
