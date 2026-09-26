"""The workflow as a picture: the steps in order, and what happens when things do not go
as planned.

Someone at the demo asked for one because "a list of steps is hard to follow when
things don't go as planned". So the picture is built around the other paths: a step
that only happens sometimes and what happens otherwise, each outcome of a decision and
where it leads, where a run stops for a person, and an outcome nobody has decided yet,
drawn as a dead end. It is drawn from the definition itself, the same one a run
follows, so it cannot say something the run does not do.

``flow`` works out what to show; ``render_svg`` draws it. They are separate so what the
picture says can be tested without reading SVG.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

from wf.schema import Step, Workflow, Workspace
from wf.validate import Finding
from wf.validate.findings import plain_value

_TEST = re.compile(r'steps\.([a-z0-9_]+)\.output\.([a-z0-9_.]+)\s*(==|!=)\s*"([^"]*)"')

KIND_WORD = {
    "agent": "Uses judgement",
    "check": "A check, no judgement",
    "tool": "An action",
    "subworkflow": "Starts more research",
    "wait": "Waits for a person",
}


def _test(step: Step) -> tuple[str, str, str, str] | None:
    """``steps.review.output.verdict == "revise"`` as (step, field, op, value)."""
    m = _TEST.search(step.when or "")
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def _outcomes_of(ws: Workspace | None, wf: Workflow, step: Step, field: str) -> list[str]:
    """The values a step's output field can take: its declared enum, or what is known."""
    values: list[str] = []
    if ws is not None and step.output and step.output.schema_:
        try:
            schema = ws.load_schema(step.output.schema_) or {}
        except Exception:  # noqa: BLE001 - a missing shape is a question elsewhere
            schema = {}
        prop = (schema.get("properties") or {}).get(field) or {}
        values = [str(v) for v in prop.get("enum") or []]
    for v in (step.output.continue_on if step.output else {}).get(field, []):
        if v not in values:
            values.append(v)
    return values


def _badges(wf: Workflow, step: Step, is_self: bool) -> list[dict[str, str]]:
    """What is worth knowing about a step at a glance, as short lines. ``person`` marks
    where a person comes in; ``note`` is how it copes when something is off."""
    out: list[dict[str, str]] = []
    t = wf.trust_for(step)
    if step.kind == "wait":
        out.append({"text": "The run waits here for a person", "tone": "person"})
    elif step.kind == "subworkflow":
        if t and t.policy in ("always_ask", "earned"):
            out.append({"text": "Asks you before it starts", "tone": "person"})
        else:
            out.append({"text": "Starts by itself, within the limits", "tone": "info"})
    elif step.kind != "check" and t and t.policy == "always_ask":
        out.append({"text": "Stops for your OK, every time", "tone": "person"})
    elif step.kind != "check" and t and t.policy == "earned":
        out.append(
            {
                "text": f"Stops for your OK until {t.promote_after or 3} in a row",
                "tone": "person",
            }
        )
    if step.kind == "tool" and step.requires_approval:
        out.append({"text": "Needs someone to approve it", "tone": "person"})
    if step.for_each:
        cap = step.effective_max_fanout
        what = "follow-up" if step.kind == "subworkflow" else "item"
        out.append(
            {"text": f"Once per {what}" + (f", at most {cap}" if cap else ""), "tone": "info"}
        )
    if step.kind == "subworkflow" and is_self:
        depth = step.limits.max_depth if step.limits else None
        out.append(
            {
                "text": "Runs this whole workflow again"
                + (f", up to {depth} level{'s' if depth != 1 else ''} deep" if depth else ""),
                "tone": "info",
            }
        )
    if step.kind == "check":
        what = {
            "annotate": "If it fails: noted, and the run carries on",
            "pause": "If it fails: the run stops for you",
            "fail": "If it fails: the run stops",
        }.get(step.on_fail or "")
        if what:
            out.append({"text": what, "tone": "note"})
    return out


