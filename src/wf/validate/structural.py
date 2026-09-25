"""Structural validation: required fields, references, bounds, expressions parse.

Nothing here needs a model. Every failure becomes a finding with a question.
"""

from __future__ import annotations

import re

from wf.expr import ExprError, expressions_in, parse, walk
from wf.schema import Step, Workflow, Workspace, parse_pin
from wf.settings import quick_model

from .findings import RUNNERS, Finding, Option, make_finding, model_options, runner_options
from .templates import (
    PRODUCES,
    STEP_TEMPLATES,
    WEB_TOOLS,
    everything_before,
    follows_the_answer,
    holds_urls,
    says_it_searches,
)

COMMON_REQUIRED = ("title", "shows_user", "trust")

# registered routines a check or tool may name. Definitions cannot add code.
KNOWN_RUNNERS = set(RUNNERS)

# What a model name looks like, to every provider here: the vendor's own name, or a
# gateway's vendor and model (``vendor/model``, ``~vendor/model-latest``). Not a list of models, which changes weekly; only the shape,
# so a stray character is caught before a run spends anything on the steps before it.
MODEL_NAME = re.compile(r"~?[A-Za-z0-9][A-Za-z0-9._:-]*(/[A-Za-z0-9][A-Za-z0-9._:-]*)?")


def _bad_model_name(model: str | None) -> bool:
    return model is not None and MODEL_NAME.fullmatch(model) is None


def _model_finding(field: str, model: str, step: Step | None = None) -> Finding:
    who = f"“{step.title or step.id}”" if step else "steps that name no model of their own"
    return make_finding(
        "conflict",
        field,
        step=step,
        detail=f"“{model}” is not a model name. A name is the vendor's own, like "
        f"{quick_model()}, or a gateway's vendor and model, like vendor/model: no spaces, "
        "and it starts with a letter or a digit.",
        answer_kind="choice",
        options=model_options(),
    ).model_copy(update={"question": f"Which model should {who} run on?"})


def _has(step: Step, wf: Workflow, field: str) -> bool:
    if field == "trust":
        return wf.trust_for(step) is not None
    if field == "model":
        return wf.model_for(step) is not None
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


def input_options(wf: Workflow, idx: int) -> list[Option]:
    """What a step at ``idx`` could start from: everything before it, the step just
    before it, or the inputs alone."""
    earlier = [(s.id, s.kind) for s in wf.spec.steps[:idx]]
    names = list(wf.spec.inputs)
    producing = [(sid, k) for sid, k in earlier if k in PRODUCES]
    opts = [
        Option(
            value=everything_before(names, earlier),
            label="Everything before it",
            consequence="The "
            + (" and ".join(names) or "inputs")
            + " you type, and what every earlier step produced.",
        )
    ]
    if producing:
        prev = wf.step(producing[-1][0])
        opts.append(
            Option(
                value={
                    **{n: f"${{inputs.{n}}}" for n in names},
                    prev.id: f"${{steps.{prev.id}.output}}",
                },
                label=f"The {' and '.join(names) or 'inputs'}, and “{prev.title or prev.id}”",
                consequence="Only the step just before it, which keeps what it reads small.",
            )
        )
    return opts


