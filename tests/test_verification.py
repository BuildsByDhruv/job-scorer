"""Tests for edgedash/verification.py.

Every test is pure: no disk, no network, no real Config object.
Config is faked with types.SimpleNamespace — the checks use getattr()
with defaults so any object with the right attributes works.

Coverage matrix
---------------
check_score_spread          pass | fail (spread) | fail (stdev) | < 5 scores
check_extraction_sanity     pass | fail (empty%)  | fail (size)  | empty list
check_gap_sample_size       pass | fail           | empty gaps
check_freshness             pass | fail (stale)   | None ts      | bad ts
run_all_checks              all-pass | one-fail
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from edgedash.verification import (
    CheckResult,
    Verdict,
    check_extraction_sanity,
    check_freshness,
    check_gap_sample_size,
    check_score_spread,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> types.SimpleNamespace:
    """Build a minimal config namespace with sensible defaults."""
    defaults = {
        "min_score_spread": 10,
        "min_score_stdev": 5.0,
        "max_empty_extraction_pct": 20.0,
        "max_skills_per_listing": 20,
        "min_gap_sample": 3,
        "max_data_age_days": 3,
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _gap(skill: str, listings_blocked: int) -> dict:
    return {
        "skill": skill,
        "listings_blocked": listings_blocked,
        "opportunity_cost": listings_blocked * 0.7,
        "mean_score": 70,
        "top_score": 85,
        "also_nice_to_have": 0,
        "low_confidence": listings_blocked < 3,
        "example_ids": [],
    }


def _facts(skills: list[str]) -> dict:
    return {"required_skills": skills, "nice_to_have": []}


_NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


# ===========================================================================
# check_score_spread
# ===========================================================================


class TestCheckScoreSpread:

    def test_pass_adequate_spread(self) -> None:
        # Scores span 55 points; stdev well above 5.
        scores = [20, 35, 50, 65, 75]
        result = check_score_spread(scores, _cfg())
        assert result.passed is True
        assert result.name == "score_spread"
        assert result.observed["spread"] == 55
        assert result.observed["stdev"] > 5.0

    def test_fail_spread_too_small(self) -> None:
        # All scores within a 6-point window → spread fails (< 10).
        scores = [60, 62, 63, 65, 66]
        result = check_score_spread(scores, _cfg(min_score_spread=10))
        assert result.passed is False
        assert result.observed["spread"] == 6
        assert "spread" in result.message

    def test_fail_stdev_too_small(self) -> None:
        # Spread is 12 (passes), but stdev is tiny — all clustered in the middle.
        # Use scores that give spread >= 10 but stdev < 5.
        scores = [49, 50, 50, 50, 61]   # spread=12, pstdev ≈ 4.0
        result = check_score_spread(scores, _cfg(min_score_spread=10, min_score_stdev=5.0))
        assert result.observed["spread"] == 12
        assert result.observed["stdev"] < 5.0
        assert result.passed is False
        assert "stdev" in result.message

    def test_pass_trivially_fewer_than_five(self) -> None:
        # Fewer than 5 scores → trivial pass, no arithmetic.
        for count in (0, 1, 3, 4):
            scores = list(range(count))
            result = check_score_spread(scores, _cfg())
            assert result.passed is True, f"Expected trivial pass for {count} scores"
            assert str(count) in result.message or "only" in result.message.lower()

    def test_observed_fields_present(self) -> None:
        scores = [10, 30, 50, 70, 90]
        result = check_score_spread(scores, _cfg())
        assert "spread" in result.observed
        assert "stdev" in result.observed
        assert "count" in result.observed

    def test_threshold_fields_present(self) -> None:
        result = check_score_spread([10, 30, 50, 70, 90], _cfg())
        assert "min_score_spread" in result.threshold
        assert "min_score_stdev" in result.threshold


# ===========================================================================
# check_extraction_sanity
# ===========================================================================


class TestCheckExtractionSanity:

    def test_pass_all_non_empty(self) -> None:
        facts = [_facts(["python", "sql"]), _facts(["java"]), _facts(["go", "k8s"])]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed is True
        assert result.observed["empty"] == 0

    def test_fail_too_many_empty(self) -> None:
        # 3 of 5 listings are empty → 60 % > default 20 %.
        facts = [
            _facts([]),
            _facts([]),
            _facts([]),
            _facts(["python"]),
            _facts(["sql"]),
        ]
        result = check_extraction_sanity(facts, _cfg(max_empty_extraction_pct=20.0))
        assert result.passed is False
        assert result.observed["empty"] == 3
        assert result.observed["empty_pct"] == 60.0
        assert "empty" in result.message.lower() or "extraction" in result.message.lower()

    def test_fail_skills_list_too_long(self) -> None:
        # One listing has 25 skills → exceeds max_skills_per_listing=20.
        fat_skills = [f"skill_{i}" for i in range(25)]
        facts = [_facts(["python"]), _facts(fat_skills), _facts(["sql"])]
        result = check_extraction_sanity(facts, _cfg(max_skills_per_listing=20))
        assert result.passed is False
        assert result.observed["max_skills_observed"] == 25
        assert "25" in result.message

    def test_pass_trivially_empty_list(self) -> None:
        result = check_extraction_sanity([], _cfg())
        assert result.passed is True
        assert "trivially" in result.message.lower()

    def test_boundary_exactly_at_threshold(self) -> None:
        # Exactly 20 % empty with 5 listings → should PASS (boundary is inclusive).
        facts = [_facts([]), _facts(["a"]), _facts(["b"]), _facts(["c"]), _facts(["d"])]
        result = check_extraction_sanity(facts, _cfg(max_empty_extraction_pct=20.0))
        assert result.observed["empty_pct"] == 20.0
        assert result.passed is True

    def test_both_failures_captured(self) -> None:
        # Trigger both failure modes simultaneously.
        fat_skills = [f"s{i}" for i in range(25)]
        facts = [_facts([]), _facts([]), _facts([]), _facts(fat_skills), _facts(["x"])]
        result = check_extraction_sanity(facts, _cfg(
            max_empty_extraction_pct=20.0,
            max_skills_per_listing=20,
        ))
        assert result.passed is False
        # Both conditions should be mentioned in the message.
        assert "25" in result.message
        assert "empty" in result.message.lower() or "60" in result.message


# ===========================================================================
# check_gap_sample_size
# ===========================================================================


class TestCheckGapSampleSize:

    def test_pass_top_gap_has_enough_listings(self) -> None:
        gaps = [_gap("kubernetes", 5), _gap("spark", 2)]
        result = check_gap_sample_size(gaps, _cfg(min_gap_sample=3))
        assert result.passed is True
        assert result.observed["listings_blocked"] == 5

    def test_fail_top_gap_has_too_few_listings(self) -> None:
        gaps = [_gap("terraform", 2), _gap("spark", 8)]
        result = check_gap_sample_size(gaps, _cfg(min_gap_sample=3))
        assert result.passed is False
        assert result.observed["top_skill"] == "terraform"
        assert result.observed["listings_blocked"] == 2
        assert "2" in result.message
        assert "3" in result.message

    def test_pass_trivially_no_gaps(self) -> None:
        result = check_gap_sample_size([], _cfg())
        assert result.passed is True
        assert "trivially" in result.message.lower()

    def test_boundary_exactly_at_min(self) -> None:
        gaps = [_gap("docker", 3)]
        result = check_gap_sample_size(gaps, _cfg(min_gap_sample=3))
        assert result.passed is True

    def test_top_skill_name_in_observed(self) -> None:
        gaps = [_gap("airflow", 10)]
        result = check_gap_sample_size(gaps, _cfg())
        assert result.observed["top_skill"] == "airflow"


# ===========================================================================
# check_freshness
# ===========================================================================


class TestCheckFreshness:

    def test_pass_data_is_fresh(self) -> None:
        # Fetch was 1 day ago; threshold is 3.
        ts = "2026-08-23T10:00:00+00:00"
        result = check_freshness(ts, _cfg(max_data_age_days=3), _NOW)
        assert result.passed is True
        assert result.observed["age_days"] == pytest.approx(1.083, abs=0.01)

    def test_fail_data_is_stale(self) -> None:
        # Fetch was 5 days ago; threshold is 3.
        ts = "2026-08-19T12:00:00+00:00"
        result = check_freshness(ts, _cfg(max_data_age_days=3), _NOW)
        assert result.passed is False
        assert result.observed["age_days"] == pytest.approx(5.0, abs=0.01)
        assert "5.0" in result.message or "exceed" in result.message.lower()

    def test_fail_none_timestamp(self) -> None:
        result = check_freshness(None, _cfg(), _NOW)
        assert result.passed is False
        assert result.observed["latest_fetch_at"] is None
        assert "no fetch" in result.message.lower() or "none" in result.message.lower() or "empty" in result.message.lower()

    def test_fail_unparseable_timestamp(self) -> None:
        result = check_freshness("not-a-date", _cfg(), _NOW)
        assert result.passed is False
        assert "not-a-date" in result.message

    def test_naive_now_treated_as_utc(self) -> None:
        # Passing a naive datetime should not raise; still produces a valid result.
        naive_now = datetime(2026, 8, 24, 12, 0, 0)
        ts = "2026-08-23T12:00:00+00:00"
        result = check_freshness(ts, _cfg(max_data_age_days=3), naive_now)
        assert result.passed is True

    def test_z_suffix_accepted(self) -> None:
        ts = "2026-08-24T11:00:00Z"
        result = check_freshness(ts, _cfg(max_data_age_days=3), _NOW)
        assert result.passed is True

    def test_threshold_in_result(self) -> None:
        ts = "2026-08-22T12:00:00+00:00"
        result = check_freshness(ts, _cfg(max_data_age_days=3), _NOW)
        assert result.threshold == {"max_data_age_days": 3}


# ===========================================================================
# run_all_checks
# ===========================================================================


class TestRunAllChecks:

    def _good_inputs(self) -> dict:
        """Returns kwargs that cause all four checks to pass."""
        return {
            "scores": [20, 40, 55, 70, 85],
            "facts_list": [_facts(["python"]), _facts(["sql"]), _facts(["go"])],
            "gaps": [_gap("kubernetes", 5), _gap("spark", 4)],
            "latest_fetch_at": "2026-08-23T12:00:00+00:00",
            "config": _cfg(),
            "now": _NOW,
        }

    def test_all_pass(self) -> None:
        verdict = run_all_checks(**self._good_inputs())
        assert isinstance(verdict, Verdict)
        assert verdict.passed is True
        assert verdict.failed_checks == []
        assert "All" in verdict.summary

    def test_one_fail_captured(self) -> None:
        inputs = self._good_inputs()
        # Make freshness fail: fetch was 10 days ago.
        inputs["latest_fetch_at"] = "2026-08-14T12:00:00+00:00"
        verdict = run_all_checks(**inputs)
        assert verdict.passed is False
        assert len(verdict.failed_checks) == 1
        assert verdict.failed_checks[0].name == "freshness"
        assert "freshness" in verdict.summary

    def test_multiple_failures_all_collected(self) -> None:
        # Force score_spread AND freshness to fail simultaneously.
        verdict = run_all_checks(
            scores=[60, 62, 63, 65, 66],          # tight band → spread fails
            facts_list=[_facts(["python"])],
            gaps=[_gap("k8s", 5)],
            latest_fetch_at="2026-08-14T12:00:00+00:00",  # stale → freshness fails
            config=_cfg(),
            now=_NOW,
        )
        assert verdict.passed is False
        failed_names = {r.name for r in verdict.failed_checks}
        assert "score_spread" in failed_names
        assert "freshness" in failed_names
        assert "2/4" in verdict.summary

    def test_verdict_summary_contains_counts(self) -> None:
        verdict = run_all_checks(**self._good_inputs())
        assert "4" in verdict.summary   # "All 4 checks passed"
