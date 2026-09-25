"""The chat asks the open questions one at a time, and moves on when one is closed."""

from __future__ import annotations

from tests.test_api import client  # noqa: F401 - the fixture
from tests.test_audit import DOC
from wf.audit.asking import DONE, DONE_BUT_SKIPPED, move_on, skip
from wf.validate import Finding


def draft(client):  # noqa: F811
    return client.post("/api/audits", json={"document": DOC.read_text(), "name": "p"}).json()


def first_open(audit):
    return audit["questions"][0]["findings"][0]


def asked(audit):
    return [m["asks"]["finding_id"] for m in audit["chat"] if m.get("asks")]


def test_the_chat_opens_by_asking_the_question_that_matters_most(client):  # noqa: F811
    audit = draft(client)
    assert "one at a time" in audit["chat"][0]["text"]
    assert asked(audit) == [first_open(audit)["id"]], "the one at the top of the list"
    assert audit["asking"] == first_open(audit)["id"]

    page = client.get(f"/audits/{audit['id']}").text
    assert f'data-asking="{audit["asking"]}"' in page, "what they type is about it"
    assert "data-from-chat" in page and "Skip for now" in page


def test_answering_in_the_chat_says_what_they_chose_and_asks_the_next(client):  # noqa: F811
    audit = draft(client)
    q = first_open(audit)
    choice = q["options"][0]
    after = client.post(
        f"/api/audits/{audit['id']}/answer",
        json={"finding_id": q["id"], "answer": choice["value"], "from_chat": True},
    ).json()
    said, nxt = after["chat"][-2], after["chat"][-1]
    assert said == {"role": "user", "text": choice["label"]}
    assert nxt["asks"]["finding_id"] == first_open(after)["id"] != q["id"]
    assert after["asked"][q["id"]]["said"] == choice["label"], (
        "shown as answered where it was asked"
    )


def test_an_answer_on_the_right_to_another_question_does_not_interrupt(client):  # noqa: F811
    audit = draft(client)
    other = audit["questions"][-1]["findings"][-1]
    assert other["id"] != audit["asking"]
    after = client.post(
        f"/api/audits/{audit['id']}/answer",
        json={"finding_id": other["id"], "answer": other["options"][0]["value"]},
    ).json()
    assert asked(after) == asked(audit), "still waiting on the question it asked"
    assert after["chat"][-1] == audit["chat"][-1], "and nothing said on their behalf"


def test_a_skipped_question_is_put_aside_and_asked_again_last(client):  # noqa: F811
    audit = draft(client)
    q = audit["asking"]
    after = client.post(f"/api/audits/{audit['id']}/skip", json={"finding_id": q}).json()
    assert after["asking"] not in (None, q)
    assert after["chat"][-2]["asks"] == {**audit["chat"][-1]["asks"], "skipped": True}


def test_the_chat_is_told_the_question_it_asked(client):  # noqa: F811
    audit = draft(client)
    q = first_open(audit)
    body = client.post(
        f"/api/audits/{audit['id']}/chat", json={"message": "what did you ask?", "about": q["id"]}
    ).json()
    reply = next(m for m in reversed(body["chat"]) if m["role"] == "assistant" and m["text"])
    assert reply["text"] == f"{q['question']} | {q['question']}"


def finding(fid: str, status: str = "open") -> Finding:
    return Finding(id=fid, type="gap", field=f"spec.{fid}", question=fid, status=status)


def test_it_does_not_ask_again_while_its_question_is_open_and_says_when_it_is_done():
    a, b = finding("a"), finding("b")
    chat = move_on([a, b], [])
    assert [m["asks"]["finding_id"] for m in chat] == ["a"]
    assert move_on([a, b], chat) == chat, "still waiting on a"

    a.status = "answered"
    chat = move_on([a, b], chat)
    assert chat[-1]["asks"]["finding_id"] == "b"

    b.status = "answered"
    chat = move_on([a, b], chat)
    assert chat[-1]["text"] == DONE
    assert move_on([a, b], chat) == chat, "said once"


def test_with_only_skipped_questions_left_it_says_so():
    a = finding("a")
    chat = skip(move_on([a], []), "a")
    assert move_on([a], chat)[-1]["text"] == DONE_BUT_SKIPPED
