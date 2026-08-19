"""Orchestrator — reads state, builds a plan, executes it, logs one summary row.

Rules enforced here:
  28 — state-driven, never a fixed sequence; skipping is a success
  29 — every delegation carries the Task's stop_conditions
  30 — no fetching, scoring, or analysis logic lives here
  31 — plan is printed before execution
  32 — one agent failing does not stop the cycle; cycle marked partial
  33 — exactly one summary row written per cycle
"""

from __future__ import annotations

import textwrap
import time
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.agents.fetcher import Fetcher
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.agents.mock_fetcher import MockFetcher
from edgedash.agents.scorer import Scorer
from edgedash.config import Config
from edgedash.planning import Plan, Task, build_plan
from edgedash.state import SystemState, read_state

# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------
# To add a new agent: add it here AND add a decision rule in planning.py.
# The Orchestrator resolves agents by name — nothing else changes.

def _build_registry(config: Config) -> dict[str, Agent]:
    fetcher: Agent = MockFetcher() if config.use_mock_fetcher else Fetcher()
    agents: list[Agent] = [fetcher, Scorer(), GapAnalyzer()]
    return {a.name: a for a in agents}


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"


def _print_header(text: str) -> None:
    width = 60
    print(f"\n{_BOLD}{_CYAN}{'─' * width}{_RESET}")
    print(f"{_BOLD}{_CYAN}  {text}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'─' * width}{_RESET}")


def _print_dim(text: str) -> None:
    print(f"{_DIM}{text}{_RESET}")


def _status_colour(status: str) -> str:
    return {
        "ok":      f"{_GREEN}ok{_RESET}",
        "failed":  f"{_RED}failed{_RESET}",
        "skipped": f"{_YELLOW}skipped{_RESET}",
        "partial": f"{_YELLOW}partial{_RESET}",
    }.get(status, status)


def _print_results_table(rows: list[tuple[str, str, float, int, str | None]]) -> None:
    """Print the per-agent execution table.

    rows: [(agent_name, status, duration_s, records_touched, notes), ...]
    """
    if not rows:
        return
    col_agent = max(len(r[0]) for r in rows)
    col_agent = max(col_agent, 12)
    print()
    header = (
        f"  {'AGENT':<{col_agent}}  {'STATUS':<8}  {'SECS':>5}  "
        f"{'ROWS':>6}  NOTES"
    )
    print(f"{_BOLD}{header}{_RESET}")
    print(
        f"  {'─' * col_agent}  {'─' * 8}  {'─' * 5}  "
        f"{'─' * 6}  {'─' * 30}"
    )
    for name, status, dur, rows_n, notes in rows:
        notes_s = textwrap.shorten(notes or "", width=50, placeholder="…")
        print(
            f"  {name:<{col_agent}}  {_status_colour(status):<8}  "
            f"{dur:>5.1f}  {rows_n:>6}  {notes_s}"
        )
    print()


# ---------------------------------------------------------------------------
# run_cycle
# ---------------------------------------------------------------------------


