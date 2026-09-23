"""Routines a ``check`` or ``tool`` step may name. Definitions cannot add code; they
can only name one of these. Each runs behind the activity boundary."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wf.activities import Activities


@dataclass
class RunnerContext:
    activities: Activities
    mode: str
    run_id: str
    step_id: str
    artifacts_dir: Path
    note: Callable[[str, str], None]  # (text, reason) -> records a control decision


_URL = re.compile(r"https?://[^\s<>\"'`\]\[)(]+")
_TRAILING = ").,;:!?]'\"*_"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _sources_in(input: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The links to check, and whether they came as a list of sources.

    A list of ``{line, claim, url}`` is what the step is meant to be handed. When it is
    handed something else — a report, a list of strings, a step's whole output — every
    web address in it is checked, once, with the line it sits on, rather than nothing.
    """
    listed = input.get("sources")
    out: list[dict[str, Any]] = []
    if isinstance(listed, list):
        for s in listed:
            if isinstance(s, dict) and (s.get("url") or s.get("link")):
                out.append(
                    {
                        "line": int(s.get("line") or 0),
                        "claim": str(s.get("claim") or s.get("title") or ""),
                        "url": str(s.get("url") or s.get("link")),
                    }
                )
    if out:
        return out, True
    seen: set[str] = set()
    for text in _strings(input):
        for n, line in enumerate(text.splitlines(), 1):
            for m in _URL.finditer(line):
                url = m.group(0).rstrip(_TRAILING)
                if url in seen:
                    continue
                seen.add(url)
                out.append({"line": n, "claim": line.strip()[:200], "url": url})
    return out, False


def http_resolves(ctx: RunnerContext, input: dict[str, Any]) -> dict[str, Any]:
    sources, listed = _sources_in(input or {})
    if sources and not listed:
        ctx.note(
            f"Found {len(sources)} links in what it was given, and checked each one.",
            "It was not handed a list of sources, so it read every web address in its input.",
        )
    urls = [s["url"] for s in sources]
    statuses = {ls.url: ls for ls in ctx.activities.links.check(urls)}
    out_sources = []
    for s in sources:
        ls = statuses.get(s.get("url", ""))
        out_sources.append(
            {
                "line": int(s.get("line", 0)),
                "claim": str(s.get("claim", "")),
                "url": str(s.get("url", "")),
                "link_opens": bool(ls.opens) if ls else False,
                "status": int(ls.status) if ls else 0,
            }
        )
    dead = [statuses[u] for u in dict.fromkeys(urls) if u in statuses and not statuses[u].opens]
    if dead:
        ctx.note(
            f"{len(dead)} of {len(set(urls))} links did not open: "
            + "; ".join(f"{d.url} ({d.status or d.reason or 'no answer'})" for d in dead)
            + ".",
            f"Checked from {ctx.activities.links.vantage}.",
        )
    return {
        "sources": out_sources,
        "open_count": sum(1 for s in out_sources if s["link_opens"]),
        "total": len(out_sources),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "vantage": ctx.activities.links.vantage,
    }


def _longest_text(input: dict[str, Any]) -> dict[str, Any]:
    """A report handed on under some other name — ``report``, ``report_md``, a link
    table appended to it — is the longest piece of writing in the input. A PDF of that
    is better than an empty one titled "Report"."""
    body = max(_strings(input), key=len, default="")
    if not body:
        return {}
    first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
    title = input.get("topic") if isinstance(input.get("topic"), str) else first
    return {"title": str(title or "Report")[:200], "body_md": body}


def render_pdf(ctx: RunnerContext, input: dict[str, Any]) -> dict[str, Any]:
    report = input.get("revision") or input.get("report")
    if report is None:
        report = next(
            (v for v in input.values() if isinstance(v, dict) and ("body_md" in v or "title" in v)),
            None,
        ) or _longest_text(input)
    if input.get("revision"):
        ctx.note(
            "Used the revised report, not the first draft.",
            "The reviewer asked for edits and they were applied.",
        )
    followups = [f for f in (input.get("followups") or []) if isinstance(f, dict)]
    simulated = ctx.mode != "live"
    name = ("SIMULATED-" if simulated else "") + "report.pdf"
    out_path = ctx.artifacts_dir / ctx.run_id / name
    result = ctx.activities.render.render_pdf(
        title=str(report.get("title") or "Report"),
        report=report,
        followups=followups,
        link_check=input.get("link_check"),
        simulated=simulated,
        out_path=str(out_path),
    )
    return {
        "artifact": result["artifact"],
        "pages": int(result["pages"]),
        "simulated": bool(result["simulated"]),
    }


def send_email(ctx: RunnerContext, input: dict[str, Any]) -> dict[str, Any]:
    to = str(input.get("to") or "(nobody named)")
    subject = str(input.get("subject") or "(no subject)")
    result = ctx.activities.send.send(
        to=to,
        subject=subject,
        body=str(input.get("body") or ""),
        attachments=list(input.get("attachments") or []),
    )
    ctx.note(
        f"Would have sent “{subject}” to {to}. Nothing was sent.",
        "Nothing leaves the system in this phase; the send was recorded instead.",
    )
    return result


CHECKS: dict[str, Callable[[RunnerContext, dict[str, Any]], dict[str, Any]]] = {
    "checks.http_resolves": http_resolves,
}

TOOLS: dict[str, Callable[[RunnerContext, dict[str, Any]], dict[str, Any]]] = {
    "tools.render_pdf": render_pdf,
    "tools.send_email": send_email,
}

ARTIFACT_TOOLS = {"tools.render_pdf": ("artifact", "application/pdf")}

# What each routine produces. A draft that names a routine gets this shape for its output.
RUNNER_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "checks.http_resolves": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sources", "open_count", "total", "checked_at", "vantage"],
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["line", "claim", "url", "link_opens", "status"],
                    "properties": {
                        "line": {"type": "integer"},
                        "claim": {"type": "string"},
                        "url": {"type": "string"},
                        "link_opens": {"type": "boolean"},
                        "status": {"type": "integer"},
                    },
                },
            },
            "open_count": {"type": "integer"},
            "total": {"type": "integer"},
            "checked_at": {"type": "string"},
            "vantage": {"type": "string"},
        },
    },
    "tools.render_pdf": {
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact", "pages", "simulated"],
        "properties": {
            "artifact": {"type": "string"},
            "pages": {"type": "integer"},
            "simulated": {"type": "boolean"},
        },
    },
    "tools.send_email": {
        "type": "object",
        "additionalProperties": False,
        "required": ["delivered", "recorded"],
        "properties": {
            "delivered": {"type": "boolean"},
            "recorded": {"type": "boolean"},
            "to": {"type": "string"},
            "subject": {"type": "string"},
        },
    },
}
