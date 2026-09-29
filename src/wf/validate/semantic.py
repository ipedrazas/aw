"""Semantic validation: what steps read must exist, and every outcome must be handled.

The two rules at the end catch unstated branches, the most common real gap.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from wf.expr import ExprError, Path, expressions_in, parse, walk
from wf.schema import Step, Workflow, Workspace

from .contracts import Takes, contract_for, fits
from .findings import Finding, Option, make_finding, plain_value
from .schemas import SchemaResolver
from .templates import PRODUCES


def validate_semantic(wf: Workflow, ws: Workspace | None) -> list[Finding]:
    findings: list[Finding] = []
    resolver = SchemaResolver(wf, ws)

    # branching fields: path -> [(step_id, op, value, place)]
    branch_tests: dict[str, list[tuple[str, str, Any, str]]] = defaultdict(list)
    branch_paths: dict[str, Path] = {}

    for step in wf.spec.steps:
        for place, value in (
            ("when", step.when),
            ("for_each", step.for_each),
            ("with", step.with_),
            ("input", step.input),
            # evaluated once the step has finished, so it may read its own output
            ("may_repeat.when", step.may_repeat.when if step.may_repeat else None),
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
                if place in ("when", "may_repeat.when"):
                    for p, op, lit in info.comparisons:
                        if op in ("==", "!="):
                            branch_tests[str(p)].append((step.id, op, lit, place))
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
        tested = {lit for _, op, lit, _ in tests if op == "=="}
        excluded = {lit for _, op, lit, _ in tests if op == "!="}
        handled = set(tested)
        for ex in excluded:
            handled |= {v for v in enum if v != ex}
        if producer is not None and producer.output:
            field_name = str(p.segments[-1])
            handled |= set(producer.output.continue_on.get(field_name, []))

        for step_id, _op, lit, place in tests:
            if lit not in enum:
                step = wf.step(step_id)
                findings.append(
                    make_finding(
                        "unreachable",
                        f"steps.{step_id}.{place}",
                        step=step,
                        detail=f"“{step.title if step else step_id}” runs when {p} is “{lit}”, but the possible values are {_join(enum)}. Nothing can produce it.",
                        answer_kind="choice",
                        options=[
                            Option(value=v, label=f"It meant “{plain_value(v)}”") for v in enum
                        ]
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
                        detail="Your document does not say.",
                        answer_kind="choice",
                        options=_unhandled_options(wf, producer, v),
                        unblocks=_steps_after(wf, producer),
                        value=v,
                        field_name=_plain(p),
                    )
                )

    findings.extend(_unread_inputs(wf))
    for step in wf.spec.steps:
        findings.extend(_held_to_contract(wf, step, ws, resolver))
    if ws is not None:
        for step in wf.spec.steps:
            child = (
                ws.resolve_workflow_ref(step.workflow, current=wf)
                if step.kind == "subworkflow" and step.workflow
                else None
            )
            if child is not None:
                findings.extend(_what_it_is_given(step, child, resolver))
    return findings


#: An input's type as a person says it.
TYPE_WORDS = {
    "string": "text",
    "integer": "a whole number",
    "number": "a number",
    "boolean": "a yes or no",
    "list": "a list",
    "object": "a set of fields",
}
#: A JSON schema type, as an input's type.
AS_INPUT = {
    "array": "list",
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
}


def _given_type(value: Any, step: Step, resolver: SchemaResolver) -> str | None:
    """The input type of what a step passes, when it can be told before a run: a plain
    value, or one reference to something whose shape is declared."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if not isinstance(value, str):
        return None
    srcs = expressions_in(value)
    if not srcs:
        return "string"
    if len(srcs) != 1 or value.strip() != f"${{{srcs[0]}}}":
        return "string" if srcs else None  # text with a value put in it is text
    try:
        expr = parse(srcs[0])
    except ExprError:
        return None
    if not isinstance(expr, Path):
        return None  # a sum or a comparison: its type is not declared anywhere
    r = resolver.resolve(expr, step_for_each=step.for_each)
    if not r.ok or r.unknown or r.schema is None:
        return None
    t = r.schema.get("type")
    return AS_INPUT.get(t) if isinstance(t, str) else None


