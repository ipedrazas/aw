"""What later steps read from a step, and what changing its result would take from them.

A step moved onto instructions that come with a result of their own gives back
something else. Before that happens, the person is told which later steps read a
field that would go, and which read all of it, so the choice is made knowing the cost.
After it happens, the semantic validator raises the same reads as conflicts. This
compares field names only: a field both results have, with different values, is not
named here, and a branch on a value that is gone is found by the validator afterwards.
"""

from __future__ import annotations

from typing import Any

from wf.expr import ExprError, expressions_in, parse, walk
from wf.schema import Workflow


def reads_from(wf: Workflow, sid: str) -> list[tuple[str, str | None]]:
    """(who, field) for every read of ``sid``'s output: who is a step's title, or "the
    workflow's result"; field is the top-level field read, or None for all of it."""
    places: list[tuple[str, Any]] = [
        (s.title or s.id, [s.when, s.for_each, s.with_, s.input])
        for s in wf.spec.steps
        if s.id != sid
    ]
    places.append(("the workflow's result", wf.spec.outputs))
    out: list[tuple[str, str | None]] = []
    for who, value in places:
        for src in expressions_in(value):
            try:
                info = walk(parse(src))
            except ExprError:
                continue
            for p in info.paths:
                seg = p.segments
                if p.root != "steps" or len(seg) < 3 or seg[1] != sid or seg[2] != "output":
                    continue
                read = (who, str(seg[3]) if len(seg) > 3 and isinstance(seg[3], str) else None)
                if read not in out:
                    out.append(read)
    return out


def lost_by(wf: Workflow, sid: str, schema: dict[str, Any] | None) -> list[str]:
    """What giving back ``schema`` instead would take from the steps that read ``sid``,
    one plain sentence per reader."""
    fields = set((schema or {}).get("properties") or {})
    by_reader: dict[str, list[str | None]] = {}
    for who, field in reads_from(wf, sid):
        if field is None or field not in fields:
            by_reader.setdefault(who, []).append(field)
    out = []
    for who, missing in by_reader.items():
        if None in missing:
            out.append(f"“{who}” reads everything it gives back now, which would change.")
        else:
            names = ", ".join(f"“{m.replace('_', ' ')}”" for m in missing if m)
            out.append(f"“{who}” reads {names}, which it would no longer give back.")
    return out
