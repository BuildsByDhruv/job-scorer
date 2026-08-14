"""Morning gap report — prints the latest snapshot as a readable terminal table.

Usage:
    python -m edgedash.gaps            # current snapshot
    python -m edgedash.gaps --trend    # change from earliest to latest snapshot
"""

from __future__ import annotations

import sys

import edgedash.storage as storage
from edgedash.config import load_config

# ---------------------------------------------------------------------------
# ANSI helpers (degrade gracefully if the terminal doesn't support colour)
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_GREEN  = "\033[32m"

_BAR_WIDTH  = 20   # character width of the opportunity-cost bar
_TOP_N      = 10   # rows to display


def _bar(value: float, max_value: float) -> str:
    """Return a filled Unicode block bar scaled to _BAR_WIDTH."""
    if max_value <= 0:
        return " " * _BAR_WIDTH
    filled = int(round((value / max_value) * _BAR_WIDTH))
    filled = max(0, min(filled, _BAR_WIDTH))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _confidence_tag(row: dict) -> str:
    n = row["listings_blocked"]
    if row["low_confidence"]:
        return f"{_YELLOW}low-conf (n={n}){_RESET}"
    return f"{_DIM}n={n}{_RESET}"


# ---------------------------------------------------------------------------
# Current-snapshot renderer
# ---------------------------------------------------------------------------