def _what_it_is_given(step: Step, child: Workflow, resolver: SchemaResolver) -> list[Finding]:
    """What a step passes the workflow it starts: only inputs that workflow has, each of
    the kind it takes. A name it does not have is dropped without a word at run time,
    and a value of the wrong kind stops the run it starts; both are said here instead."""
    findings: list[Finding] = []
    name = child.metadata.name
    who = step.title or step.id
    theirs = {k: v for k, v in child.spec.inputs.items() if not v.internal}
    not_given = [k for k in theirs if k not in (step.with_ or {})]
    for key, value in (step.with_ or {}).items():
        spec = child.spec.inputs.get(key)
        if spec is None:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.with.{key}",
                    step=step,
                    detail=f"“{who}” gives “{name}” a “{key}”, but “{name}” takes no input "
                    "by that name, so it would never see it.",
                    answer_kind="choice",
                    options=[
                        *(
                            Option(
                                value={"op": "rename", "to": k},
                                label=f"It is “{name}”'s “{k}”",
                            )
                            for k in not_given
                        ),
                        Option(value={"op": "leave_out"}, label="Leave it out"),
                    ],
                )
            )
            continue
        if spec.internal:
            continue
        given = _given_type(value, step, resolver)
        fits = given is None or given == spec.type or (given == "integer" and spec.type == "number")
        if not fits:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.with.{key}",
                    step=step,
                    detail=f"“{name}” takes {TYPE_WORDS[spec.type]} as its “{key}”, and “{who}” "
                    f"gives it {TYPE_WORDS.get(given or '', 'something else')}, so the run it "
                    "starts would stop at once.",
                    answer_kind="text",
                )
            )
        elif spec.enum and isinstance(value, str) and not expressions_in(value):
            if value not in spec.enum:
                findings.append(
                    make_finding(
                        "conflict",
                        f"steps.{step.id}.with.{key}",
                        step=step,
                        detail=f"“{name}” takes one of {_join(spec.enum)} as its “{key}”, not "
                        f"“{value}”.",
                        answer_kind="choice",
                        options=[Option(value=v, label=plain_value(v)) for v in spec.enum],
                    )
                )
    return findings


def _declared_shape(value: Any, step: Step, resolver: SchemaResolver) -> dict[str, Any] | None:
    """The declared shape of what an input reads, when it is one reference."""
    if not isinstance(value, str):
        return None
    srcs = expressions_in(value)
    if len(srcs) != 1 or value.strip() != f"${{{srcs[0]}}}":
        return None
    try:
        expr = parse(srcs[0])
    except ExprError:
        return None
    if not isinstance(expr, Path):
        return None
    r = resolver.resolve(expr, step_for_each=step.for_each)
    return r.schema if r.ok and not r.unknown else None


def _shaped_like(wf: Workflow, step: Step, takes: Takes, resolver: SchemaResolver) -> list[Option]:
    """The earlier results that have the shape a capability takes, as choices: the one
    right before it first."""
    out: list[Option] = []
    for earlier in reversed(wf.spec.steps[: wf.step_index(step.id)]):
        schema = resolver.step_output_schema(earlier)
        if not schema:
            continue
        name = earlier.title or earlier.id
        if not takes.many and fits(schema, takes):
            out.append(
                Option(value=f"${{steps.{earlier.id}.output}}", label=f"What “{name}” gives back")
            )
        for field, node in (schema.get("properties") or {}).items():
            if fits(node, takes):
                out.append(
                    Option(
                        value=f"${{steps.{earlier.id}.output.{field}}}",
                        label=f"“{name}”'s {field.replace('_', ' ')}",
                    )
                )
    return out


