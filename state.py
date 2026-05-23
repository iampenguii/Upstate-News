"""SQLite state wrapper for the Upstate SC feed bot.

One table per feed type tracks which items have already been posted, keyed by
a stable GUID, so nothing is posted twice. A source_errors table tracks
consecutive fetch failures so the bot can warn #bot-status when a feed has
been down too long. A runs table records per-run counters so the weekly
heartbeat can aggregate them cheaply.

The database file is small and is meant to live in the GitHub Actions cache,
not in the repository. Keeping it out of git also keeps item titles out of
git history. See the README.

Timestamps are stored as ISO 8601 strings in UTC (with a +00:00 offset).
Because every row uses the same format and zone, lexical string comparison is
also chronological, which keeps pruning queries simple.
"""

from __future__ import annotations

import sqlite3
import datetime as dt


# One de-duplication table per feed type. Same schema for each.
ITEM_TABLES = ("news_items", "weather_alerts", "meetings", "substack_items")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class State:
    """Thin wrapper around the SQLite state database."""

    def __init__(self, path: str):
        self.path = path
        # check_same_thread is fine to leave default; the bot is single-threaded.
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        cur = self.conn.cursor()
        for table in ITEM_TABLES:
            # title is stored for debugging only; the bot never keys off it.
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    guid       TEXT PRIMARY KEY,
                    source     TEXT,
                    posted_at  TIMESTAMP,
                    title      TEXT
                )
                """
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS source_errors (
                source               TEXT PRIMARY KEY,
                consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                first_failure_at      TIMESTAMP,
                last_error            TEXT,
                last_error_at         TIMESTAMP,
                last_alerted_at       TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at          TIMESTAMP NOT NULL,
                sources_polled  INTEGER NOT NULL DEFAULT 0,
                sources_ok      INTEGER NOT NULL DEFAULT 0,
                sources_failed  INTEGER NOT NULL DEFAULT 0,
                news_posted     INTEGER NOT NULL DEFAULT 0,
                weather_posted  INTEGER NOT NULL DEFAULT 0,
                meeting_posted  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.commit()

    # ----- item de-duplication ---------------------------------------------

    def is_seen(self, table: str, guid: str) -> bool:
        """True if this GUID has already been posted from this feed type."""
        if table not in ITEM_TABLES:
            raise ValueError(f"unknown table: {table}")
        row = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE guid = ?", (guid,)
        ).fetchone()
        return row is not None

    def mark_seen(self, table: str, guid: str, source: str, title: str) -> None:
        """Record a GUID as posted. Safe to call twice (INSERT OR IGNORE)."""
        if table not in ITEM_TABLES:
            raise ValueError(f"unknown table: {table}")
        self.conn.execute(
            f"INSERT OR IGNORE INTO {table} (guid, source, posted_at, title) "
            f"VALUES (?, ?, ?, ?)",
            (guid, source, _now_iso(), (title or "")[:500]),
        )
        self.conn.commit()

    # ----- per-source error tracking ---------------------------------------

    def record_failure(self, source: str, error: str) -> dict:
        """Increment the failure counter for a source.

        Returns a dict describing the streak: consecutive_failures,
        first_failure_at, last_alerted_at. main.py uses it to decide whether
        to post a sustained-outage notice to #bot-status.
        """
        now = _now_iso()
        error = (error or "")[:1000]
        row = self.conn.execute(
            "SELECT * FROM source_errors WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO source_errors "
                "(source, consecutive_failures, first_failure_at, last_error, "
                " last_error_at, last_alerted_at) "
                "VALUES (?, 1, ?, ?, ?, NULL)",
                (source, now, error, now),
            )
            self.conn.commit()
            return {
                "consecutive_failures": 1,
                "first_failure_at": now,
                "last_alerted_at": None,
            }
        new_count = row["consecutive_failures"] + 1
        self.conn.execute(
            "UPDATE source_errors SET consecutive_failures = ?, last_error = ?, "
            "last_error_at = ? WHERE source = ?",
            (new_count, error, now, source),
        )
        self.conn.commit()
        return {
            "consecutive_failures": new_count,
            "first_failure_at": row["first_failure_at"],
            "last_alerted_at": row["last_alerted_at"],
        }

    def record_success(self, source: str) -> None:
        """Clear a source's failure streak after a successful fetch."""
        self.conn.execute("DELETE FROM source_errors WHERE source = ?", (source,))
        self.conn.commit()

    def set_alerted(self, source: str, when_iso: str) -> None:
        """Record that a sustained-outage notice was posted for this source."""
        self.conn.execute(
            "UPDATE source_errors SET last_alerted_at = ? WHERE source = ?",
            (when_iso, source),
        )
        self.conn.commit()

    def get_failures(self) -> list:
        """All sources currently in a failure streak, oldest failure first."""
        return self.conn.execute(
            "SELECT * FROM source_errors ORDER BY first_failure_at"
        ).fetchall()

    # ----- per-run stats (for the weekly heartbeat) ------------------------

    def record_run(
        self,
        sources_polled: int,
        sources_ok: int,
        sources_failed: int,
        news_posted: int,
        weather_posted: int,
        meeting_posted: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO runs (run_at, sources_polled, sources_ok, "
            "sources_failed, news_posted, weather_posted, meeting_posted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _now_iso(),
                sources_polled,
                sources_ok,
                sources_failed,
                news_posted,
                weather_posted,
                meeting_posted,
            ),
        )
        self.conn.commit()

    def runs_since(self, since_iso: str) -> list:
        """All run rows at or after the given ISO timestamp, oldest first."""
        return self.conn.execute(
            "SELECT * FROM runs WHERE run_at >= ? ORDER BY run_at", (since_iso,)
        ).fetchall()

    # ----- maintenance -----------------------------------------------------

    def prune(self, days: int) -> int:
        """Delete seen-item rows and run rows older than `days`.

        Returns the number of seen-item rows deleted.
        """
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        ).isoformat()
        deleted = 0
        for table in ITEM_TABLES:
            cur = self.conn.execute(
                f"DELETE FROM {table} WHERE posted_at < ?", (cutoff,)
            )
            deleted += cur.rowcount
        self.conn.execute("DELETE FROM runs WHERE run_at < ?", (cutoff,))
        self.conn.commit()
        return deleted

    def close(self) -> None:
        self.conn.close()
