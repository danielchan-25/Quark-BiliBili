"""Durable global reservations: uncertain submissions must never be retried blindly."""
import hashlib
import re
import unicodedata


def text_key(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'https?://\S+', '', text)
    return hashlib.sha256(''.join(text.split()).encode()).hexdigest()


def text_used(db, text):
    key = text_key(text)
    if db.connection.execute('SELECT 1 FROM promotion_reservations WHERE text_key=?', (key,)).fetchone():
        return True
    return any(text_key(row['text']) == key for row in db.connection.execute("SELECT text FROM promotion_records WHERE platform='bilibili'"))


def reserve(db, candidate, text, account_id):
    db.connection.execute('BEGIN IMMEDIATE')
    try:
        used = db.connection.execute("SELECT 1 FROM promotion_records WHERE platform='bilibili' AND (bvid=? OR video_url=?)", (candidate.bvid, candidate.url)).fetchone()
        if used or text_used(db, text):
            raise ValueError('video or comment already used by a promotion account')
        db.connection.execute('INSERT INTO promotion_reservations(bvid,text_key,account_id) VALUES(?,?,?)', (candidate.bvid, text_key(text), account_id))
        db.connection.commit()
    except Exception:
        db.connection.rollback()
        raise
