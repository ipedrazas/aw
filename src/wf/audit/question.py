"""Findings as questions, and answers written back into the definition.

Every write-back returns a ``Change`` (path, before, after, reason) so the UI can show
it beside the draft and undo it.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

from wf.schema import Workspace
from wf.validate import Finding


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


def _get(defn: dict[str, Any], path: str) -> Any:
    cur: Any = defn
    for part in _parts(defn, path):
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
        idx = next(i for i, s in enumerate(_steps(defn)) if s["id"] == bits[1])
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
                import re

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
        val = answer if isinstance(answer, dict) else {}
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
        val = answer if isinstance(answer, dict) else {"max_usd": float(answer)}
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
    elif key in ("deadline", "on_timeout", "model", "skill", "run", "title", "workflow"):
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


def _as_list(answer: Any) -> list[Any]:
    if isinstance(answer, list):
        return answer
    if isinstance(answer, str):
        return [ln.strip(" -•") for ln in answer.replace(";", "\n").splitlines() if ln.strip(" -•")]
    return [answer]


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
    import re

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
