"""Read-only database diagnostic.

Usage:
    python -m edgedash.diagnose

Prints four sections from the existing database.
Makes no writes and changes no schema.
"""

from __future__ import annotations

import sys
import textwrap

import edgedash.storage as storage
from edgedash.config import load_config

# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_DIM    = "\033[2m"
_WIDTH  = 60


def _header(text: str) -> None:
    print(f"\n{_BOLD}{_CYAN}{'─' * _WIDTH}{_RESET}")
    print(f"{_BOLD}{_CYAN}  {text}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'─' * _WIDTH}{_RESET}")


def _row(label: str, value: str, colour: str = "") -> None:
    reset = _RESET if colour else ""
    print(f"  {_DIM}{label:<28}{_RESET}{colour}{value}{reset}")


def _warn(text: str) -> None:
    print(f"  {_YELLOW}⚠  {text}{_RESET}")


def _bad(text: str) -> None:
    print(f"  {_RED}✗  {text}{_RESET}")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _section_counts(db: str) -> None:
    _header("1 · Listing counts")
    total = storage.count_total(db)
    _row("Total listings", str(total))

    by_source = storage.count_by_source(db)
    if by_source:
        print()
        print(f"  {_BOLD}{'SOURCE':<18}  {'COUNT':>6}{_RESET}")
        print(f"  {'─' * 18}  {'─' * 6}")
        for r in by_source:
            print(f"  {r['source']:<18}  {r['count']:>6}")
    else:
        _warn("No listings in the database yet.")


def _section_cross_dupes(db: str) -> None:
    _header("2 · Probable cross-source duplicates")
    dupes = storage.cross_source_duplicates(db)
    if not dupes:
        print(f"  {_DIM}No cross-source duplicates found.{_RESET}")
        return

    print(f"  {_BOLD}{'TITLE':<40}  {'COMPANY':<22}  SOURCES{_RESET}")
    print(f"  {'─' * 40}  {'─' * 22}  {'─' * 20}")
    for r in dupes:
        title   = textwrap.shorten(r["title"]   or "", width=40, placeholder="…")
        company = textwrap.shorten(r["company"] or "", width=22, placeholder="…")
        print(
            f"  {_YELLOW}{title:<40}{_RESET}  "
            f"{company:<22}  "
            f"{r['sources']}"
        )


def _section_recent(db: str) -> None:
    _header("3 · 5 most recent listings")
    rows = storage.recent_listings(db, limit=5)
    if not rows:
        _warn("No listings in the database yet.")
        return

    print(f"  {_BOLD}{'FETCHED AT':<26}  {'SOURCE':<12}  {'TITLE':<35}  COMPANY{_RESET}")
    print(f"  {'─' * 26}  {'─' * 12}  {'─' * 35}  {'─' * 20}")
    for r in rows:
        title   = textwrap.shorten(r["title"]   or "", width=35, placeholder="…")
        company = textwrap.shorten(r["company"] or "", width=20, placeholder="…")
        print(
            f"  {r['fetched_at']:<26}  "
            f"{r['source']:<12}  "
            f"{title:<35}  "
            f"{company}"
        )


def _section_quality(db: str) -> None:
    _header("4 · Data quality issues (null / empty url, title, or company)")
    issues = storage.quality_issues(db)
    if not issues:
        print(f"  {_DIM}No data quality issues found.{_RESET}")
        return

    print(f"  {_BOLD}{'ID[:12]':<14}  {'SOURCE':<12}  {'TITLE':<30}  {'COMPANY':<20}  URL{_RESET}")
    print(f"  {'─' * 14}  {'─' * 12}  {'─' * 30}  {'─' * 20}  {'─' * 30}")
    for r in issues:
        id_short = (r["id"] or "")[:12]
        title    = textwrap.shorten(r["title"]   or _flag("title"),   width=30, placeholder="…")
        company  = textwrap.shorten(r["company"] or _flag("company"), width=20, placeholder="…")
        url      = textwrap.shorten(r["url"]     or _flag("url"),     width=30, placeholder="…")
        print(
            f"  {_RED}{id_short:<14}{_RESET}  "
            f"{r['source'] or '':<12}  "
            f"{title:<30}  "
            f"{company:<20}  "
            f"{url}"
        )


def _flag(field: str) -> str:
    return f"[NULL {field}]"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    db = config.db_path
    print(f"\n{_BOLD}EdgeDash diagnostics{_RESET}  {_DIM}db: {db}{_RESET}")

    _section_counts(db)
    _section_cross_dupes(db)
    _section_recent(db)
    _section_quality(db)

    print()


if __name__ == "__main__":
    main()
