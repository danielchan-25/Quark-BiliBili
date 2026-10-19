from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

LOG_ROOT = Path(__file__).resolve().parents[1] / 'logs'
_trace = ContextVar('audit_trace', default=None)
_span = ContextVar('audit_span', default=None)
_secrets: set[str] = set()
_sensitive = {'api_key', 'secret', 'webhook', 'cookie', 'cookies', 'authorization',
              'password', 'token', 'text', 'payload', 'headers', 'cdp_url', 'resource_url'}


def register_secrets(*values: str | None) -> None:
    _secrets.update(value for value in values if value and len(value) >= 4)


def redact(value: Any, key: str = '') -> Any:
    if key.lower() in _sensitive or key.lower().endswith(('_api_key', '_secret', '_webhook')):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for secret in sorted(_secrets, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        # Exception bodies and browser diagnostics can embed private URLs.
        return re.sub(r'(?:https?|wss?)://[^\s<>\"\']+', '[URL]', value)
    return value


@contextmanager
def audit_step(name: str, log_name: str = 'workflow.jsonl', **fields: Any):
    """Correlate nested steps and record a terminal event even on exceptions."""
    trace_token = _trace.set(_trace.get() or uuid4().hex)
    parent = _span.get()
    span_token = _span.set(uuid4().hex)
    started = time.perf_counter()
    path = LOG_ROOT / log_name
    result: dict[str, Any] = {}
    try:
        write_audit_event(path, name + '_started', parent_span_id=parent, **fields)
        yield result
    except BaseException as exc:
        write_audit_event(path, name + '_failed', parent_span_id=parent,
                          elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                          error_type=type(exc).__name__, **fields)
        raise
    else:
        write_audit_event(path, name + '_completed', parent_span_id=parent,
                          elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                          **fields, **result)
    finally:
        _span.reset(span_token)
        _trace.reset(trace_token)


def audited(name: str, log_name: str = 'workflow.jsonl'):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with audit_step(name, log_name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def write_audit_event(path: Path, event: str, **fields: Any) -> None:
    """Append an analysis-friendly local event without recording credentials."""
    override = os.getenv('MEDIAFLOW_LOG_DIR')
    if override and path.is_relative_to(LOG_ROOT):
        path = Path(override) / path.relative_to(LOG_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        'trace_id': _trace.get(),
        'span_id': _span.get(),
        **redact(fields),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
