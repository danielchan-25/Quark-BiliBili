"""Summarize local TypeSafe usage without making API calls."""
import json
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'logs/typesafe-usage.jsonl'
day = sys.argv[1] if len(sys.argv) > 1 else None
from datetime import datetime, timedelta

rows = []
if path.exists():
    for line in path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        local_date = (datetime.fromisoformat(row['timestamp']) + timedelta(hours=8)).date().isoformat()
        if not day or day == local_date:
            rows.append(row)
calls = [r for r in rows if r['event'] == 'typesafe_request_completed']
hits = [r for r in rows if r['event'] == 'typesafe_cache_hit']
failures = [r for r in rows if r['event'] == 'typesafe_request_failed']
print(json.dumps(dict(date_beijing=day or 'all', completed_requests=len(calls), cache_hits=len(hits),
    failed_requests=len(failures),
    reported_input_tokens=sum(r['input_tokens'] or 0 for r in calls),
    reported_output_tokens=sum(r['output_tokens'] or 0 for r in calls),
    usage_unknown_requests=sum(r['input_tokens'] is None or r['output_tokens'] is None for r in calls)+len(failures),
    estimated_avoided_input_tokens=sum(r.get('estimated_avoided_input_tokens') or 0 for r in hits),
    estimated_avoided_output_tokens=sum(r.get('estimated_avoided_output_tokens') or 0 for r in hits)), indent=2))
