"""What the system can already do, as the extractor, the instruction writer and the chat
are shown it.

The workflows this workspace already has: enough to recognise one when a document or a
person mentions it, and to say what it needs. Its name, what it is for, what it starts
from and its steps in order; nothing about how the steps do their work.

And what a step can use without the document saying how: the tools a judgement step
can be given, the fixed routines a check or tool step can name, and the hand-written
instructions at the top of ``skills/``, and the models a step can run on. A document that says "search the web" or
"check the links" has said enough when the system already knows how; only what it
cannot match is asked. Every entry is read from the code or the workspace, so the list
stays true when either changes.
"""

from __future__ import annotations

import logging
from typing import Any

from wf.interpret.interpreter import CONTENTS_TOOL, SEARCH_TOOL
from wf.schema import Workspace
from wf.settings import step_models
from wf.validate.findings import RUNNERS

log = logging.getLogger(__name__)


def known_workflows(ws: Workspace, exclude: str | None = None) -> list[dict[str, Any]]:
    """Every definition in the workspace but ``exclude``; one that cannot be read is left
    out, and logged, rather than stopping the draft or the chat."""
    out = []
    for name in ws.list_definitions():
        if name == exclude:
            continue
        try:
            wf = ws.load_definition(name)
        except Exception as e:  # noqa: BLE001 - a broken definition is not this draft's problem
            log.warning("left %s out of the known workflows: %s", name, e)
            continue
        out.append(
            {
                "name": wf.metadata.name,
                "description": wf.metadata.description or "",
                "starts_from": [
                    {
                        "name": n,
                        "required": s.required and s.default is None,
                        "description": s.description or "",
                    }
                    for n, s in wf.spec.inputs.items()
                    if not s.internal
                ],
                "steps": [s.title or s.id for s in wf.spec.steps],
            }
        )
    return out


def known_tools() -> list[dict[str, str]]:
    """The tools a judgement step can be given, in the words the step itself is given."""
    return [{"name": t.name, "what": t.description} for t in (SEARCH_TOOL, CONTENTS_TOOL)]


def known_routines() -> list[dict[str, str]]:
    """The fixed routines a check or tool step names. A workflow cannot add its own."""
    return [
        {"name": name, "kind": kind, "does": label, "gives_back": what}
        for name, (kind, label, what) in sorted(RUNNERS.items())
    ]


def _first_paragraph(body: str) -> str:
    for block in body.split("\n\n"):
        block = block.strip()
        if block and not block.startswith("#"):
            return " ".join(block.split())[:400]
    return ""


def known_instructions(ws: Workspace) -> list[dict[str, Any]]:
    """The hand-written instruction files at the top of ``skills/``: how the system
    already does a kind of step well. Generated ones live a level down, under their
    workflow's name, and are that workflow's own."""
    out = []
    for p in sorted(ws.path("skills").glob("*.md")):
        skill = ws.load_skill(f"skills/{p.name}")
        if skill is None or not skill.body.strip():
            continue
        out.append(
            {
                "name": p.stem,
                # what a step's ``skill`` is set to, to follow these instructions
                "ref": f"skills/{p.name}@{skill.version or 1}",
                "title": skill.title,
                "what": _first_paragraph(skill.body),
                "version": skill.version,
                "body": skill.body.strip(),
            }
        )
    return out


def capabilities(ws: Workspace, exclude: str | None = None) -> dict[str, Any]:
    """Everything a draft can build on, in one place."""
    return {
        "tools": known_tools(),
        "routines": known_routines(),
        "instructions": known_instructions(ws),
        "models": step_models(),
        "workflows": known_workflows(ws, exclude=exclude),
    }
