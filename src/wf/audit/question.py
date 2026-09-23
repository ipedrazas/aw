"""Findings as questions, and answers written back into the definition.

Every write-back returns a ``Change`` (path, before, after, reason) so the UI can show
it beside the draft and undo it.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel

from wf.schema import Workspace
from wf.validate import KNOWN_RUNNERS, Finding


class AnswerRejected(ValueError):
    """An answer that cannot go into the draft, with a plain reason for the person."""


GROUP_ORDER = ["conflict", "gap", "unreachable", "assumption"]
GROUP_TITLES = {
    "conflict": "Things that cannot work as written",
    "gap": "Things the document does not say",
    "unreachable": "Branches nothing can reach",
    "assumption": "Things we assumed",
}


class Change(BaseModel):
    path: str
    before: Any = None
    after: Any = None
    reason: str = ""


def group_questions(findings: list[Finding]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for t in GROUP_ORDER:
        items = sorted((f for f in findings if f.type == t), key=lambda f: (-f.unblocks, f.field))
        if items:
            groups.append({"type": t, "title": GROUP_TITLES[t], "findings": items})
    return groups


def _steps(defn: dict[str, Any]) -> list[dict[str, Any]]:
    return defn["spec"]["steps"]


def _step(defn: dict[str, Any], sid: str) -> dict[str, Any]:
    for s in _steps(defn):
        if s["id"] == sid:
            return s
    raise KeyError(sid)


def remove_step(defn: dict[str, Any], sid: str) -> list[dict[str, Any]]:
    """The steps without ``sid``, with whatever read from it reading the workflow's inputs.

    A step is usually removed because it was not a step at all: “get my topic” is how
    the workflow starts, so what read the topic from that step reads it from the inputs.
    """
    reads_it = re.compile(rf"steps\.{re.escape(sid)}\b")
    from_inputs = {name: f"${{inputs.{name}}}" for name in defn["spec"].get("inputs") or {}}
    steps = [copy.deepcopy(s) for s in _steps(defn) if s["id"] != sid]
    for s in steps:
        inp = s.get("input")
        if not isinstance(inp, dict) or not any(reads_it.search(str(v)) for v in inp.values()):
            continue
        kept = {k: v for k, v in inp.items() if not reads_it.search(str(v))}
        s["input"] = {**from_inputs, **kept} if from_inputs else kept
        if not s["input"]:
            s.pop("input")
    return steps


def _get(defn: dict[str, Any], path: str) -> Any:
    cur: Any = defn
    try:
        parts = _parts(defn, path)
    except KeyError:
        return None  # the step is gone, so is everything under it
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and isinstance(part, int) and part < len(cur):
            cur = cur[part]
        else:
            return None
    return cur


def _set(defn: dict[str, Any], path: str, value: Any) -> None:
    parts = _parts(defn, path)
    cur: Any = defn
    for part in parts[:-1]:
        if isinstance(cur, list):
            cur = cur[part]
            continue
        if part not in cur or cur[part] is None:
            cur[part] = {}
        cur = cur[part]
    if value is None:
        cur.pop(parts[-1], None)
    else:
        cur[parts[-1]] = value


def _parts(defn: dict[str, Any], path: str) -> list[Any]:
    """steps.review.when -> ['spec','steps',<index>,'when']; spec.budget -> ['spec','budget']."""
    bits = path.split(".")
    if bits[0] == "steps":
        idx = next((i for i, s in enumerate(_steps(defn)) if s["id"] == bits[1]), None)
        if idx is None:
            raise KeyError(f"There is no step “{bits[1]}”.")
        return ["spec", "steps", idx, *bits[2:]]
    return bits


def apply_answer(
    defn: dict[str, Any], finding: Finding, answer: Any, ws: Workspace | None = None
) -> tuple[dict[str, Any], list[Change]]:
    """Write an answer into a copy of the definition. Returns the new definition and the changes made."""
    answer = coerce_answer(finding, answer)
    d = copy.deepcopy(defn)
    field = finding.field
    key = field.rsplit(".", 1)[-1]
    changes: list[Change] = []
    sid = finding.step_id

    def change(path: str, after: Any, reason: str) -> None:
        before = _get(d, path)
        _set(d, path, after)
        changes.append(Change(path=path, before=before, after=after, reason=reason))

    if finding.type == "assumption":
        if isinstance(answer, dict) and answer.get("keep") is False:
            # reopen as a gap: clear the assumed value so the validator asks
            if key in ("model", "skill") or field.endswith("output.schema"):
                change(
                    field, None, "You said the assumption was wrong, so the field is open again."
                )
        finding.status = "answered"
        finding.answer = answer
        return d, changes

    if ".output.continue_on." in field:
        rest = field.split(".output.continue_on.", 1)[1]
        fname, _, value = rest.rpartition(".")
        op = answer.get("op") if isinstance(answer, dict) else answer
        if op == "continue":
            cur = _get(d, f"steps.{sid}.output.continue_on.{fname}") or []
            change(
                f"steps.{sid}.output.continue_on.{fname}",
                sorted({*cur, value}),
                f"When the {fname} is “{value}”, nothing more runs.",
            )
        elif op == "stop":
            steps = _steps(d)
            idx = next(i for i, s in enumerate(steps) if s["id"] == sid)
            new = {
                "id": f"{sid}_{value}_stop",
                "kind": "wait",
                "title": f"Stop and ask you when {fname} is “{value}”",
                "when": f'${{steps.{sid}.output.{fname} == "{value}"}}',
                "deadline": "1d",
                "on_timeout": "stop",
                "shows_user": ["output"],
                "trust": {"policy": "always_ask"},
                "origin": {
                    "by": "system",
                    "kind": "answered",
                    "reason": f"You said the run should stop when the {fname} is “{value}”.",
                },
            }
            steps.insert(idx + 1, new)
            changes.append(
                Change(
                    path=f"steps.{new['id']}",
                    before=None,
                    after=new,
                    reason=f"Added a step that stops and asks you when the {fname} is “{value}”.",
                )
            )
        elif op == "branch_to" and isinstance(answer, dict):
            target = answer["step"]
            change(
                f"steps.{target}.when",
                f'${{steps.{sid}.output.{fname} == "{value}"}}',
                f"“{_step(d, target).get('title', target)}” now runs only when the {fname} is “{value}”.",
            )
    elif key == "when":
        if finding.type == "unreachable":
            if isinstance(answer, dict) and "add_enum" in answer and ws is not None:
                _add_enum_value(d, ws, _get(d, f"steps.{sid}.when"), answer["add_enum"], changes)
            elif isinstance(answer, str):
                cur = _get(d, field) or ""
                new = re.sub(r'==\s*"[^"]*"', f'== "{answer}"', cur, count=1)
                change(field, new, f"The branch now tests for “{answer}”.")
        elif isinstance(answer, dict) and answer.get("always"):
            change(field, None, f"“{_step(d, sid).get('title', sid)}” runs every time.")
        elif isinstance(answer, dict) and "step" in answer:
            change(
                field,
                f'${{steps.{answer["step"]}.output.{answer["field"]} == "{answer["equals"]}"}}',
                f"Runs when “{answer['step']}” says “{answer['equals']}”.",
            )
        elif isinstance(answer, str) and answer.strip():
            expr = answer.strip()
            if not expr.startswith("${"):
                expr = "${" + expr + "}"
            change(field, expr, "The condition you gave.")
    elif ".output.enum." in field:
        fname = field.split(".output.enum.", 1)[1]
        values = _as_list(answer)
        if ws is not None and values:
            _set_enum(d, ws, sid, fname, values, changes)
    elif key in ("does_not_check", "checks"):
        change(field, _as_list(answer), "Your words, shown next to the result.")
    elif key == "requires_approval":
        if isinstance(answer, str):
            answer = answer.strip()
            if answer.lower() in ("yes", "true"):
                answer = True
            elif answer.lower() in ("no", "false", "nobody"):
                answer = False
        change(
            field,
            answer,
            "Who signs this off."
            if isinstance(answer, str)
            else ("Someone must approve it." if answer else "It goes without approval."),
        )
    elif key == "side_effects":
        change(
            field,
            "none" if answer in ("none", [], None) else _as_list(answer),
            "What this action does outside the system.",
        )
    elif key == "limits":
        val = answer if isinstance(answer, dict) else _parse_limits(answer)
        cur = _get(d, field) or {}
        change(
            field, {**cur, "budget": cur.get("budget", "inherit"), **val}, "How far this can go."
        )
    elif key == "max_fanout":
        s = _step(d, sid)
        if s.get("kind") == "subworkflow":
            cur = _get(d, f"steps.{sid}.limits") or {}
            change(
                f"steps.{sid}.limits",
                {**cur, "max_fanout": int(answer)},
                f"At most {int(answer)} at a time.",
            )
        else:
            change(field, int(answer), f"At most {int(answer)} at a time.")
    elif key == "max_depth":
        cur = _get(d, f"steps.{sid}.limits") or {}
        change(
            f"steps.{sid}.limits",
            {**cur, "max_depth": int(answer)},
            f"At most {int(answer)} levels deep.",
        )
    elif field == "spec.budget":
        val = answer if isinstance(answer, dict) else _parse_budget(answer)
        change(
            field,
            {
                "shared_with_children": True,
                "on_exceeded": "pause_and_ask",
                "max_minutes": 45,
                **val,
            },
            "One spending limit for the whole run.",
        )
    elif key == "trust":
        change(
            field,
            answer if isinstance(answer, dict) else {"policy": str(answer)},
            "When this step waits for you.",
        )
    elif key == "read_by" and isinstance(answer, dict):
        name = field.split(".")[2]
        if answer.get("remove"):
            gone = _step(d, answer["remove"])
            change(
                "spec.steps",
                remove_step(d, answer["remove"]),
                f"“{gone.get('title') or answer['remove']}” is how the run starts, not a step.",
            )
        reader = answer["step"]
        cur = dict(_get(d, f"steps.{reader}.input") or {})
        cur[name] = f"${{inputs.{name}}}"
        change(
            f"steps.{reader}.input",
            cur,
            f"“{_step(d, reader).get('title') or reader}” starts from the {name} you type.",
        )
    elif key == "run":
        # the routines are a closed set: a name the system does not have would only
        # come straight back as a conflict, so it is refused here with the reason.
        if answer not in KNOWN_RUNNERS:
            raise AnswerRejected(why_rejected(finding, answer))
        change(field, answer, "Your answer.")
        use_runner_schema(d, ws, sid, changes)
    elif key in ("model", "deadline"):
        # both are asked as a closed set of options (the models the deployment
        # configured, or one of the standard waits), so prose that matches none of
        # them is refused rather than written in. A value already stated in the
        # document skips this path: it is written straight into the draft, not
        # answered through a question.
        valid = {o.value for o in finding.options}
        if valid and answer not in valid:
            raise AnswerRejected(why_rejected(finding, answer))
        change(field, answer, "Your answer.")
    elif key in ("on_timeout", "skill", "title", "workflow"):
        change(field, answer, "Your answer.")
    elif key == "shows_user":
        change(field, _as_list(answer), "What you see when this step finishes.")
    elif field.endswith("output.schema"):
        change(field, str(answer), "The declared shape of this step's output.")
    elif (
        finding.type == "conflict"
        and isinstance(answer, dict)
        and answer.get("op") == "move_before"
    ):
        steps = _steps(d)
        moving = next(s for s in steps if s["id"] == answer["step"])
        steps.remove(moving)
        idx = next(i for i, s in enumerate(steps) if s["id"] == sid)
        steps.insert(idx, moving)
        changes.append(
            Change(
                path=f"steps.{moving['id']}",
                before=None,
                after=moving,
                reason=f"“{moving.get('title', moving['id'])}” now runs before “{_step(d, sid).get('title', sid)}”.",
            )
        )
    elif (
        finding.type == "conflict" and isinstance(answer, dict) and answer.get("op") == "remove_ref"
    ):
        place = field.rsplit(".", 1)[-1]
        cur = _get(d, field)
        ref = answer["ref"]
        if isinstance(cur, dict):
            new = {k: v for k, v in cur.items() if f"steps.{ref}" not in str(v)}
            change(field, new or None, f"No longer reads from “{ref}”.")
        elif isinstance(cur, str) and place == "when":
            change(field, None, f"No longer reads from “{ref}”.")
    else:
        change(field, answer, "Your answer.")

    finding.status = "answered"
    finding.answer = answer
    return d, changes


def coerce_answer(finding: Finding, answer: Any) -> Any:
    """Match a written answer to one of a closed question's choices, when it is one of them."""
    if finding.answer_kind != "choice" or not finding.options or not isinstance(answer, str):
        return answer
    said = answer.strip().casefold()
    for o in finding.options:
        if said in (str(o.value).casefold(), o.label.casefold()):
            return o.value
    return answer