def _held_to_contract(
    wf: Workflow, step: Step, ws: Workspace | None, resolver: SchemaResolver
) -> list[Finding]:
    """A step that names a capability gives it what it takes: under the input it reads,
    in the shape it needs, once or once per item. Otherwise it reads whatever it finds,
    which is how a link check came to open every address in a whole pipeline."""
    got = contract_for(step, ws)
    if got is None:
        return []
    name, takes = got
    who = step.title or step.id
    given = step.input or {}
    out: list[Finding] = []

    def ask(kind: str, field: str, question: str, detail: str, options: list[Option]) -> None:
        f = make_finding(
            kind,
            f"steps.{step.id}.{field}",
            step=step,
            detail=detail,
            answer_kind="choice" if options else "text",
            options=options or None,
        )
        f.question = question
        out.append(f)

    if takes.per:
        if step.for_each is None:
            lists = [
                Option(value=o.value, label=f"Once for each of {o.label}")
                for o in _shaped_like(wf, step, takes, resolver)
            ]
            ask(
                "conflict",
                "for_each",
                f"What should “{who}” run over, one {takes.per} at a time?",
                f"“{name}” takes {takes.what}, one {takes.per} at a time, but “{who}” runs once.",
                lists,
            )
        for f in takes.fields:
            if f not in given:
                ask(
                    "gap",
                    f"input.{f}",
                    f"Where does “{who}” get each {takes.per}'s {f}?",
                    f"“{name}” needs each {takes.per}'s {f}.",
                    [Option(value=f"${{item.{f}}}", label=f"The {takes.per}'s {f}")]
                    if step.for_each
                    else [],
                )
    elif takes.key:
        value = given.get(takes.key)
        choices = _shaped_like(wf, step, takes, resolver)
        them = "them" if takes.many else "it"
        if value is None:
            ask(
                "gap",
                f"input.{takes.key}",
                f"Where does “{who}” get {takes.what}?",
                f"“{name}” needs {takes.what}, and “{who}” is not given {them}, so it would "
                f"look for {them} in everything it is given.",
                choices,
            )
        elif fits(_declared_shape(value, step, resolver), takes) is False:
            ask(
                "conflict",
                f"input.{takes.key}",
                f"Where does “{who}” get {takes.what}?",
                f"“{name}” needs {takes.what}, and what “{who}” is given is not that.",
                choices,
            )
    else:
        for f in takes.fields:
            if f not in given:
                ask(
                    "gap",
                    f"input.{f}",
                    f"Where does “{who}” get its {f}?",
                    f"“{name}” needs {takes.what}.",
                    [],
                )
    return out


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
            label="Carry on with the next step",
            consequence="Nothing extra happens for this outcome.",
        )
    ]
    opts.append(
        Option(
            value={"op": "stop"},
            label="Stop and show me",
            consequence="The run waits here for you.",
        )
    )
    if producer is not None:
        idx = wf.step_index(producer.id)
        for later in wf.spec.steps[idx + 1 :]:
            if later.when is None:
                opts.append(
                    Option(
                        value={"op": "branch_to", "step": later.id},
                        label=f"Only then, do “{later.title or later.id}”",
                        consequence=f"“{later.title or later.id}” is skipped for every other outcome.",
                    )
                )
                break
        for earlier in _repeat_candidates(wf, producer):
            opts.append(
                Option(
                    value={"op": "repeat", "step": earlier.id},
                    label=f"Send it back to “{earlier.title or earlier.id}”",
                    consequence=f"“{earlier.title or earlier.id}” and everything after it runs again, "
                    "up to 3 times, then it carries on instead.",
                )
            )
    return opts


def _repeat_candidates(wf: Workflow, producer: Step) -> list[Step]:
    """Earlier steps ``producer`` reads from directly, in the order it reads them: what
    it judges is what its own work came from, and there is nowhere else a "send it
    back" answer could plausibly point without asking someone to name a step."""
    idx = wf.step_index(producer.id)
    seen: list[str] = []
    for src in expressions_in([producer.input, producer.with_]):
        try:
            info = walk(parse(src))
        except ExprError:
            continue
        for p in info.paths:
            if p.root == "steps" and len(p.segments) > 1:
                sid = str(p.segments[1])
                if sid not in seen:
                    seen.append(sid)
    out = []
    for sid in seen:
        s = wf.step(sid)
        if s is not None and s.kind in PRODUCES and wf.step_index(sid) < idx:
            out.append(s)
    return out


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
