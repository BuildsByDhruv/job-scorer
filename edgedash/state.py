"""System state inspection — read-only, deterministic, no LLM.

Public API
----------
read_state(config, now) -> SystemState

`now` is a required parameter so callers control the clock and the function
is fully testable without patching datetime.
All DB access goes through the storage module (rule 2).
All queries are cheap: counts and MAX(timestamp), no full table loads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.config import Config


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime.

    Returns None for a None or unparseable input rather than raising —
    missing timestamps are a valid state (e.g. first-ever run).
    """
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _hours_since(ts: str | None, now: datetime) -> float | None:
    """Return the number of hours between *ts* and *now*, or None if ts is None."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    delta = now - dt
    return delta.total_seconds() / 3600


@dataclass
class SystemState:
    """A snapshot of system state at a single point in time.

    All fields are derived from cheap DB queries plus arithmetic on timestamps.
    `now` records when the snapshot was taken so callers can log it.
    """

    now: datetime

    # Fetch state
    last_fetch_at: str | None       # raw ISO string from DB, None if never fetched
    hours_since_fetch: float | None # None if never fetched

    # Scoring state
    unscored_count: int

    # Gap analysis state
    gaps_computed_at: str | None    # ISO string of most recent gap snapshot
    gaps_stale: bool                # True if any score is newer than gap snapshot

    # Last cycle outcome
    last_cycle_verdict: str | None  # "ok" / "failed" / "partial" / None
    last_cycle_at: str | None       # finished_at of the most recent cycle_log row


def read_state(config: Config, now: datetime) -> SystemState:
    """Query the DB and return a SystemState snapshot.

    Parameters
    ----------
    config:
        Project config; only config.db_path is used here.
    now:
        The current time, supplied by the caller — never datetime.now()
        inside this function so tests can control the clock exactly.
    """
    db = config.db_path

    last_fetch_at     = storage.last_fetch_time(db)
    hours_since_fetch = _hours_since(last_fetch_at, now)
    unscored_count    = storage.count_unscored(db)
    last_score_at     = storage.last_score_time(db)
    gaps_computed_at  = storage.last_gap_snapshot_time(db)

    # gaps_stale: True when there is no gap snapshot at all, OR when a
    # successful scorer run finished AFTER the most recent gap snapshot.
    if gaps_computed_at is None:
        gaps_stale = True
    elif last_score_at is not None:
        gaps_stale = last_score_at > gaps_computed_at
    else:
        gaps_stale = False

    last_cycle_verdict, last_cycle_at = _last_cycle(db)

    return SystemState(
        now=now,
        last_fetch_at=last_fetch_at,
        hours_since_fetch=hours_since_fetch,
        unscored_count=unscored_count,
        gaps_computed_at=gaps_computed_at,
        gaps_stale=gaps_stale,
        last_cycle_verdict=last_cycle_verdict,
        last_cycle_at=last_cycle_at,
    )


def _last_cycle(db: str) -> tuple[str | None, str | None]:
    """Return (verdict, finished_at) for the most recent cycle_log row."""
    row = storage.get_last_cycle_row(db)
    if row is None:
        return None, None
    return row["status"], row["finished_at"]
