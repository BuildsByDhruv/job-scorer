"""Real Fetcher agent — queries live job-board sources.

Per-source failures are caught and logged individually (steering rule 12).
A single dead source never kills the cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.sources.base import SOURCES
from edgedash.sources.http import SourceError

# Import all registered source modules so @register decorators run.
import edgedash.sources.arbeitnow  # noqa: F401
import edgedash.sources.apify      # noqa: F401


class Fetcher:
    name: str = "fetcher"

    def run(self, config: Config, db_path: str) -> AgentResult:
        source_summaries: list[str] = []
        total_new = 0

        for source_name in config.sources:
            source_cls = SOURCES.get(source_name)
            if source_cls is None:
                msg = f"unknown source '{source_name}' — not in registry"
                print(f"  ⚠  [fetcher] {msg}")
                _log_source(db_path, source_name, "failed", 0, msg)
                source_summaries.append(f"{source_name}: FAILED (not registered)")
                continue

            source = source_cls()
            started_at = datetime.now(timezone.utc).isoformat()

            try:
                rows = source.fetch(config)
            except (SourceError, Exception) as exc:
                reason = type(exc).__name__
                msg = f"{source_name}: {exc}"
                print(f"  ⚠  [fetcher] source '{source_name}' failed — {exc}")
                _log_source(db_path, source_name, "failed", 0, str(exc))
                source_summaries.append(f"{source_name}: FAILED ({reason})")
                continue

            # Translate normalised source rows → storage schema.
            storage_rows = _to_storage_rows(rows)
            new_count = storage.upsert_listings(db_path, storage_rows)
            total_new += new_count

            finished_at = datetime.now(timezone.utc).isoformat()
            _log_source(db_path, source_name, "ok", new_count,
                        f"{len(rows)} fetched, {new_count} new",
                        started_at=started_at, finished_at=finished_at)
            source_summaries.append(
                f"{source_name}: {len(rows)} rows ({new_count} new)"
            )

        notes = " | ".join(source_summaries) if source_summaries else "no sources ran"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=total_new,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_storage_rows(rows: list[dict]) -> list[dict]:
    """Map normalised source rows to the schema expected by upsert_listings.

    upsert_listings computes the stable id itself via storage._stable_id,
    so we don't recompute it here — we just pass source + url through.
    Required keys: title, company, location, url, source.
    """
    storage_rows = []
    for r in rows:
        # Skip rows missing any required field — fail loudly with a print.
        missing = [k for k in ("title", "company", "location", "url", "source")
                   if not r.get(k)]
        if missing:
            print(f"  ⚠  [fetcher] dropping row missing {missing}: {r.get('url')}")
            continue
        storage_rows.append({
            "title":       r["title"],
            "company":     r["company"],
            "location":    r["location"],
            "url":         r["url"],
            "source":      r["source"],
            "description": r.get("description"),
            "posted_at":   r.get("posted_at"),
        })
    return storage_rows


def _log_source(
    db_path: str,
    source_name: str,
    status: str,
    records_touched: int,
    notes: str,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    storage.log_cycle(
        path=db_path,
        agent=f"fetcher/{source_name}",
        started_at=started_at or now,
        finished_at=finished_at or now,
        records_touched=records_touched,
        status=status,
        notes=notes,
    )
