"""Build an execution plan from system state. Pure function, no I/O.

Public API
----------
build_plan(state, config) -> Plan

A Plan is an ordered list of Task objects — one per agent, whether it will
run or be skipped. Skipped tasks are explicit (rule 31): they appear with
a reason so the operator sees exactly why each agent was omitted.

Decision rules
--------------
fetch   : run if hours_since_fetch is None (never fetched)
                OR hours_since_fetch >= config.fetch_interval_hours
score   : run if unscored_count > 0
analyse : run if gaps_stale is True OR gaps_computed_at is None

All thresholds come from config — no magic numbers here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from edgedash.config import Config
from edgedash.state import SystemState

# Width used by render() for alignment.
_NAME_WIDTH = 14


@dataclass
class StopConditions:
    """Hard limits passed to an agent by the Orchestrator (rule 29).

    The agent never decides its own limits.
    """
    max_items: int | None = None
    max_seconds: int | None = None
    max_pages: int | None = None

    def render(self) -> str:
        parts = []
        if self.max_items is not None:
            parts.append(f"max_items={self.max_items}")
        if self.max_pages is not None:
            parts.append(f"max_pages={self.max_pages}")
        if self.max_seconds is not None:
            parts.append(f"max_seconds={self.max_seconds}")
        return ", ".join(parts) if parts else "none"


@dataclass
class Task:
    """One entry in a Plan — either a run or an explicit skip."""

    agent_name: str
    goal: str
    stop_conditions: StopConditions
    reason: str          # names the state value that caused the decision
    will_run: bool       # False = explicitly skipped


@dataclass
class Plan:
    """An ordered list of Tasks produced by build_plan."""

    tasks: list[Task] = field(default_factory=list)

    def render(self) -> str:
        """Return a compact human-readable plan string, one line per agent."""
        lines = []
        for t in self.tasks:
            flag   = "▶ RUN " if t.will_run else "○ SKIP"
            name   = t.agent_name.ljust(_NAME_WIDTH)
            stop   = t.stop_conditions.render()
            lines.append(
                f"  {flag}  {name}  {t.reason}"
                + (f"  [{stop}]" if t.will_run else "")
            )
        return "\n".join(lines)

    @property
    def agents_to_run(self) -> list[Task]:
        return [t for t in self.tasks if t.will_run]

    @property
    def agents_skipped(self) -> list[Task]:
        return [t for t in self.tasks if not t.will_run]


# ---------------------------------------------------------------------------
# Core planner — pure function of (state, config), no I/O
# ---------------------------------------------------------------------------


def build_plan(state: SystemState, config: Config) -> Plan:
    """Return a Plan describing what to run and what to skip, and why.

    Pure function: same (state, config) always produces the same Plan.
    No DB access, no network, no model calls.
    """
    tasks: list[Task] = []

    # ── Fetch ─────────────────────────────────────────────────────────────
    if state.hours_since_fetch is None:
        fetch_reason  = "hours_since_fetch=never (first run)"
        fetch_will_run = True
    elif state.hours_since_fetch >= config.fetch_interval_hours:
        h = f"{state.hours_since_fetch:.1f}"
        fetch_reason  = (
            f"hours_since_fetch={h} >= fetch_interval_hours={config.fetch_interval_hours}"
        )
        fetch_will_run = True
    else:
        h = f"{state.hours_since_fetch:.1f}"
        fetch_reason  = (
            f"skipped: hours_since_fetch={h} < fetch_interval_hours={config.fetch_interval_hours}"
        )
        fetch_will_run = False

    tasks.append(Task(
        agent_name="fetcher",
        goal="Fetch new listings from all enabled sources",
        stop_conditions=StopConditions(
            max_pages=config.fetch_max_pages,
            max_items=config.fetch_max_listings,
        ),
        reason=fetch_reason,
        will_run=fetch_will_run,
    ))

    # ── Score ─────────────────────────────────────────────────────────────
    if state.unscored_count > 0:
        score_reason  = f"unscored_count={state.unscored_count}"
        score_will_run = True
    else:
        score_reason  = "skipped: unscored_count=0"
        score_will_run = False

    tasks.append(Task(
        agent_name="scorer",
        goal=f"Score up to {config.llm_batch_size} unscored listings",
        stop_conditions=StopConditions(
            max_items=config.llm_batch_size,
            max_seconds=config.score_max_seconds,
        ),
        reason=score_reason,
        will_run=score_will_run,
    ))

    # ── Analyse ───────────────────────────────────────────────────────────
    if state.gaps_computed_at is None:
        analyse_reason  = "gaps_computed_at=never (no snapshot exists)"
        analyse_will_run = True
    elif state.gaps_stale:
        analyse_reason  = (
            f"gaps_stale=True (scores updated after gaps_computed_at={state.gaps_computed_at[:10]})"
        )
        analyse_will_run = True
    else:
        analyse_reason  = (
            f"skipped: gaps_stale=False, gaps_computed_at={state.gaps_computed_at[:10]}"
        )
        analyse_will_run = False

    tasks.append(Task(
        agent_name="gap_analyzer",
        goal="Compute skill gap snapshot from all scored listings",
        stop_conditions=StopConditions(
            max_seconds=config.analyse_max_seconds,
        ),
        reason=analyse_reason,
        will_run=analyse_will_run,
    ))

    return Plan(tasks=tasks)