def run_cycle(
    config: Config,
    dry_run: bool = False,
    force: list[str] | None = None,
    explain: bool = False,
) -> None:
    """Run one full state-driven orchestration cycle.

    Parameters
    ----------
    dry_run:
        Read state and print the plan, then exit without executing anything.
    force:
        Agent names to add to the plan even if state says skip.
        Appended with reason "forced by operator".
    explain:
        Print full SystemState alongside each planning decision.
    """
    force = force or []

    cycle_start = datetime.now(timezone.utc)
    cycle_start_mono = time.monotonic()

    # ── 1. Init DB ──────────────────────────────────────────────────────────
    storage.init_db(config.db_path)

    # ── 2. Read state ────────────────────────────────────────────────────────
    _print_header("EdgeDash — reading state")
    state = read_state(config, now=cycle_start)

    _print_dim(f"  db path            : {config.db_path}")
    _print_dim(f"  last fetch         : {state.last_fetch_at or 'never'}")
    _print_dim(f"  hours since fetch  : "
               f"{f'{state.hours_since_fetch:.1f}h' if state.hours_since_fetch is not None else 'n/a'}")
    _print_dim(f"  unscored rows      : {state.unscored_count}")
    _print_dim(f"  gaps computed at   : {state.gaps_computed_at or 'never'}")
    _print_dim(f"  gaps stale         : {state.gaps_stale}")
    _print_dim(f"  last cycle         : "
               f"{state.last_cycle_verdict or 'none'} at {state.last_cycle_at or 'n/a'}")
    _print_dim(f"  target role        : {config.target_role}  |  city: {config.target_city}")
    _print_dim(f"  fetcher mode       : {'mock' if config.use_mock_fetcher else 'live'}"
               f"  |  sources: {', '.join(config.sources)}")

    # ── 3. Build and PRINT plan (rule 31) ────────────────────────────────────
    plan = build_plan(state, config)
    registry = _build_registry(config)

    _print_header("Plan")

    if explain:
        _print_explain(state, plan)
    else:
        print(plan.render())

    # ── Apply --force overrides AFTER build_plan (pure function stays pure) ──
    forced_names: list[str] = []
    if force:
        plan, forced_names = _apply_force(plan, force, config)
        print()
        print(f"  {_YELLOW}{_BOLD}⚠  PLAN MANUALLY OVERRIDDEN{_RESET}")
        for name in forced_names:
            print(f"  {_YELLOW}  forced: {name}{_RESET}")
        print()
        print("  Updated plan:")
        print(plan.render())

    # ── Dry run — print plan and exit cleanly, no writes ────────────────────
    if dry_run:
        print()
        print(f"  {_CYAN}--dry-run: no agents will execute, no writes made.{_RESET}")
        print()
        return

    # ── Nothing to do — success, exit cleanly (rule 28, 6) ──────────────────
    if not plan.agents_to_run:
        _print_header("Cycle summary")
        elapsed = time.monotonic() - cycle_start_mono
        _print_dim(f"  outcome  : nothing_to_do (all agents skipped — this is a success)")
        _print_dim(f"  elapsed  : {elapsed:.2f}s")
        _write_cycle_summary(
            config=config,
            plan=plan,
            exec_rows=[],
            outcome="nothing_to_do",
            cycle_start=cycle_start,
            elapsed=elapsed,
            forced_names=forced_names,
        )
        return

    # ── 4. Execute plan ──────────────────────────────────────────────────────
    _print_header("Running agents")

    exec_rows: list[tuple[str, str, float, int, str | None]] = []
    any_failed = False

    for task in plan.tasks:
        if not task.will_run:
            exec_rows.append((task.agent_name, "skipped", 0.0, 0, task.reason))
            continue

        agent = registry.get(task.agent_name)
        if agent is None:
            # Plan references an agent not in the registry — log and continue.
            msg = f"agent '{task.agent_name}' not found in registry"
            print(f"\n  ✗  {msg}")
            exec_rows.append((task.agent_name, "failed", 0.0, 0, msg))
            any_failed = True
            continue

        print(f"\n  → {_BOLD}{task.agent_name}{_RESET}  "
              f"{_DIM}goal: {task.goal}{_RESET}")
        print(f"     {_DIM}stop: {task.stop_conditions.render()}{_RESET}")

        agent_start = time.monotonic()
        agent_started_at = datetime.now(timezone.utc).isoformat()

        try:
            # Rule 29 — pass stop_conditions from the plan, not from inside agent
            result = agent.run(config, config.db_path, task.stop_conditions)
        except Exception as exc:
            # Rule 32 — catch, log, continue
            duration = time.monotonic() - agent_start
            result = AgentResult(
                agent=task.agent_name,
                status="failed",
                records_touched=0,
                notes=str(exc),
            )
            any_failed = True
            print(f"  ✗  [{task.agent_name}] unhandled exception: {exc}")

        duration = time.monotonic() - agent_start
        agent_finished_at = datetime.now(timezone.utc).isoformat()

        if result.status == "failed":
            any_failed = True

        storage.log_cycle(
            path=config.db_path,
            agent=result.agent,
            started_at=agent_started_at,
            finished_at=agent_finished_at,
            records_touched=result.records_touched,
            status=result.status,
            notes=result.notes,
        )

        exec_rows.append((
            result.agent, result.status, duration,
            result.records_touched, result.notes,
        ))

        _print_dim(f"     status : {result.status}  |  "
                   f"rows: {result.records_touched}  |  {duration:.1f}s")
        if result.notes:
            _print_dim(f"     notes  : {result.notes}")

    # ── 5. Summary (rule 33 — exactly one summary row) ───────────────────────
    _print_header("Cycle summary")
    elapsed = time.monotonic() - cycle_start_mono
    outcome = "partial" if any_failed else "complete"

    _print_dim(f"  started  : {cycle_start.isoformat()}")
    _print_dim(f"  finished : {datetime.now(timezone.utc).isoformat()}")
    _print_dim(f"  elapsed  : {elapsed:.2f}s")
    _print_dim(f"  outcome  : {outcome}")
    _print_results_table(exec_rows)

    _write_cycle_summary(
        config=config,
        plan=plan,
        exec_rows=exec_rows,
        outcome=outcome,
        cycle_start=cycle_start,
        elapsed=elapsed,
        forced_names=forced_names,
    )


# ---------------------------------------------------------------------------
# --force: post-processing on an existing Plan (build_plan stays pure)
# ---------------------------------------------------------------------------


