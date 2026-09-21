"""Resolve a state path (``steps.review.output.verdict``) to the JSON Schema node
that describes it, using the definition plus the workspace's schema files.

This is what lets the semantic pass say "no step produces that" or "that value is
not one of the outcomes" without running anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wf.expr import Path
from wf.expr.parser import parse
from wf.expr.template import _TEMPLATE
from wf.schema import Step, Workflow, Workspace

INPUT_TYPES = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "list": {"type": "array"},
    "object": {"type": "object"},
}


@dataclass
class Resolution:
    schema: dict[str, Any] | None
    error: str | None = None
    unknown: bool = False  # the producing step declares no shape, so we cannot tell

    @property
    def ok(self) -> bool:
        return self.error is None


class SchemaResolver:
    def __init__(self, wf: Workflow, ws: Workspace | None, depth: int = 0):
        self.wf = wf
        self.ws = ws
        self.depth = depth

    # -- per-step output shapes ------------------------------------------

    def step_output_schema(self, step: Step) -> dict[str, Any] | None:
        if step.kind == "subworkflow":
            return None
        if step.output and step.output.schema_ and self.ws:
            return self.ws.load_schema(step.output.schema_)
        return None

    def workflow_outputs_schema(self, wf: Workflow) -> dict[str, Any]:
        props: dict[str, Any] = {}
        sub = SchemaResolver(wf, self.ws, self.depth + 1)
        for name, expr in wf.spec.outputs.items():
            m = _TEMPLATE.fullmatch(expr.strip())
            if not m:
                props[name] = {"type": "string"}
                continue
            try:
                node = parse(m.group(1))
            except Exception:
                props[name] = {}
                continue
            if isinstance(node, Path):
                r = sub.resolve(node, step_for_each=None)
                props[name] = r.schema or {}
            else:
                props[name] = {}
        return {"type": "object", "properties": props}

    # -- path resolution --------------------------------------------------

    def resolve(self, path: Path, step_for_each: str | None = None) -> Resolution:
        segs = list(path.segments)
        root = segs[0]
        if root == "inputs":
            if len(segs) == 1:
                return Resolution({"type": "object"})
            spec = self.wf.spec.inputs.get(str(segs[1]))
            if spec is None:
                return Resolution(None, f"the workflow has no input called “{segs[1]}”")
            node: dict[str, Any] = dict(INPUT_TYPES.get(spec.type, {}))
            if spec.enum:
                node["enum"] = list(spec.enum)
            return self._descend(node, segs[2:], str(path))
        if root == "item":
            if step_for_each is None:
                return Resolution(
                    None, "“item” is only available inside a step that runs over a list"
                )
            m = _TEMPLATE.fullmatch(step_for_each.strip())
            if not m:
                return Resolution(None, "the list this step runs over is not a single expression")
            try:
                list_path = parse(m.group(1))
            except Exception as e:
                return Resolution(None, f"the list expression cannot be read: {e}")
            if not isinstance(list_path, Path):
                return Resolution(None, "the list this step runs over is not a plain reference")
            r = self.resolve(list_path)
            if not r.ok or r.schema is None:
                return r
            if r.schema.get("type") != "array":
                return Resolution(None, f"“{list_path}” is not a list")
            return self._descend(r.schema.get("items", {}), segs[1:], str(path))
        if root == "steps":
            if len(segs) < 2:
                return Resolution({"type": "object"})
            step = self.wf.step(str(segs[1]))
            if step is None:
                return Resolution(None, f"there is no step called “{segs[1]}”")
            if len(segs) == 2:
                return Resolution({"type": "object"})
            what = segs[2]
            if what == "status":
                return Resolution(
                    {"type": "string", "enum": ["done", "skipped", "failed", "waiting"]}
                )
            if what == "output":
                if step.kind == "subworkflow":
                    child = (
                        self.ws.resolve_workflow_ref(step.workflow or "", current=self.wf)
                        if self.ws and step.workflow
                        else None
                    )
                    if step.for_each is not None:
                        return Resolution(
                            None,
                            f"“{step.id}” runs once per item; read its results with outputs[*]",
                        )
                    if child is None or self.depth > 2:
                        return Resolution(None, unknown=True)
                    return self._descend(self.workflow_outputs_schema(child), segs[3:], str(path))
                schema = self.step_output_schema(step)
                if schema is None:
                    return Resolution(None, unknown=True)
                return self._descend(schema, segs[3:], str(path))
            if what == "outputs":
                if step.for_each is None:
                    return Resolution(None, f"“{step.id}” runs once; read its result with output")
                if step.kind == "subworkflow":
                    child = (
                        self.ws.resolve_workflow_ref(step.workflow or "", current=self.wf)
                        if self.ws and step.workflow
                        else None
                    )
                    item = (
                        self.workflow_outputs_schema(child)
                        if (child is not None and self.depth <= 2)
                        else None
                    )
                else:
                    item = self.step_output_schema(step)
                if item is None:
                    return Resolution(None, unknown=True)
                return self._descend({"type": "array", "items": item}, segs[3:], str(path))
            return Resolution(None, f"a step has output, outputs and status, not “{what}”")
        return Resolution(None, f"“{root}” is not something a step can read")

    def _descend(self, node: dict[str, Any], segs: list[str | int], full: str) -> Resolution:
        cur = node
        for seg in segs:
            if not isinstance(cur, dict):
                return Resolution(None, f"cannot read into {full}")
            if seg == "*" or isinstance(seg, int):
                if cur.get("type") != "array" and "items" not in cur:
                    return Resolution(None, f"“{full}” indexes into something that is not a list")
                cur = cur.get("items", {})
                continue
            props = cur.get("properties")
            if (
                props is None
                and cur.get("type") == "object"
                and cur.get("additionalProperties") in (None, True)
            ):
                return Resolution({}, unknown=True)
            if props is None or seg not in props:
                return Resolution(None, f"nothing produces “{seg}” in {full}")
            cur = props[seg]
        return Resolution(cur)
