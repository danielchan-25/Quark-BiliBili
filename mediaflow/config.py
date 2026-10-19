from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from mediaflow.audit import audited, register_secrets, write_audit_event

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_path: Path
    log_dir: Path
    feishu_webhook: str | None
    feishu_secret: str | None
    ai_api_url: str | None
    ai_api_key: str | None
    ai_model: str | None
    typesafe_api_key: str | None

    @classmethod
    @audited('configuration_load')
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        register_secrets(*(os.getenv(name) for name in ('AI_API_KEY', 'TYPESAFE_API_KEY', 'FEISHU_WEBHOOK', 'FEISHU_SECRET')))
        write_audit_event(ROOT/'logs/workflow.jsonl', 'configuration_checked',
                          typesafe_configured=bool(os.getenv('TYPESAFE_API_KEY')),
                          feishu_configured=bool(os.getenv('FEISHU_WEBHOOK')))
        return cls(
            database_path=ROOT / "data" / "mediaflow.db",
            log_dir=ROOT / "logs",
            feishu_webhook=os.getenv("FEISHU_WEBHOOK") or None,
            feishu_secret=os.getenv("FEISHU_SECRET") or None,
            ai_api_url=os.getenv("AI_API_URL") or None,
            ai_api_key=os.getenv("AI_API_KEY") or None,
            ai_model=os.getenv("AI_MODEL") or None,
            typesafe_api_key=os.getenv("TYPESAFE_API_KEY") or None,
        )
