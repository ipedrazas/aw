"""The chat asks the open questions, one at a time, most important first.

The list beside the draft still has them all; the chat is a second way through the
same questions, for people who would rather be asked than read a form. What the chat
asks is a message of its own (``asks`` names the question), and it is shown from the
question as it is now, so a question answered on the right shows as answered here too.

After anything that can close a question — an answer, a chat turn, an undo — the chat
moves on if the question it asked is no longer open. It does not ask again while its
question is still open: the person may be talking about something else.

Which question comes next, and how it is put, is the chat's to decide
(``wf.audit.triage``). Its plan is kept on the chat's first message: the order to ask
in, how it puts each one, and the ones that can wait until the draft has been tried.
A question the plan does not know, raised by an answer since, comes after the ones
it ordered; with no plan at all, the order is the list's.
"""

from __future__ import annotations

from typing import Any

from wf.validate import Finding

from .question import fold_similar, group_questions


def ordered(findings: list[Finding]) -> list[tuple[Finding, list[Finding]]]:
    """The open questions in the order the list shows them, each with the ones that ask
    the same thing of other steps."""
    out: list[tuple[Finding, list[Finding]]] = []
    for g in group_questions([f for f in findings if f.status == "open"]):
        out.extend(fold_similar(g["findings"]))
    return out


def _asked(chat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m["asks"] for m in chat if m.get("asks")]


def plan_of(chat: list[dict[str, Any]]) -> dict[str, Any]:
    """The chat's plan for its questions, or an empty one."""
    return (chat[0].get("plan") if chat else None) or {}


def with_plan(chat: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    out = [dict(m) for m in chat]
    if out:
        out[0]["plan"] = plan
    return out


def ask_first(chat: list[dict[str, Any]], finding_id: str, text: str) -> list[dict[str, Any]]:
    """The chat with its plan changed to ask this question next, put this way."""
    plan = plan_of(chat)
    return with_plan(
        chat,
        {
            "order": [finding_id, *(i for i in plan.get("order", []) if i != finding_id)],
            "lead": {**plan.get("lead", {}), **({finding_id: text} if text else {})},
            "later": {k: v for k, v in plan.get("later", {}).items() if k != finding_id},
        },
    )


def next_question(
    findings: list[Finding], chat: list[dict[str, Any]]
) -> tuple[Finding, list[Finding]] | None:
    """The first open question in the chat's order that nobody has put aside: not
    skipped by the person, not left by the chat until the draft has been tried."""
    plan = plan_of(chat)
    aside = {a["finding_id"] for a in _asked(chat) if a.get("skipped")} | set(plan.get("later", {}))
    rank = {fid: n for n, fid in enumerate(plan.get("order", []))}
    queue = ordered(findings)
    # a question asked of several steps goes where the chat put any of them
    queue.sort(key=lambda q: min(rank.get(f.id, len(rank)) for f in [q[0], *q[1]]))
    return next((q for q in queue if q[0].id not in aside), None)


def ask(f: Finding, similar: list[Finding], text: str = "") -> dict[str, Any]:
    """``text`` is how the chat puts it; without it the page shows the question as written."""
    return {
        "role": "assistant",
        "text": text,
        "changes": [],
        "point_to_finding": None,
        "asks": {"finding_id": f.id, "similar": [o.id for o in similar]},
    }


DONE = "That was the last question. You can try it on a topic, or keep changing it here."
DONE_BUT_SKIPPED = (
    "That is everything except the questions you skipped. They are still on the right, "
    "and a dry run guesses them and says where."
)
DONE_BUT_LATER = (
    "That is all I need from you to try it. The questions left can wait until you have: "
    "they are on the right, a dry run guesses them and says where, and a real run needs "
    "them answered."
)


def move_on(findings: list[Finding], chat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chat with the next question asked, if the one it asked last is closed, or put
    aside. Nothing changes while that question is still open."""
    asked = _asked(chat)
    if asked:
        last = asked[-1]
        still_open = any(f.id == last["finding_id"] and f.status == "open" for f in findings)
        if still_open and not last.get("skipped"):
            return chat
    nxt = next_question(findings, chat)
    if nxt is not None:
        # a skipped question comes round again as a new message, not the old one
        # how the chat put it, for whichever of the steps it put it for
        lead = plan_of(chat).get("lead", {})
        text = next((lead[f.id] for f in [nxt[0], *nxt[1]] if lead.get(f.id)), "")
        return [*chat, ask(*nxt, text=text)]
    if not asked or chat[-1].get("done"):
        return chat
    left = {f.id for f in findings if f.status == "open"}
    skipped = {a["finding_id"] for a in asked if a.get("skipped")}
    return [
        *chat,
        {
            "role": "assistant",
            "text": (DONE_BUT_SKIPPED if left & skipped else DONE_BUT_LATER) if left else DONE,
            "changes": [],
            "point_to_finding": None,
            "done": True,
        },
    ]


def skip(chat: list[dict[str, Any]], finding_id: str) -> list[dict[str, Any]]:
    """The chat with the question put aside for now."""
    out = [dict(m) for m in chat]
    for m in reversed(out):
        if m.get("asks") and m["asks"]["finding_id"] == finding_id:
            m["asks"] = {**m["asks"], "skipped": True}
            break
    return out


def asking(findings: list[Finding], chat: list[dict[str, Any]]) -> Finding | None:
    """The question the chat is waiting on, if it is still open."""
    asked = _asked(chat)
    if not asked or asked[-1].get("skipped"):
        return None
    return next(
        (f for f in findings if f.id == asked[-1]["finding_id"] and f.status == "open"), None
    )
