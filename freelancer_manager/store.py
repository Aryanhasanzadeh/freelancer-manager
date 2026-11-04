"""SQLite state: per-user (tenant) projects, drafts, bids and settings.

Multi-tenant by construction: every user-scoped table carries a `tenant` column and
its primary key starts with it, so a query that forgets the tenant cannot return
another user's row even by accident. `TenantStore` binds one chat id and is the only
handle the bot uses; the raw `Store` keeps the user directory and the migration.

The polling loop and the scout threads share one connection, so every access goes
through a lock and the connection is opened with check_same_thread=False.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    TEXT PRIMARY KEY,
    name       TEXT,
    role       TEXT NOT NULL DEFAULT 'member',   -- owner | member | blocked
    created_at INTEGER NOT NULL,
    last_seen  INTEGER
);
CREATE TABLE IF NOT EXISTS seen (
    tenant     TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    title      TEXT,
    seen_at    INTEGER NOT NULL,
    PRIMARY KEY (tenant, project_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    tenant      TEXT NOT NULL,
    project_id  INTEGER NOT NULL,
    title       TEXT,
    amount      REAL,
    period      INTEGER,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | discarded | failed
    created_at  INTEGER NOT NULL,
    sent_at     INTEGER,
    bid_id      INTEGER,
    PRIMARY KEY (tenant, project_id)
);
CREATE TABLE IF NOT EXISTS bids (
    tenant     TEXT NOT NULL,
    bid_id     INTEGER NOT NULL,
    project_id INTEGER,
    amount     REAL,
    period     INTEGER,
    placed_at  INTEGER NOT NULL,
    PRIMARY KEY (tenant, bid_id)
);
CREATE TABLE IF NOT EXISTS meta (
    tenant TEXT NOT NULL,
    k      TEXT NOT NULL,
    v      TEXT,
    PRIMARY KEY (tenant, k)
);
"""


