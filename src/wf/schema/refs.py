"""Parsing of pinned references such as ``skills/research-brief.md@3`` and ``deep-research@4``."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PIN = re.compile(r"^(?P<path>[^@\s]+)@(?P<version>\d+)$")


@dataclass(frozen=True)
class PinnedRef:
    path: str
    version: int

    def __str__(self) -> str:
        return f"{self.path}@{self.version}"


def parse_pin(ref: str) -> PinnedRef | None:
    m = _PIN.match(ref.strip())
    if not m:
        return None
    return PinnedRef(m.group("path"), int(m.group("version")))
