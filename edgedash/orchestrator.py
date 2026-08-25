"""Orchestrator — reads state, builds a plan, executes it, logs one summary row.

Rules enforced here:
  28 — state-driven, never a fixed sequence; skipping is a success
  29 — every delegation carries the Task's stop_conditions
  30 — no fetching, scoring, or analysis logic lives here
  31 — plan is printed before execution
  32 — one agent failing does not stop the cycle; cycle marked partial
  33 — exactly one summary row written per cycle
  34 — Verifier writes no data; Orchestrator acts on its verdict
  36 — at most ONE retry of the failing agent; degrade and stop after that
  38 — cycle summary row embeds VERDICT so get_last_verified_cycle() can query it
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
from edgedash.agents.verifier import Verifier
from edgedash.config import Config
from edgedash.planning import Plan, StopConditions, Task, build_plan
from edgedash.state import SystemState, read_state
from edgedash.verification import Verdict

# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------
# To add a new agent: add it here AND add a decision rule in planning.py.
# The Orchestrator resolves agents by name — nothing else changes.

def _build_registry(config: Config) -> dict[str, Agent]:
    fetcher: Agent = MockFetcher() if config.use_mock_fetcher else Fetcher()
    agents: list[Agent] = [fetcher, Scorer(), GapAnalyzer(), Verifier()]
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
        "ok":       f"{_GREEN}ok{_RESET}",
        "failed":   f"{_RED}failed{_RESET}",
        "skipped":  f"{_YELLOW}skipped{_RESET}",
        "partial":  f"{_YELLOW}partial{_RESET}",
        "degraded": f"{_RED}degraded{_RESET}",
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

    cycle_start      = datetime.now(timezone.utc)
    cycle_start_mono = time.monotonic()

    # ── 1. Init DB ──────────────────────────────────────────────────────────
    storage.init_db(config.db_path)

    # ── 2. Read state ────────────────────────────────────────────────────────
    _print_header("EdgeDash — reading state")
    state = read_state(config, now=cycle_start)

    _print_dim(f"  db path            : {config.db_path}")
    _print_dim(f"  last fetch         : {state.last_fetch_at or 'never'}")
    _print_dim(
        f"  hours since fetch  : "
        f"{f'{state.hours_since_fetch:.1f}h' if state.hours_since_fetch is not None else 'n/a'}"
    )
    _print_dim(f"  unscored rows      : {state.unscored_count}")
    _print_dim(f"  gaps computed at   : {state.gaps_computed_at or 'never'}")
    _print_dim(f"  gaps stale         : {state.gaps_stale}")
    _print_dim(
        f"  last cycle         : "
        f"{state.last_cycle_verdict or 'none'} at {state.last_cycle_at or 'n/a'}"
    )
    _print_dim(f"  target role        : {config.target_role}  |  city: {config.target_city}")
    _print_dim(
        f"  fetcher mode       : {'mock' if config.use_mock_fetcher else 'live'}"
        f"  |  sources: {', '.join(config.sources)}"
    )

    # ── 3. Build and PRINT plan (rule 31) ────────────────────────────────────
    plan     = build_plan(state, config)
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

    # ── Nothing to do — success, exit cleanly (rule 28) ──────────────────────
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
            verdict="n/a",
            retry_count=0,
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

        result, duration = _run_one_agent(task, registry, config)
        exec_rows.append((
            result.agent, result.status, duration,
            result.records_touched, result.notes,
        ))
        if result.status == "failed":
            any_failed = True

    # ── 5. Verification (rules 34–38) ────────────────────────────────────────
    #
    # The Verifier is NOT in the plan — it always runs after the main agents
    # complete, once we have data to judge. It is excluded from plan.tasks so
    # the Orchestrator controls when and how many times it fires.

    _print_header("Verification")

    verdict_str, retry_count = _verification_pass(
        config=config,
        registry=registry,
        exec_rows=exec_rows,
    )

    # ── 6. Summary (rule 33 — exactly one summary row) ───────────────────────
    _print_header("Cycle summary")
    elapsed = time.monotonic() - cycle_start_mono
    outcome = _outcome(any_failed, verdict_str)

    _print_dim(f"  started  : {cycle_start.isoformat()}")
    _print_dim(f"  finished : {datetime.now(timezone.utc).isoformat()}")
    _print_dim(f"  elapsed  : {elapsed:.2f}s")
    _print_dim(f"  outcome  : {outcome}")
    _print_dim(f"  verdict  : {verdict_str}")
    _print_dim(f"  retries  : {retry_count}")
    _print_results_table(exec_rows)

    _write_cycle_summary(
        config=config,
        plan=plan,
        exec_rows=exec_rows,
        outcome=outcome,
        cycle_start=cycle_start,
        elapsed=elapsed,
        forced_names=forced_names,
        verdict=verdict_str,
        retry_count=retry_count,
    )


# ---------------------------------------------------------------------------
# Verification + conditional retry (rule 36)
# ---------------------------------------------------------------------------


def _verification_pass(
    config: Config,
    registry: dict[str, Agent],
    exec_rows: list[tuple[str, str, float, int, str | None]],
) -> tuple[str, int]:
    """Run verification. On fail, retry the owning agent once, then re-verify.

    Returns (verdict_str, retry_count) where:
        verdict_str  in {"pass", "fail", "degraded", "skipped"}
        retry_count  in {0, 1}

    "degraded" means the first retry also failed verification — cycle stops.
    "skipped"  means the verifier itself raised an exception.

    Never raises. Logs every step to cycle_log.
    """
    verifier = registry.get("verifier")
    if verifier is None:
        _print_dim("  ⚠  verifier not in registry — verification skipped")
        return "skipped", 0

    # ── First verification run ────────────────────────────────────────────
    verdict1, result1, dur1 = _run_verifier(verifier, config)

    exec_rows.append((
        result1.agent, result1.status, dur1,
        result1.records_touched, result1.notes,
    ))
    _log_agent_result(result1, dur1, config)

    _print_dim(f"  {result1.notes}")

    if verdict1 is None:
        # Verifier raised — treat as skipped (fail-loudly already logged)
        return "skipped", 0

    if verdict1.passed:
        return "pass", 0

    # ── Verdict failed — identify which agent to retry ────────────────────
    first_failed_check = verdict1.failed_checks[0].name
    retry_agent_name   = _check_to_agent(first_failed_check)
    retry_stop         = _retry_stop_conditions(first_failed_check, config)

    print(
        f"\n  {_YELLOW}⚠  Verification failed "
        f"({first_failed_check}). "
        f"Retrying '{retry_agent_name}' with adjusted context.{_RESET}"
    )

    retry_task = registry.get(retry_agent_name)
    if retry_task is None:
        msg = (
            f"retry agent '{retry_agent_name}' not in registry — "
            "cannot retry; marking cycle degraded"
        )
        print(f"  {_RED}✗  {msg}{_RESET}")
        storage.log_cycle(
            path=config.db_path,
            agent=f"orchestrator/retry-missing",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            records_touched=0,
            status="failed",
            notes=msg,
        )
        return "degraded", 1

    # ── Retry the failing agent ───────────────────────────────────────────
    print(f"\n  → {_BOLD}{retry_agent_name}{_RESET} [retry]  "
          f"{_DIM}stop: {retry_stop.render()}{_RESET}")

    dummy_task = Task(
        agent_name=retry_agent_name,
        goal=f"retry after {first_failed_check} failure",
        stop_conditions=retry_stop,
        reason=f"verification retry: {first_failed_check}",
        will_run=True,
    )

    retry_result, retry_dur = _run_one_agent(dummy_task, registry, config)
    exec_rows.append((
        retry_result.agent, retry_result.status, retry_dur,
        retry_result.records_touched, retry_result.notes,
    ))
    _print_dim(
        f"     status : {retry_result.status}  |  "
        f"rows: {retry_result.records_touched}  |  {retry_dur:.1f}s"
    )
    if retry_result.notes:
        _print_dim(f"     notes  : {retry_result.notes}")

    # ── Second (and final) verification run ───────────────────────────────
    print(f"\n  → {_BOLD}verifier{_RESET} [re-verify after retry]")

    verdict2, result2, dur2 = _run_verifier(verifier, config)

    exec_rows.append((
        result2.agent, result2.status, dur2,
        result2.records_touched, result2.notes,
    ))
    _log_agent_result(result2, dur2, config)

    _print_dim(f"  {result2.notes}")

    if verdict2 is None:
        return "degraded", 1

    if verdict2.passed:
        print(f"  {_GREEN}✓  Re-verification passed after retry.{_RESET}")
        return "pass", 1

    # ── Both verification runs failed — degrade (rule 36) ─────────────────
    still_failing = ", ".join(c.name for c in verdict2.failed_checks)
    print(
        f"\n  {_RED}✗  Re-verification still failing "
        f"({still_failing}). "
        f"Cycle marked degraded. No further retries.{_RESET}"
    )
    storage.log_cycle(
        path=config.db_path,
        agent="orchestrator/degraded",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        records_touched=0,
        status="degraded",
        notes=(
            f"verification failed after 1 retry — "
            f"still failing: {still_failing}"
        ),
    )
    return "degraded", 1


def _run_verifier(
    verifier: Agent,
    config: Config,
) -> tuple[Verdict | None, AgentResult, float]:
    """Run the Verifier, return (Verdict | None, AgentResult, duration_s).

    Returns Verdict=None if the verifier itself raises — that is a
    different failure mode from a failed verdict.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    try:
        result = verifier.run(config, config.db_path, None)
    except Exception as exc:
        dur    = time.monotonic() - t0
        result = AgentResult(
            agent="verifier",
            status="failed",
            records_touched=0,
            notes=f"verifier raised: {exc}",
        )
        storage.log_cycle(
            path=config.db_path,
            agent="verifier",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            records_touched=0,
            status="failed",
            notes=result.notes,
        )
        print(f"  {_RED}✗  [verifier] unhandled exception: {exc}{_RESET}")
        return None, result, dur

    dur = time.monotonic() - t0

    # Re-hydrate the Verdict from the AgentResult.notes so the Orchestrator
    # can inspect which checks failed without coupling to the Verifier's
    # internal Verdict object — the notes string is the contract.
    # But the Verifier also returns the Verdict embedded in result for us to
    # use directly. We extract it here to keep the Orchestrator clean.
    # Since the Verifier doesn't attach the Verdict object to AgentResult
    # (AgentResult has no such field), we reconstruct passed from status.
    # The check names are parsed from the notes for logging; for routing we
    # only need the _first_ failed check name, which is always present in
    # the notes string after "VERDICT: fail — ".
    verdict = _parse_verdict_from_notes(result.notes, result.status)
    return verdict, result, dur


