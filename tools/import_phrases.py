"""Validate AI-generated JSON [{category, text}] and add unique templates."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediaflow.config import ROOT
from mediaflow.database import Database
from mediaflow.dedup import text_key, text_used
from mediaflow.audit import write_audit_event


def import_phrases(db, rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Expected 1-100 phrase objects')
    categories = {r[0] for r in db.connection.execute('SELECT category FROM promotion_resources WHERE enabled=1')}
    known = {text_key(r[0].replace('{url}', '')) for r in db.connection.execute('SELECT text FROM phrases')}
    added = 0
    for row in rows:
        category, text = row['category'], row['text'].strip()
        if category not in categories or text.count('{url}') != 1 or not 15 <= len(text) <= 240 or 'http://' in text or 'https://' in text:
            raise ValueError('Invalid category, length or URL placeholder')
        key = text_key(text.replace('{url}', ''))
        if key in known or text_used(db, text.replace('{url}', '')):
            continue
        db.connection.execute("INSERT INTO phrases(category,text,source) VALUES(?,?,'ai_generated')", (category, text))
        known.add(key)
        added += 1
    return added


if __name__ == '__main__':
    db = Database(ROOT / 'data/mediaflow.db')
    try:
        with db.connection:
            added = import_phrases(db, json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')))
        write_audit_event(ROOT / 'logs/phrase-generation.jsonl', 'phrases_imported', source_file=str(Path(sys.argv[1]).resolve()), added=added)
        print(f'added={added}')
        if not added:
            raise SystemExit(1)
    finally:
        db.close()