def _apply_force(
    plan: Plan,
    force: list[str],
    config: Config,
) -> tuple[Plan, list[str]]:
    """Flip any named skipped tasks to will_run=True with an operator reason.

    Returns the mutated plan and the list of names that were actually changed.
    Unknown names (not in the plan) are printed as warnings and ignored.
    """
    from edgedash.planning import Task

    known = {t.agent_name for t in plan.tasks}
    changed: list[str] = []

    for name in force:
        if name not in known:
            print(f"  {_YELLOW}⚠  --force: '{name}' is not a known agent — ignored{_RESET}")
            continue

        for i, task in enumerate(plan.tasks):
            if task.agent_name == name and not task.will_run:
                plan.tasks[i] = Task(
                    agent_name=task.agent_name,
                    goal=task.goal,
                    stop_conditions=task.stop_conditions,
                    reason=f"forced by operator (original: {task.reason})",
                    will_run=True,
                )
                changed.append(name)
            elif task.agent_name == name and task.will_run:
                print(f"  {_DIM}--force: '{name}' was already scheduled to run — no change{_RESET}")

    return plan, changed


# ---------------------------------------------------------------------------
# --explain: full state + per-decision breakdown
# ---------------------------------------------------------------------------


def _print_explain(state: "SystemState", plan: Plan) -> None:
    """Print every state value alongside the decision it drove."""
    print()
    print(f"  {_BOLD}SYSTEM STATE{_RESET}")
    print(f"  {'─' * 56}")
    print(f"  {'now':<30}  {state.now.isoformat()}")
    print(f"  {'last_fetch_at':<30}  {state.last_fetch_at or 'never'}")
    print(f"  {'hours_since_fetch':<30}  "
          f"{f'{state.hours_since_fetch:.2f}h' if state.hours_since_fetch is not None else 'n/a'}")
    print(f"  {'unscored_count':<30}  {state.unscored_count}")
    print(f"  {'gaps_computed_at':<30}  {state.gaps_computed_at or 'never'}")
    print(f"  {'gaps_stale':<30}  {state.gaps_stale}")
    print(f"  {'last_cycle_verdict':<30}  {state.last_cycle_verdict or 'none'}")
    print(f"  {'last_cycle_at':<30}  {state.last_cycle_at or 'never'}")
    print()
    print(f"  {_BOLD}PLANNING DECISIONS{_RESET}")
    print(f"  {'─' * 56}")

    _STATE_KEYS: dict[str, str] = {
        "fetcher":      "hours_since_fetch",
        "scorer":       "unscored_count",
        "gap_analyzer": "gaps_stale / gaps_computed_at",
    }

    for task in plan.tasks:
        flag     = f"{_GREEN}▶ RUN {_RESET}" if task.will_run else f"{_YELLOW}○ SKIP{_RESET}"
        key      = _STATE_KEYS.get(task.agent_name, "—")
        print(f"  {flag}  {task.agent_name:<14}  driven by: {key}")
        print(f"         {'':14}  reason   : {task.reason}")
        if task.will_run:
            print(f"         {'':14}  stop     : {task.stop_conditions.render()}")
        print()


# ---------------------------------------------------------------------------
# Summary row (rule 33)
# ---------------------------------------------------------------------------


def _write_cycle_summary(
    config: Config,
    plan: Plan,
    exec_rows: list[tuple[str, str, float, int, str | None]],
    outcome: str,
    cycle_start: datetime,
    elapsed: float,
    forced_names: list[str] | None = None,
) -> None:
    """Write exactly one summary row to cycle_log for this cycle."""
    ran     = [t.agent_name for t in plan.agents_to_run]
    skipped = [t.agent_name for t in plan.agents_skipped]
    skip_reasons = {t.agent_name: t.reason for t in plan.agents_skipped}

    per_agent_dur = ", ".join(
        f"{name}={dur:.1f}s"
        for name, status, dur, _, _ in exec_rows
        if status != "skipped"
    )

    skip_summary = "; ".join(
        f"{name}: {skip_reasons.get(name, 'skipped')}"
        for name in skipped
    )

    notes_parts = []
    if forced_names:
        notes_parts.append(f"FORCED: {', '.join(forced_names)}")
    if ran:
        notes_parts.append(f"ran: {', '.join(ran)}")
    if skipped:
        notes_parts.append(f"skipped: {skip_summary}")
    if per_agent_dur:
        notes_parts.append(f"durations: {per_agent_dur}")
    notes_parts.append(f"total: {elapsed:.1f}s")

    storage.log_cycle(
        path=config.db_path,
        agent="orchestrator/cycle",
        started_at=cycle_start.isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        records_touched=sum(r for _, _, _, r, _ in exec_rows),
        status=outcome,
        notes=" | ".join(notes_parts),
    )