def _parse_verdict_from_notes(notes: str, status: str) -> Verdict:
    """Re-construct a minimal Verdict from the notes string.

    We don't need a fully populated Verdict — just `passed` and
    `failed_checks[0].name` for routing the retry. A lightweight parse
    is enough and keeps the coupling to the notes format explicit.
    """
    from edgedash.verification import CheckResult, Verdict

    passed = (status == "ok")

    if passed:
        return Verdict(passed=True, failed_checks=[], summary=notes)

    # Extract check names from "VERDICT: fail — name1 observed ...; name2 ..."
    # The first token before " observed" in each semicolon-delimited segment
    # is the check name.
    failed_checks: list[CheckResult] = []
    try:
        # Slice off the "VERDICT: fail — " prefix.
        tail = notes.split("VERDICT: fail — ", 1)[-1]
        for segment in tail.split(";"):
            segment = segment.strip()
            name = segment.split(" observed")[0].strip()
            if name:
                failed_checks.append(
                    CheckResult(
                        name=name,
                        passed=False,
                        observed=segment,
                        threshold=None,
                        message=segment,
                    )
                )
    except Exception:
        pass   # Keep at least an empty list — the cycle will still degrade.

    return Verdict(
        passed=False,
        failed_checks=failed_checks,
        summary=notes,
    )


