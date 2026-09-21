"""Routines a ``check`` or ``tool`` step may name. Definitions cannot add code; they
can only name one of these. Each runs behind the activity boundary."""

from __future__ import annotations

from collections.abc import Callable
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


def http_resolves(ctx: RunnerContext, input: dict[str, Any]) -> dict[str, Any]:
    sources = list(input.get("sources") or [])
    urls = [s.get("url", "") for s in sources]
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
    return {
        "sources": out_sources,
        "open_count": sum(1 for s in out_sources if s["link_opens"]),
        "total": len(out_sources),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "vantage": ctx.activities.links.vantage,
    }


def render_pdf(ctx: RunnerContext, input: dict[str, Any]) -> dict[str, Any]:
    report = input.get("revision") or input.get("report") or {}
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
