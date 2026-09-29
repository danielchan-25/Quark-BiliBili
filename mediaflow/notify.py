from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests


class FeishuNotifier:
    def __init__(self, webhook: str | None, secret: str | None) -> None:
        self.webhook, self.secret = webhook, secret

    def send(self, title: str, detail: str) -> None:
        self.send_text(f"【MediaFlow {title}】\n{detail}")

    def send_text(self, text: str) -> None:
        if not self.webhook:
            return
        payload: dict[str, object] = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(hmac.new(f"{timestamp}\n{self.secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
        response = requests.post(self.webhook, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get('code', result.get('StatusCode', 0)) != 0:
            raise RuntimeError('Feishu rejected notification')
