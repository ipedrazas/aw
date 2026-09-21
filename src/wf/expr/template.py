"""``${...}`` templates inside definition values.

A string that is exactly one ``${expr}`` evaluates to the expression's value, of
whatever type. A string with embedded expressions renders to a string. Dicts and
lists are rendered recursively.
"""

from __future__ import annotations

import re
from typing import Any

from .ast import Expr
from .evaluator import evaluate
from .parser import parse

_TEMPLATE = re.compile(r"\$\{([^}]*)\}")


def expressions_in(value: Any) -> list[str]:
    """Every expression source found in a value, in document order."""
    out: list[str] = []
    if isinstance(value, str):
        out.extend(m.group(1).strip() for m in _TEMPLATE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(expressions_in(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(expressions_in(v))
    return out


def parse_all(value: Any) -> list[Expr]:
    return [parse(src) for src in expressions_in(value)]


def is_single_expression(s: str) -> bool:
    m = _TEMPLATE.fullmatch(s.strip())
    return m is not None


def render(value: Any, state: Any) -> Any:
    if isinstance(value, str):
        if is_single_expression(value):
            return evaluate(parse(_TEMPLATE.fullmatch(value.strip()).group(1)), state)  # type: ignore[union-attr]

        def sub(m: re.Match[str]) -> str:
            v = evaluate(parse(m.group(1)), state)
            return "" if v is None else str(v)

        return _TEMPLATE.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, state) for v in value]
    return value