def why_rejected(finding: Finding, answer: Any) -> str:
    """Why an answer could not go into the draft, in the person's words."""
    from .diff import FIELD_LABELS

    key = finding.field.rsplit(".", 1)[-1]
    label = FIELD_LABELS.get(key, key.replace("_", " "))
    said = str(answer)
    if len(said) > 80:
        said = said[:77] + "…"
    if not finding.options:
        return f"“{said}” does not fit {label}, so the draft is unchanged."
    labels = [o.label for o in finding.options]
    choices = f"{', '.join(labels[:-1])} or {labels[-1]}" if len(labels) > 1 else labels[0]
    return (
        f"“{said}” is not one of the choices for {label}, so the draft is unchanged. "
        f"It can be {choices}."
    )


MONEY = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:dollars?|usd|bucks?)", re.I
)
MINUTES = re.compile(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", re.I)
HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", re.I)


def _parse_limits(answer: Any) -> dict[str, Any]:
    """“2 levels, 3 at a time” -> depth 2, fan-out 3. The numbers are read in that order;
    words without a number are refused rather than recorded as an answer that sets nothing."""
    nums = [int(n) for n in re.findall(r"\d+", str(answer))]
    if not nums:
        raise AnswerRejected(
            "Say how many levels deep it may go and how many at a time, "
            "for example “1 level, 3 at a time”, or pick one of the choices."
        )
    out: dict[str, Any] = {"max_depth": nums[0]}
    if len(nums) > 1:
        out["max_fanout"] = nums[1]
    return out


def _parse_budget(answer: Any) -> dict[str, Any]:
    """A spending limit out of the words someone types: “about $5 and 30 minutes”.

    The money is the point: this question is asked because a run can start more work,
    and a budget with no amount in it would not cap anything. Time is optional and
    keeps its default when it is not said.
    """
    if isinstance(answer, int | float):
        return {"max_usd": float(answer)}
    said = str(answer).strip()
    try:
        return {"max_usd": float(said)}  # a bare number is an amount of money
    except ValueError:
        pass

    m = MONEY.search(said)
    if not m:
        raise AnswerRejected(
            f"“{said}” does not say how much one run may spend, so the draft is unchanged. "
            "Give an amount, and a length of time if you want one, like “$8 and 30 minutes”."
        )
    val: dict[str, Any] = {"max_usd": float((m.group(1) or m.group(2)).replace(",", ""))}

    minutes = 0.0
    if h := HOURS.search(said):
        minutes += float(h.group(1)) * 60
    if mi := MINUTES.search(said):
        minutes += float(mi.group(1))
    if not minutes and re.search(r"\bhalf an hour\b", said, re.I):
        minutes = 30.0
    if not minutes and re.search(r"\b(?:an|one)\s+hour\b", said, re.I):
        minutes = 60.0
    if minutes:
        val["max_minutes"] = minutes
    return val


def _as_list(answer: Any) -> list[Any]:
    if isinstance(answer, list):
        return answer
    if isinstance(answer, str):
        return [ln.strip(" -•") for ln in answer.replace(";", "\n").splitlines() if ln.strip(" -•")]
    return [answer]


def use_runner_schema(
    d: dict[str, Any], ws: Workspace | None, sid: str, changes: list[Change] | None = None
) -> None:
    """A step that runs a routine gives back what the routine gives back, whatever the
    document said it would: write the routine's shape into the step's schema file."""
    from wf.interpret.registry import RUNNER_OUTPUT_SCHEMAS

    step = _step(d, sid)
    shape = RUNNER_OUTPUT_SCHEMAS.get(step.get("run") or "")
    if ws is None or shape is None:
        return
    rel = _get(d, f"steps.{sid}.output.schema") or f"schemas/{d['metadata']['name']}/{sid}.json"
    before = ws.load_schema(rel)
    if before == shape and _get(d, f"steps.{sid}.output.schema") == rel:
        return
    ws.save_schema(rel, copy.deepcopy(shape))
    _set(d, f"steps.{sid}.output.schema", rel)
    if changes is not None:
        changes.append(
            Change(
                path=f"{rel}#",
                before=before,
                after=shape,
                reason=f"“{step.get('title') or sid}” gives back what its routine gives back.",
            )
        )


def _set_enum(
    d: dict[str, Any], ws: Workspace, sid: str, fname: str, values: list[str], changes: list[Change]
) -> None:
    rel = _get(d, f"steps.{sid}.output.schema")
    if not rel:
        return
    schema = ws.load_schema(rel) or {"type": "object", "properties": {}}
    props = schema.setdefault("properties", {})
    before = copy.deepcopy(props.get(fname))
    props[fname] = {**(props.get(fname) or {"type": "string"}), "enum": list(values)}
    ws.save_schema(rel, schema)
    changes.append(
        Change(
            path=f"{rel}#{fname}",
            before=before,
            after=props[fname],
            reason=f"“{fname}” can be: {', '.join(values)}.",
        )
    )


def _add_enum_value(
    d: dict[str, Any], ws: Workspace, when: str | None, value: str, changes: list[Change]
) -> None:
    m = re.search(r"steps\.([a-z0-9_]+)\.output\.([a-z0-9_.]+)", when or "")
    if not m:
        return
    sid, fname = m.group(1), m.group(2).split(".")[0]
    rel = _get(d, f"steps.{sid}.output.schema")
    schema = ws.load_schema(rel) if rel else None
    if not schema:
        return
    node = schema.get("properties", {}).get(fname)
    if node is None:
        return
    enum = list(node.get("enum", []))
    if value not in enum:
        enum.append(value)
    node["enum"] = enum
    ws.save_schema(rel, schema)
    changes.append(
        Change(
            path=f"{rel}#{fname}",
            before=None,
            after=node,
            reason=f"“{value}” is now a possible outcome.",
        )
    )


def answer_definition(
    defn: dict[str, Any], finding: Finding, answer: Any, ws: Workspace
) -> tuple[dict[str, Any], list[Change]]:
    """Write an answer into a copy of the definition, and refuse it if the question is
    still open afterwards: an answer that sets nothing is not an answer, and recording it
    as one hides the gap until a real run trips over it."""
    from pydantic import ValidationError

    from wf.schema import load_workflow_dict
    from wf.validate import validate

    was = (finding.status, finding.answer)
    try:
        new_def, changes = apply_answer(defn, finding, answer, ws)
        wf = load_workflow_dict(new_def)
    except AnswerRejected:
        finding.status, finding.answer = was
        raise
    except (ValidationError, TypeError, ValueError, KeyError, IndexError, StopIteration) as e:
        finding.status, finding.answer = was
        raise AnswerRejected(why_rejected(finding, answer)) from e
    if finding.type != "assumption" and any(
        f.id == finding.id and f.status == "open" for f in validate(wf, ws).findings
    ):
        finding.status, finding.answer = was
        raise AnswerRejected(why_rejected(finding, answer))
    return new_def, changes
