from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS accounts (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, platform TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('promotion','nurture')),
 cdp_url TEXT NOT NULL UNIQUE, profile_label TEXT, uid TEXT, enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS promotion_resources (
 id INTEGER PRIMARY KEY, category TEXT NOT NULL, title TEXT, url TEXT NOT NULL, keywords TEXT NOT NULL, custom_text TEXT,
 enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS promotion_candidates (
 id INTEGER PRIMARY KEY, resource_id INTEGER NOT NULL REFERENCES promotion_resources(id), platform TEXT NOT NULL, video_url TEXT NOT NULL,
 bvid TEXT NOT NULL, title TEXT, author TEXT, relevance TEXT, status TEXT NOT NULL DEFAULT 'archived', note TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(resource_id, platform, video_url)
);
CREATE TABLE IF NOT EXISTS phrases (
 id INTEGER PRIMARY KEY, category TEXT NOT NULL, text TEXT NOT NULL UNIQUE, source TEXT NOT NULL, use_count INTEGER NOT NULL DEFAULT 0,
 last_used_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS promotion_records (
 id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), resource_id INTEGER REFERENCES promotion_resources(id),
 candidate_id INTEGER REFERENCES promotion_candidates(id), platform TEXT NOT NULL, video_url TEXT NOT NULL, bvid TEXT NOT NULL,
 target_type TEXT NOT NULL DEFAULT 'video' CHECK(target_type IN ('video', 'article')), target_id TEXT,
 text TEXT NOT NULL, content_hash TEXT NOT NULL, publish_status TEXT NOT NULL, verification_status TEXT NOT NULL DEFAULT 'not_required',
 remote_comment_id TEXT, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(account_id, video_url)
);
CREATE TABLE IF NOT EXISTS videos (
 id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), bvid TEXT NOT NULL, title TEXT, url TEXT NOT NULL,
 status TEXT NOT NULL, duration REAL, watched_seconds REAL NOT NULL DEFAULT 0, watched_at TEXT, last_error TEXT,
 UNIQUE(account_id,bvid)
);
CREATE TABLE IF NOT EXISTS interactions (
 id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), bvid TEXT NOT NULL, action TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(account_id,bvid,action)
);
CREATE TABLE IF NOT EXISTS task_runs (
 id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), task_type TEXT NOT NULL, status TEXT NOT NULL,
 detail TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), task_run_id INTEGER REFERENCES task_runs(id), level TEXT NOT NULL,
 event_type TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_promotion_records_rate ON promotion_records(account_id, created_at, publish_status);
CREATE INDEX IF NOT EXISTS idx_videos_account_status ON videos(account_id, status);
INSERT OR IGNORE INTO schema_migrations(version) VALUES(1);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.executescript(SCHEMA)
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        """Apply additive migrations without rewriting existing promotion history."""
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(promotion_records)")
        }
        if "target_type" not in columns:
            self.connection.execute(
                "ALTER TABLE promotion_records ADD COLUMN target_type TEXT NOT NULL DEFAULT 'video' "
                "CHECK(target_type IN ('video', 'article'))"
            )
        if "target_id" not in columns:
            self.connection.execute("ALTER TABLE promotion_records ADD COLUMN target_id TEXT")
            self.connection.execute("UPDATE promotion_records SET target_id=bvid WHERE target_id IS NULL")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_promotion_records_target ON promotion_records(account_id, platform, target_type, target_id)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(2)")

    def close(self) -> None:
        self.connection.close()
