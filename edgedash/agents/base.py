"""Shared contract for every EdgeDash agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from edgedash.config import Config


@dataclass
class AgentResult:
    agent: str
    status: str          # "ok" | "failed"
    records_touched: int
    notes: str | None = None


@runtime_checkable
class Agent(Protocol):
    """Every agent must expose a name and a run() method."""

    name: str

    def run(self, config: Config, db_path: str) -> AgentResult:
        ...
