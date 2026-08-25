"""Read-only verification history viewer.

Usage
-----
    python -m edgedash.verdicts
    python -m edgedash.verdicts --check extraction_sanity
    python -m edgedash.verdicts --limit 10

Reads from cycle_log only through the storage module (rule 2).
No writes. No schema changes.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.config import load_config

# ---------------------------------------------------------------------------
# ANSI colours — disabled automatically on non-TTY (e.g. piped output)
# ---------------------------------------------------------------------------

_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _IS_TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str)  -> str: return _c("32", t)
def _red(t: str)    -> str: return _c("31", t)
def _yellow(t: str) -> str: return _c("33", t)
def _bold(t: str)   -> str: return _c("1",  t)
def _dim(t: str)    -> str: return _c("2",  t)
def _cyan(t: str)   -> str: return _c("36", t)


# ---------------------------------------------------------------------------
# Notes parser — same logic as app.py, kept local so this module is
# self-contained and doesn't import from app.py
# ---------------------------------------------------------------------------

def _parse_notes(notes: str | None) -> dict[str, str]:
    """Extract structured fields from the pipe-delimited orchestrator notes string.

    Returns a plain dict with string values.  Missing keys are absent —
    callers use .get() with a default.
    """
    if not notes:
        return {}

    out: dict[str, str] = {}

    for segment in notes.split("|"):
        segment = segment.strip()
        if ": " in segment:
            k, _, v = segment.partition(": ")
            out[k.strip().lower()] = v.strip()

    # Verdict token
    if "VERDICT: pass" in notes:
        out["verdict"] = "pass"
    elif "VERDICT: degraded" in notes:
        out["verdict"] = "degraded"
        # Extract failing check names if embedded (same format as fail)
        if "VERDICT: degraded — " in notes:
            try:
                tail = notes.split("VERDICT: degraded — ", 1)[1]
                tail = tail.split(" | ")[0]
                check_names: list[str] = []
                observed_vals: list[str] = []
                for seg in tail.split(";"):
                    seg = seg.strip()
                    if " observed" in seg:
                        name = seg.split(" observed")[0].strip()
                        obs  = seg.split(" observed", 1)[1].strip()
                        check_names.append(name)
                        observed_vals.append(f"{name}: {obs}")
                out["failed_checks"] = ", ".join(check_names)
                out["observed_vals"] = " | ".join(observed_vals)
            except (IndexError, ValueError):
                pass
    elif "VERDICT: fail" in notes:
        out["verdict"] = "fail"
        # Extract all failing check names from
        # "VERDICT: fail — name1 observed ...; name2 observed ..."
        try:
            tail = notes.split("VERDICT: fail — ", 1)[1]
            # Strip any trailing pipe-segment garbage
            tail = tail.split(" | ")[0]
            check_names = []
            observed_vals = []
            for seg in tail.split(";"):
                seg = seg.strip()
                if " observed" in seg:
                    name = seg.split(" observed")[0].strip()
                    obs  = seg.split(" observed", 1)[1].strip()
                    check_names.append(name)
                    observed_vals.append(f"{name}: {obs}")
            out["failed_checks"]  = ", ".join(check_names)
            out["observed_vals"]  = " | ".join(observed_vals)
        except (IndexError, ValueError):
            pass
    elif "VERDICT: n/a" in notes:
        out["verdict"] = "n/a"

    return out


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def _verdict_display(verdict: str, status: str) -> str:
    """Return a coloured verdict string."""
    v = verdict or status
    if v == "pass":
        return _green("pass")
    if v == "degraded":
        return _red("degraded")
    if v in ("fail", "failed"):
        return _red("fail")
    if v in ("partial",):
        return _yellow("partial")
    if v in ("n/a", "nothing_to_do"):
        return _dim("n/a")
    return _dim(v)


# ---------------------------------------------------------------------------
# Table renderer
# ---------------------------------------------------------------------------

_COL_TS       = 19
_COL_VERDICT  =  8   # "degraded" is 8 chars
_COL_RETRIES  =  3
_COL_AGENTS   = 32
_COL_CHECKS   = 48


def _header_line() -> str:
    return (
        _bold(
            f"  {'TIMESTAMP':<{_COL_TS}}  "
            f"{'VERDICT':<{_COL_VERDICT}}  "
            f"{'RTY':>{_COL_RETRIES}}  "
            f"{'AGENTS RAN':<{_COL_AGENTS}}  "
            f"FAILED CHECKS"
        )
    )


def _separator() -> str:
    return _dim(
        f"  {'─' * _COL_TS}  "
        f"{'─' * _COL_VERDICT}  "
        f"{'─' * _COL_RETRIES}  "
        f"{'─' * _COL_AGENTS}  "
        f"{'─' * 40}"
    )


def _render_row(row: dict) -> str:
    notes   = _parse_notes(row.get("notes"))
    verdict = notes.get("verdict", "")
    status  = row.get("status", "")

    ts      = _fmt_ts(row.get("finished_at") or row.get("started_at"))
    retries = notes.get("retries", "0").strip()
    agents  = notes.get("ran", "—")

    # Use embedded check names if present; fall back to backfilled values
    # for older rows written before the orchestrator embedded them.
    checks = notes.get("failed_checks") or row.get("_backfilled_checks", "")
    obs    = notes.get("observed_vals")  or row.get("_backfilled_obs",    "")

    verdict_str = _verdict_display(verdict, status)

    # Truncate agents column so the table stays readable
    if len(agents) > _COL_AGENTS:
        agents = agents[: _COL_AGENTS - 1] + "…"

    main_line = (
        f"  {ts:<{_COL_TS}}  "
        f"{verdict_str:<{_COL_VERDICT}}  "
        f"{retries:>{_COL_RETRIES}}  "
        f"{agents:<{_COL_AGENTS}}  "
        f"{checks}"
    )

    # For failures, add a second indented line with the observed values
    # so the operator sees exactly what tripped the threshold.
    if obs and verdict in ("fail", "degraded"):
        # Wrap observed_vals at 90 chars across continuation lines
        obs_line = _dim(f"  {'':>{_COL_TS + _COL_VERDICT + _COL_RETRIES + _COL_AGENTS + 8}}{obs}")
        return main_line + "\n" + obs_line
    return main_line


# ---------------------------------------------------------------------------
# Summary line
# ---------------------------------------------------------------------------

def _render_summary(
    rows: list[dict],
    check_filter: str | None,
) -> str:
    total   = len(rows)
    if total == 0:
        return _dim("  No cycles to summarise.")

    verdicts = [_parse_notes(r.get("notes")).get("verdict", r.get("status", "")) for r in rows]
    passes   = sum(1 for v in verdicts if v == "pass")
    pass_pct = 100 * passes // total

    # Count how often each check name appears in failures
    check_counter: Counter[str] = Counter()
    for r in rows:
        notes = _parse_notes(r.get("notes"))
        failed = notes.get("failed_checks") or r.get("_backfilled_checks", "")
        if failed:
            for name in failed.split(","):
                check_counter[name.strip()] += 1

    pass_str  = _green(f"{passes}/{total} passed ({pass_pct}%)")
    label     = "─" * 58

    if check_filter:
        header = _bold(f"  Filter: cycles where '{check_filter}' failed")
    else:
        header = _bold("  Summary (last %d cycles)" % total)

    lines = [
        _dim(f"  {label}"),
        header,
        f"  Pass rate : {pass_str}",
    ]

    if check_counter:
        top_check, top_count = check_counter.most_common(1)[0]
        top_str = _red(f"{top_check}") + _dim(f" ({top_count}x)")
        lines.append(f"  Most failing check : {top_str}")

        if len(check_counter) > 1:
            others = ", ".join(
                f"{name} ({n}x)"
                for name, n in check_counter.most_common()[1:]
            )
            lines.append(_dim(f"  Other failures     : {others}"))
    else:
        lines.append(_dim("  No check failures recorded."))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.verdicts",
        description="Read-only verification history from cycle_log.",
    )
    parser.add_argument(
        "--check", metavar="NAME",
        help=(
            "Filter to cycles where this check failed "
            "(e.g. extraction_sanity, score_spread, freshness, gap_sample_size)"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Number of orchestrator cycles to show (default: 20).",
    )
    args = parser.parse_args(argv)

    try:
        cfg    = load_config()
        db     = cfg.db_path
    except FileNotFoundError as exc:
        print(_red(f"Error: {exc}"), file=sys.stderr)
        sys.exit(1)

    storage.init_db(db)

    rows = storage.get_recent_orchestrator_cycles(
        db,
        limit=args.limit,
        check_filter=args.check,
    )

    # Back-fill failed_checks from verifier rows for older cycles that
    # predate the orchestrator change that embeds check names in the
    # summary row.  One extra query only when needed — most rows will
    # already have the data after the first new cycle runs.
    all_logs_by_id: dict[int, dict] = {}
    needs_backfill  = any(
        not _parse_notes(r.get("notes")).get("failed_checks")
        and _parse_notes(r.get("notes")).get("verdict") in ("fail", "degraded")
        for r in rows
    )

    # When --check is active and results are empty, it may be because
    # the check name only lives in verifier rows (old-style).  Fetch
    # all orchestrator rows and filter by backfilled check names.
    fallback_filter = (
        args.check
        and not rows
    )

    if needs_backfill or fallback_filter:
        all_logs      = storage.get_recent_cycle_logs(db, limit=500)
        verifier_rows = [r for r in all_logs if r["agent"] == "verifier"]

    if fallback_filter:
        # Re-fetch without the SQL filter, then apply manually after backfill.
        rows = storage.get_recent_orchestrator_cycles(db, limit=args.limit)

    for row in rows:
        notes = _parse_notes(row.get("notes"))
        if (
            not notes.get("failed_checks")
            and notes.get("verdict") in ("fail", "degraded")
            and (needs_backfill or fallback_filter)
        ):
            # Find the verifier row with the highest id less than this
            # orchestrator/cycle row — that's the verifier run for this cycle.
            cycle_id   = row["id"]
            candidates = [
                v for v in verifier_rows
                if v["id"] < cycle_id and v.get("status") == "failed"
            ]
            if candidates:
                best    = max(candidates, key=lambda r: r["id"])
                v_notes = _parse_notes(best.get("notes"))
                if v_notes.get("failed_checks"):
                    row["_backfilled_checks"] = v_notes.get("failed_checks", "")
                    row["_backfilled_obs"]    = v_notes.get("observed_vals", "")

    # Apply manual --check filter on backfilled rows
    if fallback_filter and args.check:
        rows = [
            r for r in rows
            if args.check in (
                _parse_notes(r.get("notes")).get("failed_checks", "")
                + r.get("_backfilled_checks", "")
            )
        ]
        rows = rows[: args.limit]

    # ── Header ──────────────────────────────────────────────────────────────
    title = "EdgeDash — Verification History"
    if args.check:
        title += f"  [filter: {args.check}]"
    print()
    print(_bold(_cyan(f"  {title}")))
    print(_dim(f"  db: {db}  |  showing up to {args.limit} orchestrator cycles"))
    print()

    if not rows:
        if args.check:
            print(_dim(f"  No cycles found where '{args.check}' failed."))
        else:
            print(_dim("  No orchestrator cycles in cycle_log yet."))
            print(_dim("  Run:  python run_cycle.py"))
        print()
        return

    # ── Table ────────────────────────────────────────────────────────────────
    print(_header_line())
    print(_separator())
    for row in rows:
        print(_render_row(row))
    print()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(_render_summary(rows, args.check))
    print()


if __name__ == "__main__":
    main()
