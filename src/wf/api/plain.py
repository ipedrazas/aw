"""The definition in the user's language. No model names, file names or tool names
unless technical details are asked for."""

from __future__ import annotations

from typing import Any

from wf.schema import Step, Workflow
from wf.validate.models import model_label

KIND_LABEL = {
    "agent": "AI agent",
    "check": "Script, no AI",
    "tool": "Tool",
    "subworkflow": "Runs this workflow again",
    "wait": "Waits for a person",
}

ORIGIN_LABEL = {
    "suggested": "I suggested this",
    "split": "I split your step",
    "answered": "From your answer",
}


# How often a step checks with you, in the words of every page that shows or sets it.
TRUST_CHOICES = [
    {
        "policy": "earned",
        "label": "Check until it has earned my trust",
        "consequence": "A real run stops after it for your OK. Once you have said OK enough "
        "times in a row, it carries on by itself; any change to it starts the count again.",
    },
    {
        "policy": "always_ask",
        "label": "Check every time",
        "consequence": "A real run stops after it, every time, for your OK. You stay in the "
        "loop, and runs take longer.",
    },
    {
        "policy": "auto",
        "label": "Don't check with me",
        "consequence": "Runs go straight on. Everything it decided is still on the run, to "
        "look at afterwards, but nothing waits for you.",
    },
]


def trust_label(wf: Workflow, step: Step) -> tuple[str, str]:
    t = wf.trust_for(step)
    if step.kind == "check":
        return ("Doesn't stop", "A check: you see its result on the run.")
    if step.kind == "wait":
        return ("Doesn't stop", "It already waits for a person.")
    if t is None:
        return ("Not chosen yet", "Answer the question about it before a real run.")
    if t.policy == "auto":
        return ("Doesn't check with you", "Runs straight on. What it decided is on the run.")
    when = (
        "asks you before it starts more research"
        if step.kind == "subworkflow"
        else "stops after it for your OK"
    )
    if t.policy == "always_ask":
        return ("Checks every time", f"A real run {when}.")
    n = t.promote_after or 3
    return ("Checks until trusted", f"A real run {when}, until {n} OKs in a row.")


def shows_label(step: Step) -> str:
    shows = step.shows_user or []
    parts = []
    for s in shows:
        if s == "output":
            parts.append("its result")
        elif s == "decisions":
            parts.append("what it decided and why")
        elif s.startswith("output."):
            parts.append(s.split(".", 1)[1].replace("_", " "))
        else:
            parts.append(s.replace("_", " "))
    return ", ".join(parts).capitalize() if parts else "Nothing until it finishes"


def when_label(wf: Workflow, step: Step) -> str | None:
    if not step.when:
        return None
    import re

    m = re.search(r'steps\.([a-z0-9_]+)\.output\.([a-z0-9_.]+)\s*(==|!=)\s*"([^"]*)"', step.when)
    if not m:
        return "Only in some cases"
    src = wf.step(m.group(1))
    name = src.title if src else m.group(1)
    verb = "says" if m.group(3) == "==" else "does not say"
    return f"Only if “{name}” {verb} “{m.group(4).replace('_', ' ')}”"


def resets_on(wf: Workflow, step: Step, what: str) -> bool:
    """Whether changing this step's ``what`` (its model, its skill) starts its count of
    accepted runs again."""
    t = wf.trust_for(step)
    return bool(t and t.policy == "earned" and what in (t.reset_on or []))