def flow(wf: Workflow, findings: list[Finding], ws: Workspace | None = None) -> dict[str, Any]:
    """What the picture shows, top to bottom."""
    open_by_step: dict[str | None, list[Finding]] = {}
    for f in findings:
        if f.status == "open":
            open_by_step.setdefault(f.step_id, []).append(f)
    index = {s.id: i + 1 for i, s in enumerate(wf.spec.steps)}
    title = {s.id: s.title or s.id for s in wf.spec.steps}

    # which later step each outcome leads to, read from the steps' own conditions
    leads: dict[tuple[str, str, str], list[str]] = {}
    for s in wf.spec.steps:
        t = _test(s)
        if t and t[2] == "==":
            leads.setdefault((t[0], t[1], t[3]), []).append(s.id)

    rows = []
    for s in wf.spec.steps:
        t = _test(s)
        condition = None
        undecided = any(f.field == f"steps.{s.id}.when" for f in open_by_step.get(s.id, []))
        if undecided and not s.when:
            condition = "When this happens is not decided yet"
        elif s.when:
            condition = (
                f"Only if “{title.get(t[0], t[0])}” "
                + ("says" if t[2] == "==" else "does not say")
                + f" “{plain_value(t[3])}”"
                if t
                else "Only in some cases"
            )
        outcomes = []
        fields = sorted(
            {f for (src, f, _v) in leads if src == s.id}
            | set((s.output.continue_on if s.output else {}).keys())
        )
        for field in fields:
            for v in _outcomes_of(ws, wf, s, field):
                targets = leads.get((s.id, field, v), [])
                target = wf.step(targets[0]) if targets else None
                if target is not None and target.kind == "wait" and target.id.endswith("_stop"):
                    outcomes.append(
                        {"value": plain_value(v), "goes": "Stops and shows you", "tone": "person"}
                    )
                elif target is not None:
                    outcomes.append(
                        {
                            "value": plain_value(v),
                            "goes": f"{index[target.id]}. {title[target.id]}",
                            "tone": "step",
                        }
                    )
                elif v in (s.output.continue_on if s.output else {}).get(field, []):
                    outcomes.append({"value": plain_value(v), "goes": "Carries on", "tone": "on"})
                else:
                    outcomes.append(
                        {"value": plain_value(v), "goes": "Not decided yet", "tone": "question"}
                    )
        is_self = s.kind == "subworkflow" and (s.workflow or "").split("@")[0] == wf.metadata.name
        rows.append(
            {
                "id": s.id,
                "n": index[s.id],
                "title": title[s.id],
                "kind": KIND_WORD.get(s.kind, s.kind),
                "condition": condition,
                "otherwise": "Otherwise skipped" if s.when else None,
                "condition_open": undecided,
                "badges": _badges(wf, s, is_self),
                "outcomes": outcomes,
                "loops_back": is_self,
                "questions": len(open_by_step.get(s.id, [])),
            }
        )
    b = wf.spec.budget
    return {
        "starts_from": [n for n, i in wf.spec.inputs.items() if not i.internal],
        "rows": rows,
        "ends_with": list(wf.spec.outputs),
        "workflow_questions": len(open_by_step.get(None, [])),
        "when_it_breaks": [
            "A step that breaks stops the run there. Fix the cause, then pick it up from that "
            "step, or skip the step and carry on.",
            *(
                [
                    f"A run that would spend more than ${b.max_usd:g} or {b.max_minutes:g} minutes stops."
                ]
                if b and b.max_usd and b.max_minutes
                else []
            ),
        ],
    }


# -- drawing -----------------------------------------------------------------

W = 760
NODE_X, NODE_W = 64, 380
LANE_X = NODE_X + NODE_W + 34
LANE_W = W - LANE_X - 24
LINE = 17


def _wrap(text: str, width_px: int, px: float = 7.0) -> list[str]:
    """Lines that fit ``width_px`` at about ``px`` per character."""
    per = max(8, int(width_px / px))
    lines: list[str] = []
    cur = ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > per:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _text(x: float, y: float, s: str, cls: str, anchor: str = "start") -> str:
    return f'<text x="{x:.0f}" y="{y:.0f}" class="{cls}" text-anchor="{anchor}">{escape(s)}</text>'


