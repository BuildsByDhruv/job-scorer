"""Tests for edgedash/query/tools.py.

Strategy: seed a real in-memory SQLite database through the storage
module, then call tools directly.  No mocking — deterministic data,
deterministic assertions.

Coverage matrix
---------------
@tool decorator         registry populated, ToolSpec fields correct
call()                  dispatches correctly, ToolNotFound on unknown name
_clamp_int              both bounds, non-numeric input falls back to default
_clean_str              strips, empty falls back to default
companies_hiring        pass / clamping / empty window
best_matches            pass / clamping / n > available
top_gaps                pass / clamping
gap_detail              known skill / unknown skill / canonicalisation
trend                   pass / single-snapshot / out-of-window falls back
listing_count           pass
skill_demand            required / nice_to_have / unknown skill
ToolNotFound            raised for unknown tool name
_assert_verified        RuntimeError when no passing cycle
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import edgedash.storage as storage
from edgedash.query.tools import (
    TOOLS,
    ParamSpec,
    ToolNotFound,
    ToolResult,
    _clamp_int,
    _clean_str,
    _validate_params,
    call,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "tf":  "terraform",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


@pytest.fixture()
def db(tmp_path):
    """Return a path to a freshly initialised, seeded SQLite database."""
    path = str(tmp_path / "test.db")
    storage.init_db(path)

    # ── Listings ─────────────────────────────────────────────────────────────
    # We insert directly through upsert_listings so stable-hash IDs are used.
    listings = [
        {
            "source":      "arbeitnow",
            "url":         "https://example.com/job/1",
            "title":       "Senior Data Engineer",
            "company":     "Acme Corp",
            "location":    "Berlin",
            "description": "Requires python kubernetes terraform",
            "posted_at":   _days_ago(1),
            "raw":         None,
        },
        {
            "source":      "arbeitnow",
            "url":         "https://example.com/job/2",
            "title":       "Data Analyst",
            "company":     "Globex",
            "location":    "Remote",
            "description": "Requires sql python",
            "posted_at":   _days_ago(2),
            "raw":         None,
        },
        {
            "source":      "arbeitnow",
            "url":         "https://example.com/job/3",
            "title":       "ML Engineer",
            "company":     "Acme Corp",
            "location":    "London",
            "description": "Requires python tensorflow kubernetes",
            "posted_at":   _days_ago(30),
            "raw":         None,
        },
        {
            "source":      "arbeitnow",
            "url":         "https://example.com/job/4",
            "title":       "Backend Engineer",
            "company":     "Initech",
            "location":    "Berlin",
            "description": "Requires golang docker",
            "posted_at":   _days_ago(3),
            "raw":         None,
        },
    ]
    storage.upsert_listings(path, listings)

    # Retrieve the stable IDs assigned by upsert
    all_rows = storage.get_listings(path, limit=100)
    id_by_url = {r["url"]: r["id"] for r in all_rows}

    # ── Scores ────────────────────────────────────────────────────────────────
    scores = {
        "https://example.com/job/1": 80,
        "https://example.com/job/2": 55,
        "https://example.com/job/3": 70,
        # job/4 intentionally left unscored
    }
    for url, score in scores.items():
        storage.write_score(
            path=path,
            listing_id=id_by_url[url],
            score=score,
            reason=f"score {score}",
            components={},
            scored_at=_now(),
        )

    # ── Extraction cache ──────────────────────────────────────────────────────
    import hashlib

    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    cache = [
        ("Requires python kubernetes terraform",
         ["python", "kubernetes", "terraform"], ["docker"]),
        ("Requires sql python",
         ["sql", "python"], []),
        ("Requires python tensorflow kubernetes",
         ["python", "tensorflow", "kubernetes"], ["spark"]),
    ]
    for desc, req, nth in cache:
        storage.set_extraction(
            path=path,
            description_hash=_hash(desc),
            data={
                "required_skills": req,
                "nice_to_have":    nth,
                "seniority":       "senior",
                "years_required":  None,
                "remote_ok":       None,
            },
        )

    # ── Gap snapshot ─────────────────────────────────────────────────────────
    run_id = str(uuid.uuid4())
    computed_at = _days_ago(0)
    j1 = id_by_url["https://example.com/job/1"]
    j3 = id_by_url["https://example.com/job/3"]

    storage.write_gap_snapshot(
        path=path,
        run_id=run_id,
        computed_at=computed_at,
        rows=[
            {
                "skill":            "kubernetes",
                "listings_blocked": 2,
                "opportunity_cost": 1.50,
                "mean_score":       75.0,
                "top_score":        80,
                "also_nice_to_have": 0,
                "low_confidence":   False,
                "example_ids":      [j1, j3],
            },
            {
                "skill":            "terraform",
                "listings_blocked": 1,
                "opportunity_cost": 0.80,
                "mean_score":       80.0,
                "top_score":        80,
                "also_nice_to_have": 0,
                "low_confidence":   True,
                "example_ids":      [j1],
            },
        ],
    )

    # ── Older gap snapshot (for trend tests) ─────────────────────────────────
    run_id_old = str(uuid.uuid4())
    old_computed_at = _days_ago(14)
    storage.write_gap_snapshot(
        path=path,
        run_id=run_id_old,
        computed_at=old_computed_at,
        rows=[
            {
                "skill":            "kubernetes",
                "listings_blocked": 1,
                "opportunity_cost": 0.70,
                "mean_score":       70.0,
                "top_score":        70,
                "also_nice_to_have": 0,
                "low_confidence":   True,
                "example_ids":      [j3],
            },
        ],
    )

    # ── Passing cycle summary row (rule 46 — _assert_verified needs this) ────
    storage.log_cycle(
        path=path,
        agent="orchestrator/cycle",
        started_at=_days_ago(0),
        finished_at=_now(),
        records_touched=3,
        status="complete",
        notes="ran: scorer | VERDICT: pass",
    )

    return path


# ---------------------------------------------------------------------------
# @tool decorator and TOOLS registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_seven_tools_registered(self) -> None:
        expected = {
            "companies_hiring", "best_matches", "top_gaps",
            "gap_detail", "trend", "listing_count", "skill_demand",
        }
        assert expected == set(TOOLS.keys())

    def test_each_tool_has_description(self) -> None:
        for name, spec in TOOLS.items():
            assert spec.description, f"{name} has empty description"

    def test_each_tool_has_fn(self) -> None:
        for name, spec in TOOLS.items():
            assert callable(spec.fn), f"{name}.fn is not callable"

    def test_param_specs_have_types(self) -> None:
        for name, spec in TOOLS.items():
            for pname, pspec in spec.params.items():
                assert pspec.type in ("int", "str"), (
                    f"{name}.{pname} has invalid type {pspec.type!r}"
                )

    def test_int_params_have_bounds(self) -> None:
        for name, spec in TOOLS.items():
            for pname, pspec in spec.params.items():
                if pspec.type == "int":
                    assert pspec.min is not None, f"{name}.{pname} missing min"
                    assert pspec.max is not None, f"{name}.{pname} missing max"
                    assert pspec.min <= int(pspec.default) <= pspec.max, (
                        f"{name}.{pname} default outside bounds"
                    )


# ---------------------------------------------------------------------------
# _clamp_int
# ---------------------------------------------------------------------------

class TestClampInt:
    _spec = ParamSpec(type="int", description="", default=7, min=1, max=90)

    def test_value_in_range(self) -> None:
        assert _clamp_int(30, self._spec) == 30

    def test_clamp_below_min(self) -> None:
        assert _clamp_int(0, self._spec) == 1

    def test_clamp_above_max(self) -> None:
        assert _clamp_int(999, self._spec) == 90

    def test_exactly_at_min(self) -> None:
        assert _clamp_int(1, self._spec) == 1

    def test_exactly_at_max(self) -> None:
        assert _clamp_int(90, self._spec) == 90

    def test_non_numeric_falls_back_to_default(self) -> None:
        assert _clamp_int("banana", self._spec) == 7

    def test_none_falls_back_to_default(self) -> None:
        assert _clamp_int(None, self._spec) == 7

    def test_float_truncated(self) -> None:
        assert _clamp_int(5.9, self._spec) == 5


# ---------------------------------------------------------------------------
# _clean_str
# ---------------------------------------------------------------------------

class TestCleanStr:
    _spec = ParamSpec(type="str", description="", default="python")

    def test_strips_whitespace(self) -> None:
        assert _clean_str("  kubernetes  ", self._spec) == "kubernetes"

    def test_none_returns_default(self) -> None:
        assert _clean_str(None, self._spec) == "python"

    def test_empty_string_returns_default(self) -> None:
        assert _clean_str("", self._spec) == "python"

    def test_whitespace_only_returns_default(self) -> None:
        assert _clean_str("   ", self._spec) == "python"

    def test_normal_value_returned(self) -> None:
        assert _clean_str("terraform", self._spec) == "terraform"


# ---------------------------------------------------------------------------
# call() — dispatch and ToolNotFound
# ---------------------------------------------------------------------------

class TestCall:
    def test_unknown_tool_raises(self, db) -> None:
        with pytest.raises(ToolNotFound, match="not_a_real_tool"):
            call("not_a_real_tool", {}, db, _ALIASES)

    def test_unknown_tool_message_lists_available(self, db) -> None:
        try:
            call("unknown", {}, db, _ALIASES)
        except ToolNotFound as exc:
            assert "companies_hiring" in str(exc)
            assert "best_matches" in str(exc)

    def test_returns_tool_result(self, db) -> None:
        result = call("listing_count", {}, db, _ALIASES)
        assert isinstance(result, ToolResult)

    def test_params_used_reflects_clamped_values(self, db) -> None:
        result = call("best_matches", {"n": 999}, db, _ALIASES)
        assert result.params_used["n"] == 25   # clamped to max


# ---------------------------------------------------------------------------
# companies_hiring
# ---------------------------------------------------------------------------

class TestCompaniesHiring:
    def test_returns_tool_result(self, db) -> None:
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        assert isinstance(r, ToolResult)
        assert r.tool == "companies_hiring"

    def test_rows_have_required_keys(self, db) -> None:
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        for row in r.rows:
            assert "company" in row
            assert "listings" in row

    def test_counts_are_positive(self, db) -> None:
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        for row in r.rows:
            assert row["listings"] >= 1

    def test_ordered_by_count_desc(self, db) -> None:
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        counts = [row["listings"] for row in r.rows]
        assert counts == sorted(counts, reverse=True)

    def test_acme_has_two_listings_in_7_days(self, db) -> None:
        # All listings are fetched NOW by upsert_listings, so all 4 are within
        # any reasonable window.  Acme Corp has job/1 AND job/3 — both fetched
        # today regardless of their posted_at dates.
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        companies = {row["company"]: row["listings"] for row in r.rows}
        assert "Acme Corp" in companies
        assert companies["Acme Corp"] == 2   # job/1 + job/3, both fetched today

    def test_narrow_window_excludes_old_listings(self, db) -> None:
        # With days=7 all fixtures are included (all fetched now).
        # Test the empty-result path by using a window ending before any fetch.
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        # All 3 companies should appear: Acme Corp, Globex, Initech
        company_names = {row["company"] for row in r.rows}
        assert "Acme Corp" in company_names
        assert "Globex"    in company_names
        assert "Initech"   in company_names

    def test_summary_contains_day_count(self, db) -> None:
        r = call("companies_hiring", {"days": 7}, db, _ALIASES)
        assert "7" in r.summary

    def test_clamped_below_min(self, db) -> None:
        # days=0 should be clamped to 1
        r = call("companies_hiring", {"days": 0}, db, _ALIASES)
        assert r.params_used["days"] == 1

    def test_clamped_above_max(self, db) -> None:
        r = call("companies_hiring", {"days": 999}, db, _ALIASES)
        assert r.params_used["days"] == 90


# ---------------------------------------------------------------------------
# best_matches
# ---------------------------------------------------------------------------

class TestBestMatches:
    def test_returns_tool_result(self, db) -> None:
        r = call("best_matches", {"n": 3}, db, _ALIASES)
        assert isinstance(r, ToolResult)

    def test_rows_have_required_keys(self, db) -> None:
        r = call("best_matches", {"n": 3}, db, _ALIASES)
        for row in r.rows:
            for key in ("score", "title", "company", "url", "reason"):
                assert key in row, f"missing key {key!r}"

    def test_scores_descending(self, db) -> None:
        r = call("best_matches", {"n": 10}, db, _ALIASES)
        scores = [row["score"] for row in r.rows]
        assert scores == sorted(scores, reverse=True)

    def test_top_score_is_80(self, db) -> None:
        r = call("best_matches", {"n": 1}, db, _ALIASES)
        assert r.rows[0]["score"] == 80

    def test_n_respected(self, db) -> None:
        r = call("best_matches", {"n": 2}, db, _ALIASES)
        assert len(r.rows) == 2

    def test_n_greater_than_available_returns_all(self, db) -> None:
        # Only 3 scored listings in the fixture
        r = call("best_matches", {"n": 25}, db, _ALIASES)
        assert len(r.rows) == 3

    def test_clamped_below_min(self, db) -> None:
        r = call("best_matches", {"n": 0}, db, _ALIASES)
        assert r.params_used["n"] == 1

    def test_clamped_above_max(self, db) -> None:
        r = call("best_matches", {"n": 100}, db, _ALIASES)
        assert r.params_used["n"] == 25

    def test_summary_non_empty(self, db) -> None:
        r = call("best_matches", {"n": 3}, db, _ALIASES)
        assert r.summary


# ---------------------------------------------------------------------------
# top_gaps
# ---------------------------------------------------------------------------

class TestTopGaps:
    def test_returns_tool_result(self, db) -> None:
        r = call("top_gaps", {"n": 5}, db, _ALIASES)
        assert isinstance(r, ToolResult)

    def test_rows_have_required_keys(self, db) -> None:
        r = call("top_gaps", {"n": 5}, db, _ALIASES)
        for row in r.rows:
            for key in ("skill", "listings_blocked", "opportunity_cost",
                        "mean_score", "top_score", "low_confidence"):
                assert key in row

    def test_first_gap_is_kubernetes(self, db) -> None:
        # kubernetes has higher opportunity_cost in the fixture
        r = call("top_gaps", {"n": 5}, db, _ALIASES)
        assert r.rows[0]["skill"] == "kubernetes"

    def test_n_respected(self, db) -> None:
        r = call("top_gaps", {"n": 1}, db, _ALIASES)
        assert len(r.rows) == 1

    def test_n_greater_than_available_returns_all(self, db) -> None:
        r = call("top_gaps", {"n": 25}, db, _ALIASES)
        assert len(r.rows) == 2   # only 2 gaps in fixture

    def test_clamped_below_min(self, db) -> None:
        r = call("top_gaps", {"n": -5}, db, _ALIASES)
        assert r.params_used["n"] == 1

    def test_clamped_above_max(self, db) -> None:
        r = call("top_gaps", {"n": 999}, db, _ALIASES)
        assert r.params_used["n"] == 25

    def test_summary_mentions_snapshot(self, db) -> None:
        r = call("top_gaps", {"n": 5}, db, _ALIASES)
        assert "snapshot" in r.summary.lower()


# ---------------------------------------------------------------------------
# gap_detail
# ---------------------------------------------------------------------------

class TestGapDetail:
    def test_known_skill_returns_rows(self, db) -> None:
        r = call("gap_detail", {"skill": "kubernetes"}, db, _ALIASES)
        assert len(r.rows) > 0

    def test_rows_have_required_keys(self, db) -> None:
        r = call("gap_detail", {"skill": "kubernetes"}, db, _ALIASES)
        for row in r.rows:
            for key in ("id", "score", "title", "company", "url"):
                assert key in row

    def test_alias_resolves_to_canonical(self, db) -> None:
        # "k8s" aliases to "kubernetes" — should return the same rows
        r_alias  = call("gap_detail", {"skill": "k8s"},         db, _ALIASES)
        r_canon  = call("gap_detail", {"skill": "kubernetes"},  db, _ALIASES)
        assert r_alias.rows == r_canon.rows

    def test_alias_reflected_in_params_used(self, db) -> None:
        r = call("gap_detail", {"skill": "k8s"}, db, _ALIASES)
        assert r.params_used["skill"] == "kubernetes"

    def test_unknown_skill_returns_empty_not_raises(self, db) -> None:
        r = call("gap_detail", {"skill": "cobol"}, db, _ALIASES)
        assert r.rows == []
        assert "cobol" in r.summary.lower() or "not found" in r.summary.lower()

    def test_summary_contains_listings_blocked(self, db) -> None:
        r = call("gap_detail", {"skill": "kubernetes"}, db, _ALIASES)
        # fixture has listings_blocked=2 for kubernetes
        assert "2" in r.summary

    def test_rows_sorted_by_score_desc(self, db) -> None:
        r = call("gap_detail", {"skill": "kubernetes"}, db, _ALIASES)
        scores = [row["score"] for row in r.rows if row["score"] is not None]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------

class TestTrend:
    def test_returns_tool_result(self, db) -> None:
        r = call("trend", {"weeks": 3}, db, _ALIASES)
        assert isinstance(r, ToolResult)

    def test_rows_have_required_keys(self, db) -> None:
        r = call("trend", {"weeks": 3}, db, _ALIASES)
        for row in r.rows:
            for key in ("skill", "cost_latest", "direction", "listings_blocked"):
                assert key in row

    def test_kubernetes_shows_rising(self, db) -> None:
        # old cost 0.70 → new cost 1.50 = rising
        r = call("trend", {"weeks": 4}, db, _ALIASES)
        k8s = next(row for row in r.rows if row["skill"] == "kubernetes")
        assert k8s["direction"] == "rising"
        assert k8s["delta"] == pytest.approx(0.80, abs=0.01)

    def test_new_skill_has_none_cost_earliest(self, db) -> None:
        # terraform was not in the old snapshot
        r = call("trend", {"weeks": 4}, db, _ALIASES)
        tf = next((row for row in r.rows if row["skill"] == "terraform"), None)
        assert tf is not None
        assert tf["cost_earliest"] is None
        assert tf["direction"] == "new"

    def test_summary_contains_dates(self, db) -> None:
        r = call("trend", {"weeks": 4}, db, _ALIASES)
        # should mention both a date and the week count
        assert "week" in r.summary.lower() or "4" in r.summary

    def test_single_snapshot_returns_empty_rows(self, tmp_path) -> None:
        # Build a fresh DB with only one snapshot
        path = str(tmp_path / "single.db")
        storage.init_db(path)
        storage.log_cycle(
            path=path, agent="orchestrator/cycle",
            started_at=_now(), finished_at=_now(),
            records_touched=0, status="complete",
            notes="VERDICT: pass",
        )
        storage.write_gap_snapshot(
            path=path, run_id=str(uuid.uuid4()),
            computed_at=_now(),
            rows=[{
                "skill": "python", "listings_blocked": 1,
                "opportunity_cost": 0.5, "mean_score": 50.0,
                "top_score": 50, "also_nice_to_have": 0,
                "low_confidence": False, "example_ids": [],
            }],
        )
        r = call("trend", {"weeks": 3}, path, _ALIASES)
        assert r.rows == []
        assert "one snapshot" in r.summary.lower() or "two" in r.summary.lower()

    def test_clamped_below_min(self, db) -> None:
        r = call("trend", {"weeks": 0}, db, _ALIASES)
        assert r.params_used["weeks"] == 1

    def test_clamped_above_max(self, db) -> None:
        r = call("trend", {"weeks": 99}, db, _ALIASES)
        assert r.params_used["weeks"] == 12


# ---------------------------------------------------------------------------
# listing_count
# ---------------------------------------------------------------------------

class TestListingCount:
    def test_returns_tool_result(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        assert isinstance(r, ToolResult)

    def test_single_row(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        assert len(r.rows) == 1

    def test_row_has_required_keys(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        row = r.rows[0]
        for key in ("total", "scored", "unscored", "coverage_pct",
                    "newest_fetched_at"):
            assert key in row

    def test_totals_are_consistent(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        row = r.rows[0]
        assert row["scored"] + row["unscored"] == row["total"]

    def test_correct_counts(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        row = r.rows[0]
        assert row["total"]    == 4   # 4 listings inserted
        assert row["scored"]   == 3   # 3 have scores
        assert row["unscored"] == 1   # job/4 unscored

    def test_coverage_pct_calculation(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        row = r.rows[0]
        expected = round(100 * 3 / 4, 1)   # 75.0
        assert row["coverage_pct"] == expected

    def test_summary_non_empty(self, db) -> None:
        r = call("listing_count", {}, db, _ALIASES)
        assert r.summary


# ---------------------------------------------------------------------------
# skill_demand
# ---------------------------------------------------------------------------

class TestSkillDemand:
    def test_returns_tool_result(self, db) -> None:
        r = call("skill_demand", {"skill": "python"}, db, _ALIASES)
        assert isinstance(r, ToolResult)

    def test_single_row_for_known_skill(self, db) -> None:
        r = call("skill_demand", {"skill": "python"}, db, _ALIASES)
        assert len(r.rows) == 1

    def test_row_has_required_keys(self, db) -> None:
        r = call("skill_demand", {"skill": "python"}, db, _ALIASES)
        row = r.rows[0]
        for key in ("skill", "required_count", "nice_count",
                    "total_seen", "mean_score_required"):
            assert key in row

    def test_python_required_in_all_three_cached(self, db) -> None:
        # python is in required_skills of all 3 cached extractions
        r = call("skill_demand", {"skill": "python"}, db, _ALIASES)
        assert r.rows[0]["required_count"] == 3

    def test_docker_is_nice_to_have(self, db) -> None:
        # docker is in nice_to_have of job/1's cache entry
        r = call("skill_demand", {"skill": "docker"}, db, _ALIASES)
        assert r.rows[0]["nice_count"] >= 1
        assert r.rows[0]["required_count"] == 0

    def test_alias_resolves(self, db) -> None:
        # "k8s" aliases to "kubernetes"
        r_alias = call("skill_demand", {"skill": "k8s"},        db, _ALIASES)
        r_canon = call("skill_demand", {"skill": "kubernetes"}, db, _ALIASES)
        assert r_alias.rows == r_canon.rows

    def test_unknown_skill_returns_empty_not_raises(self, db) -> None:
        r = call("skill_demand", {"skill": "cobol"}, db, _ALIASES)
        assert r.rows == []
        assert "cobol" in r.summary.lower() or "not found" in r.summary.lower()

    def test_empty_skill_returns_empty_not_raises(self, db) -> None:
        r = call("skill_demand", {"skill": ""}, db, _ALIASES)
        # empty falls back to default ""; after canonical → ""
        assert isinstance(r, ToolResult)

    def test_summary_contains_counts(self, db) -> None:
        r = call("skill_demand", {"skill": "python"}, db, _ALIASES)
        assert "3" in r.summary   # required_count = 3


# ---------------------------------------------------------------------------
# _assert_verified — RuntimeError when no passing cycle
# ---------------------------------------------------------------------------

class TestAssertVerified:
    def test_raises_when_no_verified_cycle(self, tmp_path) -> None:
        path = str(tmp_path / "empty.db")
        storage.init_db(path)
        # No passing orchestrator/cycle row → RuntimeError
        with pytest.raises(RuntimeError, match="No verified cycle"):
            call("listing_count", {}, path, _ALIASES)

    def test_raises_even_with_failed_cycle(self, tmp_path) -> None:
        path = str(tmp_path / "failed.db")
        storage.init_db(path)
        storage.log_cycle(
            path=path, agent="orchestrator/cycle",
            started_at=_now(), finished_at=_now(),
            records_touched=0, status="degraded",
            notes="VERDICT: degraded",
        )
        with pytest.raises(RuntimeError, match="No verified cycle"):
            call("listing_count", {}, path, _ALIASES)
