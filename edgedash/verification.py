"""Deterministic verification checks for EdgeDash agent output.

No LLM anywhere in this file. A model cannot be the judge of a model's output.

Every check is a pure function:
    - Takes only the data it needs plus a Config.
    - Returns a CheckResult.
    - No clock reads, no network, no database access.

Public API
----------
check_score_spread(scores, config)           -> CheckResult
check_extraction_sanity(facts_list, config)  -> CheckResult
check_gap_sample_size(gaps, config)          -> CheckResult
check_freshness(latest_fetch_at, config, now) -> CheckResult
run_all_checks(...)                          -> Verdict
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    observed: Any         # the value(s) that were measured
    threshold: Any        # the limit(s) from config
    message: str          # human-readable verdict sentence


@dataclass(frozen=True)
class Verdict:
    passed: bool
    failed_checks: list[CheckResult]
    summary: str


# ---------------------------------------------------------------------------
# 1. check_score_spread
# ---------------------------------------------------------------------------


def check_score_spread(scores: list[int], config: Any) -> CheckResult:
    """Fail if the score distribution is suspiciously tight.

    Two conditions are checked:
        - spread (max - min) must be >= min_score_spread
        - population stdev must be >= min_score_stdev

    Either failure is sufficient to fail the check. Both thresholds are
    reported in one result so the operator sees the full picture at a glance.

    Passes trivially (with an explanatory message) when fewer than 5 scores
    are present — a meaningful distribution cannot be asserted on tiny samples.
    """
    name = "score_spread"
    min_spread = int(getattr(config, "min_score_spread", 10))
    min_stdev  = float(getattr(config, "min_score_stdev", 5.0))

    if len(scores) < 5:
        return CheckResult(
            name=name,
            passed=True,
            observed={"count": len(scores)},
            threshold={"min_spread": min_spread, "min_score_stdev": min_stdev},
            message=(
                f"Trivially passed — only {len(scores)} score(s) present; "
                "distribution checks require at least 5."
            ),
        )

    spread = max(scores) - min(scores)
    stdev  = statistics.pstdev(scores)   # population stdev; no inference needed

    spread_ok = spread >= min_spread
    stdev_ok  = stdev  >= min_stdev
    passed    = spread_ok and stdev_ok

    parts: list[str] = []
    if not spread_ok:
        parts.append(
            f"spread {spread} < min_score_spread {min_spread} "
            "(all scores inflated into a narrow band)"
        )
    if not stdev_ok:
        parts.append(
            f"stdev {stdev:.1f} < min_score_stdev {min_stdev} "
            "(scores cluster too tightly — possible model inflation)"
        )

    message = (
        "; ".join(parts)
        if parts
        else (
            f"Spread {spread} (>= {min_spread}) and "
            f"stdev {stdev:.1f} (>= {min_stdev}) both acceptable."
        )
    )

    return CheckResult(
        name=name,
        passed=passed,
        observed={"spread": spread, "stdev": round(stdev, 2), "count": len(scores)},
        threshold={"min_score_spread": min_spread, "min_score_stdev": min_stdev},
        message=message,
    )


# ---------------------------------------------------------------------------
# 2. check_extraction_sanity
# ---------------------------------------------------------------------------


def check_extraction_sanity(facts_list: list[dict[str, Any]], config: Any) -> CheckResult:
    """Fail if extracted facts look like a broken or hallucinating extractor.

    Two conditions are checked:
        - The fraction of listings with an empty required_skills list must be
          <= max_empty_extraction_pct (default 20 %).  Catches a dead extractor.
        - No single listing may have more than max_skills_per_listing (default
          20) required_skills.  Catches a model that returned a full sentence
          or an entire job description as a skill list.

    Both thresholds are checked independently; either failure fails the check.
    Passes trivially when facts_list is empty (nothing to verify).
    """
    name = "extraction_sanity"
    max_empty_pct  = float(getattr(config, "max_empty_extraction_pct", 20.0))
    max_skills     = int(getattr(config, "max_skills_per_listing", 20))

    if not facts_list:
        return CheckResult(
            name=name,
            passed=True,
            observed={"count": 0},
            threshold={
                "max_empty_extraction_pct": max_empty_pct,
                "max_skills_per_listing": max_skills,
            },
            message="Trivially passed — no extracted facts to validate.",
        )

    total   = len(facts_list)
    empty   = sum(
        1 for f in facts_list
        if not (f.get("required_skills") or [])
    )
    empty_pct = (empty / total) * 100.0

    # Find the worst offender for the oversized-list check.
    skill_counts = [len(f.get("required_skills") or []) for f in facts_list]
    max_observed = max(skill_counts)
    max_idx      = skill_counts.index(max_observed)   # first listing with the max

    empty_ok = empty_pct <= max_empty_pct
    size_ok  = max_observed <= max_skills
    passed   = empty_ok and size_ok

    parts: list[str] = []
    if not empty_ok:
        parts.append(
            f"{empty}/{total} listings ({empty_pct:.1f}%) have empty "
            f"required_skills, exceeds max_empty_extraction_pct {max_empty_pct:.0f}% "
            "(extractor may be broken)"
        )
    if not size_ok:
        parts.append(
            f"listing index {max_idx} has {max_observed} required_skills, "
            f"exceeds max_skills_per_listing {max_skills} "
            "(model may have returned a sentence instead of a skill list)"
        )

    message = (
        "; ".join(parts)
        if parts
        else (
            f"{empty}/{total} empty ({empty_pct:.1f}%, <= {max_empty_pct:.0f}%) "
            f"and max skill list size {max_observed} (<= {max_skills}) — both acceptable."
        )
    )

    return CheckResult(
        name=name,
        passed=passed,
        observed={
            "total": total,
            "empty": empty,
            "empty_pct": round(empty_pct, 1),
            "max_skills_observed": max_observed,
        },
        threshold={
            "max_empty_extraction_pct": max_empty_pct,
            "max_skills_per_listing": max_skills,
        },
        message=message,
    )


# ---------------------------------------------------------------------------
# 3. check_gap_sample_size
# ---------------------------------------------------------------------------


def check_gap_sample_size(gaps: list[dict[str, Any]], config: Any) -> CheckResult:
    """Fail if the top-ranked gap was computed from too few listings.

    The top-ranked gap is the first row in `gaps` (already sorted by
    opportunity_cost descending by the GapAnalyzer).  If its
    listings_blocked count is below min_gap_sample, the ranking is based
    on a rumour and should not be acted on.

    Passes trivially when there are no gaps.
    """
    name = "gap_sample_size"
    min_sample = int(getattr(config, "min_gap_sample", 3))

    if not gaps:
        return CheckResult(
            name=name,
            passed=True,
            observed={"count": 0},
            threshold={"min_gap_sample": min_sample},
            message="Trivially passed — no gaps to validate.",
        )

    top_gap    = gaps[0]
    skill      = top_gap.get("skill", "<unknown>")
    n_listings = int(top_gap.get("listings_blocked", 0))
    passed     = n_listings >= min_sample

    if passed:
        message = (
            f"Top gap '{skill}' computed from {n_listings} listing(s) "
            f"(>= min_gap_sample {min_sample})."
        )
    else:
        message = (
            f"Top gap '{skill}' computed from only {n_listings} listing(s), "
            f"below min_gap_sample {min_sample} — "
            "this ranking is based on too little data to act on."
        )

    return CheckResult(
        name=name,
        passed=passed,
        observed={"top_skill": skill, "listings_blocked": n_listings},
        threshold={"min_gap_sample": min_sample},
        message=message,
    )


# ---------------------------------------------------------------------------
# 4. check_freshness
# ---------------------------------------------------------------------------


def check_freshness(
    latest_fetch_at: str | None,
    config: Any,
    now: datetime,
) -> CheckResult:
    """Fail if the newest fetched listing is older than max_data_age_days.

    `now` is an explicit parameter — never datetime.now() inside this
    function — so the check is fully testable without patching the clock.

    `latest_fetch_at` is an ISO-8601 string (UTC) or None if no data exists.
    A None value is treated as maximally stale (always fails).
    """
    name = "freshness"
    max_age_days = int(getattr(config, "max_data_age_days", 3))

    if latest_fetch_at is None:
        return CheckResult(
            name=name,
            passed=False,
            observed={"latest_fetch_at": None},
            threshold={"max_data_age_days": max_age_days},
            message=(
                "No fetch timestamp found — database may be empty or "
                "the first fetch has not run yet."
            ),
        )

    # Parse the ISO timestamp; treat any parse failure as maximally stale.
    try:
        # Handle both naive and offset-aware strings.
        ts = datetime.fromisoformat(latest_fetch_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return CheckResult(
            name=name,
            passed=False,
            observed={"latest_fetch_at": latest_fetch_at},
            threshold={"max_data_age_days": max_age_days},
            message=(
                f"Could not parse latest_fetch_at '{latest_fetch_at}' as an "
                "ISO-8601 timestamp — treating as stale."
            ),
        )

    # Ensure `now` is offset-aware for safe comparison.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_seconds  = (now - ts).total_seconds()
    age_days     = age_seconds / 86_400.0
    passed       = age_days <= max_age_days

    if passed:
        message = (
            f"Data is {age_days:.1f} day(s) old "
            f"(<= max_data_age_days {max_age_days})."
        )
    else:
        message = (
            f"Data is {age_days:.1f} day(s) old, "
            f"exceeds max_data_age_days {max_age_days} — "
            "the fetcher may not have run recently."
        )

    return CheckResult(
        name=name,
        passed=passed,
        observed={"latest_fetch_at": latest_fetch_at, "age_days": round(age_days, 2)},
        threshold={"max_data_age_days": max_age_days},
        message=message,
    )


# ---------------------------------------------------------------------------
# 5. run_all_checks
# ---------------------------------------------------------------------------


def run_all_checks(
    *,
    scores: list[int],
    facts_list: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    latest_fetch_at: str | None,
    config: Any,
    now: datetime,
) -> Verdict:
    """Run every check and return a single Verdict.

    Passed only when ALL checks pass.  Collects all failures rather than
    short-circuiting so the operator sees the full picture in one run.
    """
    results = [
        check_score_spread(scores, config),
        check_extraction_sanity(facts_list, config),
        check_gap_sample_size(gaps, config),
        check_freshness(latest_fetch_at, config, now),
    ]

    failed = [r for r in results if not r.passed]
    passed = len(failed) == 0

    if passed:
        summary = (
            f"All {len(results)} checks passed."
        )
    else:
        failed_names = ", ".join(r.name for r in failed)
        summary = (
            f"{len(failed)}/{len(results)} check(s) failed: {failed_names}."
        )

    return Verdict(passed=passed, failed_checks=failed, summary=summary)
