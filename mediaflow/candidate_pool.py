"""Shared stock counts and bounded, locked candidate replenishment."""
import contextlib
import io
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from mediaflow.audit import write_audit_event
from mediaflow.config import ROOT


@dataclass(frozen=True)
class PoolPolicy:
    low_watermark: int = 10
    target: int = 30
    pages_per_keyword: int = 4
    page_size: int = 20
    max_reviews: int = 120
    max_seconds: int = 600
    cooldown_seconds: int = 900

    def __post_init__(self):
        if not (1 <= self.low_watermark <= self.target <= 100
                and 1 <= self.pages_per_keyword <= 10 and 1 <= self.page_size <= 20
                and 1 <= self.max_reviews <= 300 and 1 <= self.max_seconds <= 900
                and 0 <= self.cooldown_seconds <= 3600):
            raise ValueError('Invalid candidate pool policy')


def eligible_count(db, resource_id):
    return db.connection.execute('''SELECT COUNT(DISTINCT c.bvid) FROM promotion_candidates c
        WHERE c.resource_id=? AND c.platform='bilibili' AND c.status='available'
        AND c.ai_status='approved'
        AND NOT EXISTS(SELECT 1 FROM historical_comment_videos h WHERE h.bvid=c.bvid)
        AND NOT EXISTS(SELECT 1 FROM promotion_records p WHERE p.platform='bilibili'
            AND (p.bvid=c.bvid OR p.video_url=c.video_url))
        AND NOT EXISTS(SELECT 1 FROM promotion_reservations r WHERE r.bvid=c.bvid)''',
        (resource_id,)).fetchone()[0]


def inventory(db):
    """Database eligibility only; access and account permissions need live checks."""
    result = []
    for resource in db.connection.execute('SELECT id,category FROM promotion_resources WHERE enabled=1 ORDER BY id'):
        counts = db.connection.execute('''SELECT COUNT(*) total,
            COALESCE(SUM(ai_status='approved'),0) approved,
            COALESCE(SUM(ai_status='rejected'),0) rejected,
            COALESCE(SUM(status='unavailable'),0) unavailable
            FROM promotion_candidates WHERE resource_id=? AND platform='bilibili' ''',
            (resource['id'],)).fetchone()
        result.append(dict(resource_id=resource['id'], category=resource['category'],
                           **dict(counts), eligible=eligible_count(db, resource['id'])))
    return result


def maintain_candidate_pool(db, account, refresh, *, state_dir=None, policy=None, force=False, manual=False):
    """Do not discard approved progress or make an optional refill block usable stock."""
    policy = policy or PoolPolicy()
    folder = Path(state_dir) if state_dir else ROOT/'data/candidate-pool'
    folder.mkdir(parents=True, exist_ok=True)
    before = inventory(db)
    result = dict(status='healthy', account=account, before=before, after=before,
                  policy=asdict(policy), refresh={})
    log = ROOT/'logs/candidate-pool.jsonl'
    if not before:
        result.update(status='failed', reason='no_enabled_resource')
        return result
    threshold = policy.target if manual else policy.low_watermark
    if not force and all(row['eligible'] >= threshold for row in before):
        write_audit_event(log, 'candidate_pool_checked', **result)
        return result
    lock = folder/'maintenance.lock'
    for attempt in range(2):
        try:
            with lock.open('x', encoding='utf-8') as handle:
                handle.write(json.dumps(dict(account=account, started=time.time())))
            break
        except FileExistsError:
            try:
                raw = lock.read_text(encoding='utf-8')
                lease = json.loads(raw).get('started')
                expired = type(lease) in (int, float) and time.time() - lease > policy.max_seconds + 120
                if attempt == 0 and expired and lock.read_text(encoding='utf-8') == raw:
                    lock.unlink()
                    write_audit_event(log, 'candidate_pool_expired_lease_recovered', account=account)
                    continue
            except (OSError, ValueError, AttributeError):
                pass
            result.update(status='already_running', reason='candidate_pool_maintenance_running')
            return result
    state_path = folder/'maintenance.json'
    try:
        previous = {}
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass
        last = previous.get('started_epoch') if isinstance(previous, dict) else None
        if not manual and type(last) in (int, float) and 0 <= time.time() - last < policy.cooldown_seconds:
            result.update(status='cooldown', reason='candidate_pool_refresh_cooldown')
            write_audit_event(log, 'candidate_pool_checked', **result)
            return result
        started = time.time()
        before = inventory(db)
        result.update(before=before, after=before)
        target = max(policy.target, max(row['eligible'] for row in before) + policy.low_watermark) if force else policy.target
        target = min(target, 100)
        result['target'] = target
        write_audit_event(log, 'candidate_pool_refill_started', **result)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                refresh(policy, target, result['refresh'])
            result['after'] = inventory(db)
            result['status'] = 'refilled' if all(row['eligible'] >= target for row in result['after']) else 'partial'
        except Exception as exc:
            result.update(status='failed', reason='candidate_pool_refill_failed', error_type=type(exc).__name__)
            result['after'] = inventory(db)
        result['low_stock'] = any(row['eligible'] < policy.low_watermark for row in result['after'])
        result['elapsed_ms'] = round((time.time() - started) * 1000, 2)
        saved = dict(result, started_epoch=started, ended_epoch=time.time())
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(state_path)
        write_audit_event(log, 'candidate_pool_refill_completed', **result)
        return result
    finally:
        lock.unlink(missing_ok=True)
