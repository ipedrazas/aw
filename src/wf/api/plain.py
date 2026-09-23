"""The definition in the user's language. No model names, file names or tool names
unless technical details are asked for."""

from __future__ import annotations

from typing import Any

from wf.schema import Step, Workflow

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


def trust_label(wf: Workflow, step: Step) -> tuple[str, str]:
    t = wf.trust_for(step)
    if step.kind == "check" or (t and t.policy == "auto"):
        return ("Automatic", "You see the result afterwards.")
    if t is None:
        return ("Asks you", "Nothing runs unattended by default.")
    if t.policy == "always_ask":
        return ("Always asks you", "Never promoted to running on its own.")
    if t.policy == "earned":
        n = t.promote_after or 3
        return ("Asks you", f"Runs on its own after you accept it {n} times in a row.")
    return ("Asks you", "")


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


def plain_steps(wf: Workflow) -> list[dict[str, Any]]:
    out = []
    for i, s in enumerate(wf.spec.steps, 1):
        label, trust_detail = trust_label(wf, s)
        tech: dict[str, Any] = {
            "id": s.id,
            "kind": s.kind,
            "model": s.model,
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
    models = {s.model for s in steps if s.model}
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
        "skill_count": len(skills),
        "budget": (
            f"${b.max_usd:g} and {b.max_minutes:g} minutes per run"
            + (", shared with any follow-up research" if b.shared_with_children else "")
            if b
            else None
        ),
        "budget_on_exceeded": (
            "If either would be exceeded, the run pauses and asks."
            if b and b.on_exceeded == "pause_and_ask"
            else ("If either would be exceeded, the run stops." if b else None)
        ),
        "recursion": (
            f"Yes, through “{sub.title}” only. Up to {sub.limits.max_depth if sub.limits else '?'} levels deep and {sub.effective_max_fanout or '?'} follow-ups at a time, and never without your approval."
            if sub
            else "No. It never starts more work than the steps listed."
        ),
        "inputs": {
            k: v.model_dump(exclude_none=True) for k, v in wf.spec.inputs.items() if not v.internal
        },
    }
