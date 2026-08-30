"""Skill name canonicalisation — deterministic only, no LLM, no network.

Public API
----------
canonical(raw, aliases) -> str
    Normalise a raw skill string and apply the project alias map.

Run as a module for the audit/suggest CLI:
    python -m edgedash.skills --audit
    python -m edgedash.skills --suggest-aliases
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
    import edgedash.storage as storage

    results: list[str] = []
    for raw in storage.get_all_required_skills(db_path):
        try:
            skills = json.loads(raw or "[]")
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
# Alias suggestion (ONE model call — read-only, no file writes)
# ---------------------------------------------------------------------------

# Maximum number of canonical skill strings sent to the model in one call.
# Keeps the prompt manageable and the cost predictable.
_SUGGEST_MAX_SKILLS = 80

_SUGGEST_SCHEMA: dict = {"proposals": list}

_SUGGEST_PROMPT = """\
You are helping a user maintain a skill alias map for a job-scoring tool.

Below is a list of canonical skill strings found in job listings, with their
occurrence counts. Your task is to identify groups of strings that clearly refer
to the same underlying skill — for example "js" and "javascript", or "postgres"
and "postgresql".

Rules you MUST follow:
- Only group strings that unambiguously refer to the same skill.
- Do NOT group skills that are genuinely distinct, even if they are related.
  For example: "python" and "django" are different skills. "node" and
  "javascript" are different skills. "aws" and "cloud" are different skills.
- For each group, choose the most widely-recognised canonical name.
- Set confidence "high" only when the strings are clearly abbreviations or
  alternate spellings of exactly the same thing.
- Set confidence "low" when there is any reasonable interpretation where they
  could be considered separate.
- If you are unsure, do NOT group — omit the pair entirely.
- Do not invent skills that are not in the input list.
- Do not include groups with fewer than 2 variants.

Input skill strings (format: "string": count):
{skill_list}

Return a single JSON object with one key "proposals", whose value is a list.
Each item in the list must have exactly:
  "canonical"  : string  — the preferred canonical name (must be from the input list)
  "variants"   : list    — the OTHER strings in this group (each must be from the input list)
  "confidence" : string  — exactly "high" or "low"

