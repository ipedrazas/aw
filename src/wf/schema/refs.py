"""Parsing of pinned references such as ``skills/research-brief.md@3`` and ``deep-research@4``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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


def rename_references(data: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """Point a definition (as a plain dict) at its new name, in place.

    What a draft writes for a definition lives under ``skills/<name>/`` and
    ``schemas/<name>/``, and a step that goes deeper calls the definition by name. All
    three move with a rename, so every reference to them has to move too, or the steps
    point at files that are no longer there.
    """
    data.setdefault("metadata", {})["name"] = new
    for step in (data.get("spec") or {}).get("steps") or []:
        skill = step.get("skill")
        if isinstance(skill, str) and skill.startswith(f"skills/{old}/"):
            step["skill"] = f"skills/{new}/" + skill[len(f"skills/{old}/") :]
        out = step.get("output")
        if isinstance(out, dict) and isinstance(out.get("schema"), str):
            if out["schema"].startswith(f"schemas/{old}/"):
                out["schema"] = f"schemas/{new}/" + out["schema"][len(f"schemas/{old}/") :]
        wf_ref = step.get("workflow")
        if isinstance(wf_ref, str):
            pin = parse_pin(wf_ref)
            if (pin.path if pin else wf_ref.strip()) == old:
                step["workflow"] = f"{new}@{pin.version}" if pin else new
    return data
