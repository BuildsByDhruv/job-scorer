"""Shared contract for every EdgeDash agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from edgedash.config import Config

if TYPE_CHECKING:
    from edgedash.planning import StopConditions


@dataclass
class AgentResult:
    agent: str
    status: str          # "ok" | "failed" | "skipped"
    records_touched: int
    notes: str | None = None


@runtime_checkable
class Agent(Protocol):
    """Every agent must expose a name and a run() method.

    stop_conditions is supplied by the Orchestrator (rule 29).
    Agents must respect it — they never decide their own limits.
    The parameter is Optional so standalone CLI entry points continue
    to work without passing stop conditions.
    """

    name: str

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: "StopConditions | None" = None,
    ) -> AgentResult:
        ...
