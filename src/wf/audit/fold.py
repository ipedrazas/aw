"""A separate "go deeper" step, folded into the step that searches.

Drafts made before searching further existed (and any extraction that still splits it
out) have a step after the search that says to go deeper, with its limits only as
words in its instructions: nothing keeps to them. Plain code spots that shape and asks,
once, whether the searching step should search further by itself instead, with the
limits read from the step's own words. Yes removes the step and has whatever read it
read the searching step; no leaves both as they are, and is not asked again.
(plans/search-further.md, piece 3)
"""

from __future__ import annotations

import copy
import re
from typing import Any

from wf.validate import FURTHER_DEFAULT, Finding, Option, further_limits

#: What a step that goes deeper says about itself.
DEEPER = re.compile(
    r"\b(?:go(?:es|ing)?|dig(?:s|ging)?|search(?:es|ing)?|look(?:s|ing)?)\s+deeper\b"
    r"|\bdeeper\s+(?:research|search(?:es)?|dive)\b"
    r"|\b(?:related|discovered|new)\s+topics?\b.*\b(?:search|investigat)",
    re.I,
)
SEARCHES = re.compile(r"\b(\d{1,3})\s+(?:more\s+)?searches\b", re.I)
LEVELS = re.compile(r"\b(\d)\s+levels?\b", re.I)


def _text(step: dict[str, Any]) -> str:
    return f"{step.get('title') or ''}. {step.get('description') or ''}"


def _searches(step: dict[str, Any]) -> bool:
    return any(t.split(".")[-1] == "search" for t in step.get("tools") or {})


def candidates(definition: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(searching step, step that goes deeper) pairs: an agent step that says it goes
    deeper, after the nearest agent step before it that searches and does not search
    further yet."""
    out = []
    steps = definition["spec"]["steps"]
    for i, s in enumerate(steps):
        if s.get("kind") != "agent" or not DEEPER.search(_text(s)):
            continue
        before = next((b for b in reversed(steps[:i]) if b.get("kind") == "agent"), None)
        if before and _searches(before) and not before.get("search_further"):
            out.append((before, s))
    return out


def limits_in(step: dict[str, Any]) -> dict[str, Any]:
    """The limits a step's own words give, the rest as a draft would set them."""
    text = _text(step)
    given: dict[str, int] = {}
    if m := SEARCHES.search(text):
        given["max_searches"] = int(m.group(1))
    if m := LEVELS.search(text):
        given["levels"] = int(m.group(1))
    return further_limits(**{**FURTHER_DEFAULT, **given})


def suggestions(definition: dict[str, Any]) -> list[Finding]:
    """One question per step that could be folded into the step it follows."""
    out = []
    for searcher, deeper in candidates(definition):
        limits = limits_in(deeper)
        sf = limits["search_further"]
        into, name = searcher.get("title") or searcher["id"], deeper.get("title") or deeper["id"]
        out.append(
            Finding(
                type="assumption",
                step_id=searcher["id"],
                field=f"steps.{searcher['id']}.search_further",
                question=f"Should “{into}” search further by itself, instead of “{name}”?",
                detail=f"“{name}” is a step of its own, so its limits are only words in its "
                "instructions and nothing keeps to them. Searching further, the system keeps "
                "them, and what it finds goes into the same result.",
                source_text=deeper.get("description") or None,
                answer_kind="choice",
                options=[
                    Option(
                        value={"op": "fold", "step": deeper["id"], **limits},
                        label=f"Yes, fold “{name}” into it: {sf['levels']} levels deeper, "
                        f"{sf['max_searches']} searches in all",
                        consequence="The later steps read what it finds from the one step.",
                    ),
                    Option(value={"keep": True}, label="No, keep them separate"),
                ],
                raised_by="auditor",
            )
        )
    return out


def is_suggestion(f: Finding) -> bool:
    return any(isinstance(o.value, dict) and o.value.get("op") == "fold" for o in f.options)


def fold(definition: dict[str, Any], into: str, sid: str) -> list[dict[str, Any]]:
    """The steps without ``sid``, with whatever read from it reading ``into`` instead."""
    reads_it = re.compile(rf"\bsteps\.{re.escape(sid)}\b")

    def point(v: Any) -> Any:
        if isinstance(v, str):
            return reads_it.sub(f"steps.{into}", v)
        if isinstance(v, dict):
            return {k: point(x) for k, x in v.items()}
        if isinstance(v, list):
            return [point(x) for x in v]
        return v

    steps = []
    for s in definition["spec"]["steps"]:
        if s["id"] == sid:
            continue
        s = copy.deepcopy(s)
        for k in ("input", "when", "for_each", "with", "may_repeat"):
            if k in s:
                s[k] = point(s[k])
        steps.append(s)
    return steps
