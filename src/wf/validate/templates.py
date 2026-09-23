"""Predefined step templates: the fields the auditor expects for each step kind.

The extractor sometimes gets a step's kind wrong. "First, get my topic" was once read
as a ``wait`` step, so the auditor asked the ``wait`` template's question -- "how long
do you wait, and then what?" -- of a step that was never going to wait for anyone: the
workflow starts the moment the topic is typed. Rather than let a misread kind drive an
improvised question, each recognized kind has a template naming the fields it needs
answered, and a step that does not actually fit the kind it was given is steered to
the one it does fit before any question is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wf.schema import StepKind


@dataclass(frozen=True)
class StepTemplate:
    kind: StepKind
    required: tuple[str, ...]  # fields, beyond title/shows_user/trust, this kind needs answered


STEP_TEMPLATES: dict[StepKind, StepTemplate] = {
    "agent": StepTemplate("agent", ("model", "skill", "output.schema")),
    "check": StepTemplate("check", ("run", "checks", "does_not_check")),
    "tool": StepTemplate("tool", ("run", "side_effects", "requires_approval")),
    "subworkflow": StepTemplate("subworkflow", ("workflow", "limits")),
    "wait": StepTemplate("wait", ("deadline", "on_timeout")),
}


def is_workflow_input(step: dict[str, Any]) -> bool:
    """Whether a step guessed as ``wait`` is really how the workflow starts.

    A step that reads nothing and is guessed as ``wait`` does not fit the ``wait``
    template: there is no earlier step for it to wait on, and nothing to time out on
    the very first thing that happens. It fits no step template at all, because it is
    not a step -- it is the workflow's input, given the moment someone starts it.
    """
    return step.get("kind") == "wait" and not step.get("reads_from")
