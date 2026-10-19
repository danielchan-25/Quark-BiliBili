"""Validate AI-generated JSON [{category, text}] and add unique templates."""
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediaflow.config import ROOT
from mediaflow.database import Database
from mediaflow.dedup import text_key, text_used
from mediaflow.audit import write_audit_event, audited


@audited('phrase_import', 'phrase-generation.jsonl')
def import_phrases(db, rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Expected 1-100 phrase objects')
    categories = {r[0] for r in db.connection.execute('SELECT category FROM promotion_resources WHERE enabled=1')}
    validated = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('category'), str) or not isinstance(row.get('text'), str):
            raise ValueError('Each phrase must contain string category and text fields')
        category, text = row['category'], row['text'].strip()
        if category not in categories or text.count('{url}') != 1 or not 15 <= len(text) <= 240 or 'http://' in text.lower() or 'https://' in text.lower():
            raise ValueError('Invalid category, length or URL placeholder')
        validated.append((category, text))

    # Validate the whole batch before writing. A nested savepoint also rolls
    # back insertion errors without committing or discarding caller changes.
    # Keep an outer transaction open when callers have not started one yet.
    if not db.connection.in_transaction:
        db.connection.execute('BEGIN')
    savepoint = 'phrase_import_' + uuid4().hex
    db.connection.execute(f'SAVEPOINT {savepoint}')
    try:
        known = {text_key(r[0].replace('{url}', '')) for r in db.connection.execute('SELECT text FROM phrases')}
        added = 0
        for category, text in validated:
            key = text_key(text.replace('{url}', ''))
            if key in known or text_used(db, text.replace('{url}', '')):
                continue
            db.connection.execute("INSERT INTO phrases(category,text,source) VALUES(?,?,'ai_generated')", (category, text))
            known.add(key)
            added += 1
    except BaseException:
        db.connection.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
        db.connection.execute(f'RELEASE SAVEPOINT {savepoint}')
        raise
    db.connection.execute(f'RELEASE SAVEPOINT {savepoint}')
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
