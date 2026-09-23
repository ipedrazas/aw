"""Put back the files a definition's steps point at, when they are not there.

A draft writes an instruction file and an output shape for each step it makes, under
``skills/<name>/`` and ``schemas/<name>/``. Those files can go missing under it — a
workflow deleted while its draft is kept, a workspace copied without them, a rename
made before renames moved the references — and then every step asks the person a
question they cannot answer: "the output shape is not in the workspace". Nothing in
those files came from the person that the definition does not still hold, so they
are written again from the definition instead of being asked about.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from wf.interpret.registry import RUNNER_OUTPUT_SCHEMAS
from wf.schema import Workflow, Workspace, WorkspaceError, dump_workflow, parse_pin

from .draft import _skill_body


def restore_missing_files(
    wf: Workflow, ws: Workspace, source_for: dict[str, str] | None = None
) -> list[str]:
    """Write every instruction file and output shape a step names and the workspace
    lacks. ``source_for`` maps a step id to the document's words for it, when there is a
    document. Returns the workspace-relative paths written."""
    text = dump_workflow(wf)
    written: list[str] = []
    for step in wf.spec.steps:
        try:
            if step.skill and ws.load_skill(step.skill) is None:
                pin = parse_pin(step.skill)
                rel = pin.path if pin else step.skill
                body = _skill_body(
                    {"title": step.title or step.id, "description": step.description},
                    None,
                    (source_for or {}).get(step.id),
                )
                ws.save_skill(rel, pin.version if pin else 1, body)
                written.append(rel)
            rel = step.output.schema_ if step.output else None
            if rel and ws.load_schema(rel) is None:
                ws.save_schema(rel, _shape_for(step.id, step.run, text))
                written.append(rel)
        except WorkspaceError:
            continue  # a path outside the workspace stays a question; it is not ours to write
    return written


def _shape_for(sid: str, run: str | None, definition_text: str) -> dict[str, Any]:
    """A routine's own shape; otherwise the fields later steps read from this one,
    or a one-line summary when they read it whole."""
    if run in RUNNER_OUTPUT_SCHEMAS:
        return copy.deepcopy(RUNNER_OUTPUT_SCHEMAS[run])
    read = sorted(
        set(
            re.findall(
                rf"steps\.{re.escape(sid)}\.output\.([A-Za-z_][A-Za-z0-9_]*)", definition_text
            )
        )
    )
    props: dict[str, Any] = {f: {"type": "string"} for f in read} or {
        "summary": {"type": "string", "description": "What this step produced, in a sentence."}
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(props),
        "properties": props,
    }
