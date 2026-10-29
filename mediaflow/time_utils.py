"""Explicit business time without depending on the host's local timezone.

Existing publication timestamps are naive Beijing wall times. SQLite's
CURRENT_TIMESTAMP task timestamps remain naive UTC; neither is rewritten.
"""
from datetime import datetime, timedelta, timezone


BEIJING = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def publication_time(value: str) -> datetime:
    """Interpret legacy publication/slot times as Beijing, honoring offsets."""
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None
            else parsed.astimezone(BEIJING))


def utc_time(value: str) -> datetime:
    """Interpret SQLite task timestamps as UTC, honoring explicit offsets."""
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc))
