"""Semantic validation: what steps read must exist, and every outcome must be handled.

The two rules at the end catch unstated branches, the most common real gap.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from wf.expr import ExprError, Path, expressions_in, parse, walk
from wf.schema import Step, Workflow, Workspace

from .findings import Finding, Option, make_finding
from .schemas import SchemaResolver


def validate_semantic(wf: Workflow, ws: Workspace | None) -> list[Finding]:
    findings: list[Finding] = []
    resolver = SchemaResolver(wf, ws)

    # branching fields: path -> {step_id: [(op, value)]}
    branch_tests: dict[str, list[tuple[str, str, Any]]] = defaultdict(list)
    branch_paths: dict[str, Path] = {}

    for step in wf.spec.steps:
        for place, value in (
            ("when", step.when),
            ("for_each", step.for_each),
            ("with", step.with_),
            ("input", step.input),
        ):
            if value is None:
                continue
            for src in expressions_in(value):
                try:
                    info = walk(parse(src))
                except ExprError:
                    continue  # structural pass already reported it
                for p in info.paths:
                    r = resolver.resolve(p, step_for_each=step.for_each)
                    if r.unknown:
                        continue
                    if not r.ok:
                        findings.append(
                            make_finding(
                                "conflict",
                                f"steps.{step.id}.{place}",
                                step=step,
                                detail=f"“{step.title or step.id}” reads {p}, but {r.error}.",
                                answer_kind="text",
                            )
                        )
                if place == "for_each":
                    for p in info.paths:
                        r = resolver.resolve(p, step_for_each=None)
                        if (
                            r.ok
                            and r.schema is not None
                            and not r.unknown
                            and r.schema.get("type") not in ("array", None)
                        ):
                            findings.append(
                                make_finding(
                                    "conflict",
                                    f"steps.{step.id}.for_each",
                                    step=step,
                                    detail=f"“{p}” is not a list, so the step cannot run once per item.",
                                    answer_kind="text",
                                )
                            )
                if place == "when":
                    for p, op, lit in info.comparisons:
                        if op in ("==", "!="):
                            branch_tests[str(p)].append((step.id, op, lit))
                            branch_paths[str(p)] = p

    # every value tested for must be possible; every possible value must be handled
    for key, tests in branch_tests.items():
        p = branch_paths[key]
        r = resolver.resolve(p)
        if not r.ok or r.schema is None or r.unknown:
            continue
        enum = r.schema.get("enum")
        producer = (
            wf.step(str(p.segments[1])) if p.root == "steps" and len(p.segments) > 1 else None
        )
        if enum is None:
            if r.schema.get("type") == "string" and producer is not None:
                findings.append(
                    make_finding(
                        "gap",
                        f"steps.{producer.id}.output.enum.{'.'.join(str(s) for s in p.segments[3:])}",
                        step=producer,
                        detail=f"Later steps branch on {p}, but “{producer.title or producer.id}” does not list which values it can produce.",
                        answer_kind="list",
                        unblocks=len(tests),
                    )
                )
            continue
        tested = {lit for _, op, lit in tests if op == "=="}
        excluded = {lit for _, op, lit in tests if op == "!="}
        handled = set(tested)
        for ex in excluded:
            handled |= {v for v in enum if v != ex}
        if producer is not None and producer.output:
            field_name = str(p.segments[-1])
            handled |= set(producer.output.continue_on.get(field_name, []))

        for step_id, _op, lit in tests:
            if lit not in enum:
                step = wf.step(step_id)
                findings.append(
                    make_finding(
                        "unreachable",
                        f"steps.{step_id}.when",
                        step=step,
                        detail=f"“{step.title if step else step_id}” runs when {p} is “{lit}”, but the possible values are {_join(enum)}. Nothing can produce it.",
                        answer_kind="choice",
                        options=[Option(value=v, label=f"It meant “{v}”") for v in enum]
                        + [
                            Option(
                                value={"add_enum": lit}, label=f"Add “{lit}” as a possible outcome"
                            )
                        ],
                    )
                )
        for v in enum:
            if v not in handled:
                findings.append(
                    make_finding(
                        "gap",
                        f"steps.{producer.id if producer else p.segments[1]}.output.continue_on.{p.segments[-1]}.{v}",
                        step=producer,
                        detail=f"“{producer.title if producer else p.segments[1]}” can decide “{v}”, but no step says what happens then.",
                        answer_kind="choice",
                        options=_unhandled_options(wf, producer, v),
                        unblocks=_steps_after(wf, producer),
                        value=v,
                        field_name=_plain(p),
                    )
                )

    findings.extend(_unread_inputs(wf))
    return findings


def _unread_inputs(wf: Workflow) -> list[Finding]:
    """What someone types to start a run has to reach a step that does something with it.
    A wait passes nothing on, so an input only a wait reads reaches nothing."""
    out: list[Finding] = []

    def reads(step: Step, name: str) -> bool:
        return any(
            f"inputs.{name}" in str(v) for v in (step.input, step.with_, step.for_each, step.when)
        )

    doers = [s for s in wf.spec.steps if s.kind != "wait"]
    for name in wf.spec.inputs or {}:
        if not doers or any(reads(s, name) for s in doers):
            continue
        first = wf.spec.steps[0]
        options: list[Option] = []
        if first.kind == "wait" and reads(first, name):
            options.append(
                Option(
                    value={"remove": first.id, "step": doers[0].id},
                    label=f"“{first.title or first.id}” is how it starts: remove it, and “{doers[0].title or doers[0].id}” starts from the {name}",
                )
            )
        options += [
            Option(value={"step": s.id}, label=f"“{s.title or s.id}” starts from it")
            for s in doers[:3]
        ]
        waits = [s for s in wf.spec.steps if s.kind == "wait" and reads(s, name)]
        detail = (
            f"Only “{waits[0].title or waits[0].id}” reads it, and a wait passes nothing on, so every step after it works without the {name}."
            if waits
            else f"No step reads it, so every step works without the {name}."
        )
        out.append(
            make_finding(
                "conflict",
                f"spec.inputs.{name}.read_by",
                detail=detail,
                options=options,
                input=name,
            )
        )
    return out


def _unhandled_options(wf: Workflow, producer: Step | None, value: Any) -> list[Option]:
    opts = [
        Option(
            value={"op": "continue"},
            label="Nothing more runs. Carry on to the next step.",
            consequence="The run continues as if the step had nothing to add.",
        )
    ]
    opts.append(
        Option(
            value={"op": "stop"},
            label="Stop the run and show me.",
            consequence="Adds a step that pauses here for you.",
        )
    )
    if producer is not None:
        idx = wf.step_index(producer.id)
        for later in wf.spec.steps[idx + 1 :]:
            if later.when is None:
                opts.append(
                    Option(
                        value={"op": "branch_to", "step": later.id},
                        label=f"Run “{later.title or later.id}” only in this case",
                        consequence=f"Adds a condition to “{later.title or later.id}”.",
                    )
                )
                break
    return opts


def _steps_after(wf: Workflow, step: Step | None) -> int:
    if step is None:
        return 0
    return len(wf.spec.steps) - wf.step_index(step.id) - 1


def _plain(p: Path) -> str:
    """steps.review.output.verdict -> “the review's verdict”, for questions."""
    if p.root == "steps" and len(p.segments) >= 4:
        return f"the {p.segments[-1]} from “{p.segments[1]}”"
    return str(p)


def _join(values: list[Any]) -> str:
    return ", ".join(f"“{v}”" for v in values)
