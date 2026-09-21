"""Compare a run with the outcome the customer knows, and name the first step where
the run went a different way."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from wf.expr import Path, resolve_path


class ExpectationResult(BaseModel):
    step: str
    field: str
    expected: Any
    actual: Any
    matched: bool
    why: str = ""


def evaluate_expectations(
    state: dict[str, Any], expectations: list[dict[str, Any]]
) -> list[ExpectationResult]:
    out = []
    for e in expectations:
        segs = ["steps", e["step"], *str(e["field"]).split(".")]
        actual = resolve_path(Path(tuple(segs)), state)
        expected = e.get("equals")
        matched = _same(actual, expected)
        out.append(
            ExpectationResult(
                step=e["step"],
                field=e["field"],
                expected=expected,
                actual=actual,
                matched=matched,
                why=e.get("why", ""),
            )
        )
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return str(a) == str(b) if (a is not None and b is not None) else a == b


def first_divergence(
    results: list[ExpectationResult], step_order: list[str]
) -> ExpectationResult | None:
    order = {s: i for i, s in enumerate(step_order)}
    misses = [r for r in results if not r.matched]
    if not misses:
        return None
    return min(misses, key=lambda r: order.get(r.step, 10_000))
