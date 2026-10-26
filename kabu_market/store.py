"""Minimal SQLite store the game runs on (a host bot can pass its own compatible store).

The game needs exactly: ``db`` (an sqlite3 connection with ``sqlite3.Row`` rows),
``transaction()`` (a non-nesting BEGIN IMMEDIATE context) and ``get`` / ``put`` on a
small key-value table. Transactions are synchronous and must not contain awaits.
"""

import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
        )

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            raise RuntimeError("nested_transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value, now):
        self.db.execute(
            "INSERT INTO app_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now),
        )

    def close(self):
        self.db.close()
