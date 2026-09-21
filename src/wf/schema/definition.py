"""Pydantic models for a workflow definition.

The models are deliberately permissive about *presence*: a draft produced by the
auditor is allowed to have holes, and it is the validator's job to turn each hole
into a typed finding with a question. What the models are strict about is
*shape*: unknown keys are rejected, so a typo cannot masquerade as a missing field.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepKind = Literal["agent", "check", "tool", "subworkflow", "wait"]
TrustPolicy = Literal["earned", "auto", "always_ask"]
Mode = Literal["live", "dry", "eval"]
FindingType = Literal["gap", "conflict", "assumption", "unreachable"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InputSpec(Strict):
    type: Literal["string", "integer", "number", "boolean", "list", "object"] = "string"
    required: bool = False
    default: Any = None
    min_length: int | None = None
    internal: bool = False
    enum: list[str] | None = None
    description: str | None = None


class Budget(Strict):
    max_usd: float | None = None
    max_minutes: float | None = None
    shared_with_children: bool = True
    on_exceeded: Literal["pause_and_ask", "stop"] = "pause_and_ask"


class Trust(Strict):
    policy: TrustPolicy
    promote_after: int | None = None
    reset_on: list[str] | None = None


class Defaults(Strict):
    trust: Trust | None = None
    decision_log: Literal["optional", "required"] = "optional"
    on_error: Literal["pause_and_explain", "fail"] = "pause_and_explain"


class Output(Strict):
    schema_: str | None = Field(default=None, alias="schema")
    artifact: str | None = None
    # Values of a branching field that need no branch of their own: the run simply
    # continues to the next step in order. Stated explicitly so silence is a finding.
    continue_on: dict[str, list[str]] = Field(default_factory=dict)


class Limits(Strict):
    max_depth: int | None = None
    max_fanout: int | None = None
    budget: Literal["inherit"] | float | None = None


class ToolPermission(Strict):
    max_calls: int = 10


class Origin(Strict):
    """Who put this step here. Drives the "I suggested this" markers in the UI."""

    by: Literal["user", "system"] = "user"
    kind: Literal["stated", "suggested", "split", "answered"] = "stated"
    reason: str | None = None


class Step(Strict):
    id: str
    kind: StepKind
    title: str | None = None
    description: str | None = None
    shows_user: list[str] | None = None
    trust: Trust | None = None

    # control flow (the only places it may live)
    when: str | None = None
    for_each: str | None = None
    max_fanout: int | None = None
    with_: dict[str, Any] | None = Field(default=None, alias="with")

    input: dict[str, Any] | None = None
    output: Output | None = None

    # agent
    model: str | None = None
    skill: str | None = None
    tools: dict[str, ToolPermission] | None = None
    decision_log: Literal["optional", "required"] | None = None

    # check / tool
    run: str | None = None
    checks: list[str] | None = None
    does_not_check: list[str] | None = None
    on_fail: Literal["annotate", "pause", "fail"] | None = None
    side_effects: Literal["none"] | list[str] | None = None
    requires_approval: bool | str | None = None

    # subworkflow
    workflow: str | None = None
    limits: Limits | None = None

    # wait
    deadline: str | None = None
    on_timeout: Literal["continue", "stop", "escalate", "remind"] | None = None

    origin: Origin | None = None

    @property
    def effective_max_fanout(self) -> int | None:
        if self.max_fanout is not None:
            return self.max_fanout
        if self.limits and self.limits.max_fanout is not None:
            return self.limits.max_fanout
        return None


class Evals(Strict):
    model_config = ConfigDict(extra="allow")


class Spec(Strict):
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    budget: Budget | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    steps: list[Step] = Field(default_factory=list)
    evals: Evals | None = None


class Metadata(Strict):
    name: str
    version: int = 1
    description: str | None = None


class Workflow(Strict):
    apiVersion: str = "workflows.tavon.io/v1alpha1"
    kind: Literal["Workflow"] = "Workflow"
    metadata: Metadata
    spec: Spec

    def step(self, step_id: str) -> Step | None:
        for s in self.spec.steps:
            if s.id == step_id:
                return s
        return None

    def step_index(self, step_id: str) -> int:
        for i, s in enumerate(self.spec.steps):
            if s.id == step_id:
                return i
        return -1

    def trust_for(self, step: Step) -> Trust | None:
        return step.trust or self.spec.defaults.trust

    def decision_log_for(self, step: Step) -> str:
        return step.decision_log or self.spec.defaults.decision_log

    def has_subworkflow(self) -> bool:
        return any(s.kind == "subworkflow" for s in self.spec.steps)