def render_svg(model: dict[str, Any], label: str) -> str:
    """The picture, as inline SVG styled by the page's own classes (``dg-*``)."""
    out: list[str] = []
    y = 16.0
    cx = NODE_X + NODE_W / 2
    centres: dict[str, tuple[float, float]] = {}

    # where it starts
    start = "Starts from: " + (", ".join(model["starts_from"]) or "nothing")
    out.append(
        f'<rect x="{cx - 120:.0f}" y="{y:.0f}" width="240" height="32" rx="16" class="dg-end"/>'
    )
    out.append(_text(cx, y + 21, start, "dg-small", "middle"))
    start_mid = y + 16
    y += 32

    for r in model["rows"]:
        gap = 26 if not r["condition"] else 44
        # the arrow down into the step, with the condition written beside it
        out.append(
            f'<line x1="{cx:.0f}" y1="{y:.0f}" x2="{cx:.0f}" y2="{y + gap - 4:.0f}" class="dg-line" marker-end="url(#dg-arrow)"/>'
        )
        if r["condition"]:
            out.append(
                _text(
                    cx + 10,
                    y + 20,
                    r["condition"],
                    "dg-q" if r.get("condition_open") else "dg-cond",
                )
            )
        y += gap

        title_lines = _wrap(f"{r['n']}. {r['title']}", NODE_W - 28, 7.6)
        badge_lines = [(b, ln) for b in r["badges"] for ln in _wrap(b["text"], NODE_W - 40, 6.6)]
        h = 14 + len(title_lines) * LINE + LINE + len(badge_lines) * 16 + 10
        if r["questions"]:
            h += 18
        outcome_h = sum(
            8 + 16 * len(_wrap(f"{o['value']}: {o['goes']}", LANE_W - 20, 6.6))
            for o in r["outcomes"]
        )
        cls = "dg-node" + (" dg-person" if any(b["tone"] == "person" for b in r["badges"]) else "")
        out.append(
            f'<rect x="{NODE_X}" y="{y:.0f}" width="{NODE_W}" height="{h:.0f}" rx="10" class="{cls}"/>'
        )
        ty = y + 14 + 12
        for ln in title_lines:
            out.append(_text(NODE_X + 14, ty, ln, "dg-title"))
            ty += LINE
        out.append(_text(NODE_X + 14, ty, r["kind"], "dg-kind"))
        ty += LINE
        for b, ln in badge_lines:
            out.append(_text(NODE_X + 26, ty, ln, f"dg-badge dg-{b['tone']}"))
            ty += 16
        if r["questions"]:
            q = r["questions"]
            out.append(
                _text(
                    NODE_X + 14, ty + 2, f"{q} question{'s' if q != 1 else ''} still open", "dg-q"
                )
            )
        centres[r["id"]] = (y, y + h)

        # the step that only happens sometimes: the way round it
        if r["otherwise"]:
            # it leaves the line just above the step and joins it again just below
            top, bottom, x = y - gap + 6, y + h + 12, NODE_X - 34
            out.append(
                f'<path d="M {cx:.0f} {top:.0f} L {x + 14} {top:.0f} Q {x} {top:.0f}, {x} {top + 14:.0f} '
                f'L {x} {bottom - 14:.0f} Q {x} {bottom:.0f}, {x + 14} {bottom:.0f} L {cx - 3:.0f} {bottom:.0f}" '
                f'class="dg-skip" marker-end="url(#dg-arrow-muted)"/>'
            )
            out.append(
                f'<text x="{NODE_X - 40}" y="{y + h / 2:.0f}" class="dg-skip-label" text-anchor="middle" '
                f'transform="rotate(-90 {NODE_X - 40} {y + h / 2:.0f})">{escape(r["otherwise"])}</text>'
            )

        # each outcome, to the right, and where it leads
        oy = y + max(0.0, (h - outcome_h) / 2)
        for o in r["outcomes"]:
            lines = _wrap(f"{o['value']}: {o['goes']}", LANE_W - 20, 6.6)
            oh = 4 + 16 * len(lines)
            out.append(
                f'<line x1="{NODE_X + NODE_W}" y1="{y + h / 2:.0f}" x2="{LANE_X}" y2="{oy + oh / 2:.0f}" class="dg-line dg-{o["tone"]}-line"/>'
            )
            out.append(
                f'<rect x="{LANE_X}" y="{oy:.0f}" width="{LANE_W}" height="{oh:.0f}" rx="6" class="dg-out dg-out-{o["tone"]}"/>'
            )
            ly = oy + 15
            for i, ln in enumerate(lines):
                out.append(
                    _text(LANE_X + 10, ly, ln, "dg-small" + (" dg-strong" if i == 0 else ""))
                )
                ly += 16
            oy += oh + 8
        y += max(h, outcome_h)

    # where it ends
    out.append(
        f'<line x1="{cx:.0f}" y1="{y:.0f}" x2="{cx:.0f}" y2="{y + 22:.0f}" class="dg-line" marker-end="url(#dg-arrow)"/>'
    )
    y += 26
    end = "Done" + (
        ": " + ", ".join(plain_value(e) for e in model["ends_with"]) if model["ends_with"] else ""
    )
    out.append(
        f'<rect x="{cx - 120:.0f}" y="{y:.0f}" width="240" height="32" rx="16" class="dg-end"/>'
    )
    out.append(_text(cx, y + 21, end, "dg-small", "middle"))
    y += 32

    # a workflow that runs itself again: back to the start, one level deeper
    for r in model["rows"]:
        if r["loops_back"] and r["id"] in centres:
            top, bottom = centres[r["id"]]
            mid = (top + bottom) / 2
            x = W - 10
            out.append(
                f'<path d="M {NODE_X + NODE_W} {mid:.0f} L {LANE_X - 14} {mid:.0f} '
                f"C {x} {mid:.0f}, {x} {mid:.0f}, {x} {mid - 20:.0f} L {x} {start_mid + 20:.0f} "
                f'C {x} {start_mid:.0f}, {x} {start_mid:.0f}, {cx + 124:.0f} {start_mid:.0f}" class="dg-loop" marker-end="url(#dg-arrow-loop)"/>'
            )
            out.append(
                _text(x - 6, start_mid - 6, "again, one level deeper", "dg-loop-label", "end")
            )

    # what happens when something goes wrong anywhere
    y += 18
    out.append(_text(NODE_X, y + 12, "When something goes wrong", "dg-title"))
    y += 20
    for note in model["when_it_breaks"]:
        for ln in _wrap(note, W - NODE_X - 24, 6.6):
            out.append(_text(NODE_X, y + 12, ln, "dg-small"))
            y += 16
        y += 4
    height = y + 12

    defs = (
        "<defs>"
        '<marker id="dg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="dg-arrowhead"/></marker>'
        '<marker id="dg-arrow-muted" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="dg-arrowhead-muted"/></marker>'
        '<marker id="dg-arrow-loop" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="dg-arrowhead-loop"/></marker>'
        "</defs>"
    )
    return (
        f'<svg class="dg" viewBox="0 0 {W} {height:.0f}" width="100%" role="img" '
        f'aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{escape(label)}</title>{defs}{''.join(out)}</svg>"
    )


def describe(model: dict[str, Any]) -> str:
    """The picture in words, for a screen reader and for anyone who reads better than
    they look: the same content, in order."""
    parts = ["Starts from " + (", ".join(model["starts_from"]) or "nothing") + "."]
    for r in model["rows"]:
        s = f"{r['n']}. {r['title']}."
        if r["condition"]:
            s += f" {r['condition']}; otherwise skipped."
        for b in r["badges"]:
            s += f" {b['text']}."
        for o in r["outcomes"]:
            s += f" If {o['value']}: {o['goes']}."
        parts.append(s)
    return " ".join(parts)
