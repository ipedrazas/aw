"""What a capability takes: the routines a check or tool step names, and the system's own
instructions that say.

What a capability gives back was already declared (``RUNNER_OUTPUT_SCHEMAS``, a skill's
``result:``); what it takes was left to the code to guess, which read "every web address
in its input" when it was handed a whole pipeline. A contract says it: the input it
comes under and the fields each item needs, or, for instructions a step runs once per
item, the fields of each item. The validator holds a step to it, and the offered fixes
are the earlier results that have that shape. (plans/capability-contracts.md)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wf.schema import Step, Workspace


@dataclass(frozen=True)
class Takes:
    """What a capability needs to be given.

    ``key`` is the input it comes under, holding a list of items (``many``) or one
    thing, each with ``fields``. With no key, ``fields`` are the step's own inputs:
    given once, or, when ``per`` names what it runs over, once per item.
    """

    fields: tuple[str, ...]
    what: str
    key: str | None = None
    many: bool = True
    per: str | None = None


#: The routines' contracts, beside their description in ``RUNNERS``.
ROUTINE_TAKES: dict[str, Takes] = {
    "checks.http_resolves": Takes(
        key="sources", fields=("url",), what="the links to check, each with its address"
    ),
    "tools.fetch_pages": Takes(
        key="sources",
        fields=("url",),
        what="the cited links, each with its address and the claim it backs",
    ),
    "tools.render_pdf": Takes(
        key="report", fields=("body_md",), many=False, what="the report, with its text"
    ),
    "tools.send_email": Takes(
        fields=("to", "subject", "body"), many=False, what="who to send to, a subject and a body"
    ),
}


def skill_takes(meta: dict[str, Any] | None) -> Takes | None:
    """A skill's contract from its front matter: ``takes: {per, fields, what}``."""
    if not isinstance(meta, dict) or not meta.get("fields"):
        return None
    per = meta.get("per")
    return Takes(
        fields=tuple(str(f) for f in meta["fields"]),
        what=str(meta.get("what") or ", ".join(meta["fields"])),
        many=bool(per),
        per=str(per) if per else None,
    )


def contract_for(step: Step, ws: Workspace | None) -> tuple[str, Takes] | None:
    """(what the capability is called, what it takes) for a step that names one."""
    if step.run and step.run in ROUTINE_TAKES:
        from .findings import RUNNERS

        return RUNNERS[step.run][1], ROUTINE_TAKES[step.run]
    if step.skill and ws is not None:
        skill = ws.load_skill(step.skill)
        takes = skill_takes(skill.takes) if skill else None
        if skill and takes:
            return skill.title, takes
    return None


def fits(schema: dict[str, Any] | None, takes: Takes) -> bool | None:
    """Whether a declared shape is what ``takes`` needs; None when nothing is declared."""
    if not isinstance(schema, dict) or not schema:
        return None
    if takes.many:
        if schema.get("type") != "array":
            return False
        item = schema.get("items") or {}
    else:
        item = schema
    if item.get("type") not in ("object", None):
        return False
    props = item.get("properties")
    if not isinstance(props, dict):
        return None
    return all(f in props for f in takes.fields)
