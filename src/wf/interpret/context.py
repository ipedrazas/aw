from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BudgetTracker:
    """Shared between a run and its children when the budget says so."""

    max_usd: float | None
    max_minutes: float | None
    started: float = field(default_factory=time.monotonic)
    spent_usd: float = 0.0

    def add(self, usd: float) -> None:
        self.spent_usd += usd

    @property
    def spent_minutes(self) -> float:
        return (time.monotonic() - self.started) / 60.0

    def exceeded(self) -> str | None:
        if self.max_usd is not None and self.spent_usd > self.max_usd:
            return f"spent ${self.spent_usd:.2f} of a ${self.max_usd:.2f} limit"
        if self.max_minutes is not None and self.spent_minutes > self.max_minutes:
            return f"took {self.spent_minutes:.0f} minutes of a {self.max_minutes:.0f} minute limit"
        return None

    @property
    def remaining_usd(self) -> float | None:
        return None if self.max_usd is None else max(self.max_usd - self.spent_usd, 0.0)


@dataclass
class TraceEvent:
    """One control-flow decision by the interpreter. The determinism test compares these."""

    step_id: str
    event: str  # run | skip | fanout | guess | stub | wait | paused | failed
    detail: Any = None

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.step_id, self.event, repr(self.detail))


class OpenFindings(Exception):
    def __init__(self, findings: list[Any]):
        super().__init__(f"{len(findings)} open finding(s); answer them or run in dry mode")
        self.findings = findings
