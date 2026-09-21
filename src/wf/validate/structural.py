"""Structural validation: required fields, references, bounds, expressions parse.

Nothing here needs a model. Every failure becomes a finding with a question.
"""

from __future__ import annotations

from wf.expr import ExprError, expressions_in, parse, walk
from wf.schema import Step, Workflow, Workspace, parse_pin

from .findings import Finding, Option, make_finding

COMMON_REQUIRED = ("title", "shows_user", "trust")

KIND_REQUIRED: dict[str, tuple[str, ...]] = {
    "agent": ("model", "skill", "output.schema"),
    "check": ("run", "checks", "does_not_check"),
    "tool": ("run", "side_effects", "requires_approval"),
    "subworkflow": ("workflow", "limits"),
    "wait": ("deadline", "on_timeout"),
}

# registered routines a check or tool may name. Definitions cannot add code.
KNOWN_RUNNERS = {"checks.http_resolves", "tools.render_pdf", "tools.send_email"}


def _has(step: Step, wf: Workflow, field: str) -> bool:
    if field == "trust":
        return wf.trust_for(step) is not None
    if field == "output.schema":
        return bool(step.output and step.output.schema_)
    if field == "limits":
        lim = step.limits
        return bool(lim and lim.max_depth is not None and lim.budget is not None)
    v = getattr(step, field, None)
    if isinstance(v, list):
        return len(v) > 0
    return v is not None


def _unblocks(wf: Workflow, step: Step) -> int:
    """How many later steps read from this step. A rough measure of what an answer unlocks."""
    idx = wf.step_index(step.id)
    n = 0
    for later in wf.spec.steps[idx + 1 :]:
        srcs = expressions_in([later.when, later.for_each, later.with_, later.input])
        if any(f"steps.{step.id}" in s for s in srcs):
            n += 1
    return n


def validate_structural(wf: Workflow, ws: Workspace | None = None) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()

    for step in wf.spec.steps:
        unblocks = _unblocks(wf, step)
        if step.id in seen:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.id",
                    step=step,
                    detail=f"Two steps are called “{step.id}”.",
                    answer_kind="text",
                )
            )
        seen.add(step.id)

        for field in COMMON_REQUIRED + KIND_REQUIRED[step.kind]:
            if not _has(step, wf, field):
                findings.append(
                    make_finding("gap", f"steps.{step.id}.{field}", step=step, unblocks=unblocks)
                )

        # a branch needs when; a fan-out needs a ceiling
        if step.for_each is not None and step.effective_max_fanout is None:
            findings.append(
                make_finding("gap", f"steps.{step.id}.max_fanout", step=step, unblocks=unblocks)
            )
        if step.with_ is not None and step.for_each is None:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.with",
                    step=step,
                    detail="“with” fills in each item of a list, but this step has no list to run over.",
                    answer_kind="text",
                )
            )
        if (
            step.kind == "subworkflow"
            and step.limits
            and step.limits.max_depth is not None
            and step.limits.max_depth < 1
        ):
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.limits.max_depth",
                    step=step,
                    detail="A depth below 1 means this can never run.",
                    answer_kind="number",
                )
            )

        # expressions parse, and reference only earlier steps
        findings.extend(_check_expressions(wf, step))

        # references resolve
        if ws is not None:
            findings.extend(_check_references(wf, step, ws))

    if wf.has_subworkflow() and wf.spec.budget is None:
        findings.append(
            make_finding(
                "gap",
                "spec.budget",
                detail="This workflow can start more research, so it needs one spending limit for the whole tree.",
                answer_kind="choice",
                options=[
                    Option(
                        value={"max_usd": 5, "max_minutes": 20}, label="About $5 and 20 minutes"
                    ),
                    Option(
                        value={"max_usd": 12, "max_minutes": 45}, label="About $12 and 45 minutes"
                    ),
                    Option(
                        value={"max_usd": 30, "max_minutes": 120}, label="About $30 and 2 hours"
                    ),
                ],
                unblocks=len(wf.spec.steps),
            )
        )

    for name, spec in wf.spec.inputs.items():
        if spec.required and spec.default is not None:
            findings.append(
                make_finding(
                    "conflict",
                    f"spec.inputs.{name}",
                    detail=f"Input “{name}” is both required and given a default.",
                    answer_kind="text",
                )
            )

    for name, expr in wf.spec.outputs.items():
        for src in expressions_in(expr):
            try:
                info = walk(parse(src))
            except ExprError as e:
                findings.append(
                    make_finding(
                        "conflict",
                        f"spec.outputs.{name}",
                        detail=f"This cannot be read: {e}",
                        answer_kind="text",
                    )
                )
                continue
            for p in info.paths:
                if (
                    p.root == "steps"
                    and len(p.segments) > 1
                    and wf.step(str(p.segments[1])) is None
                ):
                    findings.append(
                        make_finding(
                            "conflict",
                            f"spec.outputs.{name}",
                            detail=f"Reads from a step called “{p.segments[1]}”, which does not exist.",
                            answer_kind="text",
                        )
                    )

    return findings


