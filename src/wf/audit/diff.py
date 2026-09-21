"""Their document beside the definition: what it said, what is now explicit, what we assumed."""

from __future__ import annotations

from typing import Any

from wf.schema import Workflow
from wf.validate import Finding

from .ingest import Passage

FIELD_LABELS = {
    "when": "runs when",
    "checks": "checks",
    "does_not_check": "does not check",
    "requires_approval": "approved by",
    "side_effects": "leaves the system",
    "deadline": "waits up to",
    "on_timeout": "when the wait runs out",
    "limits": "limits",
    "skill": "instructions",
    "model": "judgement",
    "output.schema": "produces",
    "run": "runs",
    "for_each": "runs once per",
}


def document_diff(
    passages: list[Passage], wf: Workflow, provenance: dict[str, str], findings: list[Finding]
) -> dict[str, Any]:
    by_passage: dict[str, list[dict[str, Any]]] = {p.id: [] for p in passages}
    for path, pid in provenance.items():
        if pid in by_passage:
            by_passage[pid].append({"path": path, "label": _label(path)})
    open_by_field = {f.field: f for f in findings}

    steps: list[dict[str, Any]] = []
    for s in wf.spec.steps:
        fields: list[dict[str, Any]] = []
        for key, label in FIELD_LABELS.items():
            value = _value(s, key)
            path = f"steps.{s.id}.{key}"
            f = open_by_field.get(path)
            if value is None and f is None:
                continue
            status = (
                "stated"
                if path in provenance
                else ("open" if f and f.status == "open" else ("answered" if f else "assumed"))
            )
            if f and f.type == "assumption" and f.status == "open":
                status = "assumed"
            fields.append(
                {
                    "key": key,
                    "label": label,
                    "value": value,
                    "status": status,
                    "passage": provenance.get(path),
                    "finding_id": f.id if f else None,
                }
            )
        steps.append(
            {
                "id": s.id,
                "title": s.title or s.id,
                "kind": s.kind,
                "origin": s.origin.model_dump() if s.origin else None,
                "passage": provenance.get(f"steps.{s.id}"),
                "fields": fields,
            }
        )
    return {
        "passages": [{**p.model_dump(), "produced": by_passage.get(p.id, [])} for p in passages],
        "steps": steps,
        "counts": {
            "stated": sum(1 for st in steps for f in st["fields"] if f["status"] == "stated"),
            "assumed": sum(1 for st in steps for f in st["fields"] if f["status"] == "assumed"),
            "open": sum(1 for st in steps for f in st["fields"] if f["status"] == "open"),
            "answered": sum(1 for st in steps for f in st["fields"] if f["status"] == "answered"),
        },
    }


def _label(path: str) -> str:
    for key, label in FIELD_LABELS.items():
        if path.endswith(key):
            return label
    if path.startswith("steps.") and path.count(".") == 1:
        return "step"
    return path.rsplit(".", 1)[-1]


def _value(step: Any, key: str) -> Any:
    if key == "output.schema":
        return step.output.schema_ if step.output else None
    v = getattr(step, key, None)
    if hasattr(v, "model_dump"):
        return v.model_dump(exclude_none=True)
    return v
