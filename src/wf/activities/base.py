"""The activity boundary.

Every side effect (model call, search, fetch, link check, render, send) goes through
an activity with declared retries and timeouts. Step code never touches the
network. The interpreter is deterministic on the near side of this boundary; all
non-determinism lives on the far side, which is what makes replay possible later.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from wf.schema import Mode


class ActivityError(Exception):
    """A side effect failed after its declared retries."""


@dataclass(frozen=True)
class ActivityPolicy:
    retries: int = 2
    timeout_s: float = 120.0
    backoff_s: float = 1.0


def run_with_policy(
    policy: ActivityPolicy, fn: Callable[[], Any], *, sleep: Callable[[float], None] = time.sleep
) -> Any:
    last: Exception | None = None
    for attempt in range(policy.retries + 1):
        try:
            return fn()
        except ActivityError:
            raise
        except Exception as e:  # noqa: BLE001 - the boundary is where we catch everything
            last = e
            if attempt < policy.retries:
                sleep(policy.backoff_s * (2**attempt))
    raise ActivityError(str(last)) from last


# -- model -------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    executor: Callable[[dict[str, Any]], Any]
    max_calls: int = 10


@dataclass
class ModelRequest:
    tag: str  # step id (plus fan-out index) so recordings can be keyed
    model: str
    system: str
    input: dict[str, Any]
    output_schema: dict[str, Any]
    tools: list[ToolSpec] = field(default_factory=list)
    decisions_required: bool = False
    max_tokens: int = 16000


@dataclass
class ToolCallRecord:
    name: str
    input: dict[str, Any]
    summary: str
    injection: str | None = None  # the text that read as instructions, if any


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ModelResponse:
    output: dict[str, Any]
    decisions: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "decisions": self.decisions,
            "usage": self.usage.__dict__,
            "tool_calls": [t.__dict__ for t in self.tool_calls],
            "model": self.model,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ModelResponse:
        return cls(
            output=d["output"],
            decisions=d.get("decisions", []),
            usage=Usage(**d.get("usage", {})),
            tool_calls=[ToolCallRecord(**t) for t in d.get("tool_calls", [])],
            model=d.get("model", ""),
        )


class ModelActivity(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


# -- search / fetch / links ------------------------------------------------------


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    published: str | None = None


class SearchActivity(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...

    def get_contents(self, url: str) -> dict[str, Any]: ...


@dataclass
class LinkStatus:
    url: str
    status: int
    opens: bool


class LinkCheckActivity(Protocol):
    vantage: str

    def check(self, urls: list[str]) -> list[LinkStatus]: ...


# -- render / send -----------------------------------------------------------


class RenderActivity(Protocol):
    def render_pdf(
        self,
        *,
        title: str,
        report: dict[str, Any],
        followups: list[dict[str, Any]],
        link_check: dict[str, Any] | None,
        simulated: bool,
        out_path: str,
    ) -> dict[str, Any]: ...


class SendActivity(Protocol):
    def send(
        self, *, to: str, subject: str, body: str, attachments: list[str]
    ) -> dict[str, Any]: ...


# -- guess -------------------------------------------------------------------


@dataclass
class Guess:
    value: Any
    text: str
    reason: str
    alternatives: list[str] = field(default_factory=list)


class Guesser(Protocol):
    def guess(self, *, finding: Any, step: Any, state: dict[str, Any], mode: Mode) -> Guess: ...


# -- the bundle the interpreter receives -----------------------------------------


@dataclass
class Activities:
    model: ModelActivity
    search: SearchActivity
    links: LinkCheckActivity
    render: RenderActivity
    send: SendActivity
    guesser: Guesser
    policy: ActivityPolicy = field(default_factory=ActivityPolicy)


SideEffectMode = Literal["real", "recorded"]
