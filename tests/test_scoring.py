"""Pytest tests for edgedash/scoring.py — pure functions, no network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from edgedash.scoring import (
    _parse_date,
    _recency,
    _seniority_fit,
    _skill_match,
    _location_fit,
    build_reason,
    score_listing,
)


# ---------------------------------------------------------------------------
# Minimal Config stub — only the fields scoring.py reads
# ---------------------------------------------------------------------------

@dataclass
class _Cfg:
    my_skills: list[str]
    target_city: str = "Berlin"
    target_seniority: str = "mid"
    w_skill_match: float = 0.45
    w_seniority_fit: float = 0.25
    w_location_fit: float = 0.15
    w_recency: float = 0.15


def _cfg(**kwargs: Any) -> Any:
    return _Cfg(**kwargs)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _days_ago_iso(n: int) -> str:
    d = datetime.now(timezone.utc).date() - timedelta(days=n)
    return d.isoformat()


# ---------------------------------------------------------------------------
# score_listing: end-to-end cases
# ---------------------------------------------------------------------------

class TestScoreListing:

    def test_perfect_match(self) -> None:
        """All required skills known, exact seniority, remote, posted today."""
        cfg = _cfg(my_skills=["python", "sql", "tableau"])
        listing = {"location": "Remote", "posted_at": _today_iso()}
        facts = {
            "required_skills": ["python", "sql"],
            "nice_to_have":    ["tableau"],
            "seniority":       "mid",
            "years_required":  3,
            "remote_ok":       True,
        }
        result = score_listing(listing, facts, cfg)
        assert result["score"] == 100
        assert "required skills" in result["reason"]
        assert "components" in result

    def test_zero_match(self) -> None:
        """No skills match, wrong seniority, not remote, old listing."""
        cfg = _cfg(my_skills=["excel"])
        listing = {"location": "Tokyo", "posted_at": _days_ago_iso(30)}
        facts = {
            "required_skills": ["kubernetes", "rust", "go"],
            "nice_to_have":    ["spark"],
            "seniority":       "lead",
            "years_required":  8,
            "remote_ok":       False,
        }
        result = score_listing(listing, facts, cfg)
        assert result["score"] <= 20
        assert "gap:" in result["reason"]

    def test_empty_required_skills_no_division_by_zero(self) -> None:
        """empty required_skills must not raise and must give neutral/full score."""
        cfg = _cfg(my_skills=["python"])
        listing = {"location": "Berlin", "posted_at": _today_iso()}
        facts = {
            "required_skills": [],
            "nice_to_have":    [],
            "seniority":       "mid",
            "years_required":  None,
            "remote_ok":       None,
        }
        result = score_listing(listing, facts, cfg)
        # must not raise; score must be a valid int in [0, 100]
        assert isinstance(result["score"], int)
        assert 0 <= result["score"] <= 100
        # skill_match component should be 1.0 (full credit, nothing to fail)
        assert result["components"]["skill_match"] == 1.0

    def test_null_posted_at(self) -> None:
        """None posted_at must not raise and must give recency 0.5."""
        cfg = _cfg(my_skills=["python"])
        listing = {"location": "Berlin", "posted_at": None}
        facts = {
            "required_skills": ["python"],
            "nice_to_have":    [],
            "seniority":       "mid",
            "years_required":  None,
            "remote_ok":       None,
        }
        result = score_listing(listing, facts, cfg)
        assert result["components"]["recency"] == 0.5
        assert isinstance(result["score"], int)

    def test_null_remote_ok(self) -> None:
        """remote_ok=None with a matching city should score location 1.0."""
        cfg = _cfg(my_skills=["python"], target_city="Berlin")
        listing = {"location": "Berlin, Germany", "posted_at": _today_iso()}
        facts = {
            "required_skills": ["python"],
            "nice_to_have":    [],
            "seniority":       "mid",
            "years_required":  None,
            "remote_ok":       None,
        }
        result = score_listing(listing, facts, cfg)
        assert result["components"]["location_fit"] == 1.0

    def test_seniority_three_bands_off(self) -> None:
        """Three or more bands away from target must give seniority_fit = 0.0."""
        cfg = _cfg(my_skills=["python"], target_seniority="junior")
        listing = {"location": "Berlin", "posted_at": _today_iso()}
        facts = {
            "required_skills": [],
            "nice_to_have":    [],
            "seniority":       "lead",   # junior=0, lead=3 → distance 3 → 0.0
            "years_required":  None,
            "remote_ok":       None,
        }
        result = score_listing(listing, facts, cfg)
        assert result["components"]["seniority_fit"] == 0.0


# ---------------------------------------------------------------------------
# Component unit tests
# ---------------------------------------------------------------------------

class TestSkillMatch:

    def test_all_known(self) -> None:
        cfg = _cfg(my_skills=["python", "sql"])
        assert _skill_match({"required_skills": ["python"], "nice_to_have": ["sql"]}, cfg) == 1.0

    def test_none_known(self) -> None:
        cfg = _cfg(my_skills=["excel"])
        val = _skill_match({"required_skills": ["python", "rust"], "nice_to_have": []}, cfg)
        assert val == 0.0

    def test_nice_counts_at_one_third(self) -> None:
        cfg = _cfg(my_skills=["tableau"])
        # required=["python"] miss, nice=["tableau"] hit
        # numerator = 0 + 1/3 = 0.333, denominator = 1 + 1/3 = 1.333
        val = _skill_match({"required_skills": ["python"], "nice_to_have": ["tableau"]}, cfg)
        assert abs(val - (1/3) / (1 + 1/3)) < 0.001

    def test_case_insensitive(self) -> None:
        cfg = _cfg(my_skills=["PostgreSQL"])
        assert _skill_match({"required_skills": ["postgres", "postgresql"], "nice_to_have": []}, cfg) > 0

    def test_empty_both_lists(self) -> None:
        cfg = _cfg(my_skills=["python"])
        assert _skill_match({"required_skills": [], "nice_to_have": []}, cfg) == 1.0


class TestSeniorityFit:

    def test_exact_match(self) -> None:
        cfg = _cfg(my_skills=[], target_seniority="senior")
        assert _seniority_fit({"seniority": "senior"}, cfg) == 1.0

    def test_one_band(self) -> None:
        cfg = _cfg(my_skills=[], target_seniority="mid")
        assert _seniority_fit({"seniority": "senior"}, cfg) == 0.6

    def test_two_bands(self) -> None:
        cfg = _cfg(my_skills=[], target_seniority="junior")
        assert _seniority_fit({"seniority": "senior"}, cfg) == 0.25

    def test_three_bands(self) -> None:
        cfg = _cfg(my_skills=[], target_seniority="junior")
        assert _seniority_fit({"seniority": "lead"}, cfg) == 0.0

    def test_unknown_seniority(self) -> None:
        cfg = _cfg(my_skills=[], target_seniority="mid")
        assert _seniority_fit({"seniority": "unknown"}, cfg) == 0.5


class TestRecency:

    def test_today(self) -> None:
        assert _recency({"posted_at": _today_iso()}) == 1.0

    def test_30_days_ago(self) -> None:
        assert _recency({"posted_at": _days_ago_iso(30)}) == 0.0

    def test_15_days_ago(self) -> None:
        val = _recency({"posted_at": _days_ago_iso(15)})
        assert abs(val - 0.5) < 0.01

    def test_null(self) -> None:
        assert _recency({"posted_at": None}) == 0.5

    def test_missing_key(self) -> None:
        assert _recency({}) == 0.5

    def test_human_string_days_ago(self) -> None:
        val = _recency({"posted_at": "3 days ago"})
        assert abs(val - (1 - 3 / 30)) < 0.01

    def test_human_string_today(self) -> None:
        assert _recency({"posted_at": "today"}) == 1.0


class TestBuildReason:

    def test_gap_lists_missing_required_skills(self) -> None:
        cfg = _cfg(my_skills=["python"])
        facts = {
            "required_skills": ["python", "kubernetes", "spark"],
            "nice_to_have":    [],
            "seniority":       "mid",
            "remote_ok":       True,
        }
        components = {"skill_match": 0.33, "seniority_fit": 1.0,
                      "location_fit": 1.0, "recency": 1.0}
        reason = build_reason(components, facts, cfg)
        assert "kubernetes" in reason
        assert "spark" in reason
        assert "gap:" in reason

    def test_no_gap_when_all_skills_known(self) -> None:
        cfg = _cfg(my_skills=["python", "sql"])
        facts = {
            "required_skills": ["python", "sql"],
            "nice_to_have":    [],
            "seniority":       "mid",
            "remote_ok":       True,
        }
        components = {"skill_match": 1.0, "seniority_fit": 1.0,
                      "location_fit": 1.0, "recency": 1.0}
        reason = build_reason(components, facts, cfg)
        assert "gap:" not in reason
