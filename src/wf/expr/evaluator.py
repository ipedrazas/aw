"""Evaluate a parsed expression against run state.

The state is a plain mapping with the roots ``inputs``, ``steps`` and ``item``.
Evaluation is total and deterministic: missing paths evaluate to ``None`` rather
than raising, so a skipped optional step simply reads as absent.
"""

from __future__ import annotations

from typing import Any

from .ast import Add, And, Compare, Expr, Literal_, Not, Or, Path

_MISSING = object()


class EvalError(ValueError):
    pass


def resolve_path(path: Path, state: Any) -> Any:
    cur: Any = state
    for seg in path.segments:
        if seg == "*":
            if cur is None:
                return None
            if not isinstance(cur, list):
                raise EvalError(f"[*] applied to a non-list in {path}")
            rest = Path(path.segments[path.segments.index("*") + 1 :])
            return [resolve_path(rest, item) if rest.segments else item for item in cur]
        cur = _get(cur, seg)
        if cur is _MISSING:
            return None
    return cur


def _get(obj: Any, key: str | int) -> Any:
    if obj is None:
        return _MISSING
    if isinstance(key, int):
        if isinstance(obj, list) and -len(obj) <= key < len(obj):
            return obj[key]
        return _MISSING
    if isinstance(obj, dict):
        return obj.get(key, _MISSING)
    if hasattr(obj, key):
        return getattr(obj, key)
    return _MISSING


def evaluate(e: Expr, state: Any) -> Any:
    if isinstance(e, Literal_):
        return e.value
    if isinstance(e, Path):
        return resolve_path(e, state)
    if isinstance(e, Not):
        return not _truthy(evaluate(e.operand, state))
    if isinstance(e, And):
        return _truthy(evaluate(e.left, state)) and _truthy(evaluate(e.right, state))
    if isinstance(e, Or):
        return _truthy(evaluate(e.left, state)) or _truthy(evaluate(e.right, state))
    if isinstance(e, Add):
        left, right = evaluate(e.left, state), evaluate(e.right, state)
        if (
            not (isinstance(left, int) and isinstance(right, int))
            or isinstance(left, bool)
            or isinstance(right, bool)
        ):
            raise EvalError(f"+ only adds integers, got {left!r} and {right!r}")
        return left + right
    if isinstance(e, Compare):
        left, right = evaluate(e.left, state), evaluate(e.right, state)
        if e.op == "==":
            return left == right
        if e.op == "!=":
            return left != right
        if left is None or right is None:
            return False
        try:
            return left < right if e.op == "<" else left > right
        except TypeError as exc:
            raise EvalError(f"cannot compare {left!r} and {right!r}") from exc
    raise EvalError(f"unknown expression node {e!r}")


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, int | float | str | list | dict):
        return bool(v)
    return True
