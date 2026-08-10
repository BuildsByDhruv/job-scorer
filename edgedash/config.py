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
    )
