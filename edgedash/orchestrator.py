"""Orchestrator — reads state, decides what to run, delegates to agents.

The Orchestrator never fetches data or scores jobs directly.
It reads state, builds a plan, runs registered agents, and logs every outcome.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config

# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------
# To swap in a real agent, replace the import and the entry here — one line.

from edgedash.agents.mock_fetcher import MockFetcher


class _PlaceholderAgent:
    """Stand-in for agents not yet implemented."""

    def __init__(self, agent_name: str) -> None:
        self.name = agent_name

    def run(self, config: Config, db_path: str) -> AgentResult:
        msg = f"{self.name}: not implemented yet — skipping."
        _print_dim(f"  ⚠  {msg}")
        return AgentResult(
            agent=self.name,
            status="skipped",
            records_touched=0,
            notes=msg,
        )


# Ordered list of agents the orchestrator will consider each cycle.
# Replace _PlaceholderAgent(...) with the real class when it's ready.
_AGENT_REGISTRY: list[Agent] = [
    MockFetcher(),
    _PlaceholderAgent("scorer"),
    _PlaceholderAgent("gap_analyzer"),
]

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
    }.get(status, status)


def _print_table(results: list[AgentResult]) -> None:
    col_agent  = max(len(r.agent) for r in results)
    col_agent  = max(col_agent, 12)
    print()
    header = (
        f"  {'AGENT':<{col_agent}}  {'STATUS':<8}  {'NEW ROWS':>8}  NOTES"
    )
    print(f"{_BOLD}{header}{_RESET}")
    print(f"  {'─' * col_agent}  {'─' * 8}  {'─' * 8}  {'─' * 30}")
    for r in results:
        notes = textwrap.shorten(r.notes or "", width=50, placeholder="…")
        status_str = _status_colour(r.status)
        print(
            f"  {r.agent:<{col_agent}}  {status_str:<8}  "
            f"{r.records_touched:>8}  {notes}"
        )
    print()


# ---------------------------------------------------------------------------
# run_cycle
# ---------------------------------------------------------------------------


def run_cycle(config: Config) -> None:
    """Run one full orchestration cycle."""
    cycle_start = datetime.now(timezone.utc)

    # ── 1. Init DB ──────────────────────────────────────────────────────────
    storage.init_db(config.db_path)

    # ── 2. Read state ───────────────────────────────────────────────────────
    _print_header("EdgeDash — reading state")
    last_fetch   = storage.last_fetch_time(config.db_path)
    unscored     = storage.count_unscored(config.db_path)

    _print_dim(f"  db path       : {config.db_path}")
    _print_dim(f"  last fetch    : {last_fetch or 'never'}")
    _print_dim(f"  unscored rows : {unscored}")
    _print_dim(f"  target role   : {config.target_role}  |  city: {config.target_city}")

    # ── 3. Plan ─────────────────────────────────────────────────────────────
    _print_header("Plan")
    decisions: list[tuple[Agent, str]] = []
    for agent in _AGENT_REGISTRY:
        reason = _decide(agent, last_fetch, unscored)
        decisions.append((agent, reason))
        flag = "▶ RUN " if not reason.startswith("SKIP") else "○ SKIP"
        print(f"  {flag}  {agent.name:<18}  {reason}")

    # ── 4. Run agents ────────────────────────────────────────────────────────
    _print_header("Running agents")
    results: list[AgentResult] = []

    for agent, reason in decisions:
        if reason.startswith("SKIP"):
            results.append(
                AgentResult(agent=agent.name, status="skipped",
                            records_touched=0, notes=reason)
            )
            continue

        started_at = datetime.now(timezone.utc).isoformat()
        print(f"\n  → {_BOLD}{agent.name}{_RESET} …")

        try:
            result = agent.run(config, config.db_path)
        except Exception as exc:
            result = AgentResult(
                agent=agent.name,
                status="failed",
                records_touched=0,
                notes=str(exc),
            )

        finished_at = datetime.now(timezone.utc).isoformat()

        # ── 5. Log every run ─────────────────────────────────────────────
        storage.log_cycle(
            path=config.db_path,
            agent=result.agent,
            started_at=started_at,
            finished_at=finished_at,
            records_touched=result.records_touched,
            status=result.status,
            notes=result.notes,
        )

        results.append(result)
        _print_dim(f"     status: {result.status}  |  new rows: {result.records_touched}")
        if result.notes:
            _print_dim(f"     notes : {result.notes}")

    # ── 6. Summary ──────────────────────────────────────────────────────────
    _print_header("Cycle summary")
    cycle_end = datetime.now(timezone.utc)
    elapsed   = (cycle_end - cycle_start).total_seconds()
    _print_dim(f"  started  : {cycle_start.isoformat()}")
    _print_dim(f"  finished : {cycle_end.isoformat()}")
    _print_dim(f"  elapsed  : {elapsed:.2f}s")
    _print_table(results)


# ---------------------------------------------------------------------------
# Decision logic (pure function — easy to test)
# ---------------------------------------------------------------------------


def _decide(agent: Agent, last_fetch: str | None, unscored: int) -> str:
    """Return a human-readable run/skip reason for a given agent."""
    if agent.name in ("mock_fetcher", "fetcher"):
        return "RUN — scheduled fetch every cycle"
    if agent.name == "scorer":
        if unscored == 0:
            return "SKIP — no unscored listings"
        return f"RUN — {unscored} listings waiting to be scored"
    if agent.name == "gap_analyzer":
        return "SKIP — runs after scorer produces results"
    # Unknown future agents: run by default.
    return "RUN — default policy"