def print_gap_report(rows: list[dict], computed_at: str) -> None:
    if not rows:
        print("No gap snapshot found. Run a full cycle first.")
        return

    display = rows[:_TOP_N]
    max_cost = display[0]["opportunity_cost"] if display else 1.0

    # ── Header ───────────────────────────────────────────────────────────────
    print()
    print(f"{_BOLD}{_CYAN}{'═' * 72}{_RESET}")
    print(f"{_BOLD}{_CYAN}  SKILL GAP REPORT{_RESET}"
          f"{_DIM}  ·  snapshot: {computed_at}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'═' * 72}{_RESET}")
    print()

    # Column widths
    w_skill = max(len(r["skill"]) for r in display)
    w_skill = max(w_skill, 20)

    header = (
        f"  {'#':>3}  "
        f"{'SKILL':<{w_skill}}  "
        f"{'BLOCKED':>7}  "
        f"{'OPP. COST':>9}  "
        f"{'MEAN':>5}  "
        f"{'TOP':>4}  "
        f"{'OPPORTUNITY BAR':<{_BAR_WIDTH}}  "
        f"CONFIDENCE"
    )
    print(f"{_BOLD}{header}{_RESET}")
    print(
        f"  {'───':>3}  "
        f"{'─' * w_skill}  "
        f"{'───────':>7}  "
        f"{'─────────':>9}  "
        f"{'────':>5}  "
        f"{'───':>4}  "
        f"{'─' * _BAR_WIDTH}  "
        f"──────────"
    )

    # ── Rows ─────────────────────────────────────────────────────────────────
    for rank, row in enumerate(display, start=1):
        skill    = row["skill"]
        blocked  = row["listings_blocked"]
        cost     = row["opportunity_cost"]
        mean_s   = row["mean_score"]
        top_s    = row["top_score"]
        nth      = row["also_nice_to_have"]
        bar      = _bar(cost, max_cost)

        # Colour the skill name: red for #1, dim for low-confidence
        if rank == 1:
            skill_fmt = f"{_BOLD}{_RED}{skill}{_RESET}"
        elif row["low_confidence"]:
            skill_fmt = f"{_DIM}{skill}{_RESET}"
        else:
            skill_fmt = skill

        nth_note = f" +{nth}✦" if nth else ""
        conf_tag = _confidence_tag(row)

        # Pad skill_fmt for alignment (ANSI codes add invisible chars)
        pad = w_skill - len(skill)
        skill_padded = skill_fmt + " " * pad

        print(
            f"  {rank:>3}  "
            f"{skill_padded}  "
            f"{blocked:>7}{nth_note:<4}  "
            f"{cost:>9.2f}  "
            f"{mean_s:>5.0f}  "
            f"{top_s:>4}  "
            f"{_CYAN}{bar}{_RESET}  "
            f"{conf_tag}"
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    print()
    total_shown = len(display)
    total_all   = len(rows)
    print(
        f"{_DIM}  Showing top {total_shown} of {total_all} gaps.  "
        f"OPP. COST = Σ(fit_score/100) for listings blocked by this gap.  "
        f"✦ = also nice-to-have.{_RESET}"
    )
    if any(r["low_confidence"] for r in display):
        print(
            f"{_YELLOW}  ⚠  Low-confidence gaps (n < 3) should not be acted "
            f"on without more data.{_RESET}"
        )

    # ── Drilldown hint ────────────────────────────────────────────────────────
    if display:
        top_ids = display[0]["example_ids"]
        ids_preview = ", ".join(i[:12] + "…" for i in top_ids[:3])
        print()
        print(f"{_DIM}  Top gap example listing IDs: {ids_preview}{_RESET}")
    print()


# ---------------------------------------------------------------------------
# Trend helpers — pure functions, no writes
# ---------------------------------------------------------------------------


def _date_part(iso_ts: str) -> str:
    """Return the date portion of an ISO timestamp, e.g. '2026-08-14'."""
    return iso_ts[:10]


def _pct_change(old: float, new: float) -> str:
    """Return a formatted percent-change string, or '  new' if old is zero."""
    if old == 0:
        return "  new"
    pct = ((new - old) / old) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _change_colour(old: float, new: float) -> str:
    """ANSI colour for a numeric change: red = rising gap, green = shrinking."""
    if new > old:
        return _RED
    if new < old:
        return _GREEN
    return _DIM


def _trend_arrow(old: float, new: float) -> str:
    if new > old * 1.05:
        return "▲"
    if new < old * 0.95:
        return "▼"
    return "─"


# ---------------------------------------------------------------------------
# Trend renderer
# ---------------------------------------------------------------------------


def print_trend_report(
    earliest_rows: list[dict],
    latest_rows: list[dict],
    earliest_date: str,
    latest_date: str,
) -> None:
    """Print how the current top-10 gaps have moved since the first snapshot."""

    # Index the earliest snapshot by skill name for O(1) lookups.
    earliest_by_skill: dict[str, dict] = {
        r["skill"]: r for r in earliest_rows
    }
    latest_top10 = latest_rows[:_TOP_N]
    latest_skills = {r["skill"] for r in latest_top10}

    # Skills that were in the earliest top-10 but are gone now.
    earliest_top10_skills = {r["skill"] for r in earliest_rows[:_TOP_N]}
    dropped_out = earliest_top10_skills - latest_skills

    # ── Header ────────────────────────────────────────────────────────────────
    print()
    print(f"{_BOLD}{_CYAN}{'═' * 78}{_RESET}")
    print(f"{_BOLD}{_CYAN}  SKILL GAP TREND{_RESET}")
    print(
        f"{_DIM}  earliest snapshot : {earliest_date}"
        f"   →   latest snapshot : {latest_date}{_RESET}"
    )
    print(f"{_BOLD}{_CYAN}{'═' * 78}{_RESET}")
    print()

    w_skill = max((len(r["skill"]) for r in latest_top10), default=20)
    w_skill = max(w_skill, 20)

    header = (
        f"  {'#':>3}  "
        f"{'SKILL':<{w_skill}}  "
        f"{'EARLIEST':>9}  "
        f"{'LATEST':>9}  "
        f"{'CHANGE':>9}  "
        f"{'%':>6}  "
        f"DIR"
    )
    print(f"{_BOLD}{header}{_RESET}")
    print(
        f"  {'───':>3}  "
        f"{'─' * w_skill}  "
        f"{'─────────':>9}  "
        f"{'─────────':>9}  "
        f"{'─────────':>9}  "
        f"{'──────':>6}  "
        f"───"
    )

    for rank, row in enumerate(latest_top10, start=1):
        skill     = row["skill"]
        new_cost  = row["opportunity_cost"]
        pad       = w_skill - len(skill)

        early = earliest_by_skill.get(skill)

        if early is None:
            # Skill is new — wasn't present in the earliest snapshot at all.
            skill_fmt  = f"{_BOLD}{skill}{_RESET}" + " " * pad
            earliest_s = f"{'—':>9}"
            latest_s   = f"{new_cost:>9.2f}"
            change_s   = f"{'  new':>9}"
            pct_s      = f"{'':>6}"
            dir_s      = f"{_BOLD}NEW{_RESET}"
        else:
            old_cost   = early["opportunity_cost"]
            delta      = new_cost - old_cost
            colour     = _change_colour(old_cost, new_cost)
            arrow      = _trend_arrow(old_cost, new_cost)
            sign       = "+" if delta >= 0 else ""

            skill_fmt  = skill + " " * pad
            earliest_s = f"{old_cost:>9.2f}"
            latest_s   = f"{new_cost:>9.2f}"
            change_s   = f"{colour}{sign}{delta:>+.2f}{_RESET}"
            pct_s      = f"{colour}{_pct_change(old_cost, new_cost):>6}{_RESET}"
            dir_s      = f"{colour}{arrow}{_RESET}"

        print(
            f"  {rank:>3}  "
            f"{skill_fmt}  "
            f"{earliest_s}  "
            f"{latest_s}  "
            f"{change_s}  "
            f"{pct_s}  "
            f"{dir_s}"
        )

    # ── Dropped-out skills ────────────────────────────────────────────────────
    if dropped_out:
        print()
        print(f"{_DIM}  Skills that were in the earliest top 10 but are gone now:{_RESET}")
        for skill in sorted(dropped_out):
            old_cost = earliest_by_skill[skill]["opportunity_cost"]
            print(
                f"{_GREEN}    ✓ {skill:<{w_skill}}  was {old_cost:.2f}  →  no longer in top 10{_RESET}"
            )

    # ── Footer ────────────────────────────────────────────────────────────────
    print()
    print(
        f"{_DIM}  OPP. COST = Σ(fit_score/100).  "
        f"▲ = rising gap (≥5%)  ▼ = shrinking gap (≥5%)  ─ = stable.{_RESET}"
    )
    print(
        f"{_DIM}  NEW = skill not present in earliest snapshot.  "
        f"Window: {earliest_date} → {latest_date}.{_RESET}"
    )
    print()


def print_only_one_snapshot(computed_at: str) -> None:
    """Honest message when there is exactly one snapshot — no fabrication."""
    print()
    print(f"{_BOLD}{_CYAN}{'═' * 72}{_RESET}")
    print(f"{_BOLD}{_CYAN}  SKILL GAP TREND{_RESET}")
    print(f"{_BOLD}{_CYAN}{'═' * 72}{_RESET}")
    print()
    print(
        f"  {_YELLOW}Only one snapshot exists (recorded {_date_part(computed_at)}).{_RESET}"
    )
    print(
        f"  Trend reporting requires at least 2 snapshots — one from an earlier"
    )
    print(
        f"  run and one from today — so there is a real window to compare."
    )
    print()
    print(
        f"  {_DIM}Run a cycle each day this week.  "
        f"After tomorrow's run you'll have a 2-point window.{_RESET}"
    )
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    trend_mode = "--trend" in sys.argv

    try:
        cfg = load_config()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    storage.init_db(cfg.db_path)

    if not trend_mode:
        rows = storage.get_latest_gap_snapshot(cfg.db_path)
        computed_at = rows[0]["computed_at"] if rows else "—"
        print_gap_report(rows, computed_at)
        return

    # ── Trend mode ────────────────────────────────────────────────────────────
    run_ids = storage.get_snapshot_run_ids(cfg.db_path)

    if not run_ids:
        print("No gap snapshots found. Run a full cycle first.")
        return

    if len(run_ids) == 1:
        print_only_one_snapshot(run_ids[0]["computed_at"])
        return

    # Two or more snapshots — compare earliest to latest.
    earliest_run = run_ids[0]
    latest_run   = run_ids[-1]

    earliest_rows = storage.get_gap_snapshot_by_run(
        cfg.db_path, earliest_run["run_id"]
    )
    latest_rows = storage.get_gap_snapshot_by_run(
        cfg.db_path, latest_run["run_id"]
    )

    print_trend_report(
        earliest_rows=earliest_rows,
        latest_rows=latest_rows,
        earliest_date=_date_part(earliest_run["computed_at"]),
        latest_date=_date_part(latest_run["computed_at"]),
    )


if __name__ == "__main__":
    main()