def validate_structural(wf: Workflow, ws: Workspace | None = None) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()

    if _bad_model_name(wf.spec.defaults.model):
        findings.append(_model_finding("defaults.model", str(wf.spec.defaults.model)))

    for step in wf.spec.steps:
        unblocks = _unblocks(wf, step)
        if _bad_model_name(step.model):
            findings.append(_model_finding(f"steps.{step.id}.model", str(step.model), step))
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

        for field in COMMON_REQUIRED + STEP_TEMPLATES[step.kind].required:
            if not _has(step, wf, field):
                # ``run`` is a closed set, so the question offers the routines of this
                # kind rather than an empty box nobody can fill.
                opts = runner_options(step.kind) if field == "run" else None
                findings.append(
                    make_finding(
                        "gap",
                        f"steps.{step.id}.{field}",
                        step=step,
                        unblocks=unblocks,
                        options=opts,
                    )
                )

        # a step after the first that reads nothing starts from nothing: it gets an
        # empty input and makes up a subject, which looks like work and is not
        idx = wf.step_index(step.id)
        if idx > 0 and step.kind in PRODUCES and not step.input:
            findings.append(
                make_finding(
                    "gap",
                    f"steps.{step.id}.input",
                    step=step,
                    unblocks=unblocks,
                    options=input_options(wf, idx),
                )
            )
        # a step that searches has to hand on what it found, addresses and all
        searches = any(t.split(".")[-1] == "search" for t in (step.tools or {}))
        rel = step.output.schema_ if step.output else None
        shape = ws.load_schema(rel) if (ws is not None and rel) else None
        if step.kind == "agent" and searches and shape is not None and not holds_urls(shape):
            findings.append(
                make_finding(
                    "gap",
                    f"steps.{step.id}.hands_on",
                    step=step,
                    unblocks=unblocks,
                    options=[
                        Option(
                            value="sources",
                            label="Every source it used: its address, title, date and what it says",
                            consequence="The report can cite them, and the link check can open them.",
                        )
                    ],
                )
            )
        # a follow-up after a person's review has to follow what they said
        wait = next((s for s in reversed(wf.spec.steps[:idx]) if s.kind == "wait"), None)
        if (
            step.kind == "subworkflow"
            and wait is not None
            and step.when is None
            and step.for_each is None
        ):
            name = next(iter(wf.spec.inputs), "topic")
            findings.append(
                make_finding(
                    "gap",
                    f"steps.{step.id}.follows",
                    step=step,
                    options=[
                        Option(
                            value=follows_the_answer(wait.id, name),
                            label=f"When “{wait.title or wait.id}” asks for it, once per topic named",
                            consequence="The person answering the review says whether to go deeper and into what. Nothing starts otherwise.",
                        )
                    ],
                )
            )
        # a step that says it searches, with nothing to search with, answers from memory
        if (
            step.kind == "agent"
            and not step.tools
            and says_it_searches(step.title, step.description)
        ):
            findings.append(
                make_finding(
                    "gap",
                    f"steps.{step.id}.tools",
                    step=step,
                    options=[
                        Option(
                            value=WEB_TOOLS,
                            label="Yes: search the web and read the pages it finds",
                            consequence="Up to 15 searches and 25 pages a run. Each search and page shows on the run.",
                        )
                    ],
                )
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
                options=runner_options(step.kind),
            )
        )
    if step.kind == "subworkflow" and step.workflow:
        child = ws.resolve_workflow_ref(step.workflow, current=wf)
        if child is None:
            findings.append(
                make_finding(
                    "conflict",
                    f"steps.{step.id}.workflow",
                    step=step,
                    detail=f"No workflow matches {step.workflow}.",
                    answer_kind="text",
                )
            )
        else:
            findings.extend(_missing_child_inputs(wf, step, child))
    return findings


def _missing_child_inputs(wf: Workflow, step: Step, child: Workflow) -> list[Finding]:
    """What the started workflow cannot run without, and this step does not give it.

    The run would stop at once in the other workflow, asking for it; here it is a
    question, with this workflow's own inputs as the likely answers."""
    given = step.with_ or {}
    out = []
    for name, spec in child.spec.inputs.items():
        if not spec.required or spec.internal or spec.default is not None or name in given:
            continue
        options = [
            Option(value=f"${{inputs.{mine}}}", label=f"This workflow's “{mine}”")
            for mine, s in wf.spec.inputs.items()
            if not s.internal
        ]
        f = make_finding(
            "gap",
            f"steps.{step.id}.with.{name}",
            step=step,
            detail=(
                f"“{child.metadata.name}” needs its “{name}” to start"
                + (f" ({spec.description})" if spec.description else "")
                + ". Pick what this workflow passes it, or say in the chat."
            ),
            options=options or None,
            answer_kind="choice" if options else "text",
        )
        f.question = (
            f"What should “{step.title or step.id}” give “{child.metadata.name}” as its “{name}”?"
        )
        out.append(f)
    return out
