"""Lightweight health reporting for the deployed EdgeDash system.

Read-only. No writes, no LLM calls, no new services.

Checks
------
  db_reachable        database is up and cycle_log is accessible
  data_freshness      newest listing is not older than 3 days
  cycle_recency       a successful cycle ran within 48 hours
  verification_streak last 3 orchestrator cycles all passed verification

Each check is a pure function returning HealthResult(name, ok, observed, message).
run_all() aggregates them and returns SystemHealth.

CLI
---
  python -m edgedash.health

  Exits 0 when all checks pass, 1 when any fail. Prints one line per check
  so a failing GitHub Actions step shows a clear diff between runs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HealthResult:
    name:     str
    ok:       bool
    observed: str   # concise human-readable value that was measured
    message:  str   # why it passed or failed


@dataclass(frozen=True)
class SystemHealth:
    ok:      bool
    checks:  list[HealthResult]
    summary: str


# ---------------------------------------------------------------------------
# ANSI helpers — disabled on non-TTY (e.g. piped GitHub Actions log)
# ---------------------------------------------------------------------------

_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def _green(t: str) -> str: return _c("32", t)
def _red(t: str)   -> str: return _c("31", t)
def _yellow(t: str)-> str: return _c("33", t)
def _bold(t: str)  -> str: return _c("1",  t)
def _dim(t: str)   -> str: return _c("2",  t)


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _hours_ago(ts: str | None, now: datetime) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600


# ---------------------------------------------------------------------------
# Individual checks (pure functions — no DB access, all data passed in)
# ---------------------------------------------------------------------------

def check_db_reachable(total_listings: int | None) -> HealthResult:
    """Fail when the DB could not be queried (total_listings is None)."""
    name = "db_reachable"
    if total_listings is None:
        return HealthResult(
            name=name, ok=False,
            observed="unreachable",
            message="Database could not be queried — check DATABASE_URL and connectivity.",
        )
    return HealthResult(
        name=name, ok=True,
        observed=f"{total_listings} listing(s) in DB",
        message="Database is reachable.",
    )


def check_data_freshness(
    latest_fetch_at: str | None,
    now: datetime,
    max_age_days: float = 3.0,
) -> HealthResult:
    """Fail when the newest listing is older than max_age_days."""
    name = "data_freshness"
    age  = _hours_ago(latest_fetch_at, now)

    if age is None:
        return HealthResult(
            name=name, ok=False,
            observed="no listings fetched yet",
            message="No listings in the database — fetcher has never run successfully.",
        )

    age_days = age / 24
    ok       = age_days <= max_age_days
    observed = f"{age_days:.1f} day(s) since last fetch"

    if ok:
        message = f"Data is fresh ({observed}, threshold {max_age_days}d)."
    else:
        message = (
            f"Data is stale ({observed}, threshold {max_age_days}d). "
            "The fetcher may not have run recently."
        )
    return HealthResult(name=name, ok=ok, observed=observed, message=message)


def check_cycle_recency(
    recent_cycles: list[dict[str, Any]],
    now: datetime,
    max_age_hours: float = 48.0,
) -> HealthResult:
    """Fail when no successful (complete/partial) cycle ran within max_age_hours."""
    name = "cycle_recency"

    # Find the most recent cycle that is not degraded/failed
    successful = [
        r for r in recent_cycles
        if r.get("status") in ("complete", "partial")
    ]

    if not successful:
        return HealthResult(
            name=name, ok=False,
            observed="no successful cycle ever",
            message="No completed or partial cycle found in the log.",
        )

    last = successful[0]
    age  = _hours_ago(last.get("finished_at"), now)

    if age is None:
        return HealthResult(
            name=name, ok=False,
            observed="unknown timestamp",
            message="Most recent successful cycle has no finished_at timestamp.",
        )

    ok       = age <= max_age_hours
    observed = f"{age:.1f}h ago"

    if ok:
        message = f"Last successful cycle {observed} (threshold {max_age_hours}h)."
    else:
        message = (
            f"Last successful cycle was {observed}, "
            f"exceeding the {max_age_hours}h threshold. "
            "The scheduled job may have failed or been skipped."
        )
    return HealthResult(name=name, ok=ok, observed=observed, message=message)


def check_verification_streak(
    recent_cycles: list[dict[str, Any]],
    streak_length: int = 3,
) -> HealthResult:
    """Fail when the last `streak_length` cycles all failed verification."""
    name = "verification_streak"

    if not recent_cycles:
        return HealthResult(
            name=name, ok=True,
            observed="no cycles yet",
            message="Trivially passed — no cycles to evaluate.",
        )

    # Look at only the most recent streak_length cycles
    window  = recent_cycles[:streak_length]
    verdicts = []
    for cy in window:
        notes = cy.get("notes") or ""
        if "VERDICT: pass" in notes:
            verdicts.append("pass")
        elif cy.get("status") in ("complete", "partial") and "VERDICT" not in notes:
            verdicts.append("unknown")
        else:
            verdicts.append("fail")

    pass_count = verdicts.count("pass")
    all_failed = pass_count == 0 and len(window) >= streak_length
    observed   = f"{pass_count}/{len(window)} passed in last {streak_length}"

    if all_failed:
        return HealthResult(
            name=name, ok=False,
            observed=observed,
            message=(
                f"All of the last {streak_length} cycles failed verification. "
                "Check the activity log for the failing check name."
            ),
        )
    return HealthResult(
        name=name, ok=True,
        observed=observed,
        message=f"Verification streak OK ({observed}).",
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def run_all(
    db: str,
    now: datetime | None = None,
) -> SystemHealth:
    """Query the DB and run all four checks. Never raises.

    Returns SystemHealth with ok=False when the DB is unreachable, so the
    caller always gets a result it can act on.
    """
    import edgedash.storage as storage  # deferred — avoids import-time side effects

    if now is None:
        now = datetime.now(timezone.utc)

    # ── Gather data (all reads; any failure → None) ───────────────────────
    try:
        total = storage.count_total(db)
    except Exception:
        total = None

    try:
        fetch_time = storage.last_fetch_time(db)
    except Exception:
        fetch_time = None

    try:
        recent_cycles = storage.get_recent_orchestrator_cycles(db, limit=10)
    except Exception:
        recent_cycles = []

    # ── Run checks ────────────────────────────────────────────────────────
    results = [
        check_db_reachable(total),
        check_data_freshness(fetch_time, now),
        check_cycle_recency(recent_cycles, now),
        check_verification_streak(recent_cycles),
    ]

    failed  = [r for r in results if not r.ok]
    ok      = len(failed) == 0
    summary = (
        f"All {len(results)} checks passed."
        if ok
        else f"{len(failed)}/{len(results)} check(s) failed: "
             + ", ".join(r.name for r in failed)
    )
    return SystemHealth(ok=ok, checks=results, summary=summary)


# ---------------------------------------------------------------------------
# Status for the dashboard — a single (colour, label) pair
# ---------------------------------------------------------------------------

def dashboard_status(db: str, now: datetime | None = None) -> tuple[str, str]:
    """Return (colour_hex, label) for the one-line status bar in app.py.

    Returns a safe fallback if anything fails (rule 50 — health reporting
    must never take the dashboard down).

    Colour semantics:
      #4ade80  green  — last verified cycle passed within 24 h
      #fbbf24  amber  — last verified cycle passed but is 24–48 h old
      #f87171  red    — last 3 cycles all failed, or no data at all
    """
    try:
        import edgedash.storage as storage
        if now is None:
            now = datetime.now(timezone.utc)

        recent = storage.get_recent_orchestrator_cycles(db, limit=3)

        # All three failed → red
        if recent:
            verdicts = [
                "pass" if "VERDICT: pass" in (r.get("notes") or "") else "fail"
                for r in recent
            ]
            if all(v == "fail" for v in verdicts):
                return "#f87171", "system degraded — last 3 cycles failed"

        # How long since last verified cycle?
        verified = storage.get_last_verified_cycle(db)
        if verified is None:
            return "#f87171", "no verified cycle yet"

        age_h = _hours_ago(verified.get("finished_at"), now) or 999

        if age_h <= 24:
            ts = _parse_iso(verified.get("finished_at"))
            label = "live" + (f" · verified {ts.strftime('%d %b %H:%M') if ts else ''} UTC")
            return "#4ade80", label
        if age_h <= 48:
            return "#fbbf24", f"stale · last verified {age_h:.0f}h ago"
        return "#f87171", f"very stale · last verified {age_h:.0f}h ago"

    except Exception:
        return "#374151", "health check unavailable"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_check(r: HealthResult) -> str:
    icon    = _green("✓") if r.ok else _red("✗")
    name    = _bold(f"{r.name:<24}")
    obs     = _dim(f"[{r.observed}]")
    msg     = r.message if r.ok else _red(r.message)
    return f"  {icon}  {name}  {obs}  {msg}"


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    try:
        from edgedash.config import load_config
        db = load_config().db_path
    except Exception:
        db = "edgedash.db"

    now    = datetime.now(timezone.utc)
    health = run_all(db, now=now)

    print()
    print(_bold("  EdgeDash — health check"))
    print(_dim(f"  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}  ·  db: {db}"))
    print()
    for r in health.checks:
        print(_fmt_check(r))
    print()
    print(f"  {'  ' + _green(health.summary) if health.ok else '  ' + _red(health.summary)}")
    print()

    sys.exit(0 if health.ok else 1)


if __name__ == "__main__":
    main()