def _check_to_agent(check_name: str) -> str:
    """Map a failed check name to the agent responsible for fixing it."""
    _MAP: dict[str, str] = {
        "score_spread":         "scorer",
        "extraction_sanity":    "scorer",   # extractor is called by scorer
        "gap_sample_size":      "gap_analyzer",
        "freshness":            "fetcher",
    }
    return _MAP.get(check_name, "scorer")   # default to scorer if unknown


def _retry_stop_conditions(check_name: str, config: Config) -> StopConditions:
    """Build adjusted StopConditions for the retry of a named failed check.

    score_spread failure — widen_distribution=True
    ------------------------------------------------
    The first scoring run may have processed a pre-filtered cluster (e.g. only
    mid-tier listings happened to be unscored) causing all scores to land in a
    tight band. The flag tells the Scorer to:
      1. Call storage.clear_score_all() — wipes existing scores so
         get_unscored_listings() returns the full current set.
      2. Score the full batch from scratch.
    This gives the distribution a chance to spread across the actual range
    of the listed jobs rather than the accidental narrow cluster from run 1.

    extraction_sanity failure — same as score_spread (re-runs extraction).
    freshness failure — re-run the fetcher with a slightly larger page limit.
    gap_sample_size failure — re-run the gap analyzer (no special flags needed;
        if freshness is fine, more listings may now qualify).
    """
    base_items   = config.llm_batch_size
    base_seconds = config.score_max_seconds

    if check_name in ("score_spread", "extraction_sanity"):
        return StopConditions(
            max_items=base_items,
            max_seconds=base_seconds,
            context_flags={"widen_distribution": True},
        )

    if check_name == "freshness":
        return StopConditions(
            max_pages=config.fetch_max_pages + 2,
            max_items=config.fetch_max_listings,
        )

    # gap_sample_size and anything else
    return StopConditions(
        max_items=base_items,
        max_seconds=base_seconds,
    )


# ---------------------------------------------------------------------------
# Single-agent execution helper (avoids duplicating run/log/row pattern)
# ---------------------------------------------------------------------------


