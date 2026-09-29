"""Wire the steps that use a capability, from what it takes. (plans/capability-contracts.md,
piece 2)

The extractor picks what a step does; this gives it what it needs, from the contracts
in ``wf.validate.contracts``, so a model's guess at the wiring is never the wiring:

- a check or tool step no routine does becomes a step a model does, said openly;
- a step that uses instructions that say what they take uses them as they are;
- a routine about the report reads the report's links, through the capability before
  it when there is one (the link check before reading the pages), and a report that
  lists none is given a list of its citations;
- instructions run once per item run over the nearest list that has what they take,
  and the step that makes that list is added (or moved) before them when there is none.

Everything it changes is said in the draft's notes, and a step it adds is marked as
suggested, with the reason.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from wf.validate.contracts import ROUTINE_TAKES, Takes, fits, skill_takes

#: One link a report cites, as the link check and the page reader take it.
CITATION_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["line", "claim", "url"],
    "properties": {
        "line": {"type": "integer", "description": "The line of the report it is cited on."},
        "claim": {"type": "string", "description": "What the report says it backs."},
        "url": {"type": "string", "description": "The address, exactly as cited."},
    },
}

_REPORT = re.compile(r"\breport\b", re.I)
_WRITES = re.compile(r"\b(?:write|writes|writing|draft|drafts|compose|composes)\b", re.I)


class Wiring:
    """The draft's parts the wiring reads and changes, in place."""

    def __init__(
        self,
        steps: list[dict[str, Any]],
        schemas: dict[str, dict[str, Any]],
        skills: dict[str, str],
        briefs: dict[str, dict[str, Any]],
        notes: list[str],
        own: dict[str, dict[str, Any]],
        uses: dict[str, str | None],
        name: str,
    ):
        self.name = name
        self.steps = steps
        self.schemas = schemas
        self.skills = skills
        self.briefs = briefs
        self.notes = notes
        self.own = own
        self.uses = uses
        self.wired: set[str] = set()

    # -- reading ---------------------------------------------------------------------

    def schema_of(self, step: dict[str, Any]) -> dict[str, Any] | None:
        rel = (step.get("output") or {}).get("schema")
        return self.schemas.get(rel) if rel else None

    def index(self, sid: str) -> int:
        return next(i for i, s in enumerate(self.steps) if s["id"] == sid)

    def takes(self, step: dict[str, Any]) -> Takes | None:
        if step.get("run") in ROUTINE_TAKES:
            return ROUTINE_TAKES[step["run"]]
        ref = step.get("skill")
        for i in self.own.values():
            if ref and i.get("ref") == ref:
                return skill_takes(i.get("contract"))
        return None

    def is_report(self, step: dict[str, Any]) -> bool:
        schema = self.schema_of(step) or {}
        if "body_md" in (schema.get("properties") or {}):
            return True
        words = f"{step.get('title') or ''} {step.get('description') or ''}"
        return step.get("kind") == "agent" and bool(_REPORT.search(words) and _WRITES.search(words))

    @staticmethod
    def fitting(schema: dict[str, Any] | None, takes: Takes) -> str | None:
        """Where in a result is what ``takes`` needs: ``""`` for all of it, a field's
        name, or None."""
        if not schema:
            return None
        if not takes.many and fits(schema, takes):
            return ""
        for name, node in (schema.get("properties") or {}).items():
            if fits(node, takes):
                return name
        return None

    @staticmethod
    def ref(step: dict[str, Any], where: str) -> str:
        return f"${{steps.{step['id']}.output{'.' + where if where else ''}}}"

    # -- the passes ------------------------------------------------------------------

    def run(self) -> None:
        self.adopt_contracts()
        for sid in [s["id"] for s in self.steps]:
            step = next((s for s in self.steps if s["id"] == sid), None)
            takes = self.takes(step) if step else None
            if step is None or takes is None:
                continue
            if takes.per:
                self.wire_per_item(step, takes)
            elif takes.key:
                self.wire_key(step, takes)

    def adopt_contracts(self) -> None:
        """A step whose instructions say what they take uses them as they are: pinned,
        giving back what they give back, with no copy of its own to drift from them."""
        for step in self.steps:
            used = self.own.get(self.uses.get(step["id"]) or "")
            if step.get("kind") != "agent" or not used or not used.get("contract"):
                continue
            mine = step.get("skill", "").partition("@")[0]
            self.skills.pop(mine, None)
            self.briefs.pop(mine, None)
            step["skill"] = used["ref"]
            if used.get("result"):
                self.schemas.pop((step.get("output") or {}).get("schema") or "", None)
                step["output"] = {"schema": used["result"]}
            self.notes.append(
                f"“{step['title']}” uses the system's own “{used['title']}” as it is."
            )

    def wire_key(self, step: dict[str, Any], takes: Takes) -> bool:
        """A routine reads what it takes under its key: from the capability about the
        same thing before it, else from the report, which lists its links if it did not."""
        earlier = self.steps[: self.index(step["id"])]
        source = None
        for e in reversed(earlier):
            # a list of links passes through the capability before it (the pages read
            # are the ones the link check found open); the report itself does not
            other = ROUTINE_TAKES.get(e.get("run") or "")
            if other and takes.many and takes.of and other.of == takes.of:
                where = self.fitting(self.schema_of(e), takes)
                if where is not None:
                    source = (e, where)
                    break
        if source is None and takes.of == "report":
            report = next((e for e in reversed(earlier) if self.is_report(e)), None)
            if report is not None:
                where = self.fitting(self.schema_of(report), takes)
                if where is None and takes.many and "url" in takes.fields:
                    where = self.give_citations(report, step)
                if where is not None:
                    source = (report, where)
        if source is None:
            return False
        e, where = source
        # what else it was given stays: a routine reads what it takes and passes over
        # the rest (the PDF also prints the follow-up reports it was handed)
        step["input"] = {**(step.get("input") or {}), takes.key: self.ref(e, where)}
        self.wired.add(step["id"])
        self.notes.append(f"“{step['title']}” reads what it needs from “{e['title']}”.")
        return True

    def wire_per_item(self, step: dict[str, Any], takes: Takes) -> None:
        """Instructions run once per item run over the nearest list with what they take;
        when there is none, the page reader is put before them to make it."""
        found = self.nearest_list(step, takes)
        if found is None:
            reader = self.reader_before(step)
            found = self.nearest_list(step, takes) if reader else None
        if found is None:
            return
        e, where = found
        step["for_each"] = self.ref(e, where)
        step.setdefault("max_fanout", 40)
        step["input"] = {f: f"${{item.{f}}}" for f in takes.fields}
        self.wired.add(step["id"])
        self.notes.append(
            f"“{step['title']}” runs once per {takes.per}, on {takes.what}, from “{e['title']}”."
        )

    def nearest_list(self, step: dict[str, Any], takes: Takes) -> tuple[dict[str, Any], str] | None:
        for e in reversed(self.steps[: self.index(step["id"])]):
            where = self.fitting(self.schema_of(e), takes)
            if where:
                return e, where
        return None

    # -- what it adds ----------------------------------------------------------------

    def reader_before(self, step: dict[str, Any]) -> dict[str, Any] | None:
        """The step that reads the cited pages, before ``step``: moved there if it runs
        after it, added if there is none, and wired either way."""
        from wf.interpret.registry import RUNNER_OUTPUT_SCHEMAS

        at = self.index(step["id"])
        reader = next(
            (s for s in self.steps[at + 1 :] if s.get("run") == "tools.fetch_pages"), None
        )
        if reader is not None:
            self.steps.remove(reader)
            self.notes.append(
                f"I moved “{reader['title']}” before “{step['title']}”, which needs each "
                "page's text."
            )
        else:
            rel = f"schemas/{self.name}/read_pages.json"
            self.schemas[rel] = copy.deepcopy(RUNNER_OUTPUT_SCHEMAS["tools.fetch_pages"])
            reader = {
                "id": self.fresh_id("read_pages"),
                "kind": "tool",
                "title": "Read the cited pages",
                "description": "Fetches the text of each cited page, for the next step to read.",
                "run": "tools.fetch_pages",
                "output": {"schema": rel},
                "side_effects": "none",
                "requires_approval": False,
                "shows_user": ["output.unread"],
                "trust": {"policy": "auto"},
                "origin": {
                    "by": "system",
                    "kind": "suggested",
                    "reason": f"“{step['title']}” needs the text of each page a citation points to.",
                },
            }
            self.notes.append(
                f"I added “Read the cited pages” before “{step['title']}”, which needs each "
                "page's text."
            )
        self.steps.insert(self.index(step["id"]), reader)
        if not self.wire_key(reader, ROUTINE_TAKES["tools.fetch_pages"]):
            return None
        return reader

    def give_citations(self, report: dict[str, Any], reader: dict[str, Any]) -> str | None:
        """A report that lists no links is given a list of its citations, with the claim
        each backs, for ``reader`` to take. Its instructions are told to fill it."""
        rel = (report.get("output") or {}).get("schema")
        schema = self.schemas.get(rel or "")
        if schema is None:
            return None
        props = schema.setdefault("properties", {})
        name = "sources" if "sources" not in props else "citations"
        props[name] = {
            "type": "array",
            "description": "Every link the report cites: the line it is on, the claim it "
            "backs and its address.",
            "items": copy.deepcopy(CITATION_ITEM),
        }
        schema.setdefault("required", []).append(name)
        field = {
            "name": name,
            "type": "list",
            "description": props[name]["description"],
        }
        skill = (report.get("skill") or "").partition("@")[0]
        if skill in self.briefs:
            self.briefs[skill].setdefault("produces", []).append(field)
        if skill in self.skills and f"`{name}`" not in self.skills[skill]:
            self.skills[skill] = _with_field(self.skills[skill], field)
        self.notes.append(
            f"“{report['title']}” lists the links it cites, with the claim each backs, for "
            f"“{reader['title']}”."
        )
        return name

    def fresh_id(self, base: str) -> str:
        taken = {s["id"] for s in self.steps}
        sid, n = base, 2
        while sid in taken:
            sid, n = f"{base}_{n}", n + 1
        return sid


def _with_field(body: str, field: dict[str, Any]) -> str:
    """Instructions that name one more thing to produce, under what they already list."""
    line = f"- `{field['name']}` ({field['type']}): {field['description']}"
    if "## What to produce\n\n" in body:
        return body.replace("## What to produce\n\n", f"## What to produce\n\n{line}\n", 1)
    return body.replace("## Decisions", f"## What to produce\n\n{line}\n\n## Decisions", 1)


def wire(
    steps: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
    skills: dict[str, str],
    briefs: dict[str, dict[str, Any]],
    notes: list[str],
    own: dict[str, dict[str, Any]],
    uses: dict[str, str | None],
    name: str,
) -> set[str]:
    """Wire the draft's capability steps in place. Returns the ids of the steps whose
    input it set, whose "I gave it everything before it" is no longer a question."""
    w = Wiring(steps, schemas, skills, briefs, notes, own, uses, name)
    w.run()
    return w.wired
