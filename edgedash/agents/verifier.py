"""Verifier agent — judges output plausibility, never repairs data (rule 34).

Reads the current cycle's scores, extracted facts, gap snapshot, and latest
fetch timestamp from storage, delegates to run_all_checks, and returns an
AgentResult whose notes field carries a human-readable verdict.

The Verifier writes NO data of its own. The only side effect is the
cycle_log row written by the Orchestrator after it receives the AgentResult.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.verification import Verdict, run_all_checks

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


class Verifier:
    name: str = "verifier"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: "StopConditions | None" = None,
    ) -> AgentResult:
        """Run all verification checks and return the verdict as an AgentResult.

        status="ok"     → all checks passed
        status="failed" → one or more checks failed; notes name each failure
        """
        # ── Collect the data each check needs from storage ─────────────────
        # All reads happen up front so the checks themselves stay pure.

        # Scores: all non-null fit_score values from the listings table.
        scores: list[int] = _read_scores(db_path)

        # Facts: required_skills list for each listing that has a cache entry.
        facts_list: list[dict[str, Any]] = _read_facts(db_path)

        # Gaps: the latest snapshot, already sorted by opportunity_cost desc.
        gaps: list[dict[str, Any]] = storage.get_latest_gap_snapshot(db_path)

        # Freshness: the most recent fetched_at timestamp across all listings.
        latest_fetch_at: str | None = storage.last_fetch_time(db_path)

        # now is a parameter to run_all_checks — never datetime.now() inside
        # the check functions themselves (rule: testable pure functions).
        now = datetime.now(timezone.utc)

        # ── Run checks ─────────────────────────────────────────────────────
        verdict: Verdict = run_all_checks(
            scores=scores,
            facts_list=facts_list,
            gaps=gaps,
            latest_fetch_at=latest_fetch_at,
            config=config,
            now=now,
        )

        # ── Build notes (rule 37: name the check and the observed value) ───
        notes = _build_notes(verdict)

        status = "ok" if verdict.passed else "failed"

        return AgentResult(
            agent=self.name,
            status=status,
            records_touched=0,   # Verifier writes no data (rule 34)
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Storage helpers — thin wrappers so the agent body stays readable
# ---------------------------------------------------------------------------


def _read_scores(db_path: str) -> list[int]:
    """Return all non-null fit_score values as a plain list of ints."""
    # Rule 2: storage is the only module that may touch the DB driver.
    # get_listings with min_score=0 returns all scored rows up to the limit.
    rows = storage.get_listings(db_path, limit=10_000, min_score=0)
    return [
        int(r["fit_score"])
        for r in rows
        if r.get("fit_score") is not None
    ]


def _read_facts(db_path: str) -> list[dict[str, Any]]:
    """Return one facts dict per listing that has an extraction cache entry."""
    # get_scored_listings_with_cache returns {id, fit_score,
    # required_skills, nice_to_have} — exactly what check_extraction_sanity
    # needs.
    return storage.get_scored_listings_with_cache(db_path)


# ---------------------------------------------------------------------------
# Notes builder
# ---------------------------------------------------------------------------


def _build_notes(verdict: Verdict) -> str:
    """Convert a Verdict into a compact, operator-readable notes string.

    Format on pass:
        VERDICT: pass — all 4 checks ok

    Format on fail (one failure):
        VERDICT: fail — score_spread observed spread=6 stdev=2.1 (min spread=10 stdev=5.0)

    Format on fail (multiple):
        VERDICT: fail — score_spread observed spread=6 stdev=2.1 (min spread=10 stdev=5.0);
                        freshness observed age_days=5.0 (max 3)
    """
    if verdict.passed:
        return f"VERDICT: pass — {verdict.summary}"

    failure_parts: list[str] = []
    for check in verdict.failed_checks:
        obs_str = _format_observed(check.observed)
        thr_str = _format_threshold(check.threshold)
        failure_parts.append(
            f"{check.name} observed {obs_str} ({thr_str})"
        )

    failures = "; ".join(failure_parts)
    return f"VERDICT: fail — {failures}"


def _format_observed(observed: Any) -> str:
    if isinstance(observed, dict):
        return " ".join(f"{k}={v}" for k, v in observed.items())
    return str(observed)


def _format_threshold(threshold: Any) -> str:
    if isinstance(threshold, dict):
        return " ".join(f"{k}={v}" for k, v in threshold.items())
    return str(threshold)
