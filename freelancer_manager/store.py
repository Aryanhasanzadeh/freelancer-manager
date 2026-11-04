"""SQLite state: seen projects, drafts, and placed bids.

The polling loop and the scout thread share one connection, so every access goes
through a lock and the connection is opened with check_same_thread=False.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    project_id INTEGER PRIMARY KEY,
    title      TEXT,
    seen_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    project_id  INTEGER PRIMARY KEY,
    title       TEXT,
    amount      REAL,
    period      INTEGER,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | discarded | failed
    created_at  INTEGER NOT NULL,
    sent_at     INTEGER,
    bid_id      INTEGER
);
CREATE TABLE IF NOT EXISTS bids (
    bid_id      INTEGER PRIMARY KEY,
    project_id  INTEGER,
    amount      REAL,
    period      INTEGER,
    placed_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur.rowcount

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.db.execute(sql, params))

    # ----------------------------------------------------------- seen
    def seen_ids(self) -> set[int]:
        return {r[0] for r in self._all("SELECT project_id FROM seen")}

    def mark_seen(self, pid: int, title: str = "") -> bool:
        """Record a project as reported. False when it was already there."""
        return self._write(
            "INSERT OR IGNORE INTO seen(project_id, title, seen_at) VALUES(?,?,?)",
            (int(pid), title[:300], int(time.time())),
        ) > 0

    # ---------------------------------------------------------- drafts
    def save_draft(self, pid: int, title: str, amount: float, period: int, description: str) -> None:
        self._write(
            """INSERT INTO drafts(project_id, title, amount, period, description, status, created_at)
               VALUES(?,?,?,?,?,'pending',?)
               ON CONFLICT(project_id) DO UPDATE SET
                 title=excluded.title, amount=excluded.amount, period=excluded.period,
                 description=excluded.description, status='pending', created_at=excluded.created_at""",
            (int(pid), title[:300], float(amount), int(period), description, int(time.time())),
        )

    def draft(self, pid: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM drafts WHERE project_id=?", (int(pid),))

    def pending_drafts(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM drafts WHERE status='pending' ORDER BY created_at DESC LIMIT 20")

    def set_draft_status(self, pid: int, status: str, bid_id: int | None = None) -> None:
        self._write(
            "UPDATE drafts SET status=?, sent_at=?, bid_id=? WHERE project_id=?",
            (status, int(time.time()) if status == "sent" else None, bid_id, int(pid)),
        )

    # ------------------------------------------------------------ bids
    def record_bid(self, bid_id: int, pid: int, amount: float, period: int) -> None:
        self._write(
            "INSERT OR REPLACE INTO bids(bid_id, project_id, amount, period, placed_at) VALUES(?,?,?,?,?)",
            (int(bid_id), int(pid), float(amount), int(period), int(time.time())),
        )

    def bid_count(self) -> int:
        row = self._one("SELECT COUNT(*) FROM bids")
        return row[0] if row else 0

    # ------------------------------------------------------------ meta
    def get_meta(self, k: str, default=None):
        row = self._one("SELECT v FROM meta WHERE k=?", (k,))
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    def set_meta(self, k: str, v) -> None:
        self._write(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v, ensure_ascii=False)),
        )
