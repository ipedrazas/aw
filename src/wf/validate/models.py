"""Which models a step can run on, and why one cannot.

The list itself is ``wf.settings.step_models``. What this adds is the workflow: the
models it already names stay choosable, so the current one is always on the list,
and a decisions model is checked against the step it would run, so that the page,
the chat and the validator refuse the same thing for the same reason, before a run
spends anything finding out.
"""

from __future__ import annotations

from wf.decisions import NotAQuestion, is_decisions_model, questions_from_schema
from wf.schema import Step, Workflow, Workspace
from wf.settings import step_models


def model_choices(wf: Workflow | None = None) -> list[dict[str, str]]:
    """The models a step or the workflow's default can be set to: the configured ones,
    and any other this workflow already names."""
    out = step_models()
    seen = {m["value"] for m in out}
    named = [wf.spec.defaults.model, *(s.model for s in wf.spec.steps)] if wf else []
    for m in named:
        if m and m not in seen:
            kind = "decisions" if is_decisions_model(m) else "text"
            out.append({"value": m, "label": m, "good_for": "", "kind": kind})
            seen.add(m)
    return out


def model_label(model: str | None, wf: Workflow | None = None) -> str | None:
    if model is None:
        return None
    return next((c["label"] for c in model_choices(wf) if c["value"] == model), model)


def resolve_model(value: str, wf: Workflow | None = None) -> str | None:
    """The model a person or the chat meant: its name, or its label ("thorough").
    None when it is not one a step can be set to."""
    choices = model_choices(wf)
    for c in choices:
        if value == c["value"]:
            return c["value"]
    for c in choices:
        if value.strip().lower() == c["label"].lower():
            return c["value"]
    return None


def labels_of_choices(wf: Workflow | None = None) -> str:
    labels = [c["label"] for c in model_choices(wf)]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]


def model_problem(wf: Workflow, step: Step, model: str | None, ws: Workspace | None) -> str | None:
    """Why ``step`` cannot run on ``model``, in words for the person; None when it can.

    Only a decisions model has limits a step can break: it cannot use tools, and it
    answers only the choices and yes/no questions in the step's output. A step whose
    output is not known yet is not refused; the validator asks for it first.
    """
    if step.kind != "agent" or not is_decisions_model(model):
        return None
    who = f"“{step.title or step.id}”"
    name = model_label(model, wf)
    if step.tools:
        return (
            f"{name} cannot search or read pages, and {who} does, so it needs a model "
            "that writes text."
        )
    rel = step.output.schema_ if step.output else None
    shape = ws.load_schema(rel) if (ws is not None and rel) else None
    if shape is None:
        return None
    try:
        questions_from_schema(shape)
    except NotAQuestion as e:
        if e.field is None:
            return f"{name} only answers pick-one and yes/no questions, and {who} asks none."
        return (
            f"{name} only answers pick-one and yes/no questions, and what {who} gives back "
            f"is not only those (“{e.field.replace('_', ' ')}” is neither)."
        )
    return None


def default_model_problem(wf: Workflow, model: str | None, ws: Workspace | None) -> str | None:
    """Why ``model`` cannot be the default: the first step that names no model of its
    own and could not run on it."""
    for step in wf.spec.steps:
        if step.model is None:
            why = model_problem(wf, step, model, ws)
            if why:
                return why
    return None


def models_steps_cannot_use(wf: Workflow, ws: Workspace | None) -> dict[str, dict[str, str]]:
    """For each step, the models it cannot run on and why, by label. A step that can
    run on every model is left out; so is one that uses no model."""
    out: dict[str, dict[str, str]] = {}
    for step in wf.spec.steps:
        cannot = {
            c["label"]: why
            for c in model_choices(wf)
            if (why := model_problem(wf, step, c["value"], ws))
        }
        if cannot:
            out[step.id] = cannot
    return out
