"""ApifySource — scrapes Indeed via the Apify jobscrawler/indeed-jobs-scraper actor.

Requires APIFY_TOKEN in the environment (loaded from .env by run_cycle.py).
If the token is absent, returns an empty list with a clear log line —
per steering rule 13, a missing key must never crash the cycle.

Actor: jobscrawler/indeed-jobs-scraper
Docs:  https://apify.com/jobscrawler/indeed-jobs-scraper

Actor output fields (jobscrawler/indeed-jobs-scraper):
    jobTitle     → title
    companyName  → company
    location     → location
    applyUrl     → url
    description  → description
    postedDate   → posted_at   (human string: "1 day ago", "2025-01-15T...")
    scrapedAt    → used to build external_id when actor provides no stable id
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import requests as _requests

from edgedash.config import Config
from edgedash.sources.base import NormalisedRow, register
from edgedash.sources.http import (
    _BACKOFF_BASE,
    _MAX_RETRIES,
    _TIMEOUT_SECONDS,
    _USER_AGENT,
    _rate_limit,
    SourceError,
)

_ACTOR_ID        = "jobscrawler~indeed-jobs-scraper"
_API_BASE        = (
    f"https://api.apify.com/v2/acts/{_ACTOR_ID}/run-sync-get-dataset-items"
)
_MAX_ITEMS       = 100
# Apify run-sync waits for the actor to complete. Actor cold-start + scraping
# easily takes 60-180s, so we give it up to 5 minutes before giving up.
_APIFY_TIMEOUT   = 300


# ---------------------------------------------------------------------------
# Source class
# ---------------------------------------------------------------------------


@register
class ApifySource:
    name: str = "apify"

    def fetch(self, config: Config) -> list[NormalisedRow]:
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("  [apify] no APIFY_TOKEN in environment — skipping")
            return []

        params = {"token": token}
        body = {
            "keyword":    config.target_role,
            "location":   config.target_city,
            "maxItems":   _MAX_ITEMS,
            "sortBy":     "date",
            "proxyEnabled": True,
        }

        from urllib.parse import urlparse

        host = urlparse(_API_BASE).netloc
        _rate_limit(host)

        headers = {
            "User-Agent":   _USER_AGENT,
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = _requests.post(
                    _API_BASE,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=_APIFY_TIMEOUT,   # actor run can take minutes
                )
                resp.raise_for_status()
                items: list[dict] = resp.json()
                break
            except _requests.RequestException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))
        else:
            raise SourceError(
                f"POST {_API_BASE} failed after {_MAX_RETRIES + 1} attempts: "
                f"{last_exc}"
            ) from last_exc

        raw_count = len(items)
        # Skip error items (actor may push {"error": "..."} sentinel rows).
        valid = [r for r in items if not r.get("error")]
        normalised = [_normalise(r) for r in valid]

        print(
            f"  [apify] raw={raw_count}  valid={len(normalised)}"
            f"  (capped at {_MAX_ITEMS})"
        )
        return normalised


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


def _stable_id(url: str | None, scraped_at: str | None) -> str | None:
    """Derive a stable external_id when the actor provides no native id field.

    Uses a SHA-256 of applyUrl so the same job URL always maps to the same id.
    Falls back to hashing (url + scraped_at) if url is absent.
    """
    key = url or (f"apify:{scraped_at}" if scraped_at else None)
    if not key:
        return None
    return hashlib.sha256(key.encode()).hexdigest()


def _normalise(raw: dict[str, Any]) -> NormalisedRow:
    """Map one jobscrawler/indeed-jobs-scraper row to our canonical schema.

    Actor field   →  our field
    ────────────────────────────
    jobTitle      →  title
    companyName   →  company
    location      →  location
    applyUrl      →  url
    description   →  description
    postedDate    →  posted_at
    (derived)     →  external_id   (hash of applyUrl)
    """
    url        = raw.get("applyUrl") or None
    scraped_at = raw.get("scrapedAt") or None
    return {
        "source":      "apify",
        "external_id": _stable_id(url, scraped_at),
        "title":       raw.get("jobTitle") or None,
        "company":     raw.get("companyName") or None,
        "location":    raw.get("location") or None,
        "url":         url,
        "description": raw.get("description") or None,
        "posted_at":   raw.get("postedDate") or None,
        "raw":         raw,
    }
