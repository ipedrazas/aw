"""Two runs of the same definition, diffed at the level of decisions.

Steps, limits, gates and checks are identical by construction. What differs is the
judgement inside steps, and this is where it shows.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SUMMARY_FIELDS = ("verdict", "open_count", "searches_run", "pages", "questions", "title")


class StepDiff(BaseModel):
    step_id: str
    title: str
    status_a: str | None
    status_b: str | None
    summary_a: dict[str, Any] = Field(default_factory=dict)
    summary_b: dict[str, Any] = Field(default_factory=dict)
    decisions_a: list[str] = Field(default_factory=list)
    decisions_b: list[str] = Field(default_factory=list)
    guesses_a: list[str] = Field(default_factory=list)
    guesses_b: list[str] = Field(default_factory=list)
    differs: bool = False
    what_differs: list[str] = Field(default_factory=list)


class RunDiff(BaseModel):
    run_a: str
    run_b: str
    steps: list[StepDiff]
    first_divergence: str | None = None
    same_control_flow: bool = True
    cost_a: float = 0.0
    cost_b: float = 0.0


def _summary(output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {}
    out: dict[str, Any] = {}
    for k in SUMMARY_FIELDS:
        if k in output:
            v = output[k]
            out[k] = len(v) if isinstance(v, list) else v
    for k, v in output.items():
        if isinstance(v, list) and k not in out:
            out[f"{k}_count"] = len(v)
    return out


def diff_runs(a: dict[str, Any], b: dict[str, Any]) -> RunDiff:
    """``a`` and ``b`` are run snapshots: {id, cost, steps: [{step_id, title, status, output, decisions: [{kind, text}]}]}."""
    steps_a = {s["step_id"]: s for s in a["steps"]}
    steps_b = {s["step_id"]: s for s in b["steps"]}
    order = [s["step_id"] for s in a["steps"]] + [
        s["step_id"] for s in b["steps"] if s["step_id"] not in steps_a
    ]
    out: list[StepDiff] = []
    first: str | None = None
    same_flow = True
    for sid in order:
        sa, sb = steps_a.get(sid), steps_b.get(sid)
        d = StepDiff(
            step_id=sid,
            title=(sa or sb or {}).get("title", sid),
            status_a=sa.get("status") if sa else None,
            status_b=sb.get("status") if sb else None,
            summary_a=_summary(sa.get("output")) if sa else {},
            summary_b=_summary(sb.get("output")) if sb else {},
            decisions_a=[
                x["text"] for x in (sa or {}).get("decisions", []) if x.get("kind") == "decision"
            ],
            decisions_b=[
                x["text"] for x in (sb or {}).get("decisions", []) if x.get("kind") == "decision"
            ],
            guesses_a=[
                x["text"] for x in (sa or {}).get("decisions", []) if x.get("kind") == "guess"
            ],
            guesses_b=[
                x["text"] for x in (sb or {}).get("decisions", []) if x.get("kind") == "guess"
            ],
        )
        if d.status_a != d.status_b:
            d.what_differs.append(
                f"ran in one run and not the other ({d.status_a} vs {d.status_b})"
            )
            same_flow = False
        for k in sorted(set(d.summary_a) | set(d.summary_b)):
            if d.summary_a.get(k) != d.summary_b.get(k):
                d.what_differs.append(f"{k}: {d.summary_a.get(k)} vs {d.summary_b.get(k)}")
        if d.guesses_a != d.guesses_b:
            d.what_differs.append("guessed differently")
        d.differs = bool(d.what_differs)
        if d.differs and first is None:
            first = sid
        out.append(d)
    return RunDiff(
        run_a=a["id"],
        run_b=b["id"],
        steps=out,
        first_divergence=first,
        same_control_flow=same_flow,
        cost_a=a.get("cost", 0.0),
        cost_b=b.get("cost", 0.0),
    )