def _check_expressions(wf: Workflow, step: Step) -> list[Finding]:
    findings: list[Finding] = []
    idx = wf.step_index(step.id)
    earlier = {s.id for s in wf.spec.steps[:idx]}
    places = {
        "when": step.when,
        "for_each": step.for_each,
        "with": step.with_,
        "input": step.input,
    }
    for place, value in places.items():
        if value is None:
            continue
        for src in expressions_in(value):
            try:
                info = walk(parse(src))
            except ExprError as e:
                findings.append(
                    make_finding(
                        "conflict",
                        f"steps.{step.id}.{place}",
                        step=step,
                        detail=f"This cannot be read as written: {e}",
                        answer_kind="text",
                    )
                )
                continue
            for p in info.paths:
                root = p.root
                if root == "steps":
                    if len(p.segments) < 2:
                        continue
                    ref = str(p.segments[1])
                    if ref == step.id:
                        findings.append(
                            make_finding(
                                "conflict",
                                f"steps.{step.id}.{place}",
                                step=step,
                                detail=f"“{step.title or step.id}” reads its own output before it exists.",
                                answer_kind="text",
                            )
                        )
                    elif ref not in earlier:
                        later = wf.step(ref)
                        if later is None:
                            detail = f"Reads from a step called “{ref}”, which does not exist."
                        else:
                            detail = (
                                f"“{step.title or step.id}” needs “{later.title or later.id}”, "
                                f"but that step runs later, so the value is not there yet."
                            )
                        findings.append(
                            make_finding(
                                "conflict",
                                f"steps.{step.id}.{place}",
                                step=step,
                                detail=detail,
                                answer_kind="choice",
                                options=[
                                    Option(
                                        value={"op": "move_before", "step": ref},
                                        label=f"Run “{later.title or ref}” before this step"
                                        if later
                                        else "Remove the reference",
                                    ),
                                    Option(
                                        value={"op": "remove_ref", "ref": ref},
                                        label="Do not use it here",
                                    ),
                                ],
                            )
                        )
                elif root == "inputs":
                    if len(p.segments) > 1 and str(p.segments[1]) not in wf.spec.inputs:
                        findings.append(
                            make_finding(
                                "conflict",
                                f"steps.{step.id}.{place}",
                                step=step,
                                detail=f"Reads an input called “{p.segments[1]}” that the workflow does not take.",
                                answer_kind="text",
                            )
                        )
                elif root == "item":
                    if step.for_each is None:
                        findings.append(
                            make_finding(
                                "conflict",
                                f"steps.{step.id}.{place}",
                                step=step,
                                detail="Uses “item” but the step does not run over a list.",
                                answer_kind="text",
                            )
                        )
                else:
                    findings.append(
                        make_finding(
                            "conflict",
                            f"steps.{step.id}.{place}",
                            step=step,
                            detail=f"“{root}” is not something a step can read. Only inputs, steps and item.",
                            answer_kind="text",
                        )
                    )
    return findings


def _check_references(wf: Workflow, step: Step, ws: Workspace) -> list[Finding]:
    findings: list[Finding] = []
    if step.skill:
        pin = parse_pin(step.skill)
        skill = ws.load_skill(step.skill)
        if skill is None:
            findings.append(
                make_finding(
                    "gap",
                    f"steps.{step.id}.skill",
                    step=step,
                    detail=f"The instruction file {step.skill} is not in the workspace.",
                )
            )
        elif pin is None:
            findings.append(
                make_finding(
                    "gap",
                    f"steps.{step.id}.skill",
                    step=step,
                    detail=f"{step.skill} is not pinned to a version. Write it as {step.skill}@{skill.version or 1}.",
                    answer_kind="text",
                )
            )
        elif skill.version != pin.version:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.skill",
                    step=step,
                    detail=f"The step pins version {pin.version} of {pin.path}, but the file is now version {skill.version}. The instructions changed since.",
                    answer_kind="choice",
                    options=[
                        Option(
                            value=f"{pin.path}@{skill.version}",
                            label=f"Use version {skill.version}",
                            consequence="This step's trust starts again.",
                        ),
                        Option(
                            value=step.skill,
                            label=f"Keep version {pin.version}",
                            consequence="Restore the older file first.",
                        ),
                    ],
                )
            )
    if step.output and step.output.schema_ and ws.load_schema(step.output.schema_) is None:
        findings.append(
            make_finding(
                "gap",
                f"steps.{step.id}.output.schema",
                step=step,
                detail=f"The output shape {step.output.schema_} is not in the workspace.",
            )
        )
    if step.run and step.run not in KNOWN_RUNNERS:
        findings.append(
            make_finding(
                "conflict",
                f"steps.{step.id}.run",
                step=step,
                detail=f"“{step.run}” is not a routine the system knows. Definitions cannot add code.",
                answer_kind="choice",
                options=[Option(value=r, label=r) for r in sorted(KNOWN_RUNNERS)],
            )
        )
    if step.kind == "subworkflow" and step.workflow:
        if ws.resolve_workflow_ref(step.workflow, current=wf) is None:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.workflow",
                    step=step,
                    detail=f"No workflow matches {step.workflow}.",
                    answer_kind="text",
                )
            )
    return findings
