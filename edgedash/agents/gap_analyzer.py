"""Gap Analyzer agent — deterministic only, no LLM anywhere in this file.

For every scored listing that has extracted facts, identify required skills
the user does not have (compared canonically), accumulate per-skill stats,
rank by opportunity_cost (rule 24), write a timestamped snapshot (rule 25),
and return an AgentResult.

opportunity_cost(skill) = Σ (listing.fit_score / 100)
                            for each scored listing where:
                              skill ∈ listing.required_skills
                              AND skill ∉ my_skills  (canonical comparison)

A listing scored 80 contributes 0.80; one scored 20 contributes 0.20.
Same raw frequency, different quality listings → different rank.
"""

from __future__ import annotations

import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.skills import canonical

if TYPE_CHECKING:
    from edgedash.planning import StopConditions

# How many gaps to surface in the report and snapshot.
_TOP_N = 10
# Gaps from fewer than this many listings are flagged low-confidence (rule 27).
_MIN_CONFIDENCE = 3


# ---------------------------------------------------------------------------
# Internal accumulator — one per canonical skill name
# ---------------------------------------------------------------------------


@dataclass
class _GapAccum:
    """Mutable accumulator for a single gap skill."""
    opportunity_cost: float = 0.0
    scores: list[int] = field(default_factory=list)
    listing_ids: list[tuple[int, str]] = field(
        default_factory=list
    )  # (score, id) — kept for sorting
    also_nice_to_have: int = 0


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def _canonicalise_set(skills: list[str], aliases: dict[str, str]) -> set[str]:
    """Return a set of canonical skill strings from a raw list."""
    return {canonical(s, aliases) for s in skills if s}


def compute_gaps(
    listings: list[dict],
    my_skills_raw: list[str],
    aliases: dict[str, str],
) -> list[dict]:
    """Return gap rows sorted by opportunity_cost descending.

    Parameters
    ----------
    listings:
        Each dict must have: id, fit_score (int), required_skills (list[str]),
        nice_to_have (list[str]).
    my_skills_raw:
        The user's skill list from config, before canonicalisation.
    aliases:
        The skill_aliases map from config.

    Returns a list of dicts with keys:
        skill, listings_blocked, opportunity_cost, mean_score, top_score,
        also_nice_to_have, low_confidence, example_ids
    """
    my_canon: set[str] = _canonicalise_set(my_skills_raw, aliases)

    accum: dict[str, _GapAccum] = {}

    for listing in listings:
        score: int = listing["fit_score"]
        listing_id: str = listing["id"]

        required_canon = _canonicalise_set(listing["required_skills"], aliases)
        nice_canon     = _canonicalise_set(listing["nice_to_have"], aliases)

        for skill in required_canon:
            if not skill or skill in my_canon:
                continue

            if skill not in accum:
                accum[skill] = _GapAccum()

            g = accum[skill]
            # opportunity_cost: fractional score contribution (rule 24)
            g.opportunity_cost += score / 100
            g.scores.append(score)
            g.listing_ids.append((score, listing_id))

        # Track nice-to-have appearances separately — never mixed into
        # required counts (rule 23 / spec requirement).
        for skill in nice_canon:
            if not skill or skill in my_canon:
                continue
            if skill in accum:
                accum[skill].also_nice_to_have += 1
            # If skill only appears as nice-to-have (never required), we
            # don't create an accum entry — it is not a required gap.

    rows: list[dict] = []
    for skill, g in accum.items():
        n = len(g.scores)
        # example_ids: up to 5 listing IDs from the highest-scoring listings
        # (rule 26 — every number must be traceable).
        top_ids = [
            lid for _, lid in sorted(g.listing_ids, reverse=True)[:5]
        ]
        rows.append({
            "skill":             skill,
            "listings_blocked":  n,
            "opportunity_cost":  round(g.opportunity_cost, 4),
            "mean_score":        round(statistics.mean(g.scores), 1),
            "top_score":         max(g.scores),
            "also_nice_to_have": g.also_nice_to_have,
            "low_confidence":    n < _MIN_CONFIDENCE,   # rule 27
            "example_ids":       top_ids,
        })

    rows.sort(key=lambda r: r["opportunity_cost"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class GapAnalyzer:
    name: str = "gap_analyzer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: "StopConditions | None" = None,
    ) -> AgentResult:
        max_seconds = (
            stop_conditions.max_seconds
            if stop_conditions and stop_conditions.max_seconds is not None
            else None
        )
        deadline = time.monotonic() + max_seconds if max_seconds else None
        started_at = datetime.now(timezone.utc).isoformat()

        listings = storage.get_scored_listings_with_cache(db_path)

        # Respect wall-clock stop condition after the DB read (rule 29).
        if deadline is not None and time.monotonic() >= deadline:
            return AgentResult(
                agent=self.name, status="ok", records_touched=0,
                notes=f"max_seconds={max_seconds} reached before analysis could start",
            )

        if not listings:
            storage.log_cycle(
                path=db_path,
                agent=self.name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                records_touched=0,
                status="ok",
                notes="no scored listings with extracted facts",
            )
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="no scored listings with extracted facts",
            )

        all_gaps = compute_gaps(
            listings=listings,
            my_skills_raw=config.my_skills,
            aliases=config.skill_aliases,
        )

        top_gaps = all_gaps[:_TOP_N]

        # ── Write snapshot (rule 25 — append-only, timestamped) ──────────
        run_id      = uuid.uuid4().hex
        computed_at = datetime.now(timezone.utc).isoformat()

        written = storage.write_gap_snapshot(
            path=db_path,
            run_id=run_id,
            computed_at=computed_at,
            rows=all_gaps,           # persist all gaps, not just top 10
        )

        # ── Build AgentResult notes ───────────────────────────────────────
        notes = _build_notes(top_gaps, len(listings))

        finished_at = datetime.now(timezone.utc).isoformat()
        storage.log_cycle(
            path=db_path,
            agent=self.name,
            started_at=started_at,
            finished_at=finished_at,
            records_touched=written,
            status="ok",
            notes=notes,
        )

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=written,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_notes(top_gaps: list[dict], listings_analysed: int) -> str:
    """Produce the human-readable AgentResult notes string."""
    if not top_gaps:
        return f"0 gaps · {listings_analysed} listings analysed"

    top = top_gaps[0]
    top_desc = (
        f"{top['skill']} "
        f"({top['listings_blocked']} listings, "
        f"cost {top['opportunity_cost']:.1f})"
    )
    return (
        f"{len(top_gaps)} gaps · top: {top_desc} · "
        f"{listings_analysed} listings analysed"
    )
