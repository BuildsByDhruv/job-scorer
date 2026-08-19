"""Scorer agent — extracts facts then deterministically scores each listing.

No model calls happen here directly. Extraction is delegated to extractor.py,
which is the only file that touches llm.py.

Rule 17: per-listing try/except — one failure is one skipped listing.
Rule 18: only listings WHERE fit_score IS NULL are processed.
Rule 20: score distribution logged after every batch.
Rule 21: batch capped at config.llm_batch_size.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.llm import LLMError, LLMQuotaExhausted
from edgedash.scoring import score_listing

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


class Scorer:
    name: str = "scorer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: "StopConditions | None" = None,
    ) -> AgentResult:
        # Respect Orchestrator-supplied limits (rule 29).
        batch_size = (
            stop_conditions.max_items
            if stop_conditions and stop_conditions.max_items is not None
            else config.llm_batch_size
        )
        max_seconds = (
            stop_conditions.max_seconds
            if stop_conditions and stop_conditions.max_seconds is not None
            else None
        )

        batch = storage.get_unscored_listings(db_path, limit=batch_size)

        if not batch:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="no unscored listings",
            )

        scored_count = 0
        failed_count = 0
        scores: list[int] = []
        started_at = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + max_seconds if max_seconds else None

        for listing in batch:
            # Wall-clock stop condition (rule 29).
            if deadline is not None and time.monotonic() >= deadline:
                print(f"  ⚠  [scorer] max_seconds={max_seconds} reached — "
                      f"stopping after {scored_count} scored")
                break

            listing_id = listing["id"]
            try:
                facts  = extract(listing, config, db_path)
                result = score_listing(listing, facts, config)
            except LLMQuotaExhausted as exc:
                # Daily quota is gone — no point attempting further listings.
                failed_count += 1
                notes = (
                    f"scored {scored_count} · STOPPED: daily quota exhausted "
                    f"· {failed_count} failed · quota resets midnight Pacific"
                )
                storage.log_cycle(
                    path=db_path,
                    agent="scorer/quota",
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    records_touched=scored_count,
                    status="failed",
                    notes=str(exc),
                )
                print(f"  ✗  [scorer] daily quota exhausted — stopping batch. "
                      f"({scored_count} scored before hitting limit)")
                return AgentResult(
                    agent=self.name,
                    status="failed",
                    records_touched=scored_count,
                    notes=notes,
                )
            except LLMError as exc:
                failed_count += 1
                storage.log_cycle(
                    path=db_path,
                    agent=f"scorer/extract/{listing_id[:12]}",
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    records_touched=0,
                    status="failed",
                    notes=str(exc),
                )
                print(f"  ⚠  [scorer] listing {listing_id[:12]}… failed — {exc}")
                continue
            except Exception as exc:
                failed_count += 1
                storage.log_cycle(
                    path=db_path,
                    agent=f"scorer/score/{listing_id[:12]}",
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    records_touched=0,
                    status="failed",
                    notes=str(exc),
                )
                print(f"  ⚠  [scorer] listing {listing_id[:12]}… error — {exc}")
                continue

            storage.write_score(
                path=db_path,
                listing_id=listing_id,
                score=result["score"],
                reason=result["reason"],
                components=result["components"],
                scored_at=datetime.now(timezone.utc).isoformat(),
            )
            scores.append(result["score"])
            scored_count += 1

        # ── Distribution log (rule 20) ────────────────────────────────────────
        notes = _distribution_notes(scores, failed_count)
        finished_at = datetime.now(timezone.utc).isoformat()

        dist_status, dist_notes = _distribution_status(scores, failed_count)
        storage.log_cycle(
            path=db_path,
            agent="scorer/distribution",
            started_at=started_at,
            finished_at=finished_at,
            records_touched=scored_count,
            status=dist_status,
            notes=dist_notes,
        )

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=scored_count,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Distribution helpers
# ---------------------------------------------------------------------------


def _distribution_status(scores: list[int], failed: int) -> tuple[str, str]:
    """Return (status, notes) for the distribution cycle_log row."""
    if not scores:
        return "ok", f"no scores produced · {failed} failed"

    lo   = min(scores)
    hi   = max(scores)
    mean = statistics.mean(scores)
    spread = hi - lo

    base = (
        f"count={len(scores)} min={lo} max={hi} "
        f"mean={mean:.1f} spread={spread}"
    )
    if spread < 10:
        return "suspect", base + " · SUSPECT: all scores within 10 points"
    return "ok", base


def _distribution_notes(scores: list[int], failed: int) -> str:
    """Build the AgentResult.notes string."""
    if not scores:
        return f"scored 0 · {failed} failed"

    lo   = min(scores)
    hi   = max(scores)
    mean = statistics.mean(scores)
    spread = hi - lo
    spread_label = "spread SUSPECT" if spread < 10 else "spread OK"

    parts = [
        f"scored {len(scores)}",
        f"range {lo}-{hi}",
        f"mean {mean:.0f}",
    ]
    if failed:
        parts.append(f"{failed} failed")
    parts.append(spread_label)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    from edgedash.config import load_config
    import edgedash.storage as _storage

    parser = argparse.ArgumentParser(
        description="Run the Scorer agent standalone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m edgedash.agents.scorer --limit 5\n"
            "  python -m edgedash.agents.scorer --limit 25 --db edgedash.db"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max listings to score (overrides config.llm_batch_size).",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to the SQLite database (overrides config.db_path).",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.limit is not None:
        config = config.__class__(**{**config.__dict__, "llm_batch_size": args.limit})
    db_path = args.db or config.db_path

    _storage.init_db(db_path)
    unscored = _storage.count_unscored(db_path)
    print(f"  db           : {db_path}")
    print(f"  unscored     : {unscored}")
    print(f"  batch limit  : {config.llm_batch_size}")
    print()

    if unscored == 0:
        print("  Nothing to score.")
        sys.exit(0)

    result = Scorer().run(config, db_path)
    print(f"  status : {result.status}")
    print(f"  notes  : {result.notes}")
