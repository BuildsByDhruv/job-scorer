"""Single storage module — the ONLY file permitted to import sqlite3 or psycopg2.

Backend selection (rule 2, rule 47)
------------------------------------
  DATABASE_URL set in environment  →  Postgres (hosted, survives restarts)
  DATABASE_URL absent              →  SQLite   (local dev, uses `path` arg)

Which backend is active is printed once at module import via _log_backend().
Every function signature is identical regardless of backend — callers never
know or care which one is running.

Week-4 swap is complete.  To move back to SQLite: unset DATABASE_URL.

CLI
---
  python -m edgedash.storage --migrate   create/update all tables (idempotent)
  python -m edgedash.storage --check     print backend, connectivity, row counts
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Backend detection — reads DATABASE_URL once at import time (rule 48)
# Never prints the URL itself; only logs the backend type.
# ---------------------------------------------------------------------------

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
_BACKEND: str = "postgres" if _DATABASE_URL else "sqlite"


def _log_backend() -> None:
    label = (
        f"postgres  (DATABASE_URL is set)"
        if _BACKEND == "postgres"
        else "sqlite    (DATABASE_URL not set — local dev mode)"
    )
    print(f"[storage] backend: {label}", flush=True)


_log_backend()


# ---------------------------------------------------------------------------
# Placeholder style per backend
# ---------------------------------------------------------------------------

def _ph(n: int = 1) -> str:
    """Return the correct positional placeholder for the active backend."""
    return "%s" if _BACKEND == "postgres" else "?"


def _phs(*args: Any) -> str:
    """Comma-separated placeholders for N values."""
    ph = "%s" if _BACKEND == "postgres" else "?"
    return ", ".join(ph for _ in args)


# ---------------------------------------------------------------------------
# DDL — expressed in the common subset; dialect patches applied at runtime
# ---------------------------------------------------------------------------

# Tables in dependency order so --migrate can run them sequentially.
_TABLES: list[tuple[str, str]] = [
    ("listings", """
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
        )
    """),
    ("skill_gaps", """
        CREATE TABLE IF NOT EXISTS skill_gaps (
            skill       TEXT PRIMARY KEY,
            frequency   INTEGER NOT NULL DEFAULT 0,
            last_seen   TEXT NOT NULL
        )
    """),
    ("cycle_log", """
        CREATE TABLE IF NOT EXISTS cycle_log (
            id              {serial} PRIMARY KEY,
            agent           TEXT    NOT NULL,
            started_at      TEXT    NOT NULL,
            finished_at     TEXT,
            records_touched INTEGER NOT NULL DEFAULT 0,
            status          TEXT    NOT NULL,
            notes           TEXT
        )
    """),
    ("extraction_cache", """
        CREATE TABLE IF NOT EXISTS extraction_cache (
            description_hash TEXT PRIMARY KEY,
            extracted_at     TEXT NOT NULL,
            required_skills  TEXT NOT NULL DEFAULT '[]',
            nice_to_have     TEXT NOT NULL DEFAULT '[]',
            seniority        TEXT NOT NULL DEFAULT 'unknown',
            years_required   TEXT,
            remote_ok        TEXT
        )
    """),
    ("skill_gap_snapshots", """
        CREATE TABLE IF NOT EXISTS skill_gap_snapshots (
            id                {serial} PRIMARY KEY,
            run_id            TEXT    NOT NULL,
            computed_at       TEXT    NOT NULL,
            skill             TEXT    NOT NULL,
            listings_blocked  INTEGER NOT NULL,
            opportunity_cost  REAL    NOT NULL,
            mean_score        REAL    NOT NULL,
            top_score         INTEGER NOT NULL,
            also_nice_to_have INTEGER NOT NULL DEFAULT 0,
            low_confidence    INTEGER NOT NULL DEFAULT 0,
            example_ids       TEXT    NOT NULL DEFAULT '[]'
        )
    """),
    ("query_log", """
        CREATE TABLE IF NOT EXISTS query_log (
            id           {serial} PRIMARY KEY,
            asked_at     TEXT    NOT NULL,
            question     TEXT    NOT NULL,
            tool_used    TEXT,
            params_json  TEXT    NOT NULL DEFAULT '{}',
            answerable   INTEGER NOT NULL DEFAULT 0,
            duration_s   REAL    NOT NULL DEFAULT 0.0,
            error        TEXT
        )
    """),
]


def _serial_token() -> str:
    return "SERIAL" if _BACKEND == "postgres" else "INTEGER"


def _render_ddl(sql: str) -> str:
    return sql.replace("{serial}", _serial_token())


# ---------------------------------------------------------------------------
# Connection context managers
# ---------------------------------------------------------------------------

@contextmanager
def _connect_sqlite(path: str) -> Generator[Any, None, None]:
    import sqlite3
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _connect_pg() -> Generator[Any, None, None]:
    import psycopg2
    import psycopg2.extras
    # Strip connection-pooler hints that psycopg2 does not understand
    # (e.g. ?pgbouncer=true appended by Supabase / PgBouncer URLs).
    # DATABASE_URL itself is never logged (rule 48).
    url = _DATABASE_URL or ""
    if "?" in url:
        url = url.split("?")[0]
    conn = psycopg2.connect(url)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        conn.close()


@contextmanager
def _connect(path: str) -> Generator[Any, None, None]:
    """Yield a unified cursor-like object for the active backend.

    SQLite:  yields the Connection (which is also the cursor for our purposes)
    Postgres: yields a RealDictCursor inside an auto-commit/rollback block

    Both yield objects support .execute(), .executemany(), .fetchone(),
    .fetchall(), and .rowcount.
    """
    if _BACKEND == "postgres":
        with _connect_pg() as cur:
            yield cur
    else:
        with _connect_sqlite(path) as conn:
            yield conn


# ---------------------------------------------------------------------------
# Row normalisation — both backends return row objects; convert to plain dict
# ---------------------------------------------------------------------------

def _row(r: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row or psycopg2 RealDictRow to a plain dict."""
    return dict(r)


