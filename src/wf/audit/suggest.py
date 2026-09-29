"""A capability offered to a step that does its job on instructions of its own.
(plans/capability-contracts.md, piece 4)

Triage, when a draft is made, and the chat, when asked, may propose that a step use one
of the system's routines or its own instructions instead: "check each link matches its
citation" is what claim-support does. A proposal is only a proposal. The same wiring a
new draft gets is tried on a copy of the draft, and only one that wires is offered, as
a question saying what would change. Yes makes that change; no leaves the step, and is
not asked again.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from wf.interpret.registry import RUNNER_OUTPUT_SCHEMAS
from wf.schema import Workspace
from wf.validate import Finding, Option
from wf.validate.contracts import ROUTINE_TAKES
from wf.validate.findings import RUNNERS

from .catalog import known_instructions
from .wiring import Wiring

log = logging.getLogger(__name__)


class Tried:
    """A capability wired onto a copy of the draft: the steps as they would be, what
    was said about it, and the files it would write."""

    def __init__(self, steps: list[dict[str, Any]], notes: list[str]):
        self.steps = steps
        self.notes = notes
        self.schemas: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, str] = {}


def _own_skill(ref: str | None, workflow: str) -> str | None:
    """The instruction file a step wrote for itself, not one of the system's."""
    rel = (ref or "").partition("@")[0]
    return rel if rel.startswith(f"skills/{workflow}/") else None


def title_of(name: str, ws: Workspace) -> str | None:
    """A capability's name as a person reads it, or None when there is no such one."""
    if name in ROUTINE_TAKES:
        return RUNNERS[name][1]
    own = {i["name"]: i for i in known_instructions(ws)}
    used = own.get(name)
    return used["title"] if used and used.get("contract") else None


def attempt(definition: dict[str, Any], ws: Workspace, sid: str, name: str) -> Tried | None:
    """``name`` wired onto step ``sid`` of a copy of the draft, or None when it does not
    wire (or is not a capability that says what it takes)."""
    workflow = definition["metadata"]["name"]
    steps = copy.deepcopy(definition["spec"]["steps"])
    step = next((s for s in steps if s["id"] == sid), None)
    if step is None or step.get("kind") != "agent" or not _own_skill(step.get("skill"), workflow):
        return None
    schemas: dict[str, dict[str, Any]] = {}
    for s in steps:
        rel = (s.get("output") or {}).get("schema")
        if rel and (shape := ws.load_schema(rel)) is not None:
            schemas[rel] = shape
    before = copy.deepcopy(schemas)
    skills: dict[str, str] = {}
    for s in steps:
        rel = _own_skill(s.get("skill"), workflow)
        if rel and (skill := ws.load_skill(s["skill"])) is not None:
            skills[rel] = skill.body
    written = dict(skills)
    own = {i["name"]: i for i in known_instructions(ws)}
    uses: dict[str, str | None] = {}
    if name in ROUTINE_TAKES:
        kind = RUNNERS[name][0]
        for k in ("skill", "model", "tools", "decision_log", "search_further"):
            step.pop(k, None)
        rel = f"schemas/{workflow}/{sid}.json"
        schemas[rel] = copy.deepcopy(RUNNER_OUTPUT_SCHEMAS.get(name) or {"type": "object"})
        step.update(kind=kind, run=name, output={"schema": rel})
        if kind == "check":
            step.setdefault("on_fail", "annotate")
        else:
            step.update(side_effects="none", requires_approval=False)
    elif (own.get(name) or {}).get("contract"):
        uses[sid] = name
    else:
        return None
    notes: list[str] = []
    w = Wiring(steps, schemas, skills, {}, notes, own, uses, workflow)
    w.run(only={sid})
    if sid not in w.wired:
        return None
    tried = Tried(steps, notes)
    tried.schemas = {rel: shape for rel, shape in schemas.items() if before.get(rel) != shape}
    tried.skills = {rel: body for rel, body in skills.items() if written.get(rel) != body}
    return tried


def offers(
    definition: dict[str, Any], ws: Workspace, proposals: list[dict[str, Any]]
) -> list[Finding]:
    """The proposals that wire, as questions. ``proposals`` are ``{step_id, name, why}``."""
    out: list[Finding] = []
    for p in proposals:
        sid, name = str(p.get("step_id") or ""), str(p.get("name") or "")
        title = title_of(name, ws)
        tried = attempt(definition, ws, sid, name) if title else None
        if tried is None:
            log.info("suggest: %s for %s does not wire; not offered", name, sid)
            continue
        step = next(s for s in definition["spec"]["steps"] if s["id"] == sid)
        why = str(p.get("why") or "").strip()
        out.append(
            Finding(
                type="assumption",
                step_id=sid,
                field=f"steps.{sid}.uses",
                question=f"Should “{step.get('title') or sid}” use the system's own “{title}”?",
                detail=" ".join(
                    x for x in [why, "What would change: " + " ".join(tried.notes)] if x
                ),
                answer_kind="choice",
                options=[
                    Option(
                        value={"op": "use_capability", "name": name},
                        label=f"Yes, use “{title}”",
                        consequence="It does the job the way the system already does it.",
                    ),
                    Option(value={"keep": True}, label="No, keep its own instructions"),
                ],
                raised_by="auditor",
            )
        )
    return out


def use(
    definition: dict[str, Any], ws: Workspace, sid: str, name: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Make the change an offer described: the new steps (to go into the draft as one
    change), with the result shapes and the instructions it changed written to the
    workspace, an instruction file as a new version the step is pinned to."""
    tried = attempt(definition, ws, sid, name)
    if tried is None:
        raise ValueError(f"“{name}” no longer fits that step")
    for rel, shape in tried.schemas.items():
        ws.save_schema(rel, shape)
    for rel, body in tried.skills.items():
        version, _ = ws.save_skill_version(rel, body)
        for s in tried.steps:
            if (s.get("skill") or "").partition("@")[0] == rel:
                s["skill"] = f"{rel}@{version}"
    return tried.steps, tried.notes
