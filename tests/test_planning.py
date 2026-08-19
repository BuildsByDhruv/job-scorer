"""Tests for build_plan() and Plan.render() — no DB, no network, no model.

Four scenarios requested:
  A. everything stale   — all three agents run
  B. nothing to do      — all three agents skipped
  C. only unscored      — fetcher skipped, scorer runs, analyser runs (gaps stale)
  D. gaps stale but no unscored — fetcher skipped, scorer skipped, analyser runs

Plus targeted unit tests for stop_conditions, reason strings, and render().
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from edgedash.config import Config
from edgedash.planning import Plan, Task, build_plan
from edgedash.state import SystemState

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)

# ISO timestamps relative to _NOW
_TS_1H_AGO  = "2026-08-17T11:00:00+00:00"   # 1 hour ago
_TS_7H_AGO  = "2026-08-17T05:00:00+00:00"   # 7 hours ago (> default 6h threshold)
_TS_SCORED  = "2026-08-17T10:00:00+00:00"   # score run finished at 10:00
_TS_GAPS    = "2026-08-17T09:00:00+00:00"   # gap snapshot at 09:00 (before score)


def _cfg(**overrides) -> Config:
    """Return a minimal Config with sane defaults for planning tests."""
    base = dict(
        target_role="Engineer",
        target_city="Berlin",
        keywords=[],
        my_skills=[],
        experience_years=0,
        db_path=":memory:",
        min_fit_score=50,
        sources=["arbeitnow"],
        use_mock_fetcher=False,
        llm_provider="gemini",
        llm_model="gemini-3.5-flash",
        llm_batch_size=25,
        target_seniority="mid",
        w_skill_match=0.45,
        w_seniority_fit=0.25,
        w_location_fit=0.15,
        w_recency=0.15,
        skill_aliases={},
        fetch_interval_hours=6,
        fetch_max_pages=5,
        fetch_max_listings=200,
        score_max_seconds=300,
        analyse_max_seconds=60,
    )
    base.update(overrides)
    return Config(**base)


def _state(**overrides) -> SystemState:
    """Return a SystemState representing a healthy, fully up-to-date system."""
    base = dict(
        now=_NOW,
        last_fetch_at=_TS_1H_AGO,
        hours_since_fetch=1.0,
        unscored_count=0,
        gaps_computed_at=_TS_GAPS,
        gaps_stale=False,
        last_cycle_verdict="ok",
        last_cycle_at=_TS_1H_AGO,
    )
    base.update(overrides)
    return SystemState(**base)


def _task(plan: Plan, agent: str) -> Task:
    return next(t for t in plan.tasks if t.agent_name == agent)


# ---------------------------------------------------------------------------
# Scenario A — everything stale: all three run
# ---------------------------------------------------------------------------

class TestEverythingStale:
    """First-ever run: never fetched, listings waiting, no gap snapshot."""

    def setup_method(self) -> None:
        self.state = _state(
            last_fetch_at=None,
            hours_since_fetch=None,
            unscored_count=42,
            gaps_computed_at=None,
            gaps_stale=True,
        )
        self.plan = build_plan(self.state, _cfg())

    def test_all_three_run(self) -> None:
        assert all(t.will_run for t in self.plan.tasks)

    def test_fetcher_reason_says_never(self) -> None:
        assert "never" in _task(self.plan, "fetcher").reason

    def test_scorer_reason_has_count(self) -> None:
        assert "unscored_count=42" in _task(self.plan, "scorer").reason

    def test_analyser_reason_says_never(self) -> None:
        assert "never" in _task(self.plan, "gap_analyzer").reason

    def test_plan_has_three_tasks(self) -> None:
        assert len(self.plan.tasks) == 3

    def test_task_order(self) -> None:
        names = [t.agent_name for t in self.plan.tasks]
        assert names == ["fetcher", "scorer", "gap_analyzer"]


# ---------------------------------------------------------------------------
# Scenario B — nothing to do: all three skipped
# ---------------------------------------------------------------------------

class TestNothingToDo:
    """Recent fetch, nothing unscored, gap snapshot is fresh."""

    def setup_method(self) -> None:
        self.state = _state(
            hours_since_fetch=1.0,      # well inside 6h threshold
            unscored_count=0,
            gaps_stale=False,
            gaps_computed_at=_TS_GAPS,
        )
        self.plan = build_plan(self.state, _cfg())

    def test_all_three_skipped(self) -> None:
        assert not any(t.will_run for t in self.plan.tasks)

    def test_fetcher_skip_reason(self) -> None:
        r = _task(self.plan, "fetcher").reason
        assert "skipped" in r
        assert "hours_since_fetch" in r

    def test_scorer_skip_reason(self) -> None:
        r = _task(self.plan, "scorer").reason
        assert "skipped" in r
        assert "unscored_count=0" in r

    def test_analyser_skip_reason(self) -> None:
        r = _task(self.plan, "gap_analyzer").reason
        assert "skipped" in r
        assert "gaps_stale=False" in r

    def test_render_contains_skip_flags(self) -> None:
        rendered = self.plan.render()
        assert rendered.count("○ SKIP") == 3
        assert "▶ RUN" not in rendered

    def test_render_nothing_to_do(self) -> None:
        """Demonstrate the rendered output for a 'nothing to do' state."""
        rendered = self.plan.render()
        # Every line must start with the skip marker after leading whitespace.
        for line in rendered.strip().splitlines():
            assert "○ SKIP" in line, f"Expected skip in: {line!r}"


# ---------------------------------------------------------------------------
# Scenario C — only unscored listings: fetcher skipped, scorer + analyser run
# ---------------------------------------------------------------------------

class TestOnlyUnscored:
    """Recent fetch left unscored listings; no gap snapshot exists yet."""

    def setup_method(self) -> None:
        self.state = _state(
            hours_since_fetch=1.0,
            unscored_count=15,
            gaps_computed_at=None,
            gaps_stale=True,
        )
        self.plan = build_plan(self.state, _cfg())

    def test_fetcher_skipped(self) -> None:
        assert not _task(self.plan, "fetcher").will_run

    def test_scorer_runs(self) -> None:
        assert _task(self.plan, "scorer").will_run

    def test_analyser_runs(self) -> None:
        assert _task(self.plan, "gap_analyzer").will_run

    def test_scorer_reason_has_count(self) -> None:
        assert "unscored_count=15" in _task(self.plan, "scorer").reason


# ---------------------------------------------------------------------------
# Scenario D — gaps stale but no unscored: fetcher + scorer skipped, analyser runs
# ---------------------------------------------------------------------------

class TestGapsStaleNoUnscored:
    """Scorer ran after gap snapshot — gaps are stale but nothing left to score."""

    def setup_method(self) -> None:
        self.state = _state(
            hours_since_fetch=1.0,
            unscored_count=0,
            gaps_computed_at=_TS_GAPS,
            gaps_stale=True,            # last score run was AFTER gap snapshot
        )
        self.plan = build_plan(self.state, _cfg())

    def test_fetcher_skipped(self) -> None:
        assert not _task(self.plan, "fetcher").will_run

    def test_scorer_skipped(self) -> None:
        assert not _task(self.plan, "scorer").will_run

    def test_analyser_runs(self) -> None:
        assert _task(self.plan, "gap_analyzer").will_run

    def test_analyser_reason_mentions_stale(self) -> None:
        r = _task(self.plan, "gap_analyzer").reason
        assert "gaps_stale=True" in r


# ---------------------------------------------------------------------------
# Fetch threshold boundary
# ---------------------------------------------------------------------------

class TestFetchThreshold:

    def test_exactly_at_threshold_runs(self) -> None:
        state = _state(hours_since_fetch=6.0)
        plan  = build_plan(state, _cfg(fetch_interval_hours=6))
        assert _task(plan, "fetcher").will_run

    def test_just_below_threshold_skipped(self) -> None:
        state = _state(hours_since_fetch=5.9)
        plan  = build_plan(state, _cfg(fetch_interval_hours=6))
        assert not _task(plan, "fetcher").will_run

    def test_custom_threshold_respected(self) -> None:
        state = _state(hours_since_fetch=3.0)
        plan  = build_plan(state, _cfg(fetch_interval_hours=2))
        assert _task(plan, "fetcher").will_run

    def test_never_fetched_always_runs(self) -> None:
        state = _state(last_fetch_at=None, hours_since_fetch=None)
        plan  = build_plan(state, _cfg(fetch_interval_hours=6))
        assert _task(plan, "fetcher").will_run


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

class TestStopConditions:

    def test_fetcher_stop_conditions(self) -> None:
        plan = build_plan(_state(), _cfg(fetch_max_pages=3, fetch_max_listings=100))
        sc   = _task(plan, "fetcher").stop_conditions
        assert sc.max_pages == 3
        assert sc.max_items == 100

    def test_scorer_stop_conditions(self) -> None:
        plan = build_plan(
            _state(unscored_count=10),
            _cfg(llm_batch_size=20, score_max_seconds=120),
        )
        sc = _task(plan, "scorer").stop_conditions
        assert sc.max_items == 20
        assert sc.max_seconds == 120

    def test_analyser_stop_conditions(self) -> None:
        plan = build_plan(
            _state(gaps_stale=True),
            _cfg(analyse_max_seconds=45),
        )
        sc = _task(plan, "gap_analyzer").stop_conditions
        assert sc.max_seconds == 45

    def test_stop_conditions_render(self) -> None:
        plan = build_plan(
            _state(unscored_count=5),
            _cfg(llm_batch_size=25, score_max_seconds=300),
        )
        rendered = _task(plan, "scorer").stop_conditions.render()
        assert "max_items=25" in rendered
        assert "max_seconds=300" in rendered


# ---------------------------------------------------------------------------
# Plan.render() format
# ---------------------------------------------------------------------------

class TestRender:

    def test_run_lines_contain_stop_conditions(self) -> None:
        state  = _state(unscored_count=5, hours_since_fetch=None,
                        gaps_stale=True, gaps_computed_at=None)
        plan   = build_plan(state, _cfg())
        rendered = plan.render()
        # Running tasks show their stop conditions in brackets.
        assert "[" in rendered
        assert "max_items" in rendered

    def test_skip_lines_have_no_stop_condition_bracket(self) -> None:
        state    = _state(hours_since_fetch=1.0, unscored_count=0, gaps_stale=False)
        plan     = build_plan(state, _cfg())
        rendered = plan.render()
        assert "[" not in rendered

    def test_render_one_line_per_agent(self) -> None:
        plan  = build_plan(_state(), _cfg())
        lines = [l for l in plan.render().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_agents_to_run_and_skipped_partitions(self) -> None:
        state = _state(unscored_count=5, hours_since_fetch=1.0, gaps_stale=False)
        plan  = build_plan(state, _cfg())
        assert len(plan.agents_to_run) + len(plan.agents_skipped) == 3