def _scalar(row: Any) -> Any:
    """Extract a single scalar value from a one-column fetchone() result.

    Works for both sqlite3.Row (index access) and psycopg2 RealDictRow
    (dict-only access).  Returns None when row is None.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(source: str, url: str) -> str:
    """Return a stable SHA-256 hex digest for a (source, url) pair."""
    return hashlib.sha256(f"{source}|{url}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def init_db(path: str) -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    if _BACKEND == "postgres":
        import psycopg2
        url = (_DATABASE_URL or "").split("?")[0]
        conn = psycopg2.connect(url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for _name, ddl in _TABLES:
                    cur.execute(_render_ddl(ddl))
        finally:
            conn.close()
    else:
        import sqlite3
        conn = sqlite3.connect(path)
        try:
            for _name, ddl in _TABLES:
                conn.execute(_render_ddl(ddl))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def upsert_listings(path: str, rows: list[dict[str, Any]]) -> int:
    """Insert listings, ignoring duplicates. Returns count of new rows."""
    if not rows:
        return 0

    fetched_at = _now_utc()
    new_count  = 0
    ph         = _ph()

    with _connect(path) as cur:
        for row in rows:
            listing_id = _stable_id(row["source"], row["url"])

            if _BACKEND == "postgres":
                sql = f"""
                    INSERT INTO listings
                        (id, title, company, location, url, description,
                         source, posted_at, fetched_at, fit_score, fit_reason)
                    VALUES ({_phs(*range(11))})
                    ON CONFLICT (id) DO NOTHING
                """
            else:
                sql = """
                    INSERT OR IGNORE INTO listings
                        (id, title, company, location, url, description,
                         source, posted_at, fetched_at, fit_score, fit_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """

            cur.execute(sql, (
                listing_id,
                row["title"],
                row["company"],
                row["location"],
                row["url"],
                row.get("description"),
                row["source"],
                row.get("posted_at"),
                fetched_at,
                row.get("fit_score"),
                row.get("fit_reason"),
            ))
            new_count += cur.rowcount

    return new_count


def count_unscored(path: str) -> int:
    with _connect(path) as cur:
        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NULL")
        row = cur.fetchone()
    return _scalar(row)


def last_fetch_time(path: str) -> str | None:
    with _connect(path) as cur:
        cur.execute("SELECT MAX(fetched_at) FROM listings")
        row = cur.fetchone()
    return _scalar(row)  # type: ignore[return-value]


def log_cycle(
    path: str,
    agent: str,
    started_at: str,
    finished_at: str,
    records_touched: int,
    status: str,
    notes: str | None = None,
) -> None:
    ph = _ph()
    sql = f"""
        INSERT INTO cycle_log
            (agent, started_at, finished_at, records_touched, status, notes)
        VALUES ({_phs(*range(6))})
    """
    with _connect(path) as cur:
        cur.execute(sql, (agent, started_at, finished_at,
                          records_touched, status, notes))


def get_listings(
    path: str,
    limit: int = 100,
    min_score: int | None = None,
) -> list[dict[str, Any]]:
    ph = _ph()
    if min_score is not None:
        sql    = f"SELECT * FROM listings WHERE fit_score >= {ph} ORDER BY fetched_at DESC LIMIT {ph}"
        params: tuple = (min_score, limit)
    else:
        sql    = f"SELECT * FROM listings ORDER BY fetched_at DESC LIMIT {ph}"
        params = (limit,)

    with _connect(path) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def get_extraction(path: str, description_hash: str) -> dict[str, Any] | None:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"SELECT required_skills, nice_to_have, seniority, "
            f"years_required, remote_ok "
            f"FROM extraction_cache WHERE description_hash = {ph}",
            (description_hash,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    r = _row(row)
    return {
        "required_skills": json.loads(r["required_skills"]),
        "nice_to_have":    json.loads(r["nice_to_have"]),
        "seniority":       r["seniority"],
        "years_required":  (
            int(r["years_required"]) if r["years_required"] is not None else None
        ),
        "remote_ok": (
            None if r["remote_ok"] is None else r["remote_ok"] == "true"
        ),
    }


def set_extraction(
    path: str,
    description_hash: str,
    data: dict[str, Any],
) -> None:
    years  = data.get("years_required")
    remote = data.get("remote_ok")
    vals   = (
        description_hash,
        _now_utc(),
        json.dumps(data.get("required_skills", [])),
        json.dumps(data.get("nice_to_have", [])),
        data.get("seniority", "unknown"),
        str(years) if years is not None else None,
        ("true" if remote else "false") if remote is not None else None,
    )

    if _BACKEND == "postgres":
        sql = f"""
            INSERT INTO extraction_cache
                (description_hash, extracted_at, required_skills, nice_to_have,
                 seniority, years_required, remote_ok)
            VALUES ({_phs(*range(7))})
            ON CONFLICT (description_hash) DO UPDATE SET
                extracted_at    = EXCLUDED.extracted_at,
                required_skills = EXCLUDED.required_skills,
                nice_to_have    = EXCLUDED.nice_to_have,
                seniority       = EXCLUDED.seniority,
                years_required  = EXCLUDED.years_required,
                remote_ok       = EXCLUDED.remote_ok
        """
    else:
        sql = """
            INSERT OR REPLACE INTO extraction_cache
                (description_hash, extracted_at, required_skills, nice_to_have,
                 seniority, years_required, remote_ok)
            VALUES (?,?,?,?,?,?,?)
        """
    with _connect(path) as cur:
        cur.execute(sql, vals)


def get_unscored_listings(path: str, limit: int) -> list[dict[str, Any]]:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"SELECT * FROM listings WHERE fit_score IS NULL "
            f"ORDER BY fetched_at ASC LIMIT {ph}",
            (limit,),
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def write_score(
    path: str,
    listing_id: str,
    score: int,
    reason: str,
    components: dict[str, Any],
    scored_at: str,
) -> None:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"UPDATE listings SET fit_score = {ph}, fit_reason = {ph} WHERE id = {ph}",
            (score, reason, listing_id),
        )


# ---------------------------------------------------------------------------
# Re-scoring support
# ---------------------------------------------------------------------------


def clear_score_all(path: str) -> int:
    with _connect(path) as cur:
        cur.execute("UPDATE listings SET fit_score = NULL, fit_reason = NULL")
        return cur.rowcount


def clear_score_one(path: str, listing_id: str) -> int:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"UPDATE listings SET fit_score = NULL, fit_reason = NULL WHERE id = {ph}",
            (listing_id,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Diagnostic queries (read-only)
# ---------------------------------------------------------------------------


def count_total(path: str) -> int:
    with _connect(path) as cur:
        cur.execute("SELECT COUNT(*) FROM listings")
        return _scalar(cur.fetchone())


def count_by_source(path: str) -> list[dict[str, Any]]:
    with _connect(path) as cur:
        cur.execute(
            "SELECT source, COUNT(*) AS count FROM listings "
            "GROUP BY source ORDER BY count DESC"
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def cross_source_duplicates(path: str) -> list[dict[str, Any]]:
    if _BACKEND == "postgres":
        sql = """
            SELECT
                title,
                company,
                COUNT(DISTINCT source) AS source_count,
                STRING_AGG(DISTINCT source, ',') AS sources
            FROM listings
            GROUP BY LOWER(title), LOWER(company)
            HAVING COUNT(DISTINCT source) > 1
            ORDER BY source_count DESC, title
        """
    else:
        sql = """
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
    with _connect(path) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def recent_listings(path: str, limit: int = 5) -> list[dict[str, Any]]:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"SELECT source, title, company, fetched_at FROM listings "
            f"ORDER BY fetched_at DESC LIMIT {ph}",
            (limit,),
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def quality_issues(path: str) -> list[dict[str, Any]]:
    with _connect(path) as cur:
        cur.execute(
            """
            SELECT id, source, title, company, url, fetched_at
            FROM listings
            WHERE
                url     IS NULL OR TRIM(url)     = ''
             OR title   IS NULL OR TRIM(title)   = ''
             OR company IS NULL OR TRIM(company) = ''
            ORDER BY fetched_at DESC
            """
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Gap snapshot storage (rule 25 — append-only, never overwrite)
# ---------------------------------------------------------------------------


def write_gap_snapshot(
    path: str,
    run_id: str,
    computed_at: str,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0

    params_list = [
        (
            run_id,
            computed_at,
            r["skill"],
            r["listings_blocked"],
            r["opportunity_cost"],
            r["mean_score"],
            r["top_score"],
            r["also_nice_to_have"],
            1 if r["low_confidence"] else 0,
            json.dumps(r["example_ids"]),
        )
        for r in rows
    ]

    sql = f"""
        INSERT INTO skill_gap_snapshots
            (run_id, computed_at, skill, listings_blocked,
             opportunity_cost, mean_score, top_score,
             also_nice_to_have, low_confidence, example_ids)
        VALUES ({_phs(*range(10))})
    """
    with _connect(path) as cur:
        cur.executemany(sql, params_list)
    return len(rows)


def get_latest_gap_snapshot(path: str) -> list[dict[str, Any]]:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            "SELECT run_id FROM skill_gap_snapshots "
            "ORDER BY computed_at DESC LIMIT 1"
        )
        latest = cur.fetchone()
        if latest is None:
            return []
        run_id = _row(latest)["run_id"]
        cur.execute(
            f"""
            SELECT skill, listings_blocked, opportunity_cost,
                   mean_score, top_score, also_nice_to_have,
                   low_confidence, example_ids, computed_at
            FROM skill_gap_snapshots
            WHERE run_id = {ph}
            ORDER BY opportunity_cost DESC
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return [
        {
            **_row(r),
            "example_ids":    json.loads(_row(r)["example_ids"]),
            "low_confidence": bool(_row(r)["low_confidence"]),
        }
        for r in rows
    ]


def get_scored_listings_with_cache(path: str) -> list[dict[str, Any]]:
    """Scored listings joined with extraction cache, Python-side by description hash."""
    with _connect(path) as cur:
        cur.execute(
            "SELECT id, fit_score, description FROM listings "
            "WHERE fit_score IS NOT NULL AND description IS NOT NULL"
        )
        listings = cur.fetchall()
        cur.execute(
            "SELECT description_hash, required_skills, nice_to_have "
            "FROM extraction_cache"
        )
        cache_rows = cur.fetchall()

    cache = {_row(r)["description_hash"]: _row(r) for r in cache_rows}

    result = []
    for listing in listings:
        r    = _row(listing)
        desc = r.get("description") or ""
        h    = hashlib.sha256(desc.encode()).hexdigest()
        if h in cache:
            c = cache[h]
            result.append({
                "id":              r["id"],
                "fit_score":       r["fit_score"],
                "required_skills": json.loads(c["required_skills"] or "[]"),
                "nice_to_have":    json.loads(c["nice_to_have"] or "[]"),
            })
    return result


def get_gap_snapshot_by_run(path: str, run_id: str) -> list[dict[str, Any]]:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"""
            SELECT skill, listings_blocked, opportunity_cost,
                   mean_score, top_score, also_nice_to_have,
                   low_confidence, example_ids, computed_at
            FROM skill_gap_snapshots
            WHERE run_id = {ph}
            ORDER BY opportunity_cost DESC
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return [
        {
            **_row(r),
            "example_ids":    json.loads(_row(r)["example_ids"]),
            "low_confidence": bool(_row(r)["low_confidence"]),
        }
        for r in rows
    ]


def get_snapshot_run_ids(path: str) -> list[dict[str, Any]]:
    with _connect(path) as cur:
        cur.execute(
            """
            SELECT run_id, MIN(computed_at) AS computed_at
            FROM skill_gap_snapshots
            GROUP BY run_id
            ORDER BY computed_at ASC
            """
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def last_score_time(path: str) -> str | None:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"SELECT MAX(finished_at) FROM cycle_log "
            f"WHERE agent = {ph} AND status = {ph}",
            ("scorer", "ok"),
        )
        return _scalar(cur.fetchone())  # type: ignore[return-value]


def last_gap_snapshot_time(path: str) -> str | None:
    with _connect(path) as cur:
        cur.execute("SELECT MAX(computed_at) FROM skill_gap_snapshots")
        return _scalar(cur.fetchone())  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Query log (rules 42-45)
# ---------------------------------------------------------------------------


def log_query(
    path: str,
    asked_at: str,
    question: str,
    tool_used: str | None,
    params_json: str,
    answerable: bool,
    duration_s: float,
    error: str | None = None,
) -> None:
    sql = f"""
        INSERT INTO query_log
            (asked_at, question, tool_used, params_json,
             answerable, duration_s, error)
        VALUES ({_phs(*range(7))})
    """
    with _connect(path) as cur:
        cur.execute(sql, (
            asked_at, question, tool_used, params_json,
            1 if answerable else 0, duration_s, error,
        ))


# ---------------------------------------------------------------------------
# Cycle log reads
# ---------------------------------------------------------------------------


def get_all_required_skills(path: str) -> list[str]:
    """Return every required_skills JSON string from the extraction cache.

    Used by skills.py for audit and alias-suggestion — read-only.
    Returns one JSON string per cache entry; callers parse them.
    """
    with _connect(path) as cur:
        cur.execute("SELECT required_skills FROM extraction_cache")
        rows = cur.fetchall()
    return [_row(r)["required_skills"] for r in rows]


def get_last_cycle_row(path: str) -> dict[str, Any] | None:
    """Return status and finished_at for the most recent cycle_log row.

    Used by state.py to determine the last cycle outcome.
    Returns None when cycle_log is empty.
    """
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            "SELECT status, finished_at FROM cycle_log "
            f"ORDER BY finished_at DESC LIMIT {ph}",
            (1,),
        )
        row = cur.fetchone()
    return _row(row) if row is not None else None


def get_recent_cycle_logs(path: str, limit: int = 30) -> list[dict[str, Any]]:
    ph = _ph()
    with _connect(path) as cur:
        cur.execute(
            f"""
            SELECT id, agent, started_at, finished_at,
                   records_touched, status, notes
            FROM cycle_log
            ORDER BY id DESC
            LIMIT {ph}
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]


def get_recent_orchestrator_cycles(
    path: str,
    limit: int = 20,
    check_filter: str | None = None,
) -> list[dict[str, Any]]:
    ph = _ph()
    if check_filter:
        pattern_obs  = f"%{check_filter} observed%"
        pattern_fail = f"%VERDICT: fail \u2014 {check_filter}%"
        if _BACKEND == "postgres":
            sql    = f"""
                SELECT id, agent, started_at, finished_at,
                       records_touched, status, notes
                FROM cycle_log
                WHERE agent = {ph}
                  AND (notes LIKE {ph} OR notes LIKE {ph})
                ORDER BY id DESC
                LIMIT {ph}
            """
            params: tuple = ("orchestrator/cycle", pattern_obs, pattern_fail, limit)
        else:
            sql    = """
                SELECT id, agent, started_at, finished_at,
                       records_touched, status, notes
                FROM cycle_log
                WHERE agent = ?
                  AND (notes LIKE ? OR notes LIKE ?)
                ORDER BY id DESC
                LIMIT ?
            """
            params = ("orchestrator/cycle", pattern_obs, pattern_fail, limit)
    else:
        sql    = f"""
            SELECT id, agent, started_at, finished_at,
                   records_touched, status, notes
            FROM cycle_log
            WHERE agent = {ph}
            ORDER BY id DESC
            LIMIT {ph}
        """
        params = ("orchestrator/cycle", limit)

    with _connect(path) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Verified-cycle query (rule 38)
# ---------------------------------------------------------------------------


def get_last_verified_cycle(path: str) -> dict[str, Any] | None:
    ph = _ph()
    sql = f"""
        SELECT id, agent, started_at, finished_at,
               records_touched, status, notes
        FROM cycle_log
        WHERE agent  = {ph}
          AND status IN ('complete', 'partial')
          AND notes  LIKE {ph}
        ORDER BY finished_at DESC
        LIMIT 1
    """
    with _connect(path) as cur:
        cur.execute(sql, ("orchestrator/cycle", "%VERDICT: pass%"))
        row = cur.fetchone()
    return _row(row) if row is not None else None


# ---------------------------------------------------------------------------
# CLI — --migrate and --check (rule from spec)
# ---------------------------------------------------------------------------


def _cmd_migrate(path: str) -> None:
    """Create / update all tables. Safe to run on an existing database."""
    print(f"[migrate] backend: {_BACKEND}")
    init_db(path)
    print("[migrate] all tables created or already exist — done.")


def _cmd_check(path: str) -> None:
    """Print backend type, connectivity, and row count per table."""
    print(f"[check] backend : {_BACKEND}")
    if _BACKEND == "postgres":
        print(f"[check] url     : *** (set, not printed — rule 48)")
    else:
        print(f"[check] path    : {path}")

    tables = [name for name, _ in _TABLES]
    try:
        with _connect(path) as cur:
            print("[check] connect : OK")
            for tbl in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    n = _scalar(cur.fetchone())
                    print(f"[check]   {tbl:<30} {n:>8} row(s)")
                except Exception as exc:
                    print(f"[check]   {tbl:<30} ERROR: {exc}")
    except Exception as exc:
        print(f"[check] connect : FAILED — {exc}")
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    # Re-evaluate DATABASE_URL after loading .env
    _DATABASE_URL = os.environ.get("DATABASE_URL")
    _BACKEND      = "postgres" if _DATABASE_URL else "sqlite"

    try:
        from edgedash.config import load_config
        _path = load_config().db_path
    except Exception:
        _path = "edgedash.db"

    parser = argparse.ArgumentParser(prog="python -m edgedash.storage")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--migrate", action="store_true",
                       help="Create all tables (idempotent)")
    group.add_argument("--check",   action="store_true",
                       help="Print backend, connectivity, row counts")
    args = parser.parse_args()

    if args.migrate:
        _cmd_migrate(_path)
    elif args.check:
        _cmd_check(_path)
