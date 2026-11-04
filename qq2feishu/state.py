"""去重与游标状态，持久化在本地 SQLite，进程重启不会重复推送。"""

import os
import sqlite3
from datetime import datetime


class StateStore:
    def __init__(self, path):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                digest     TEXT PRIMARY KEY,
                rec_id     INTEGER,
                status     TEXT,          -- sent | skipped | failed
                attempts   INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.commit()

    # ---------------- meta / 游标

    def get_cursor(self):
        row = self.conn.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
        return int(row["value"]) if row else None

    def set_cursor(self, value):
        self._set_meta("cursor", str(int(value)))

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self._set_meta(key, value)

    def _set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---------------- 去重

    def status_of(self, digest):
        row = self.conn.execute(
            "SELECT status FROM seen WHERE digest=?", (digest,)
        ).fetchone()
        return row["status"] if row else None

    def is_final(self, digest):
        """sent / skipped / failed 都是终态；pending 需要重试。"""
        return self.status_of(digest) in ("sent", "skipped", "failed")

    def mark(self, digest, rec_id, status, attempts=0):
        self.conn.execute(
            "INSERT INTO seen(digest, rec_id, status, attempts, created_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(digest) DO UPDATE SET status=excluded.status, attempts=excluded.attempts",
            (digest, rec_id, status, attempts, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def attempts(self, digest):
        row = self.conn.execute(
            "SELECT attempts FROM seen WHERE digest=?", (digest,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def stats(self):
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM seen GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def prune(self, keep_days=30):
        self.conn.execute(
            "DELETE FROM seen WHERE created_at < datetime('now', ?)",
            (f"-{int(keep_days)} days",),
        )
        self.conn.commit()
