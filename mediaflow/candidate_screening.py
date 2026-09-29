from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from mediaflow.audit import write_audit_event
from mediaflow.config import ROOT


MIN_RELEVANCE_SCORE = 2.0
MIN_CONFIDENCE = 0.70
MAX_RISK = 0.25


@dataclass(frozen=True)
class ScreeningResult:
    relevance_score: float
    confidence: float
    risk_probability: float
    status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None
    actual_model: str | None = None


def _request_screening(
    *,
    api_key: str | None,
    keyword: str,
    resource_category: str,
    title: str,
    author: str,
    model: str,
    base_url: str,
) -> ScreeningResult:
    """Use TypeSafe only to rank or reject candidates; it never authorizes publication."""
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is required to refresh promotion candidates safely")

    try:
        from typesafe_sdk import Noul, Score, TypeSafeClient
    except ImportError as exc:
        raise RuntimeError("typesafe-sdk is not installed; run pip install -r requirements.txt") from exc

    state = {
        "search_keyword": keyword,
        "resource_category": resource_category,
        "candidate": {"title": title, "author": author},
    }
    # Avoid optional decompressor version mismatches in the shared runtime.
    with TypeSafeClient(api_key=api_key, model=model, base_url=base_url, timeout=30.0,
                       headers={'Accept-Encoding': 'identity'}) as client:
        response = client.system_one(
            state=state,
            questions={
                "relevance": Score(
                    instructions=(
                        "Rate whether `candidate.title` is a good topical match for "
                        "`search_keyword` and `resource_category`. Judge only the supplied "
                        "metadata; do not infer missing facts."
                    ),
                    criteria=[
                        "Unrelated or too little metadata to establish relevance.",
                        "Adjacent topic, but not a tutorial, explanation, or direct match.",
                        "Directly relevant tutorial, explanation, or use case.",
                        "Exact, clearly relevant tutorial or explanation for the requested topic.",
                    ],
                ),
                "risk": Noul(
                    instructions=(
                        "Based only on `candidate.title` and `candidate.author`, is there clear "
                        "evidence of a serious safety or suitability risk: sensitive content, fraud, "
                        "illegal instructions, or a topic that is plainly unrelated to "
                        "`search_keyword`? Ordinary tutorials and normal paid/free descriptions are "
                        "not risks. Do not treat missing metadata as a risk; relevance is scored separately."
                    ),
                ),
            },
        )

    score = float(response.scores["relevance"].score)
    confidence = float(response.scores["relevance"].confidence)
    risk = float(response.nouls["risk"].noul)
    if not all(math.isfinite(v) for v in (score, confidence, risk)) or not (0 <= score <= 3 and 0 <= confidence <= 1 and 0 <= risk <= 1):
        raise ValueError('Invalid TypeSafe scores')
    status = "approved" if score >= MIN_RELEVANCE_SCORE and confidence >= MIN_CONFIDENCE and risk <= MAX_RISK else "rejected"
    usage = getattr(response, 'usage', None)
    return ScreeningResult(score, confidence, risk, status,
        getattr(usage, 'input_tokens', None), getattr(usage, 'output_tokens', None),
        getattr(response, 'request_id', None), getattr(response, 'model', None))


def screen_candidate(*, api_key, keyword, resource_category, title, author,
                     cache_path: Path | None = None, log_path: Path | None = None,
                     ttl_seconds: float = 86400) -> ScreeningResult:
    """Persist successful judgments for 24h; failed calls are never cached."""
    if not api_key or not api_key.strip():
        raise RuntimeError('TYPESAFE_API_KEY is required to refresh promotion candidates safely')
    cache_path = cache_path or ROOT / 'data/typesafe-cache.db'
    log_path = log_path or ROOT / 'logs/typesafe-usage.jsonl'
    model = os.getenv('TYPESAFE_DEFAULT_MODEL', '').strip() or 'jev-latest'
    base_url = os.getenv('TYPESAFE_BASE_URL', '').strip() or 'https://api.typesafe.ai'
    inputs = dict(keyword=keyword, resource_category=resource_category, title=title, author=author)
    policy = hashlib.sha256(inspect.getsource(_request_screening).encode()).hexdigest()
    key = hashlib.sha256(json.dumps(dict(inputs=inputs, model=model, base_url=base_url,
        policy=policy, thresholds=[MIN_RELEVANCE_SCORE, MIN_CONFIDENCE, MAX_RISK]), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    started = time.perf_counter()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(cache_path, timeout=15)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, created REAL NOT NULL, result TEXT NOT NULL)')
        row = connection.execute('SELECT created,result FROM scores WHERE key=?', (key,)).fetchone()
        if row and 0 <= time.time() - row[0] < ttl_seconds:
            saved = ScreeningResult(**json.loads(row[1]))
            write_audit_event(log_path, 'typesafe_cache_hit', cache_key=key, model=model,
                policy=policy, input_tokens=0, output_tokens=0, api_calls=0,
                estimated_avoided_input_tokens=saved.input_tokens,
                estimated_avoided_output_tokens=saved.output_tokens,
                elapsed_ms=round((time.perf_counter()-started)*1000, 2))
            return saved
        try:
            result = _request_screening(api_key=api_key, model=model, base_url=base_url, **inputs)
        except Exception as exc:
            # Never serialize SDK exception bodies: they may contain request data.
            write_audit_event(log_path, 'typesafe_request_failed', cache_key=key, model=model,
                policy=policy, error_type=type(exc).__name__, input_tokens=None, output_tokens=None,
                elapsed_ms=round((time.perf_counter()-started)*1000, 2))
            raise
        write_audit_event(log_path, 'typesafe_request_completed', cache_key=key, model=model,
            policy=policy, request_id=result.request_id, api_calls=1, actual_model=result.actual_model,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            elapsed_ms=round((time.perf_counter()-started)*1000, 2))
        connection.execute('INSERT OR REPLACE INTO scores VALUES(?,?,?)', (key,time.time(),json.dumps(asdict(result))))
        connection.commit()
        return result
    finally:
        connection.close()
