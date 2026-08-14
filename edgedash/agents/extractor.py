"""Extraction step for the Scorer — the only place that calls an LLM.

Public API
----------
extract(listing, config, db_path) -> dict

    Extracts structured facts from a job description. Returns a dict
    matching EXTRACTION_SCHEMA. Checks the cache first; calls the model
    only on a miss.

Schema
------
    required_skills : list[str]   skills the role explicitly requires
    nice_to_have    : list[str]   preferred / optional skills
    seniority       : str         "junior"|"mid"|"senior"|"lead"|"unknown"
    years_required  : int | None  null if not stated, never a guess
    remote_ok       : bool | None null if the listing doesn't say

Rule 16: there is no score field here and there must never be one.
Rule 17: every response is validated; failures raise LLMError to the caller.
Rule 18: cache is keyed on a SHA-256 of the description text.
"""

from __future__ import annotations

import hashlib
from typing import Any

import edgedash.storage as storage
from edgedash.config import Config
from edgedash.llm import LLMError, complete_json

# ---------------------------------------------------------------------------
# Schema (passed directly to complete_json for validation)
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA: dict[str, Any] = {
    "required_skills": list,
    "nice_to_have":    list,
    "seniority":       str,
    "years_required":  (int, type(None)),
    "remote_ok":       (bool, type(None)),
}

_VALID_SENIORITY = {"junior", "mid", "senior", "lead", "unknown"}

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are reading a job listing document. Extract structured facts from it.

Rules:
- Report only what the listing explicitly states. Do not infer, do not guess.
- If the listing does not state a value, use null or an empty list as specified.
- Do not evaluate, rank, or score anything.
- Do not mention any candidate or person.

Return a single JSON object with exactly these fields:

  "required_skills"  — list of strings: skills the listing explicitly requires.
                       Empty list [] if none stated.
  "nice_to_have"     — list of strings: skills described as preferred, optional,
                       or "nice to have". Empty list [] if none stated.
  "seniority"        — one of: "junior", "mid", "senior", "lead", "unknown".
                       "unknown" if the listing does not clearly state a level.
  "years_required"   — integer: years of experience the listing asks for.
                       null if not stated. Never estimate.
  "remote_ok"        — true if the listing explicitly says remote is available,
                       false if it explicitly says on-site only,
                       null if not stated.

Job listing:
---
{description}
---\
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _description_hash(text: str) -> str:
    """Stable SHA-256 of the description text, used as the cache key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Lowercase all skill strings; coerce seniority to a known value."""
    data["required_skills"] = [
        s.lower().strip() for s in data.get("required_skills") or []
        if isinstance(s, str) and s.strip()
    ]
    data["nice_to_have"] = [
        s.lower().strip() for s in data.get("nice_to_have") or []
        if isinstance(s, str) and s.strip()
    ]
    seniority = (data.get("seniority") or "unknown").lower().strip()
    data["seniority"] = seniority if seniority in _VALID_SENIORITY else "unknown"
    # years_required: keep int or None, reject any stray string
    yr = data.get("years_required")
    data["years_required"] = int(yr) if isinstance(yr, (int, float)) else None
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(
    listing: dict[str, Any],
    config: Config,
    db_path: str,
) -> dict[str, Any]:
    """Return extracted facts for one listing.

    Checks the cache first. On a miss, calls the model, normalises the
    result, writes it to the cache, and returns it.

    Raises LLMError if the model fails after retries — the caller (Scorer)
    handles this per rule 17 by logging the failure for that listing only.
    """
    description = (listing.get("description") or "").strip()
    if not description:
        # No text to extract from — return a safe empty result without
        # hitting the model. This is not a failure; it is a data gap.
        return {
            "required_skills": [],
            "nice_to_have":    [],
            "seniority":       "unknown",
            "years_required":  None,
            "remote_ok":       None,
        }

    key = _description_hash(description)

    # --- Cache hit ---
    cached = storage.get_extraction(db_path, key)
    if cached is not None:
        return cached

    # --- Cache miss: call the model ---
    prompt = _PROMPT_TEMPLATE.format(description=description)
    raw = complete_json(prompt, EXTRACTION_SCHEMA, config=config, max_retries=1)
    normalised = _normalise(raw)

    storage.set_extraction(db_path, key, normalised)
    return normalised
