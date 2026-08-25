"""Load and validate project configuration from config.yaml at the repo root."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "PyYAML is required: pip install pyyaml"
    ) from exc

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "target_role": "Data Analyst",
    "target_city": "Bengaluru",
    "keywords": [],
    "my_skills": [],
    "experience_years": 0,
    "db_path": "edgedash.db",
    "min_fit_score": 50,
    "sources": ["arbeitnow"],
    "use_mock_fetcher": False,
    "llm_provider": "gemini",
    "llm_model": "gemini-2.5-flash",
    "llm_batch_size": 25,
    "target_seniority": "mid",
    "w_skill_match": 0.45,
    "w_seniority_fit": 0.25,
    "w_location_fit": 0.15,
    "w_recency": 0.15,
    "skill_aliases": {},
    # Orchestration thresholds
    "fetch_interval_hours": 6,
    "fetch_max_pages": 5,
    "fetch_max_listings": 200,
    "score_max_seconds": 300,
    "analyse_max_seconds": 60,
    # Verification thresholds (rule 39)
    "min_score_spread": 10,
    "min_score_stdev": 5.0,
    "max_empty_extraction_pct": 20.0,
    "max_skills_per_listing": 20,
    "min_gap_sample": 3,
    "max_data_age_days": 3,
}

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Config:
    target_role: str
    target_city: str
    keywords: list[str]
    my_skills: list[str]
    experience_years: int
    db_path: str
    min_fit_score: int
    sources: list[str]
    use_mock_fetcher: bool
    llm_provider: str
    llm_model: str
    llm_batch_size: int
    target_seniority: str
    w_skill_match: float
    w_seniority_fit: float
    w_location_fit: float
    w_recency: float
    skill_aliases: dict[str, str]
    # Orchestration thresholds
    fetch_interval_hours: int
    fetch_max_pages: int
    fetch_max_listings: int
    score_max_seconds: int
    analyse_max_seconds: int
    # Verification thresholds (rule 39)
    min_score_spread: int
    min_score_stdev: float
    max_empty_extraction_pct: float
    max_skills_per_listing: int
    min_gap_sample: int
    max_data_age_days: int


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the repository root (the directory containing config.yaml)."""
    # Walk up from this file until we find config.yaml or hit the fs root.
    candidate = Path(__file__).resolve().parent
    for directory in [candidate, *candidate.parents]:
        if (directory / "config.yaml").exists():
            return directory
    # Fall back to cwd, which is correct when running from the repo root.
    return Path(os.getcwd())


def load_config(config_path: Path | None = None) -> Config:
    """Read config.yaml and return a populated Config instance.

    Raises FileNotFoundError with a clear message when config.yaml is absent.
    Missing individual fields fall back to sensible defaults.
    """
    if config_path is None:
        config_path = _repo_root() / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at '{config_path}'. "
            "Copy config.yaml.example to config.yaml and fill in your profile."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    merged = {**_DEFAULTS, **raw}

    return Config(
        target_role=str(merged["target_role"]),
        target_city=str(merged["target_city"]),
        keywords=list(merged["keywords"]),
        my_skills=list(merged["my_skills"]),
        experience_years=int(merged["experience_years"]),
        db_path=str(merged["db_path"]),
        min_fit_score=int(merged["min_fit_score"]),
        sources=list(merged["sources"]),
        use_mock_fetcher=bool(merged["use_mock_fetcher"]),
        llm_provider=str(merged["llm_provider"]),
        llm_model=str(merged["llm_model"]),
        llm_batch_size=int(merged["llm_batch_size"]),
        target_seniority=str(merged["target_seniority"]),
        w_skill_match=float(merged["w_skill_match"]),
        w_seniority_fit=float(merged["w_seniority_fit"]),
        w_location_fit=float(merged["w_location_fit"]),
        w_recency=float(merged["w_recency"]),
        skill_aliases={
            str(k): str(v)
            for k, v in (merged.get("skill_aliases") or {}).items()
        },
        fetch_interval_hours=int(merged["fetch_interval_hours"]),
        fetch_max_pages=int(merged["fetch_max_pages"]),
        fetch_max_listings=int(merged["fetch_max_listings"]),
        score_max_seconds=int(merged["score_max_seconds"]),
        analyse_max_seconds=int(merged["analyse_max_seconds"]),
        # Verification thresholds (rule 39)
        min_score_spread=int(merged["min_score_spread"]),
        min_score_stdev=float(merged["min_score_stdev"]),
        max_empty_extraction_pct=float(merged["max_empty_extraction_pct"]),
        max_skills_per_listing=int(merged["max_skills_per_listing"]),
        min_gap_sample=int(merged["min_gap_sample"]),
        max_data_age_days=int(merged["max_data_age_days"]),
    )