class Store:
    """Connection, schema, migration, and the cross-tenant user directory."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self._migrate()
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ----------------------------------------------------------- migration
    def _migrate(self) -> None:
        """Upgrade a v1 single-account database in place, losing nothing.

        v1 tables had no tenant column. Rename each aside, recreate the new schema,
        then copy every row under the chat id that owned the bot — so the original
        operator keeps their history the first time they open the bot.
        """
        names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" not in names:
            return
        if "tenant" in {r[1] for r in self.db.execute("PRAGMA table_info(meta)")}:
            return  # already multi-tenant
        owner = ""
        try:
            owner = (Path(__file__).resolve().parent.parent / "secrets" / "chat_id.txt").read_text().strip()
        except OSError:
            pass
        if not owner and "admins" in names:
            row = self.db.execute("SELECT chat_id FROM admins ORDER BY added_at LIMIT 1").fetchone()
            owner = row[0] if row else ""
        owner = owner or "legacy"
        now = int(time.time())
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO users(chat_id,name,role,created_at) VALUES(?,?,?,?)",
            (owner, "owner", "owner", now),
        )
        # (new table, columns carried over from v1 — tenant is added by the SELECT)
        copies = {
            "seen": "project_id,title,seen_at",
            "drafts": "project_id,title,amount,period,description,status,created_at,sent_at,bid_id",
            "bids": "bid_id,project_id,amount,period,placed_at",
            "meta": "k,v",
        }
        for table, cols in copies.items():
            old = f"{table}_v1"
            self.db.execute(f"ALTER TABLE {table} RENAME TO {old}")
            self.db.executescript(SCHEMA)  # recreate the empty new table
            self.db.execute(
                f"INSERT OR REPLACE INTO {table}(tenant,{cols}) SELECT ?,{cols} FROM {old}",
                (owner,))
            self.db.execute(f"DROP TABLE {old}")
        if "admins" in names:
            for r in self.db.execute("SELECT chat_id,name,role,added_at FROM admins"):
                self.db.execute(
                    "INSERT OR IGNORE INTO users(chat_id,name,role,created_at) VALUES(?,?,?,?)",
                    (r["chat_id"], r["name"], "owner" if r["role"] == "owner" else "member",
                     r["added_at"]))
            self.db.execute("DROP TABLE admins")
        self.db.commit()

    # ------------------------------------------------------------- plumbing
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

    # --------------------------------------------------------------- users
    def user(self, chat_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE chat_id=?", (str(chat_id),))

    def users(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM users ORDER BY created_at")

    def ensure_user(self, chat_id: str, name: str = "") -> sqlite3.Row:
        """Register a newcomer on first contact. The very first one becomes owner."""
        chat_id = str(chat_id)
        if self.user(chat_id):
            self._write("UPDATE users SET last_seen=? WHERE chat_id=?", (int(time.time()), chat_id))
        else:
            role = "owner" if not self.users() else "member"
            self._write(
                "INSERT INTO users(chat_id,name,role,created_at,last_seen) VALUES(?,?,?,?,?)",
                (chat_id, name[:60], role, int(time.time()), int(time.time())),
            )
        return self.user(chat_id)  # type: ignore[return-value]

    def set_role(self, chat_id: str, role: str) -> int:
        return self._write("UPDATE users SET role=? WHERE chat_id=?", (role, str(chat_id)))

    def delete_user(self, chat_id: str) -> int:
        """Remove a user and every row they own. Nothing is left behind."""
        chat_id = str(chat_id)
        with self.lock:
            for table in ("seen", "drafts", "bids", "meta"):
                self.db.execute(f"DELETE FROM {table} WHERE tenant=?", (chat_id,))
            cur = self.db.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
            self.db.commit()
            return cur.rowcount

    def tenant(self, chat_id: str) -> "TenantStore":
        return TenantStore(self, str(chat_id))

    def counts(self) -> dict:
        out = {}
        for table in ("seen", "drafts", "bids", "users"):
            row = self._one(f"SELECT COUNT(*) FROM {table}")
            out[table] = row[0] if row else 0
        return out


class TenantStore:
    """Every query here is scoped to one chat id — there is no unscoped method."""

    def __init__(self, store: Store, tenant: str) -> None:
        self.s = store
        self.t = str(tenant)

    # --------------------------------------------------------------- seen
    def seen_ids(self) -> set[int]:
        return {r[0] for r in self.s._all("SELECT project_id FROM seen WHERE tenant=?", (self.t,))}

    def mark_seen(self, pid: int, title: str = "") -> bool:
        """Record a project as reported. False when it was already there."""
        return self.s._write(
            "INSERT OR IGNORE INTO seen(tenant,project_id,title,seen_at) VALUES(?,?,?,?)",
            (self.t, int(pid), title[:300], int(time.time())),
        ) > 0

    def clear_seen(self) -> int:
        return self.s._write("DELETE FROM seen WHERE tenant=?", (self.t,))

    # ------------------------------------------------------------- drafts
    def save_draft(self, pid: int, title: str, amount: float, period: int, description: str) -> None:
        self.s._write(
            """INSERT INTO drafts(tenant,project_id,title,amount,period,description,status,created_at)
               VALUES(?,?,?,?,?,?,'pending',?)
               ON CONFLICT(tenant,project_id) DO UPDATE SET
                 title=excluded.title, amount=excluded.amount, period=excluded.period,
                 description=excluded.description, status='pending', created_at=excluded.created_at""",
            (self.t, int(pid), title[:300], float(amount), int(period), description, int(time.time())),
        )

    def draft(self, pid: int) -> sqlite3.Row | None:
        return self.s._one("SELECT * FROM drafts WHERE tenant=? AND project_id=?",
                           (self.t, int(pid)))

    def pending_drafts(self) -> list[sqlite3.Row]:
        return self.s._all(
            "SELECT * FROM drafts WHERE tenant=? AND status='pending' ORDER BY created_at DESC LIMIT 20",
            (self.t,))

    def set_draft_status(self, pid: int, status: str, bid_id: int | None = None) -> None:
        self.s._write(
            "UPDATE drafts SET status=?, sent_at=?, bid_id=? WHERE tenant=? AND project_id=?",
            (status, int(time.time()) if status == "sent" else None, bid_id, self.t, int(pid)),
        )

    # --------------------------------------------------------------- bids
    def record_bid(self, bid_id: int, pid: int, amount: float, period: int) -> None:
        self.s._write(
            "INSERT OR REPLACE INTO bids(tenant,bid_id,project_id,amount,period,placed_at) "
            "VALUES(?,?,?,?,?,?)",
            (self.t, int(bid_id), int(pid), float(amount), int(period), int(time.time())),
        )

    def bid_count(self) -> int:
        row = self.s._one("SELECT COUNT(*) FROM bids WHERE tenant=?", (self.t,))
        return row[0] if row else 0

    # --------------------------------------------------------------- meta
    def get_meta(self, k: str, default=None):
        row = self.s._one("SELECT v FROM meta WHERE tenant=? AND k=?", (self.t, k))
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    def set_meta(self, k: str, v) -> None:
        self.s._write(
            "INSERT INTO meta(tenant,k,v) VALUES(?,?,?) ON CONFLICT(tenant,k) DO UPDATE SET v=excluded.v",
            (self.t, k, json.dumps(v, ensure_ascii=False)),
        )

    def stats(self) -> dict:
        def count(sql: str) -> int:
            row = self.s._one(sql, (self.t,))
            return row[0] if row else 0
        return {
            "seen": count("SELECT COUNT(*) FROM seen WHERE tenant=?"),
            "pending": count("SELECT COUNT(*) FROM drafts WHERE tenant=? AND status='pending'"),
            "drafts": count("SELECT COUNT(*) FROM drafts WHERE tenant=?"),
            "bids": count("SELECT COUNT(*) FROM bids WHERE tenant=?"),
        }