def _run_one_agent(
    task: Task,
    registry: dict[str, Agent],
    config: Config,
) -> tuple[AgentResult, float]:
    """Run one agent from the registry, log the result, return (result, duration).

    Never raises — catches all exceptions and converts them to a failed
    AgentResult (rule 32).
    """
    agent = registry.get(task.agent_name)
    if agent is None:
        msg = f"agent '{task.agent_name}' not found in registry"
        print(f"\n  ✗  {msg}")
        result = AgentResult(
            agent=task.agent_name,
            status="failed",
            records_touched=0,
            notes=msg,
        )
        _log_agent_result(result, 0.0, config)
        return result, 0.0

    print(f"\n  → {_BOLD}{task.agent_name}{_RESET}  "
          f"{_DIM}goal: {task.goal}{_RESET}")
    print(f"     {_DIM}stop: {task.stop_conditions.render()}{_RESET}")

    t0         = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        result = agent.run(config, config.db_path, task.stop_conditions)
    except Exception as exc:
        result = AgentResult(
            agent=task.agent_name,
            status="failed",
            records_touched=0,
            notes=str(exc),
        )
        print(f"  ✗  [{task.agent_name}] unhandled exception: {exc}")

    duration = time.monotonic() - t0

    _log_agent_result(result, duration, config, started_at=started_at)

    _print_dim(f"     status : {result.status}  |  "
               f"rows: {result.records_touched}  |  {duration:.1f}s")
    if result.notes:
        _print_dim(f"     notes  : {result.notes}")

    return result, duration


def _log_agent_result(
    result: AgentResult,
    duration: float,
    config: Config,
    started_at: str | None = None,
) -> None:
    """Write one cycle_log row for a completed agent run."""
    now_iso    = datetime.now(timezone.utc).isoformat()
    started_at = started_at or now_iso
    storage.log_cycle(
        path=config.db_path,
        agent=result.agent,
        started_at=started_at,
        finished_at=now_iso,
        records_touched=result.records_touched,
        status=result.status,
        notes=result.notes,
    )


def _outcome(any_failed: bool, verdict_str: str) -> str:
    """Map agent failures + verdict to a single outcome label."""
    if verdict_str == "degraded":
        return "degraded"
    if any_failed:
        return "partial"
    return "complete"


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
    known   = {t.agent_name for t in plan.tasks}
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
        flag = f"{_GREEN}▶ RUN {_RESET}" if task.will_run else f"{_YELLOW}○ SKIP{_RESET}"
        key  = _STATE_KEYS.get(task.agent_name, "—")
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
    verdict: str = "n/a",
    retry_count: int = 0,
) -> None:
    """Write exactly one summary row to cycle_log for this cycle.

    The notes field always contains either 'VERDICT: pass' or
    'VERDICT: fail' / 'VERDICT: degraded' so that
    get_last_verified_cycle() can locate the most recent passing cycle
    with a simple LIKE query (rule 38).
    """
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

    notes_parts: list[str] = []
    if forced_names:
        notes_parts.append(f"FORCED: {', '.join(forced_names)}")
    if ran:
        notes_parts.append(f"ran: {', '.join(ran)}")
    if skipped:
        notes_parts.append(f"skipped: {skip_summary}")
    if per_agent_dur:
        notes_parts.append(f"durations: {per_agent_dur}")
    notes_parts.append(f"total: {elapsed:.1f}s")
    notes_parts.append(f"retries: {retry_count}")

    # Verdict token — get_last_verified_cycle() searches for VERDICT: pass.
    if verdict == "pass":
        notes_parts.append("VERDICT: pass")
    elif verdict == "n/a":
        notes_parts.append("VERDICT: n/a (no agents ran)")
    else:
        # For fail/degraded, pull the failed check names from exec_rows so the
        # orchestrator summary row is self-contained and verdicts.py can parse
        # them without a join back to the verifier row.
        failed_check_str = ""
        for name, status, dur, rows_n, row_notes in exec_rows:
            if name == "verifier" and status == "failed" and row_notes:
                if "VERDICT: fail — " in row_notes:
                    # Extract the check-name portion: everything after "VERDICT: fail — "
                    # and before the first " | " (pipe-separated notes suffix).
                    tail = row_notes.split("VERDICT: fail — ", 1)[1]
                    tail = tail.split(" | ")[0].strip()
                    failed_check_str = tail
                    break
        if failed_check_str:
            notes_parts.append(f"VERDICT: {verdict} — {failed_check_str}")
        else:
            notes_parts.append(f"VERDICT: {verdict}")

    storage.log_cycle(
        path=config.db_path,
        agent="orchestrator/cycle",
        started_at=cycle_start.isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        records_touched=sum(r for _, _, _, r, _ in exec_rows),
        status=outcome,
        notes=" | ".join(notes_parts),
    )
