"""The chat asks the open questions, one at a time, most important first.

The list beside the draft still has them all; the chat is a second way through the
same questions, for people who would rather be asked than read a form. What the chat
asks is a message of its own (``asks`` names the question), and it is shown from the
question as it is now, so a question answered on the right shows as answered here too.

After anything that can close a question — an answer, a chat turn, an undo — the chat
moves on if the question it asked is no longer open. It does not ask again while its
question is still open: the person may be talking about something else.
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


def next_question(
    findings: list[Finding], chat: list[dict[str, Any]]
) -> tuple[Finding, list[Finding]] | None:
    """The first open question the person has not put aside; a question they skipped
    comes round again only once there is nothing else."""
    skipped = {a["finding_id"] for a in _asked(chat) if a.get("skipped")}
    queue = ordered(findings)
    return next((q for q in queue if q[0].id not in skipped), None)


def ask(f: Finding, similar: list[Finding]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "text": "",
        "changes": [],
        "point_to_finding": None,
        "asks": {"finding_id": f.id, "similar": [o.id for o in similar]},
    }


DONE = "That was the last question. You can try it on a topic, or keep changing it here."
DONE_BUT_SKIPPED = (
    "That is everything except the questions you skipped. They are still on the right, "
    "and a dry run guesses them and says where."
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
        return [*chat, ask(*nxt)]
    if not asked or chat[-1].get("done"):
        return chat
    left = any(f.status == "open" for f in findings)
    return [
        *chat,
        {
            "role": "assistant",
            "text": DONE_BUT_SKIPPED if left else DONE,
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
