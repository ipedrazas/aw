"""The workflows this workspace already has, as the extractor and the chat are shown them.

Enough to recognise one when a document or a person mentions it, and to say what it
needs: its name, what it is for, what it starts from and its steps in order. Nothing
about how the steps do their work.
"""

from __future__ import annotations

import logging
from typing import Any

from wf.schema import Workspace

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
