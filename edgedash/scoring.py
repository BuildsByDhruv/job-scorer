"""Deterministic scorer — pure functions only.

No model calls. No network. No imports from llm.py.
The only inputs are the listing dict, the extracted facts dict, and Config.

Public API
----------
score_listing(listing, facts, config)  ->  {"score": int, "reason": str,
                                            "components": {...}}
build_reason(components, facts, config) ->  str

Four components, each normalised 0.0-1.0:
    skill_match    weight default 0.45
    seniority_fit  weight default 0.25
    location_fit   weight default 0.15
    recency        weight default 0.15

Final score  =  round(weighted_sum * 100)  clamped to [0, 100].
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Seniority band ordering
# ---------------------------------------------------------------------------

_SENIORITY_ORDER: dict[str, int] = {
    "junior":  0,
    "mid":     1,
    "senior":  2,
    "lead":    3,
    "unknown": -1,   # sentinel — handled explicitly
}

_SENIORITY_SCORE: dict[int, float] = {
    0: 1.0,   # exact match
    1: 0.6,   # one band away
    2: 0.25,  # two bands away
}   # three or more → 0.0

# ---------------------------------------------------------------------------
# Component functions
# ---------------------------------------------------------------------------


def _skill_match(facts: dict[str, Any], config: Config) -> float:
    """Fraction of required skills covered, with nice-to-have at 1/3 weight.

    Empty required_skills: full credit (nothing is blocked).
    All comparisons are case-insensitive; normalisation is the extractor's job.
    """
    my_skills = {s.lower().strip() for s in config.my_skills}

    required: list[str] = facts.get("required_skills") or []
    nice:     list[str] = facts.get("nice_to_have")    or []

    if not required and not nice:
        return 1.0                          # no information → neutral full credit

    required_hits = sum(1 for s in required if s.lower() in my_skills)
    nice_hits     = sum(1 for s in nice     if s.lower() in my_skills)

    # Weighted numerator / denominator
    # required counts 1.0 each, nice_to_have counts 1/3 each
    numerator   = required_hits + nice_hits / 3.0
    denominator = len(required) + len(nice) / 3.0

    if denominator == 0.0:          # guard — cannot happen after the check above
        return 1.0

    return min(numerator / denominator, 1.0)


def _seniority_fit(facts: dict[str, Any], config: Config) -> float:
    """Band-distance score between extracted seniority and target_seniority."""
    target  = getattr(config, "target_seniority", "mid")
    listing = (facts.get("seniority") or "unknown").lower()

    if listing == "unknown":
        return 0.5                          # no information → neutral

    target_idx  = _SENIORITY_ORDER.get(target.lower(),  1)   # default mid=1
    listing_idx = _SENIORITY_ORDER.get(listing,         -1)

    if listing_idx == -1:
        return 0.5                          # unrecognised value → neutral

    distance = abs(target_idx - listing_idx)
    return _SENIORITY_SCORE.get(distance, 0.0)


def _location_fit(facts: dict[str, Any], listing: dict[str, Any],
                  config: Config) -> float:
    """Score location relevance."""
    remote_ok = facts.get("remote_ok")
    if remote_ok is True:
        return 1.0

    location = (listing.get("location") or "").lower()
    city     = (config.target_city or "").lower().strip()

    if city and city in location:
        return 1.0

    if remote_ok is None and not location:
        return 0.5                          # nothing known → neutral

    if remote_ok is False or (location and city and city not in location):
        return 0.1                          # explicitly elsewhere / not remote

    return 0.5                              # ambiguous


def _recency(listing: dict[str, Any]) -> float:
    """Decay from 1.0 (today) to 0.0 at 30 days. Null posted_at → 0.5."""
    raw = listing.get("posted_at")
    if not raw:
        return 0.5

    # posted_at may be an ISO date "2026-08-01", an ISO datetime, or a
    # human string like "3 days ago" (from Apify). Handle all three.
    try:
        posted = _parse_date(str(raw))
    except ValueError:
        return 0.5

    if posted is None:
        return 0.5

    today = datetime.now(timezone.utc).date()
    age_days = (today - posted).days

    if age_days <= 0:
        return 1.0
    if age_days >= 30:
        return 0.0

    # Linear decay 1.0 → 0.0 over 30 days
    return 1.0 - age_days / 30.0


def _parse_date(raw: str) -> date | None:
    """Return a date from an ISO string or a 'N days ago' string.

    Returns None if parsing fails rather than raising (caller handles None).
    """
    raw = raw.strip()

    # ISO date or datetime
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw[:26], fmt)   # cap at 26 chars for tz
            return dt.date()
        except ValueError:
            pass

    # "today" / "yesterday"
    today = datetime.now(timezone.utc).date()
    lower = raw.lower()
    if lower in ("today", "just now", "0 days ago"):
        return today
    if lower == "yesterday":
        from datetime import timedelta
        return today - timedelta(days=1)

    # "N days ago", "N day ago"
    import re
    m = re.match(r"(\d+)\s+day", lower)
    if m:
        from datetime import timedelta
        return today - timedelta(days=int(m.group(1)))

    return None


# ---------------------------------------------------------------------------
# Weight helpers
# ---------------------------------------------------------------------------


def _weights(config: Config) -> dict[str, float]:
    """Return the four scoring weights from config, falling back to defaults."""
    w = {
        "skill_match":   float(getattr(config, "w_skill_match",   0.45)),
        "seniority_fit": float(getattr(config, "w_seniority_fit", 0.25)),
        "location_fit":  float(getattr(config, "w_location_fit",  0.15)),
        "recency":       float(getattr(config, "w_recency",        0.15)),
    }
    # Normalise so weights always sum to 1.0, tolerating small rounding errors.
    total = sum(w.values())
    if total <= 0:
        raise ValueError(f"Scoring weights sum to {total}; they must be positive.")
    return {k: v / total for k, v in w.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_listing(
    listing: dict[str, Any],
    facts:   dict[str, Any],
    config:  Config,
) -> dict[str, Any]:
    """Compute a deterministic 0-100 score for one listing.

    Returns:
        {
            "score":      int (0-100),
            "reason":     str (human-readable, assembled from components),
            "components": {
                "skill_match":   float 0-1,
                "seniority_fit": float 0-1,
                "location_fit":  float 0-1,
                "recency":       float 0-1,
            }
        }
    """
    components = {
        "skill_match":   _skill_match(facts, config),
        "seniority_fit": _seniority_fit(facts, config),
        "location_fit":  _location_fit(facts, listing, config),
        "recency":       _recency(listing),
    }

    w = _weights(config)
    raw = sum(components[k] * w[k] for k in components)
    score = max(0, min(100, round(raw * 100)))

    reason = build_reason(components, facts, config)

    return {"score": score, "reason": reason, "components": components}


def build_reason(
    components: dict[str, float],
    facts:      dict[str, Any],
    config:     Config,
) -> str:
    """Build a compact human-readable reason FROM THE NUMBERS only.

    Never calls a model. Output style:
        "4/6 required skills · seniority fits · remote · posted 2d ago · gap: kubernetes, spark"
    """
    parts: list[str] = []

    # ── skill match ──────────────────────────────────────────────────────────
    my_skills  = {s.lower().strip() for s in config.my_skills}
    required: list[str] = facts.get("required_skills") or []
    nice:     list[str] = facts.get("nice_to_have")    or []

    req_hits  = [s for s in required if s.lower() in my_skills]
    req_miss  = [s for s in required if s.lower() not in my_skills]
    nice_miss = [s for s in nice     if s.lower() not in my_skills]

    if required:
        parts.append(f"{len(req_hits)}/{len(required)} required skills")
    else:
        parts.append("no required skills listed")

    # ── seniority ────────────────────────────────────────────────────────────
    sen_score = components["seniority_fit"]
    if sen_score == 1.0:
        parts.append("seniority fits")
    elif sen_score >= 0.6:
        parts.append("seniority close")
    elif sen_score == 0.5:
        parts.append("seniority unknown")
    else:
        listing_sen = facts.get("seniority", "unknown")
        target_sen  = getattr(config, "target_seniority", "mid")
        parts.append(f"seniority mismatch ({listing_sen} vs {target_sen})")

    # ── location ─────────────────────────────────────────────────────────────
    loc_score = components["location_fit"]
    if facts.get("remote_ok") is True:
        parts.append("remote")
    elif loc_score == 1.0:
        parts.append(f"in {config.target_city}")
    elif loc_score == 0.5:
        parts.append("location unknown")
    else:
        parts.append("not remote / not local")

    # ── recency ──────────────────────────────────────────────────────────────
    rec_score = components["recency"]
    if rec_score == 0.5:
        parts.append("date unknown")
    elif rec_score == 1.0:
        parts.append("posted today")
    else:
        # Back-calculate approximate age in days from the linear decay
        age_days = round((1.0 - rec_score) * 30)
        parts.append(f"posted {age_days}d ago")

    # ── skill gaps ───────────────────────────────────────────────────────────
    gaps = req_miss[:5]   # cap at 5 for readability; required gaps first
    if not gaps:
        gaps = nice_miss[:3]
    if gaps:
        parts.append("gap: " + ", ".join(gaps))

    return " · ".join(parts)
