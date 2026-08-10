"""ArbeitnowSource — free public job board, no API key required.

API docs: https://www.arbeitnow.com/api/job-board-api
Pagination: ?page=N  (page 1 is the default)
Rate limit: steering rule 14 — max 1 req/s, enforced by http.get_json().
"""

from __future__ import annotations

import datetime
from typing import Any

from edgedash.config import Config
from edgedash.sources.base import NormalisedRow, register
from edgedash.sources.http import SourceError, get_json

_API_BASE = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_iso_date(timestamp: int | None) -> str | None:
    """Convert a UNIX timestamp to an ISO-8601 date string, or None."""
    if timestamp is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            timestamp, tz=datetime.timezone.utc
        ).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _normalise(raw: dict[str, Any]) -> NormalisedRow:
    """Map one Arbeitnow API row to our canonical schema."""
    return {
        "source": "arbeitnow",
        "external_id": raw.get("slug") or None,
        "title": raw.get("title") or None,
        "company": raw.get("company_name") or None,
        "location": raw.get("location") or None,
        "url": raw.get("url") or None,
        "description": raw.get("description") or None,
        "posted_at": _to_iso_date(raw.get("created_at")),
        "raw": raw,
    }


def _keyword_match(row: NormalisedRow, keywords: list[str]) -> bool:
    """Return True if any keyword appears in the title or description."""
    if not keywords:
        return True
    haystack = " ".join(
        filter(None, [row["title"], row["description"]])
    ).lower()
    return any(kw.lower() in haystack for kw in keywords)


def _city_match(row: NormalisedRow, city: str) -> bool:
    """Return True if the listing location mentions the target city."""
    loc = (row["location"] or "").lower()
    return city.lower() in loc


# ---------------------------------------------------------------------------
# Source class
# ---------------------------------------------------------------------------


@register
class ArbeitnowSource:
    name: str = "arbeitnow"

    def fetch(self, config: Config) -> list[NormalisedRow]:
        """Fetch up to _MAX_PAGES from Arbeitnow and filter by config.

        Paging continues while results keep matching the configured keywords.
        Location filter is applied first; if fewer than 5 results survive,
        the location filter is relaxed and the keyword-only results are used.
        """
        raw_total = 0
        keyword_matched: list[NormalisedRow] = []

        for page in range(1, _MAX_PAGES + 1):
            try:
                payload = get_json(_API_BASE, params={"page": page})
            except SourceError as exc:
                print(f"  [arbeitnow] fetch error on page {page}: {exc}")
                break

            items: list[dict] = payload.get("data", [])
            if not items:
                break

            raw_total += len(items)
            page_hits = [_normalise(r) for r in items if _keyword_match(
                _normalise(r), config.keywords
            )]
            keyword_matched.extend(page_hits)

            # Stop paging early if this page had no keyword matches at all.
            if not page_hits:
                break

        # Apply location filter.
        city_filtered = [
            r for r in keyword_matched
            if _city_match(r, config.target_city)
        ]

        if len(city_filtered) >= 5:
            results = city_filtered
            location_relaxed = False
        else:
            # Relax location filter — return all keyword-matched rows.
            results = keyword_matched
            location_relaxed = True

        print(
            f"  [arbeitnow] raw={raw_total}  keyword_match={len(keyword_matched)}"
            f"  after_location_filter={len(city_filtered)}"
            + ("  (location filter relaxed)" if location_relaxed else "")
            + f"  → returning {len(results)}"
        )

        return results