def plain_steps(wf: Workflow) -> list[dict[str, Any]]:
    out = []
    for i, s in enumerate(wf.spec.steps, 1):
        label, trust_detail = trust_label(wf, s)
        tech: dict[str, Any] = {
            "id": s.id,
            "kind": s.kind,
            "model": wf.model_for(s) if s.kind == "agent" else s.model,
            # the step names no model of its own and runs on the workflow's default
            "model_inherited": s.kind == "agent" and not s.model and wf.model_for(s) is not None,
            "model_own": s.model,
            "model_label": model_label(wf.model_for(s) if s.kind == "agent" else s.model, wf),
            "model_resets_trust": resets_on(wf, s, "model"),
            "skill_resets_trust": resets_on(wf, s, "skill"),
            "skill": s.skill,
            "run": s.run,
            "workflow": s.workflow,
            "when": s.when,
            "for_each": s.for_each,
            "output_schema": s.output.schema_ if s.output else None,
            "tools": {k: v.max_calls for k, v in (s.tools or {}).items()} or None,
            "limits": s.limits.model_dump(exclude_none=True) if s.limits else None,
            "trust": (wf.trust_for(s).model_dump(exclude_none=True) if wf.trust_for(s) else None),
        }
        out.append(
            {
                "n": i,
                "id": s.id,
                "title": s.title or s.id,
                "description": s.description or "",
                "kind": s.kind,
                "kind_label": KIND_LABEL.get(s.kind, s.kind),
                "origin": ORIGIN_LABEL.get(s.origin.kind)
                if s.origin and s.origin.by == "system"
                else None,
                "origin_reason": s.origin.reason if s.origin else None,
                "when": when_label(wf, s),
                "shows": shows_label(s),
                "trust": label,
                "trust_detail": trust_detail,
                "checks": s.checks or [],
                "does_not_check": s.does_not_check or [],
                "leaves_system": s.side_effects not in (None, "none", []),
                "requires_approval": s.requires_approval,
                "technical": tech,
            }
        )
    return out


def plain_summary(wf: Workflow) -> dict[str, Any]:
    steps = wf.spec.steps
    system_steps = [s for s in steps if s.origin and s.origin.by == "system"]
    models = {m for s in steps if s.kind == "agent" and (m := wf.model_for(s))}
    skills = {s.skill for s in steps if s.skill}
    b = wf.spec.budget
    sub = next((s for s in steps if s.kind == "subworkflow"), None)
    return {
        "name": wf.metadata.name,
        "version": wf.metadata.version,
        "title": wf.metadata.description or wf.metadata.name,
        "description": wf.metadata.description or "",
        "step_count": len(steps),
        "system_step_count": len(system_steps),
        "model_count": len(models),
        "default_model": wf.spec.defaults.model,
        "default_model_label": model_label(wf.spec.defaults.model, wf),
        "skill_count": len(skills),
        "budget": (
            " and ".join(
                part
                for part in (
                    f"${b.max_usd:g}" if b.max_usd is not None else "",
                    f"{b.max_minutes:g} minutes" if b.max_minutes is not None else "",
                )
                if part
            )
            + " per run"
            + (", shared with any follow-up research" if b.shared_with_children else "")
            if b and (b.max_usd is not None or b.max_minutes is not None)
            else None
        ),
        "budget_on_exceeded": "If either would be exceeded, the run stops." if b else None,
        "recursion": (
            f"Yes, through “{sub.title}” only. Up to {sub.limits.max_depth if sub.limits else '?'} levels deep and {sub.effective_max_fanout or '?'} follow-ups at most, "
            + (
                "and it asks you before it starts."
                if (t := wf.trust_for(sub)) and t.policy != "auto"
                else "and it starts them by itself when the review asks for them."
            )
            if sub
            else "No. It never starts more work than the steps listed."
        ),
        "inputs": {
            k: v.model_dump(exclude_none=True) for k, v in wf.spec.inputs.items() if not v.internal
        },
    }


def run_fields(wf: Workflow) -> list[dict[str, Any]]:
    """What a person fills in to start a run: the workflow's inputs, less the ones the
    runtime sets itself, each with what the form needs to ask for it."""
    return [
        {
            "name": name,
            "label": name.replace("_", " ").capitalize(),
            "type": spec.type,
            "required": spec.required,
            "default": spec.default,
            "min_length": spec.min_length,
            "enum": spec.enum,
            "description": spec.description,
        }
        for name, spec in wf.spec.inputs.items()
        if not spec.internal
    ]


def starts_more_work(wf: Workflow) -> bool:
    """Whether a run can start work beyond its own steps: a follow-up run, or searching
    further. The confirm before a real run says so only when it can."""
    return any(s.kind == "subworkflow" or s.search_further for s in wf.spec.steps)


def sends_outside(wf: Workflow) -> bool:
    """Whether a real run sends anything out of the system (an email, a message, a
    ticket), so the page does not promise that nothing is sent."""
    return any(s.side_effects not in (None, "none", []) for s in wf.spec.steps)
