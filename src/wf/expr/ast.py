"""AST for the restricted expression language.

Expressions read state and nothing else. There are no function calls, no
assignment, no side effects, and nothing that can create a step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CmpOp = Literal["==", "!=", "<", ">"]


@dataclass(frozen=True)
class Literal_:
    value: Any


@dataclass(frozen=True)
class Path:
    """``steps.review.output.verdict`` or ``steps.go_deeper.outputs[*].report``.

    Segments are identifiers, integer indexes, or ``"*"`` for a projection over a list.
    """

    segments: tuple[str | int, ...]

    def __str__(self) -> str:
        out = ""
        for s in self.segments:
            if isinstance(s, int):
                out += f"[{s}]"
            elif s == "*":
                out += "[*]"
            else:
                out += ("." if out else "") + s
        return out

    @property
    def root(self) -> str:
        return str(self.segments[0])


@dataclass(frozen=True)
class Compare:
    op: CmpOp
    left: Expr
    right: Expr


@dataclass(frozen=True)
class And:
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Or:
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Not:
    operand: Expr


@dataclass(frozen=True)
class Add:
    left: Expr
    right: Expr


Expr = Literal_ | Path | Compare | And | Or | Not | Add


@dataclass
class ExprInfo:
    """What an expression reads, and what it compares to. Used by the validator."""

    paths: list[Path] = field(default_factory=list)
    # (path, op, literal) for every comparison of a path against a literal
    comparisons: list[tuple[Path, CmpOp, Any]] = field(default_factory=list)


def walk(e: Expr, info: ExprInfo | None = None) -> ExprInfo:
    info = info or ExprInfo()
    if isinstance(e, Path):
        info.paths.append(e)
    elif isinstance(e, Compare):
        if isinstance(e.left, Path) and isinstance(e.right, Literal_):
            info.comparisons.append((e.left, e.op, e.right.value))
        elif isinstance(e.right, Path) and isinstance(e.left, Literal_):
            flipped = {"<": ">", ">": "<"}.get(e.op, e.op)
            info.comparisons.append((e.right, flipped, e.left.value))  # type: ignore[arg-type]
        walk(e.left, info)
        walk(e.right, info)
    elif isinstance(e, And | Or | Add):
        walk(e.left, info)
        walk(e.right, info)
    elif isinstance(e, Not):
        walk(e.operand, info)
    return info
