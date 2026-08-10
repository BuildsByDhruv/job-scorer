"""The ONLY module permitted to import sqlite3.

Provides a thin interface over the database so the backend can be swapped
from SQLite to Postgres in a single file change (week 4).
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT NOT NULL,
    url         TEXT NOT NULL,
    description TEXT,
    source      TEXT NOT NULL,
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL,
    fit_score   INTEGER,
    fit_reason  TEXT
);

CREATE TABLE IF NOT EXISTS skill_gaps (
    skill       TEXT PRIMARY KEY,
    frequency   INTEGER NOT NULL DEFAULT 0,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    records_touched INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    notes           TEXT
);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(source: str, url: str) -> str:
    """Return a stable SHA-256 hex digest for a (source, url) pair."""
    payload = f"{source}|{url}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _connect(path: str) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def init_db(path: str) -> None:
    """Create all tables if they do not already exist."""
    with _connect(path) as conn:
        conn.executescript(_DDL)


def upsert_listings(path: str, rows: list[dict[str, Any]]) -> int:
    """Insert listings, ignoring duplicates by primary key.

    Each row must contain: title, company, location, url, source.
    Optional fields: description, posted_at, fit_score, fit_reason.

    Returns the count of genuinely NEW rows inserted.
    """
    if not rows:
        return 0

    fetched_at = _now_utc()
    new_count = 0

    with _connect(path) as conn:
        for row in rows:
            listing_id = _stable_id(row["source"], row["url"])
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO listings
                    (id, title, company, location, url, description,
                     source, posted_at, fetched_at, fit_score, fit_reason)
                VALUES
                    (:id, :title, :company, :location, :url, :description,
                     :source, :posted_at, :fetched_at, :fit_score, :fit_reason)
                """,
                {
                    "id": listing_id,
                    "title": row["title"],
                    "company": row["company"],
                    "location": row["location"],
                    "url": row["url"],
                    "description": row.get("description"),
                    "source": row["source"],
                    "posted_at": row.get("posted_at"),
                    "fetched_at": fetched_at,
                    "fit_score": row.get("fit_score"),
                    "fit_reason": row.get("fit_reason"),
                },
            )
            new_count += cursor.rowcount

    return new_count


def count_unscored(path: str) -> int:
    """Return the number of listings that have not yet been scored."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE fit_score IS NULL"
        ).fetchone()
    return row[0]


def last_fetch_time(path: str) -> str | None:
    """Return the most recent fetched_at timestamp, or None if no rows exist."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM listings"
        ).fetchone()
    return row[0]


def log_cycle(
    path: str,
    agent: str,
    started_at: str,
    finished_at: str,
    records_touched: int,
    status: str,
    notes: str | None = None,
) -> None:
    """Write one row to cycle_log recording the outcome of an agent run."""
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO cycle_log
                (agent, started_at, finished_at, records_touched, status, notes)
            VALUES
                (:agent, :started_at, :finished_at, :records_touched, :status, :notes)
            """,
            {
                "agent": agent,
                "started_at": started_at,
                "finished_at": finished_at,
                "records_touched": records_touched,
                "status": status,
                "notes": notes,
            },
        )


def get_listings(
    path: str,
    limit: int = 100,
    min_score: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch listings for the dashboard, optionally filtered by minimum fit score."""
    query = "SELECT * FROM listings"
    params: list[Any] = []

    if min_score is not None:
        query += " WHERE fit_score >= ?"
        params.append(min_score)

    query += " ORDER BY fetched_at DESC LIMIT ?"
    params.append(limit)

    with _connect(path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Diagnostic queries (read-only)
# ---------------------------------------------------------------------------


def count_total(path: str) -> int:
    """Return the total number of listings."""
    with _connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]


def count_by_source(path: str) -> list[dict[str, Any]]:
    """Return [{"source": str, "count": int}, ...] ordered by count desc."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS count FROM listings "
            "GROUP BY source ORDER BY count DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def cross_source_duplicates(path: str) -> list[dict[str, Any]]:
    """Return listings where (title, company) appears in more than one source.

    Each returned row has: title, company, sources (comma-separated), count.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
                title,
                company,
                COUNT(DISTINCT source) AS source_count,
                GROUP_CONCAT(DISTINCT source) AS sources
            FROM listings
            GROUP BY LOWER(title), LOWER(company)
            HAVING COUNT(DISTINCT source) > 1
            ORDER BY source_count DESC, title
            """
        ).fetchall()
    return [dict(r) for r in rows]


def recent_listings(path: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recently fetched listings."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT source, title, company, fetched_at FROM listings "
            "ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def quality_issues(path: str) -> list[dict[str, Any]]:
    """Return listings with a NULL or empty url, title, or company."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, source, title, company, url, fetched_at
            FROM listings
            WHERE
                url     IS NULL OR TRIM(url)     = ''
             OR title   IS NULL OR TRIM(title)   = ''
             OR company IS NULL OR TRIM(company) = ''
            ORDER BY fetched_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
