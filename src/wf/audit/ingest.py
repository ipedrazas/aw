"""Split a process document into addressable passages so every finding can quote its source."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")


class Passage(BaseModel):
    id: str
    text: str
    start_line: int
    end_line: int
    heading: str | None = None
    kind: str = "paragraph"  # paragraph | list | heading


def ingest(document: str) -> list[Passage]:
    text = _COMMENT.sub(lambda m: "\n" * m.group().count("\n"), document)
    lines = text.splitlines()
    passages: list[Passage] = []
    heading: str | None = None
    buf: list[tuple[int, str]] = []
    kind = "paragraph"

    def flush() -> None:
        nonlocal buf, kind
        if not buf:
            return
        body = "\n".join(t for _, t in buf).strip()
        if body:
            passages.append(
                Passage(
                    id=f"p{len(passages) + 1}",
                    text=body,
                    start_line=buf[0][0],
                    end_line=buf[-1][0],
                    heading=heading,
                    kind=kind,
                )
            )
        buf = []
        kind = "paragraph"

    for n, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        h = _HEADING.match(line)
        if h:
            flush()
            heading = h.group(2).strip()
            passages.append(
                Passage(
                    id=f"p{len(passages) + 1}",
                    text=heading,
                    start_line=n,
                    end_line=n,
                    heading=heading,
                    kind="heading",
                )
            )
            continue
        if _BULLET.match(line):
            if buf and kind != "list":
                flush()
            kind = "list"
            buf.append((n, line))
            continue
        if buf and kind == "list":
            # continuation of a bullet
            buf.append((n, line))
            continue
        buf.append((n, line))
    flush()
    return passages


def passage_index(passages: list[Passage]) -> dict[str, Passage]:
    return {p.id: p for p in passages}


def as_data(passages: list[Passage]) -> list[dict[str, Any]]:
    return [p.model_dump() for p in passages if p.kind != "heading"]
