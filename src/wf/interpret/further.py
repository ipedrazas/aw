"""Searching further: what a follow-up round is told, and how its result joins the rest.

A step with ``search_further`` runs once as usual, names the topics worth following in
its result, and is run again on each of them, round by round, within its limits (the
loop is in the interpreter). Each follow-up gets its usual input unchanged plus a
``further`` block, and the same instructions with the section below added. What every
round found becomes one result: lists joined, the same address kept once, and a single
value kept from the first round, so the steps that read it need no change.
"""

from __future__ import annotations

import json
from typing import Any

#: Added to the instructions of a step that searches further, every round.
FURTHER_SECTION = """

## Searching further

Name the topics worth searching next in `{follow}`, by the rule its description gives,
and leave it empty when none are. You may be run again on each of them.

When your input has `further`, this round follows one of those topics: search
`further.topic`, keep to what your usual input asks for, and do not fetch the addresses
in `further.already_found` again. `further.from` is the topic it was found under and
`further.level` how many rounds deep this is."""


def topics_in(output: Any, follow: str) -> list[dict[str, str]]:
    """The topics a round named as worth following, as ``{topic, why}``."""
    raw = output.get(follow) if isinstance(output, dict) else None
    out = []
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, dict) and str(t.get("topic") or "").strip():
            out.append({"topic": str(t["topic"]).strip(), "why": str(t.get("why") or "")})
        elif isinstance(t, str) and t.strip():
            out.append({"topic": t.strip(), "why": ""})
    return out


def urls_in(value: Any) -> list[str]:
    """Every address in a result, in the order found, each once."""
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            url = v.get("url")
            if isinstance(url, str) and url and url not in found:
                found.append(url)
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return found


def _key(item: Any) -> str:
    if isinstance(item, dict) and isinstance(item.get("url"), str):
        return "url:" + item["url"]
    if isinstance(item, dict) and isinstance(item.get("topic"), str):
        return "topic:" + item["topic"].casefold()
    return json.dumps(item, sort_keys=True, default=str)


def merge(first: Any, more: Any) -> Any:
    """What two rounds found, as one result: lists joined with each address (or topic,
    or identical item) kept once, objects merged field by field, and any other value
    kept from the first round."""
    if isinstance(first, dict) and isinstance(more, dict):
        out = dict(first)
        for k, v in more.items():
            out[k] = merge(first[k], v) if k in first else v
        return out
    if isinstance(first, list) and isinstance(more, list):
        seen = {_key(x) for x in first}
        out_list = list(first)
        for x in more:
            if _key(x) not in seen:
                seen.add(_key(x))
                out_list.append(x)
        return out_list
    return first if first is not None else more
