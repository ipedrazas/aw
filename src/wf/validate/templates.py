"""Predefined step templates: the fields the auditor expects for each step kind.

The extractor sometimes gets a step's kind wrong. "First, get my topic" was once read
as a ``wait`` step, so the auditor asked the ``wait`` template's question -- "how long
do you wait, and then what?" -- of a step that was never going to wait for anyone: the
workflow starts the moment the topic is typed. Rather than let a misread kind drive an
improvised question, each recognized kind has a template naming the fields it needs
answered, and a step that does not actually fit the kind it was given is steered to
the one it does fit before any question is built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wf.schema import StepKind


@dataclass(frozen=True)
class StepTemplate:
    kind: StepKind
    required: tuple[str, ...]  # fields, beyond title/shows_user/trust, this kind needs answered


STEP_TEMPLATES: dict[StepKind, StepTemplate] = {
    "agent": StepTemplate("agent", ("model", "skill", "output.schema")),
    "check": StepTemplate("check", ("run", "checks", "does_not_check")),
    "tool": StepTemplate("tool", ("run", "side_effects", "requires_approval")),
    "subworkflow": StepTemplate("subworkflow", ("workflow", "limits")),
    "wait": StepTemplate("wait", ("deadline", "on_timeout")),
}


def is_workflow_input(step: dict[str, Any]) -> bool:
    """Whether a step guessed as ``wait`` is really how the workflow starts.

    A step that reads nothing and is guessed as ``wait`` does not fit the ``wait``
    template: there is no earlier step for it to wait on, and nothing to time out on
    the very first thing that happens. It fits no step template at all, because it is
    not a step -- it is the workflow's input, given the moment someone starts it.
    """
    return step.get("kind") == "wait" and not step.get("reads_from")


# Kinds whose output a later step can read. A wait hands on nothing and a follow-up
# run hands on a list of runs, so neither is something to start from by default.
PRODUCES = ("agent", "check", "tool")

# What a step that searches is given: the search and the page reader, with room to do
# the job and a ceiling so it cannot run up the bill.
WEB_TOOLS: dict[str, dict[str, int]] = {
    "search": {"max_calls": 15},
    "get_contents": {"max_calls": 25},
}

_SEARCHES = re.compile(
    # "the search results" is what a step reads, not something it does
    r"\b(search(?:es|ing)?(?!\s+results?)|web|online|internet|look(?:s|ing)? up|browse|google)\b",
    re.IGNORECASE,
)


def says_it_searches(title: str | None, description: str | None) -> bool:
    """Whether a step describes itself as going out to the web. Such a step with no
    search tool can only answer from memory, and says so nowhere."""
    return bool(_SEARCHES.search(f"{title or ''} {description or ''}"))


def everything_before(input_names: list[str], earlier: list[tuple[str, str]]) -> dict[str, str]:
    """An input that reads the workflow's inputs and every earlier step that produces
    something, keyed by name. ``earlier`` is ``[(step id, kind), ...]`` in order."""
    out = {n: f"${{inputs.{n}}}" for n in input_names}
    out.update({sid: f"${{steps.{sid}.output}}" for sid, kind in earlier if kind in PRODUCES})
    return out


def follows_the_answer(wait_id: str, input_name: str) -> dict[str, Any]:
    """What connects a follow-up to the wait before it: run when the person asked to go
    deeper, once for each topic they named, with that topic as the follow-up's input."""
    return {
        "when": f"${{steps.{wait_id}.output.go_deeper}}",
        "for_each": f"${{steps.{wait_id}.output.topics}}",
        "with": {input_name: "${item}"},
    }


# What a source is, when a step hands on what it found. The address is the point: the
# step after it cites it, and the link check opens it.
SOURCE_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url", "title", "published", "notes"],
    "properties": {
        "url": {"type": "string", "description": "The page's address, exactly as found."},
        "title": {"type": "string"},
        "published": {"type": "string", "description": "When it was published, or empty."},
        "notes": {
            "type": "string",
            "description": "What it says that matters here, in a few sentences.",
        },
    },
}

_URL_KEYS = {"url", "link", "href", "source_url", "uri"}
_SOURCE_NAMES = re.compile(r"source|result|link|reference|citation|finding", re.IGNORECASE)


def holds_urls(schema: Any) -> bool:
    """Whether an output shape has somewhere to put a web address."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and any(k.lower() in _URL_KEYS for k in props):
            return True
        if schema.get("format") == "uri":
            return True
        return any(holds_urls(v) for v in schema.values())
    if isinstance(schema, list):
        return any(holds_urls(v) for v in schema)
    return False


def with_sources(schema: dict[str, Any] | None) -> dict[str, Any]:
    """The same shape, with room for the sources: a list named like sources (search
    results, links, references) becomes a list of sources, and a shape with no such
    list gains one called ``sources``."""
    import copy

    out = copy.deepcopy(schema) if isinstance(schema, dict) else {"type": "object"}
    out.setdefault("type", "object")
    props = out.setdefault("properties", {})
    changed = False
    for name, node in props.items():
        if (
            isinstance(node, dict)
            and node.get("type") == "array"
            and _SOURCE_NAMES.search(name)
            and not holds_urls(node)
        ):
            node["items"] = copy.deepcopy(SOURCE_ITEM)
            node.setdefault("description", "Every source it found and used.")
            changed = True
    if not changed:
        props["sources"] = {
            "type": "array",
            "description": "Every source it found and used.",
            "items": copy.deepcopy(SOURCE_ITEM),
        }
        req = out.setdefault("required", [])
        if "sources" not in req:
            req.append("sources")
    return out