Example of correct output format:
{{
  "proposals": [
    {{"canonical": "javascript", "variants": ["js"], "confidence": "high"}},
    {{"canonical": "kubernetes", "variants": ["k8s", "kube"], "confidence": "high"}}
  ]
}}
"""


def _collect_unaliased_canonicals(
    db_path: str,
    aliases: dict[str, str],
) -> list[tuple[str, int]]:
    """Return canonical skill strings from the DB that are not yet in the alias map.

    Returns a list of (canonical_string, count) sorted by count descending.
    Strings that are already a key OR a value in the alias map are excluded —
    the user has already made a decision about those.
    """
    import edgedash.storage as storage

    results: list[str] = []
    for raw in storage.get_all_required_skills(db_path):
        try:
            skills = json.loads(raw or "[]")
        except json.JSONDecodeError:
            continue
        for s in skills:
            if isinstance(s, str) and s.strip():
                results.append(s.strip())

    # Canonicalise every raw string using the existing alias map.
    counts: Counter[str] = Counter(
        canonical(s, aliases) for s in results if s
    )

    # The set of strings the user has already decided on — both keys and
    # values in the alias map are considered "handled".
    already_handled: set[str] = set(aliases.keys()) | set(aliases.values())

    unaliased = [
        (skill, count)
        for skill, count in counts.most_common()
        if skill and skill not in already_handled
    ]
    return unaliased


def _validate_proposals(raw_proposals: list) -> list[dict]:
    """Validate each proposal item and discard malformed ones with a warning."""
    valid = []
    for i, item in enumerate(raw_proposals):
        if not isinstance(item, dict):
            print(f"  [warn] proposal {i} is not a dict — skipped", file=sys.stderr)
            continue
        if not isinstance(item.get("canonical"), str):
            print(f"  [warn] proposal {i} missing 'canonical' string — skipped",
                  file=sys.stderr)
            continue
        if not isinstance(item.get("variants"), list):
            print(f"  [warn] proposal {i} missing 'variants' list — skipped",
                  file=sys.stderr)
            continue
        if item.get("confidence") not in ("high", "low"):
            print(f"  [warn] proposal {i} has invalid confidence "
                  f"{item.get('confidence')!r} — skipped", file=sys.stderr)
            continue
        if not item["variants"]:
            continue  # single-item group — nothing to alias
        valid.append(item)
    return valid


def _detect_conflicts(
    proposal: dict,
    aliases: dict[str, str],
) -> list[str]:
    """Return conflict descriptions if the proposal contradicts existing aliases.

    A conflict occurs when two strings in the proposed group are explicitly
    mapped to DIFFERENT canonical values in the user's alias map.
    Strings that are not in the alias map at all are not a conflict — the
    user simply hasn't made a decision about them yet.
    """
    all_strings = [proposal["canonical"]] + list(proposal["variants"])

    # Only consider strings the user has explicitly placed in the alias map.
    explicitly_mapped: dict[str, str] = {
        s: aliases[s] for s in all_strings if s in aliases
    }

    if len(explicitly_mapped) < 2:
        return []   # zero or one explicit mapping — nothing can conflict

    unique_targets = set(explicitly_mapped.values())
    if len(unique_targets) <= 1:
        return []   # all explicit mappings point to the same canonical — fine

    conflicts = []
    items = sorted(explicitly_mapped.items())
    for i, (s1, t1) in enumerate(items):
        for s2, t2 in items[i + 1:]:
            if t1 != t2:
                conflicts.append(
                    f"'{s1}' → '{t1}'  vs  '{s2}' → '{t2}' in your alias map"
                )
    return conflicts


def _run_suggest_aliases() -> None:
    """One model call — propose aliases for unhandled canonical skill strings.

    Read-only. Does not write to config.yaml or any other file.
    """
    from dotenv import load_dotenv
    load_dotenv()

    cfg = _load_config()
    aliases: dict[str, str] = cfg.skill_aliases

    unaliased = _collect_unaliased_canonicals(cfg.db_path, aliases)

    if not unaliased:
        print("No unaliased canonical skill strings found in the database.")
        print("Either the DB is empty or everything is already in your alias map.")
        return

    # Trim to the top N by frequency to keep the prompt focused.
    to_send = unaliased[:_SUGGEST_MAX_SKILLS]

    skill_list_text = "\n".join(
        f'  "{skill}": {count}' for skill, count in to_send
    )

    prompt = _SUGGEST_PROMPT.format(skill_list=skill_list_text)

    print(f"  Sending {len(to_send)} canonical skill strings to the model …")
    print(f"  (top {_SUGGEST_MAX_SKILLS} by frequency, "
          f"{len(unaliased)} total unaliased)\n")

    from edgedash.llm import LLMError, complete_json

    try:
        result = complete_json(prompt, _SUGGEST_SCHEMA, config=cfg, max_retries=1)
    except LLMError as exc:
        print(f"  ✗  Model call failed: {exc}", file=sys.stderr)
        sys.exit(1)

    raw_proposals = result.get("proposals", [])
    proposals = _validate_proposals(raw_proposals)

    if not proposals:
        print("  Model returned no valid proposals.")
        return

    # ── Warning header ────────────────────────────────────────────────────────
    print("=" * 72)
    print("  ⚠  ALIAS SUGGESTIONS — REVIEW REQUIRED BEFORE USE")
    print("=" * 72)
    print()
    print("  These are MODEL SUGGESTIONS. They may be wrong.")
    print("  Merging two skills that are actually distinct is WORSE than")
    print("  leaving them separate. When in doubt, do not add the alias.")
    print()
    print("  To use a suggestion: copy the YAML block into the skill_aliases")
    print("  section of config.yaml. Do not paste blindly.")
    print()

    # ── Proposals ─────────────────────────────────────────────────────────────
    high_conf = [p for p in proposals if p["confidence"] == "high"]
    low_conf  = [p for p in proposals if p["confidence"] == "low"]

    def _print_group(group: list[dict], label: str) -> None:
        if not group:
            return
        print(f"  ── {label} ({'high confidence' if 'HIGH' in label else 'low confidence — extra caution'}) ──")
        print()
        for p in group:
            canon    = p["canonical"]
            variants = p["variants"]
            conflicts = _detect_conflicts(p, aliases)

            if conflicts:
                print(f"  # ⛔  CONFLICT WITH YOUR EXISTING ALIAS MAP:")
                for c in conflicts:
                    print(f"  #     {c}")
                print(f"  #     You have already made a decision about these strings.")
                print(f"  #     Do not add this entry without reviewing your map first.")
                print()

            for v in variants:
                print(f'  "{v}": "{canon}"')
            print()

    _print_group(high_conf, "HIGH CONFIDENCE")
    _print_group(low_conf,  "LOW CONFIDENCE")

    # ── Footer ────────────────────────────────────────────────────────────────
    print("=" * 72)
    print(f"  {len(proposals)} proposals ({len(high_conf)} high, {len(low_conf)} low).")
    print("  Nothing was written to config.yaml.")
    print("  Add entries manually after review.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if "--audit" in sys.argv:
        _run_audit()
    elif "--suggest-aliases" in sys.argv:
        _run_suggest_aliases()
    else:
        print("Usage:")
        print("  python -m edgedash.skills --audit")
        print("  python -m edgedash.skills --suggest-aliases")
        sys.exit(1)


if __name__ == "__main__":
    main()
