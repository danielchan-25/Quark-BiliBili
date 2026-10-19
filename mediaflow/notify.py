from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests
from mediaflow.audit import audit_step, register_secrets


class NotificationRejected(RuntimeError):
    """The service explicitly rejected a notification; no delivery occurred."""


class FeishuNotifier:
    def __init__(self, webhook: str | None, secret: str | None) -> None:
        self.webhook, self.secret = webhook, secret
        register_secrets(webhook, secret)

    def send(self, title: str, detail: str) -> None:
        self.send_text(f"【MediaFlow {title}】\n{detail}")

    def send_text(self, text: str) -> None:
        with audit_step('feishu_notification', 'notifications.jsonl', configured=bool(self.webhook)) as outcome:
            outcome['sent'] = self._send_text(text)

    def _send_text(self, text: str) -> bool:
        if not self.webhook:
            return False
        payload: dict[str, object] = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(hmac.new(f"{timestamp}\n{self.secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
        try:
            response = requests.post(self.webhook, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Request exceptions may contain the secret webhook URL.
            raise RuntimeError(f'Feishu notification request failed ({type(exc).__name__})') from None
        code = result.get('code', result.get('StatusCode')) if isinstance(result, dict) else None
        if type(code) in (int, str) and code not in (0, '0'):
            raise NotificationRejected('Feishu rejected notification')
        if type(code) not in (int, str):
            raise RuntimeError('Feishu rejected notification or returned an invalid acknowledgement')
        return True
