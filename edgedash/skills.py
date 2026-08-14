"""Skill name canonicalisation — deterministic only, no LLM, no network.

Public API
----------
canonical(raw, aliases) -> str
    Normalise a raw skill string and apply the project alias map.

Run as a module for the audit CLI:
    python -m edgedash.skills --audit
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def canonical(raw: str, aliases: dict[str, str]) -> str:
    """Return the canonical form of a raw skill string.

    Steps applied in order:
    1. Lowercase and strip surrounding whitespace.
    2. Drop parenthetical qualifiers, e.g. "kubernetes (eks)" -> "kubernetes".
    3. Strip surrounding punctuation characters.
    4. Collapse internal whitespace runs to a single space.
    5. Strip again after the previous transforms.
    6. Look up the result in *aliases*; return the mapped value if present,
       otherwise return the normalised string as-is.

    Pure function — same input always produces the same output.
    No network, no model, no side effects.
    """
    if not raw:
        return ""

    # Step 1: lowercase + strip surrounding whitespace.
    s = raw.lower().strip()

    # Step 2: drop parenthetical qualifiers BEFORE stripping surrounding
    # punctuation so the closing ')' is still present for the regex.
    # e.g. "kubernetes (eks)" -> "kubernetes"
    s = re.sub(r"\s*\([^)]*\)", "", s)

    # Step 3: strip surrounding punctuation (keep inner punctuation like
    # slashes and dots that are meaningful: "ci/cd", "node.js").
    s = s.strip(".,;:!?\"'`()[]{}")

    # Step 4: collapse internal whitespace.
    s = re.sub(r"\s+", " ", s)

    # Step 5: final strip after transforms.
    s = s.strip()

    # Step 6: alias lookup.
    return aliases.get(s, s)


# ---------------------------------------------------------------------------
# Audit CLI
# ---------------------------------------------------------------------------


def _load_config():
    """Load project config without importing the full module at top level."""
    from edgedash.config import load_config
    return load_config()


def _all_raw_skills(db_path: str) -> list[str]:
    """Read every skill string from the extraction_cache table.

    Returns a flat list of raw strings (one entry per skill per cached row).
    Read-only — no writes.
    """
    from edgedash.storage import _connect  # noqa: WPS450 — storage is the only DB module

    results: list[str] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT required_skills FROM extraction_cache"
        ).fetchall()

    for row in rows:
        try:
            skills = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            continue
        for s in skills:
            if isinstance(s, str) and s.strip():
                results.append(s.strip())

    return results


def _run_audit() -> None:
    """Print a skill frequency report to stdout. Read-only."""
    cfg = _load_config()
    aliases: dict[str, str] = cfg.skill_aliases

    raw_skills = _all_raw_skills(cfg.db_path)

    if not raw_skills:
        print("No extracted skills found in the database.")
        print("Run a scoring cycle first to populate extraction_cache.")
        return

    counts: Counter[str] = Counter(raw_skills)
    total_unique = len(counts)

    # -----------------------------------------------------------------------
    # Section 1: top 40 raw strings with canonical mapping
    # -----------------------------------------------------------------------
    top40 = counts.most_common(40)

    print("=" * 72)
    print(f"SKILL AUDIT  —  {len(raw_skills):,} total occurrences, "
          f"{total_unique:,} unique raw strings")
    print("=" * 72)
    print()
    print("TOP 40 RAW SKILL STRINGS")
    print("-" * 72)
    print(f"{'COUNT':>6}  {'RAW STRING':<35}  CANONICAL")
    print(f"{'------':>6}  {'-'*35}  {'-'*25}")

    for raw, count in top40:
        canon = canonical(raw, aliases)
        marker = "" if canon == raw.lower().strip() else f"  -> {canon!r}"
        print(f"{count:>6}  {raw:<35}{marker}")

    # -----------------------------------------------------------------------
    # Section 2: singletons (count == 1) — likely typos, junk, or sentences
    # -----------------------------------------------------------------------
    singletons = sorted(s for s, c in counts.items() if c == 1)

    print()
    print("=" * 72)
    print(f"SINGLETONS — {len(singletons):,} raw strings seen exactly once")
    print("(typos, full sentences, or rare legitimate skills — review manually)")
    print("-" * 72)

    for s in singletons:
        print(f"  {s}")

    print()
    print(f"Alias map has {len(aliases):,} entries.  "
          "Edit skill_aliases in config.yaml to fix collisions.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if "--audit" in sys.argv:
        _run_audit()
    else:
        print("Usage: python -m edgedash.skills --audit")
        sys.exit(1)


if __name__ == "__main__":
    main()
