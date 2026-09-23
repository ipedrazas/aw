"""Validation of a workflow definition: structural, then semantic.

The same rules judge a draft at audit time and a definition before a run, so a gap
found while auditing is the same object as a failure hit while running.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wf.schema import Workflow, Workspace

from .findings import QUESTIONS, Finding, Option, make_finding, question_for
from .semantic import validate_semantic
from .structural import KNOWN_RUNNERS, validate_structural
from .templates import (
    PRODUCES,
    STEP_TEMPLATES,
    WEB_TOOLS,
    StepTemplate,
    everything_before,
    follows_the_answer,
    is_workflow_input,
    says_it_searches,
)


class ValidationResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.status == "open" for f in self.findings)

    def of_type(self, t: str) -> list[Finding]:
        return [f for f in self.findings if f.type == t]

    def by_field(self) -> dict[str, Finding]:
        return {f.field: f for f in self.findings}

    def ordered(self) -> list[Finding]:
        """Ordered by how much of the process each answer unblocks, then by type."""
        rank = {"conflict": 0, "gap": 1, "unreachable": 2, "assumption": 3}
        return sorted(self.findings, key=lambda f: (-f.unblocks, rank[f.type], f.field))


def validate(wf: Workflow, ws: Workspace | None = None) -> ValidationResult:
    findings = validate_structural(wf, ws)
    findings.extend(validate_semantic(wf, ws))
    # de-duplicate identical findings raised twice
    seen: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        if f.id in seen:
            continue
        seen.add(f.id)
        unique.append(f)
    return ValidationResult(findings=unique)


__all__ = [
    "KNOWN_RUNNERS",
    "PRODUCES",
    "WEB_TOOLS",
    "QUESTIONS",
    "STEP_TEMPLATES",
    "Finding",
    "everything_before",
    "follows_the_answer",
    "says_it_searches",
    "Option",
    "StepTemplate",
    "ValidationResult",
    "is_workflow_input",
    "make_finding",
    "question_for",
    "validate",
    "validate_semantic",
    "validate_structural",
]
